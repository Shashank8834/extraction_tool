"""OCR + structured extraction. Everything runs locally via Tesseract — nothing
leaves the host.

`EXTRACTORS` maps a document type to a parser. To upgrade a messy document type
(e.g. bank_statement, utility_bill) to a local AI model later, replace its entry
here with a callable that takes raw OCR text (or, if you prefer image input,
change `extract` to hand the model the bytes) and returns the same field dict.
"""
import io
import json

import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps

from . import llm, parsers

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


def extract(extractor_name: str, file_bytes: bytes, content_type: str | None) -> tuple[str, dict]:
    """Return (raw_text, extracted_fields). Never raises — failures return empty
    results so a submission/autofill is never lost.

    Uses the vision LLM (Gemini) when configured, falling back to Tesseract."""
    # 1. Vision LLM, if enabled
    if llm.is_enabled():
        try:
            fields = llm.extract_with_gemini(extractor_name, file_bytes, content_type)
            if fields:
                summary = "[read by Gemini vision]\n" + json.dumps(fields, ensure_ascii=False, indent=2)
                return summary, fields
        except Exception:  # noqa: BLE001 - fall back to Tesseract
            pass

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
