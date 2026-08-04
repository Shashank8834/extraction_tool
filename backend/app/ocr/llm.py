"""Vision-LLM extraction via Google Gemini.

Sends the document image(s) to Gemini and asks for the exact field keys the form
needs, as JSON. Far more reliable than Tesseract on real-world scans/photos.

Enabled by setting LLM_PROVIDER=gemini + GEMINI_API_KEY. If the call fails or
returns nothing, the caller falls back to Tesseract.
"""
import base64
import io
import json
import logging

import httpx
from pdf2image import convert_from_bytes
from PIL import Image

from ..config import settings

log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Per-document instructions. The requested JSON keys match the form field keys.
_PROMPTS = {
    "pan": (
        "This is an Indian PAN card. Read it and return ONLY JSON with these keys: "
        '{"first_name":"","last_name":"","father_first_name":"","father_last_name":"",'
        '"pan_number":"","dob":""}. '
        "Split each person's full name into first name (first word) and last name (the rest). "
        "dob must be DD/MM/YYYY. Use an empty string for anything not present."
    ),
    # The front of an Aadhaar card carries the name and DOB but no address (that
    # is on the back), so ask for every field either side might show and let the
    # caller fill whatever came back.
    "aadhaar": (
        "This is an Indian Aadhaar card — it may show the front, the back, or both. "
        "Return ONLY JSON with these keys: "
        '{"first_name":"","last_name":"","dob":"","present_address":""}. '
        "Split the holder's full name into first name (first word) and last name (the rest). "
        "dob must be DD/MM/YYYY. present_address is the full postal address including PIN "
        "code, which is printed on the back — return an empty string if only the front is "
        "shown. Use an empty string for anything not visible; do not guess."
    ),
    "bank_statement": (
        "This is a bank statement. Return ONLY JSON: "
        '{"permanent_address":""} containing the account holder\'s full address '
        "(include PIN code). Empty string if not found."
    ),
    "utility_bill": (
        "This is an electricity or telephone bill. Return ONLY JSON: "
        '{"office_address":"","utility_name":""} where office_address is the full '
        "service/premises address (include PIN code) and utility_name is the name the "
        "bill is registered to. Empty string for anything not found."
    ),
}


def is_enabled() -> bool:
    return settings.llm_provider.lower() == "gemini" and bool(settings.gemini_api_key)


def _to_images(file_bytes: bytes, content_type: str | None) -> list[tuple[str, bytes]]:
    is_pdf = (content_type or "").lower() == "application/pdf" or file_bytes[:5] == b"%PDF-"
    if is_pdf:
        out = []
        for page in convert_from_bytes(file_bytes, dpi=150, first_page=1, last_page=2):
            buf = io.BytesIO()
            page.save(buf, format="PNG")
            out.append(("image/png", buf.getvalue()))
        return out
    # normalise unknown/odd image types to PNG so Gemini always accepts them
    ct = (content_type or "").lower()
    if ct in ("image/jpeg", "image/png", "image/webp"):
        return [(ct, file_bytes)]
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return [("image/png", buf.getvalue())]


def extract_with_gemini(extractor_name: str, file_bytes: bytes, content_type: str | None) -> dict:
    """Return {field_key: value} for the given document type, or {} if the model
    type is unknown or nothing was read. Raises on transport/API errors so the
    caller can fall back."""
    prompt = _PROMPTS.get(extractor_name)
    if not prompt:
        return {}

    parts = [{"text": prompt}]
    for mime, data in _to_images(file_bytes, content_type):
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    url = _ENDPOINT.format(model=settings.gemini_model)

    resp = httpx.post(url, params={"key": settings.gemini_api_key}, json=body, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    candidate = payload["candidates"][0]
    text = candidate["content"]["parts"][0]["text"]
    data = json.loads(text)
    # keep only non-empty string values
    fields = {k: v.strip() for k, v in data.items() if isinstance(v, str) and v.strip()}

    if not fields:
        # Distinguish "model read the doc but the field isn't on it" (e.g. the
        # front of an Aadhaar card carries no address) from a truncated or
        # blocked response. Log shapes only — never the values, which are PII.
        log.warning(
            "Gemini returned no usable fields for %s: keys=%s finish_reason=%s images=%d",
            extractor_name,
            {k: (len(v) if isinstance(v, str) else type(v).__name__) for k, v in data.items()},
            candidate.get("finishReason"),
            len(parts) - 1,
        )
    return fields
