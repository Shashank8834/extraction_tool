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


# What the first few bytes of a file say it is. Browsers are not reliable here:
# a phone sharing a PDF, or any file whose extension the OS does not recognise,
# arrives as application/octet-stream, and the accept rule then refuses a
# perfectly good PAN card as the wrong type. The bytes do not lie.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    # A Word file or a zip of scans: named so the accept rule can refuse it
    # with "unsupported file type" rather than letting it through unread.
    (b"PK\x03\x04", "application/zip"),
)

# HEIC/HEIF — what an iPhone produces by default, and a common way a photo of a
# PAN card arrives. The brand sits at offset 4, after the box length.
_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1"}

# Types that tell us nothing, so the bytes get the final say.
_UNINFORMATIVE = {"", "application/octet-stream", "binary/octet-stream", "text/plain"}


def sniff_content_type(data: bytes, declared: str | None) -> str:
    """The file's real content type, preferring its bytes over the browser's
    claim and falling back to the claim when the bytes are unfamiliar."""
    head = data[:16]
    for magic, ctype in _MAGIC:
        if head.startswith(magic):
            return ctype
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12].lower() in _HEIF_BRANDS:
        return "image/heic"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    stated = (declared or "").split(";", 1)[0].strip().lower()
    return "" if stated in _UNINFORMATIVE else stated


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
