import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..config_loader import get_form_config
from ..database import SessionLocal, get_db
from ..models import Submission, SubmissionLink, Upload
from ..ocr.extractor import extract, peek_cached
from ..security import is_accepted_type
from ..storage import load_decrypted, save_encrypted
from ..templating import templates

log = logging.getLogger(__name__)

router = APIRouter()


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
    """Flag the same file uploaded against two different partners.

    This is the failure that does real damage: one partner's Aadhaar uploaded
    as another's bank statement silently copies the first partner's address
    onto the second, and the LLP agreement then carries it into a filing
    looking perfectly plausible. Within a single partner the same file may
    legitimately answer two slots, so only cross-partner reuse is refused.
    """
    from hashlib import sha256

    seen: dict[str, tuple[str, str]] = {}  # digest -> (section, slot label)
    labels = {up["name"]: up["label"] for s in cfg["sections"] for up in s["uploads"]}
    sections = {up["name"]: s["title"] for s in cfg["sections"] for up in s["uploads"]}

    errors: dict = {}
    for slot_name, _meta, _filename, _ctype, data in upload_files:
        digest = sha256(data).hexdigest()
        section = sections.get(slot_name, "")
        if digest in seen:
            first_section, first_label = seen[digest]
            if first_section != section:
                errors[slot_name] = (
                    f"This is the same file uploaded under “{first_section}” as "
                    f"“{first_label}”. Each partner needs their own documents — "
                    "please upload the right one."
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
    return templates.TemplateResponse(
        "client_form.html",
        {"request": request, "form": get_form_config(), "token": token, "errors": None, "values": {}},
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
    by input name, so the client's browser can pre-fill them."""
    link = _get_link(db, token)
    if _link_state(link) != "open":
        return JSONResponse({"ok": False, "error": "This link is no longer active."}, status_code=410)

    cfg = get_form_config()
    meta = cfg["slots"].get(slot)
    if meta is None:
        return JSONResponse({"ok": False, "error": "Unknown upload."}, status_code=400)

    if not is_accepted_type(file.content_type, meta["accept"]):
        return JSONResponse({"ok": False, "error": "Unsupported file type."}, status_code=415)

    data = await file.read()
    if len(data) > _max_file_bytes():
        return JSONResponse(
            {"ok": False, "error": f"File too large (max {settings.max_upload_mb} MB)."}, status_code=413
        )
    if not data:
        return JSONResponse({"ok": False, "error": "Empty file."}, status_code=400)

    raw_text, extracted = extract(meta["extractor"], data, file.content_type)

    # Map canonical extractor keys -> fully-qualified field names this slot fills.
    prefix = meta["section"] + "__"
    fills = set(meta["fills"])
    fields = {}
    for key, value in extracted.items():
        name = prefix + key
        if name in fills and value:
            fields[name] = value

    return JSONResponse({"ok": True, "fields": fields, "found": len(fields), "had_text": bool(raw_text)})


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
             "values": {}},
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
            values[name] = val if _field_visible(field, values) else ""
            if _field_required(field, section, values) and not values[name]:
                errors[name] = "This field is required."
            elif values[name]:
                problem = _format_problem(field, values[name])
                if problem:
                    errors[name] = problem

    # --- uploads ---
    upload_files = []  # (slot_name, meta, filename, content_type, bytes)
    for slot_name, meta in cfg["slots"].items():
        file = form.get(slot_name)
        has_file = file is not None and getattr(file, "filename", "")
        # find the upload cfg to know if required
        required = _upload_required(cfg, slot_name)
        if not has_file:
            if required:
                errors[slot_name] = "Please upload this document."
            continue
        if not is_accepted_type(file.content_type, meta["accept"]):
            errors[slot_name] = "Unsupported file type. Please upload an image or PDF."
            continue
        data = await file.read()
        if len(data) > _max_file_bytes():
            errors[slot_name] = f"File is too large (max {settings.max_upload_mb} MB)."
            continue
        if not data:
            errors[slot_name] = "Uploaded file is empty."
            continue
        upload_files.append((slot_name, meta, file.filename, file.content_type, data))

    errors.update(_duplicate_upload_errors(cfg, upload_files))

    if errors:
        return templates.TemplateResponse(
            "client_form.html",
            {"request": request, "form": cfg, "token": token, "errors": errors, "values": values},
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
    for slot_name, meta, filename, content_type, data in upload_files:
        # Usually already read during autofill, in which case this is free.
        cached = peek_cached(meta["extractor"], data)
        stored = save_encrypted(data)
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
