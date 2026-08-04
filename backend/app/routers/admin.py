import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..config_loader import get_form_config
from ..database import get_db
from ..models import Submission, SubmissionLink, Upload
from ..security import is_inline_safe, sanitize_filename, verify_admin
from ..storage import delete_file, load_decrypted
from ..templating import templates

router = APIRouter(prefix="/admin")


def _is_authed(request: Request) -> bool:
    return bool(request.session.get("admin"))


def _require_auth(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


# --- auth --------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _is_authed(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_admin(username, password):
        request.session["admin"] = username
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": "Invalid credentials."}, status_code=401
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# --- dashboard ---------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    links = db.query(SubmissionLink).order_by(SubmissionLink.created_at.desc()).all()
    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "links": links,
            "base_url": settings.base_url,
            "default_expiry_days": settings.link_expiry_days,
        },
    )


@router.post("/links")
def create_link(
    request: Request,
    label: str = Form(""),
    expiry_days: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_auth(request)
    if redirect:
        return redirect

    # Per-link override; blank falls back to the configured default.
    try:
        days = int(expiry_days) if expiry_days.strip() else settings.link_expiry_days
    except ValueError:
        days = settings.link_expiry_days

    expires_at = datetime.utcnow() + timedelta(days=days) if days > 0 else None

    link = SubmissionLink(label=label.strip(), expires_at=expires_at)
    db.add(link)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/links/{link_id}/delete")
def delete_link(link_id: str, request: Request, db: Session = Depends(get_db)):
    """Permanently remove a link, and with it any submission and uploaded
    documents. Intended for links created by mistake; POST-only so a link
    can't be deleted by a stray GET (prefetch, crawler, pasted URL)."""
    redirect = _require_auth(request)
    if redirect:
        return redirect

    link = db.query(SubmissionLink).filter(SubmissionLink.id == link_id).first()
    if link is None:
        return RedirectResponse("/admin", status_code=303)

    # Delete the encrypted blobs before the rows: the DB rows are the only
    # record of which files on disk belong to this link, so dropping them first
    # would strand the blobs in the upload volume forever.
    if link.submission is not None:
        for upload in link.submission.uploads:
            delete_file(upload.stored_filename)

    db.delete(link)  # cascades to the submission and its upload rows
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.get("/submissions/{link_id}", response_class=HTMLResponse)
def view_submission(link_id: str, request: Request, db: Session = Depends(get_db)):
    redirect = _require_auth(request)
    if redirect:
        return redirect
    link = db.query(SubmissionLink).filter(SubmissionLink.id == link_id).first()
    if link is None or link.submission is None:
        return RedirectResponse("/admin", status_code=303)
    submission = link.submission
    uploads_by_slot = {u.field_key: u for u in submission.uploads}
    return templates.TemplateResponse(
        "admin_submission.html",
        {
            "request": request,
            "link": link,
            "submission": submission,
            "form": get_form_config(),
            "uploads_by_slot": uploads_by_slot,
        },
    )


@router.get("/files/{upload_id}")
def download_file(upload_id: str, request: Request, db: Session = Depends(get_db)):
    if not _is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if upload is None:
        return RedirectResponse("/admin", status_code=303)
    data = load_decrypted(upload.stored_filename)

    # Only render known-safe types inline; everything else is forced to download
    # and served as an opaque octet-stream so it can't execute in the admin's
    # browser origin. nosniff stops the browser second-guessing the type.
    inline = is_inline_safe(upload.content_type)
    media_type = upload.content_type if inline else "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    filename = sanitize_filename(upload.original_filename)

    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
