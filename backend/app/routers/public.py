import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..config_loader import get_form_config
from ..database import SessionLocal, get_db
from ..models import StagedUpload, Submission, SubmissionLink, Upload
from ..ocr.extractor import extract, peek_cached
from ..security import is_accepted_type, sniff_content_type
from ..storage import delete_file, load_decrypted, save_encrypted
from ..templating import templates

log = logging.getLogger(__name__)

router = APIRouter()

# How long a chosen-but-not-submitted document is kept. Long enough that a
# client can be interrupted mid-form and come back to it, short enough that an
# abandoned link does not leave documents lying about indefinitely.
_STAGED_TTL = timedelta(hours=48)


def _max_file_bytes() -> int:
    return settings.max_upload_mb * 1024 * 1024


def _max_request_bytes(cfg: dict) -> int:
    n_uploads = max(1, len(cfg.get("slots", {})))
    return n_uploads * _max_file_bytes() + 2 * 1024 * 1024  # + 2 MB for fields/overhead


def _get_link(db: Session, token: str) -> SubmissionLink | None:
    return db.query(SubmissionLink).filter(SubmissionLink.token == token).first()


def _link_state(link: SubmissionLink | None) -> str:
    if link is None:
        return "invalid"
    return "open" if link.is_open else link.effective_status


# --- documents held between attempts -----------------------------------------

def _staged_by_slot(db: Session, link: SubmissionLink) -> dict[str, StagedUpload]:
    """Documents already chosen against this link, keyed by upload slot."""
    rows = db.query(StagedUpload).filter(StagedUpload.link_id == link.id).all()
    return {row.field_key: row for row in rows}


def _drop_staged(db: Session, row: StagedUpload, keep_blob: bool = False) -> None:
    """Forget a staged document. `keep_blob` when a real Upload has taken the
    blob over — deleting it then would delete the submitted document."""
    if not keep_blob:
        delete_file(row.stored_filename)
    db.delete(row)


def _stage_upload(
    db: Session, link: SubmissionLink, slot: str, filename: str,
    content_type: str, data: bytes,
) -> StagedUpload:
    """Hold this document against the link so a failed submit cannot lose it.
    A second document for the same slot replaces the first."""
    existing = db.query(StagedUpload).filter(
        StagedUpload.link_id == link.id, StagedUpload.field_key == slot
    ).all()
    for row in existing:
        _drop_staged(db, row)

    row = StagedUpload(
        link_id=link.id,
        field_key=slot,
        original_filename=filename or "document",
        stored_filename=save_encrypted(data),
        content_type=content_type,
        size_bytes=len(data),
    )
    db.add(row)
    db.commit()
    return row


def _prune_staged(db: Session) -> None:
    """Clear out documents staged against links nobody came back to."""
    cutoff = datetime.utcnow() - _STAGED_TTL
    for row in db.query(StagedUpload).filter(StagedUpload.created_at < cutoff).all():
        _drop_staged(db, row)
    db.commit()


def _staged_names(db: Session, link: SubmissionLink) -> dict[str, str]:
    """{slot: filename} for the form to show what is already attached."""
    return {slot: row.original_filename for slot, row in _staged_by_slot(db, link).items()}


def _section_has_din(section: dict, values: dict) -> bool:
    """True if this section has a DIN field with a value entered."""
    for field in section["fields"]:
        if field["key"] == "din" and (values.get(field["name"]) or "").strip():
            return True
    return False


# Everything typed here ends up verbatim on an MCA filing, so a placeholder
# like "na" or "nil" is worse than a blank — it gets filed. These are the ones
# that actually turned up in real submissions.
_PLACEHOLDERS = {"na", "n/a", "nil", "none", "no", "-", "--", "nan", "xx", "xxx", "tbd", "."}


def _format_problem(field: dict, value: str) -> str:
    """A human explanation of why this value cannot be filed, or ''."""
    kind = field.get("type")
    lowered = value.strip().lower()

    if kind in ("tel", "email") or field.get("key") in ("pan_number", "dob"):
        if lowered in _PLACEHOLDERS:
            return "Please give the real details — this goes onto the filing as typed."

    if kind == "email":
        # deliberately loose: one @, a dot in the domain, no spaces
        if " " in value or value.count("@") != 1 or "." not in value.split("@")[-1]:
            return "Please enter a valid email address, e.g. name@example.com."

    if kind == "tel":
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        elif digits.startswith("0") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) != 10:
            return "Please enter a 10-digit mobile number."

    if field.get("key") == "pan_number":
        pan = value.strip().upper()
        if len(pan) != 10 or not (pan[:5].isalpha() and pan[5:9].isdigit() and pan[9].isalpha()):
            return "A PAN is 10 characters, like ABCDE1234F. Please check it."

    return ""


