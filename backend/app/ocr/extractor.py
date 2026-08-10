"""OCR + structured extraction. Everything runs locally via Tesseract — nothing
leaves the host.

`EXTRACTORS` maps a document type to a parser. To upgrade a messy document type
(e.g. bank_statement, utility_bill) to a local AI model later, replace its entry
here with a callable that takes raw OCR text (or, if you prefer image input,
change `extract` to hand the model the bytes) and returns the same field dict.
"""
import hashlib
import io
import json
import logging
from collections import OrderedDict

import httpx
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps

from . import llm, parsers

log = logging.getLogger(__name__)

# doc type -> function(raw_text) -> {field_key: value}
EXTRACTORS = {
    "pan": parsers.parse_pan,
    "aadhaar": parsers.parse_aadhaar,
    "bank_statement": parsers.parse_bank_statement,
    "utility_bill": parsers.parse_utility_bill,
    "generic": lambda _text: {},
}


def _ocr_image(img: Image.Image) -> str:
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    return pytesseract.image_to_string(gray)


def run_ocr(file_bytes: bytes, content_type: str | None) -> str:
    is_pdf = (content_type or "").lower() == "application/pdf" or file_bytes[:5] == b"%PDF-"
    texts: list[str] = []
    if is_pdf:
        pages = convert_from_bytes(file_bytes, dpi=200, first_page=1, last_page=3)
        for page in pages:
            texts.append(_ocr_image(page))
    else:
        texts.append(_ocr_image(Image.open(io.BytesIO(file_bytes))))
    return "\n".join(texts).strip()


# --- result cache -----------------------------------------------------------
# The same file is read twice: once when the client picks it (live autofill) and
# again when they submit. A vision-LLM round trip per document is the slow part
# of both, so the second read is served from here, keyed by content hash. Only
# successful reads are cached — a failed one should be retried, not remembered.
_CACHE: "OrderedDict[tuple[str, str], tuple[str, dict]]" = OrderedDict()
_CACHE_MAX = 64


def _cache_key(extractor_name: str, file_bytes: bytes) -> tuple[str, str]:
    return (extractor_name, hashlib.sha256(file_bytes).hexdigest())


def peek_cached(extractor_name: str, file_bytes: bytes) -> tuple[str, dict] | None:
    """The already-computed result for this exact file, or None. Lets the submit
    reuse the autofill's work instead of paying for the extraction again."""
    return _CACHE.get(_cache_key(extractor_name, file_bytes))


def extract(extractor_name: str, file_bytes: bytes, content_type: str | None) -> tuple[str, dict]:
    """Return (raw_text, extracted_fields). Never raises — failures return empty
    results so a submission/autofill is never lost. Results are cached by content
    hash; see `peek_cached`."""
    key = _cache_key(extractor_name, file_bytes)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit

    result = _extract_uncached(extractor_name, file_bytes, content_type)
    if result[1]:
        _CACHE[key] = result
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return result


def _extract_uncached(extractor_name: str, file_bytes: bytes, content_type: str | None) -> tuple[str, dict]:
    """Uses the vision LLM (Gemini) when configured, falling back to Tesseract."""
    # 1. Vision LLM, if enabled
    if llm.is_enabled():
        try:
            fields = llm.extract_with_gemini(extractor_name, file_bytes, content_type)
            if fields:
                summary = "[read by Gemini vision]\n" + json.dumps(fields, ensure_ascii=False, indent=2)
                return summary, fields
            log.warning("Gemini returned no fields for %s; falling back to Tesseract", extractor_name)
        except httpx.HTTPStatusError as exc:  # noqa: PERF203 - each branch logs differently
            # Surface the API's own message (bad key, quota, unknown model) — without
            # it, an API failure is indistinguishable from "no key configured".
            log.warning(
                "Gemini HTTP %s for %s: %s",
                exc.response.status_code, extractor_name, exc.response.text[:300],
            )
        except Exception as exc:  # noqa: BLE001 - fall back to Tesseract
            log.warning("Gemini extraction failed for %s: %r", extractor_name, exc)

    # 2. Tesseract fallback
    try:
        raw_text = run_ocr(file_bytes, content_type)
    except Exception as exc:  # noqa: BLE001 - OCR must be non-fatal
        return f"[OCR failed: {exc}]", {}

    parser = EXTRACTORS.get(extractor_name, EXTRACTORS["generic"])
    try:
        fields = parser(raw_text) or {}
    except Exception:  # noqa: BLE001 - parsing must be non-fatal
        fields = {}
    return raw_text, fields
