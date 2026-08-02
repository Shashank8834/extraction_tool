"""Parsers that turn raw OCR text from documents into structured fields.

Each parser returns a dict whose keys match the form field keys they fill
(see form_config.yaml). OCR is noisy, so every value is best-effort — the
client reviews and edits before submitting, and the admin sees the raw text.

Reliability, high -> low:
  PAN, Aadhaar (fixed layouts)  >  Aadhaar address  >  bank statement, utility
The messy ones are the intended targets for a future local-AI extractor
(swap the entry in ocr/extractor.py:EXTRACTORS).
"""
import re

# --- shared regexes ----------------------------------------------------------

PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")
DOB_RE = re.compile(r"\b(\d{2}[/\-]\d{2}[/\-]\d{4})\b")
PIN_RE = re.compile(r"\b(\d{6})\b")
AADHAAR_RE = re.compile(r"\b(\d{4}\s?\d{4}\s?\d{4})\b")
YOB_RE = re.compile(r"(?:year of birth|yob)\D*(\d{4})", re.IGNORECASE)
GENDER_RE = re.compile(r"\b(male|female|transgender)\b", re.IGNORECASE)


def _split_name(full: str) -> tuple[str, str]:
    """Split a full name into (first, last). Everything after the first token
    becomes the last name."""
    parts = (full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# --- PAN ---------------------------------------------------------------------

def parse_pan(text: str) -> dict:
    up = text.upper()
    result: dict = {}

    m = PAN_RE.search(up.replace(" ", ""))
    if m:
        result["pan_number"] = m.group(1)

    dob = DOB_RE.search(up)
    if dob:
        result["dob"] = dob.group(1).replace("-", "/")

    name = _value_for(text, {"name"})
    if name:
        first, last = _split_name(name)
        result["first_name"] = first
        if last:
            result["last_name"] = last

    father = _value_for(text, {"fathers name", "father name"})
    if father:
        first, last = _split_name(father)
        result["father_first_name"] = first
        if last:
            result["father_last_name"] = last

    return result


# --- Aadhaar -----------------------------------------------------------------

def parse_aadhaar(text: str) -> dict:
    result: dict = {}

    address = _address_block(text, labels=("address", "s/o", "d/o", "c/o", "w/o"))
    if address:
        result["present_address"] = address

    return result


# --- Bank statement (best-effort) -------------------------------------------

def parse_bank_statement(text: str) -> dict:
    result: dict = {}
    address = _address_block(text, labels=("address", "customer address", "mailing address"))
    if address:
        result["permanent_address"] = address
    return result


# --- Utility bill (best-effort) ---------------------------------------------

def parse_utility_bill(text: str) -> dict:
    result: dict = {}

    address = _address_block(text, labels=("address", "installation address", "service address", "premise"))
    if address:
        result["office_address"] = address

    name = _value_for(text, {"name", "consumer name", "customer name", "account name"})
    if name:
        result["utility_name"] = name

    return result


# --- labelled-value helper (shared) -----------------------------------------

_NON_ALPHA = re.compile(r"[^a-z ]")

_KNOWN_LABELS = {
    "name", "fathers name", "father name", "consumer name", "customer name",
    "account name", "account holder", "permanent account number",
    "permanent account number card", "income tax department", "govt of india",
    "government of india", "date of birth", "signature", "dob", "address",
    "installation address", "service address", "customer address", "premise",
}


def _norm(s: str) -> str:
    return _NON_ALPHA.sub("", s.lower()).strip()


def _looks_like_label(line: str) -> bool:
    return _norm(line) in _KNOWN_LABELS


def _value_for(text: str, labels: set[str]) -> str | None:
    """Find a labelled value. Handles 'Label: value' and the layout where the
    value sits on the line(s) after a standalone label."""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        if ":" in raw:
            label_part, after = raw.split(":", 1)
            if _norm(label_part) in labels:
                after = after.strip()
                if after and re.search(r"[A-Za-z]", after):
                    return after
        if _norm(raw) not in labels:
            continue
        for nxt in lines[i + 1 : i + 4]:
            cand = nxt.strip()
            if (
                cand
                and re.search(r"[A-Za-z]", cand)
                and not _looks_like_label(cand)
                and not PAN_RE.search(cand.replace(" ", ""))
            ):
                return cand
    return None


def _address_block(text: str, labels: tuple) -> str | None:
    """Best-effort multi-line address extraction.

    Strategy 1: after an address-ish label, collect lines until a PIN code
    (inclusive) or a blank run.
    Strategy 2 (fallback): the line containing a 6-digit PIN plus the two lines
    above it.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    label_set = {re.sub(r"[^a-z/ ]", "", lbl.lower()).strip() for lbl in labels}

    # Strategy 1 — label driven
    for i, raw in enumerate(lines):
        low = re.sub(r"[^a-z/ ]", "", raw.lower()).strip()
        matched = any(low.startswith(lbl) or (":" in raw and lbl in low) for lbl in label_set)
        if not matched:
            continue
        collected = []
        # value may start on the same line after a colon
        if ":" in raw:
            tail = raw.split(":", 1)[1].strip()
            if tail:
                collected.append(tail)
        for nxt in lines[i + 1 : i + 7]:
            if not nxt:
                if collected:
                    break
                continue
            collected.append(nxt)
            if PIN_RE.search(nxt):
                break
        block = _clean_address(" ".join(collected))
        if block:
            return block

    # Strategy 2 — PIN anchored
    for i, raw in enumerate(lines):
        if PIN_RE.search(raw):
            window = [x for x in lines[max(0, i - 2): i + 1] if x]
            block = _clean_address(" ".join(window))
            if block and PIN_RE.search(block):
                return block

    return None


def _clean_address(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" ,;:-")
    # drop obvious noise-only results
    if len(s) < 6 or not re.search(r"[A-Za-z]", s):
        return ""
    return s


# --- Verhoeff (Aadhaar checksum, kept for optional validation) ---------------

_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    try:
        c = 0
        for i, digit in enumerate(reversed(number)):
            c = _D[c][_P[i % 8][int(digit)]]
        return c == 0
    except (ValueError, IndexError):
        return False