def _duplicate_upload_errors(cfg: dict, upload_files: list) -> dict:
    """Refuse one person's identity document uploaded for another partner.

    The damage this prevents is real: partner 1's Aadhaar uploaded as partner
    2's document silently copies partner 1's address onto partner 2, and the
    LLP agreement carries it into a filing looking perfectly plausible.

    Only PAN and Aadhaar are checked, and only across partners. Two people
    cannot share those. They can share the rest — spouses with a joint account
    file one bank statement, and the office utility bill belongs to the LLP
    rather than to any partner — so refusing those would block honest
    submissions. Within one partner, the same PDF may answer two slots.
    """
    from hashlib import sha256

    identity = {"pan", "aadhaar"}
    labels = {up["name"]: up["label"] for s in cfg["sections"] for up in s["uploads"]}
    sections = {up["name"]: s["title"] for s in cfg["sections"] for up in s["uploads"]}

    seen: dict[str, tuple[str, str]] = {}  # digest -> (section, slot label)
    errors: dict = {}
    for slot_name, meta, _filename, _ctype, data, _stored in upload_files:
        if meta.get("extractor") not in identity:
            continue
        digest = sha256(data).hexdigest()
        section = sections.get(slot_name, "")
        if digest in seen:
            first_section, first_label = seen[digest]
            if first_section != section:
                errors[slot_name] = (
                    f"This looks like the same file already uploaded under "
                    f"{first_section} as {first_label}. A PAN or Aadhaar belongs "
                    "to one person — please upload this partner's own document."
                )
                continue
        else:
            seen[digest] = (section, labels.get(slot_name, slot_name))
    return errors


def _field_visible(field: dict, values: dict) -> bool:
    """False for a `show_if` field whose controlling field doesn't match — e.g.
    "please specify your qualification", shown only when Education is Other."""
    name = field.get("show_if_name")
    if not name:
        return True
    return (values.get(name) or "").strip() == field.get("show_if_value")


def _field_required(field: dict, section: dict, values: dict) -> bool:
    if not field.get("required"):
        return False
    if not _field_visible(field, values):
        return False
    if field.get("hide_if_din") and _section_has_din(section, values):
        return False
    return True


# --- GET form ----------------------------------------------------------------

@router.get("/f/{token}", response_class=HTMLResponse)
def show_form(token: str, request: Request, db: Session = Depends(get_db)):
    link = _get_link(db, token)
    state = _link_state(link)
    if state != "open":
        return templates.TemplateResponse(
            "client_closed.html",
            {"request": request, "state": state},
            status_code=404 if state == "invalid" else 410,
        )
    _prune_staged(db)
    return templates.TemplateResponse(
        "client_form.html",
        {"request": request, "form": get_form_config(), "token": token, "errors": None,
         "values": {}, "staged": _staged_names(db, link)},
    )


# --- live extraction (autofill) ---------------------------------------------

