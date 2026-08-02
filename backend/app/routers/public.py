from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..config_loader import get_form_config
from ..database import get_db
from ..models import Submission, SubmissionLink, Upload
from ..ocr.extractor import extract
from ..security import is_accepted_type
from ..storage import save_encrypted
from ..templating import templates

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


def _field_required(field: dict, section: dict, values: dict) -> bool:
    if not field.get("required"):
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

@router.post("/f/{token}", response_class=HTMLResponse)
async def submit_form(token: str, request: Request, db: Session = Depends(get_db)):
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
            values[name] = val
            if _field_required(field, section, values) and not val:
                errors[name] = "This field is required."

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

    for slot_name, meta, filename, content_type, data in upload_files:
        raw_text, extracted = extract(meta["extractor"], data, content_type)
        stored = save_encrypted(data)
        db.add(
            Upload(
                submission_id=submission.id,
                field_key=slot_name,
                doc_type=meta["extractor"],
                original_filename=filename,
                stored_filename=stored,
                content_type=content_type,
                size_bytes=len(data),
                raw_text=raw_text,
                extracted_data=extracted,
            )
        )

    link.status = "completed"
    link.completed_at = datetime.utcnow()
    db.commit()

    return templates.TemplateResponse("client_done.html", {"request": request})


def _upload_required(cfg: dict, slot_name: str) -> bool:
    for section in cfg["sections"]:
        for up in section["uploads"]:
            if up["name"] == slot_name:
                return bool(up.get("required"))
    return False
