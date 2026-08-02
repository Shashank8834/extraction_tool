import hmac
import os
import re

from cryptography.fernet import Fernet

from .config import settings

# --- File encryption at rest -------------------------------------------------

_fernet: Fernet | None = None
if settings.file_encryption_key:
    _fernet = Fernet(settings.file_encryption_key.encode())


def encrypt_bytes(data: bytes) -> bytes:
    if _fernet is None:
        return data
    return _fernet.encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    if _fernet is None:
        return data
    return _fernet.decrypt(data)


# --- Admin auth --------------------------------------------------------------

def verify_admin(username: str, password: str) -> bool:
    """Constant-time comparison against configured admin credentials."""
    user_ok = hmac.compare_digest(username or "", settings.admin_username)
    pass_ok = hmac.compare_digest(password or "", settings.admin_password)
    return user_ok and pass_ok


# --- Upload safety -----------------------------------------------------------

# Content types we're willing to render inline in the admin's browser. Anything
# else is forced to download, so a malicious upload can never execute in the
# admin's session origin.
_INLINE_SAFE_PREFIXES = ("image/",)
_INLINE_SAFE_EXACT = {"application/pdf"}


def is_inline_safe(content_type: str | None) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct in _INLINE_SAFE_EXACT or any(ct.startswith(p) for p in _INLINE_SAFE_PREFIXES)


def is_accepted_type(content_type: str | None, accept: str | None) -> bool:
    """Server-side check that an upload matches the field's `accept` rule
    (e.g. 'image/*,application/pdf'). Empty accept -> allow anything."""
    if not accept:
        return True
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return False
    for rule in accept.split(","):
        rule = rule.strip().lower()
        if not rule:
            continue
        if rule.endswith("/*") and ct.startswith(rule[:-1]):
            return True
        if rule == ct:
            return True
    return False


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str | None) -> str:
    """Strip any path and reduce to a safe basename for Content-Disposition."""
    base = os.path.basename(name or "").replace("\\", "/").split("/")[-1]
    base = _SAFE_FILENAME_RE.sub("_", base).strip("._") or "file"
    return base[:120]
