import os
import uuid

from .config import settings
from .security import decrypt_bytes, encrypt_bytes


def save_encrypted(file_bytes: bytes) -> str:
    """Encrypt and write bytes to the upload dir. Returns the stored filename."""
    os.makedirs(settings.upload_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.enc"
    path = os.path.join(settings.upload_dir, stored_name)
    with open(path, "wb") as fh:
        fh.write(encrypt_bytes(file_bytes))
    return stored_name


def load_decrypted(stored_filename: str) -> bytes:
    path = os.path.join(settings.upload_dir, stored_filename)
    with open(path, "rb") as fh:
        return decrypt_bytes(fh.read())


def delete_file(stored_filename: str) -> None:
    """Remove a stored blob. Missing files are ignored — a delete must not fail
    just because the blob is already gone."""
    try:
        os.remove(os.path.join(settings.upload_dir, stored_filename))
    except FileNotFoundError:
        pass