@router.post("/f/{token}/extract")
async def extract_document(
    token: str,
    request: Request,
    slot: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """OCR a single uploaded document and return the field values it fills, keyed
    by input name, so the client's browser can pre-fill them.

    The document is also kept against the link, so it survives a submit that
    comes back with a validation error.
    """
    link = _get_link(db, token)
    if _link_state(link) != "open":
        return JSONResponse({"ok": False, "error": "This link is no longer active."}, status_code=410)

    cfg = get_form_config()
    meta = cfg["slots"].get(slot)
    if meta is None:
        return JSONResponse({"ok": False, "error": "Unknown upload."}, status_code=400)

    data = await file.read()
    if len(data) > _max_file_bytes():
        return JSONResponse(
            {"ok": False, "error": f"File too large (max {settings.max_upload_mb} MB)."}, status_code=413
        )
    if not data:
        return JSONResponse({"ok": False, "error": "Empty file."}, status_code=400)

    # The bytes decide the type, not the browser's guess — see sniff_content_type.
    content_type = sniff_content_type(data, file.content_type)
    if not is_accepted_type(content_type, meta["accept"]):
        return JSONResponse(
            {"ok": False, "error": "This file is not an image or a PDF. Please upload a "
                                   "photo or a scan of the document."},
            status_code=415,
        )

    staged = _stage_upload(db, link, slot, file.filename, content_type, data)

    raw_text, extracted = extract(meta["extractor"], data, content_type)

    # Map canonical extractor keys -> fully-qualified field names this slot fills.
    prefix = meta["section"] + "__"
    fills = set(meta["fills"])
    fields = {}
    for key, value in extracted.items():
        name = prefix + key
        if name in fills and value:
            fields[name] = value

    return JSONResponse({
        "ok": True, "fields": fields, "found": len(fields), "had_text": bool(raw_text),
        "saved": staged.original_filename,
    })


@router.post("/f/{token}/unstage")
async def unstage_document(
    token: str,
    slot: str = Form(...),
    db: Session = Depends(get_db),
):
    """Forget the document held for one slot — the client cleared the field or
    wants to attach a different one. Without this there is no way to take back
    a document uploaded against the wrong partner."""
    link = _get_link(db, token)
    if _link_state(link) != "open":
        return JSONResponse({"ok": False, "error": "This link is no longer active."}, status_code=410)

    for row in db.query(StagedUpload).filter(
        StagedUpload.link_id == link.id, StagedUpload.field_key == slot
    ).all():
        _drop_staged(db, row)
    db.commit()
    return JSONResponse({"ok": True})


# --- POST submit -------------------------------------------------------------

def _extract_after_response(pending: list[tuple[str, str]]) -> None:
    """Read the documents whose extraction wasn't already done during autofill.

    Runs after the client has been sent the thank-you page. Reading a document
    takes a vision-LLM round trip each; doing all of them inside the submit
    request is what pushed it past the reverse proxy's gateway timeout — the
    client saw a 504 even though the submission itself had been saved.
    """
    db = SessionLocal()
    try:
        for upload_id, extractor_name in pending:
            upload = db.get(Upload, upload_id)
            if upload is None:
                continue
            try:
                data = load_decrypted(upload.stored_filename)
                raw_text, extracted = extract(extractor_name, data, upload.content_type)
            except Exception:  # noqa: BLE001 - a failed read must not lose the document
                log.exception("Post-submit extraction failed for upload %s", upload_id)
                continue
            upload.raw_text = raw_text
            upload.extracted_data = extracted
            db.commit()
    finally:
        db.close()


@router.post("/f/{token}", response_class=HTMLResponse)
async def submit_form(
    token: str,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    link = _get_link(db, token)
    state = _link_state(link)
    if state != "open":
        return templates.TemplateResponse(
            "client_closed.html",
            {"request": request, "state": state},
            status_code=404 if state == "invalid" else 410,
        )

    cfg = get_form_config()

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _max_request_bytes(cfg):
        return templates.TemplateResponse(
            "client_form.html",
            {"request": request, "form": cfg, "token": token,
             "errors": {"__all__": f"Upload too large (max {settings.max_upload_mb} MB per file)."},
             "values": {}, "staged": _staged_names(db, link)},
            status_code=413,
        )

    form = await request.form()
    values: dict = {}
    errors: dict = {}

    # --- text fields (validate with conditional DIN logic) ---
    for section in cfg["sections"]:
        for field in section["fields"]:
            name = field["name"]
            val = (form.get(name) or "").strip()
            # A hidden conditional field is stored blank, so a stale answer
            # (Other + text, then switched to Graduate) can't reach the record.
            # a PAN is upper case on every filing; store it that way rather
            # than however the client happened to type it
            if field.get("key") == "pan_number":
                val = val.upper()
            values[name] = val if _field_visible(field, values) else ""
            if _field_required(field, section, values) and not values[name]:
                errors[name] = "This field is required."
            elif values[name]:
                problem = _format_problem(field, values[name])
                if problem:
                    errors[name] = problem

    # --- uploads ---
    # (slot_name, meta, filename, content_type, bytes, already_stored_blob|None)
    upload_files = []
    staged = _staged_by_slot(db, link)

    for slot_name, meta in cfg["slots"].items():
        file = form.get(slot_name)
        has_file = file is not None and getattr(file, "filename", "")
        held = staged.get(slot_name)

        if not has_file:
            # Nothing attached this time round, but the document chosen earlier
            # is still here — that is the whole point of staging it.
            if held is not None:
                try:
                    data = load_decrypted(held.stored_filename)
                except Exception as exc:  # noqa: BLE001 - a lost blob must not 500
                    log.warning("Staged blob for %s unreadable: %r", slot_name, exc)
                    _drop_staged(db, held)
                    db.commit()
                    staged.pop(slot_name, None)
                    if _upload_required(cfg, slot_name):
                        errors[slot_name] = "Please attach this document again."
                    continue
                upload_files.append((slot_name, meta, held.original_filename,
                                     held.content_type, data, held.stored_filename))
            elif _upload_required(cfg, slot_name):
                errors[slot_name] = "Please upload this document."
            continue

        data = await file.read()
        if len(data) > _max_file_bytes():
            errors[slot_name] = f"File is too large (max {settings.max_upload_mb} MB)."
            continue
        if not data:
            errors[slot_name] = "Uploaded file is empty."
            continue
        content_type = sniff_content_type(data, file.content_type)
        if not is_accepted_type(content_type, meta["accept"]):
            errors[slot_name] = ("This file is not an image or a PDF. Please upload a "
                                 "photo or a scan of the document.")
            continue
        # A freshly attached file wins over any document held for this slot;
        # the one it replaces is cleared out after the submission is saved.
        upload_files.append((slot_name, meta, file.filename, content_type, data, None))

    errors.update(_duplicate_upload_errors(cfg, upload_files))

    if errors:
        # Whatever the client attached this time is staged too, so the next
        # attempt starts with every document in place rather than none of them.
        # A document that was itself refused is not held: keeping it would show
        # it as attached and fail the same way on every further attempt.
        for slot_name, _meta, filename, content_type, data, stored in upload_files:
            if stored is None and slot_name not in errors:
                _stage_upload(db, link, slot_name, filename, content_type, data)
        return templates.TemplateResponse(
            "client_form.html",
            {"request": request, "form": cfg, "token": token, "errors": errors,
             "values": values, "staged": _staged_names(db, link)},
            status_code=400,
        )

    # --- persist ---
    submission = Submission(
        link_id=link.id,
        form_data=values,
        client_ip=request.client.host if request.client else None,
    )
    db.add(submission)
    db.flush()

    pending: list[tuple[str, str]] = []  # (upload_id, extractor) still to be read
    for slot_name, meta, filename, content_type, data, staged_blob in upload_files:
        # Usually already read during autofill, in which case this is free.
        cached = peek_cached(meta["extractor"], data)
        # A staged document is already encrypted on disk; the submission takes
        # the blob over rather than writing a second copy of it.
        stored = staged_blob or save_encrypted(data)
        upload = Upload(
            submission_id=submission.id,
            field_key=slot_name,
            doc_type=meta["extractor"],
            original_filename=filename,
            stored_filename=stored,
            content_type=content_type,
            size_bytes=len(data),
            raw_text=cached[0] if cached else None,
            extracted_data=cached[1] if cached else None,
        )
        db.add(upload)
        db.flush()  # assign the id the background pass needs
        if cached is None:
            pending.append((upload.id, meta["extractor"]))

    # The staging area has done its job. A blob adopted above stays on disk
    # under its Upload; the rest — documents a later attachment replaced — go.
    adopted = {stored for _s, _m, _f, _c, _d, stored in upload_files if stored}
    for row in staged.values():
        _drop_staged(db, row, keep_blob=row.stored_filename in adopted)

    link.status = "completed"
    link.completed_at = datetime.utcnow()
    db.commit()

    # The client's answers are safe on disk now; anything left to read happens
    # after the response goes out, so the submit itself is always quick.
    if pending:
        background.add_task(_extract_after_response, pending)

    return templates.TemplateResponse("client_done.html", {"request": request})


def _upload_required(cfg: dict, slot_name: str) -> bool:
    for section in cfg["sections"]:
        for up in section["uploads"]:
            if up["name"] == slot_name:
                return bool(up.get("required"))
    return False
