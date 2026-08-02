# Client Intake & Document Extraction Tool

Send a secure link to a client → they fill in their details and upload documents
(Aadhaar / PAN / etc.) → the documents are **OCR-processed on your own server**
and the extracted text lands in your admin dashboard. Built to self-host with
Docker on a VPS.

Currently configured for **LLP incorporation intake** (two designated partners,
capital/sharing, registered office), but the form is fully config-driven.

## Highlights

- **Config-driven form** — edit `backend/app/form_config.yaml` to change every
  field, section, upload, and extraction mapping. No code changes.
- **Live autofill** — when the client uploads a document, it's OCR'd immediately
  and the related fields pre-fill in their browser; they review and edit before
  submitting.
- **Offline OCR** — extraction runs locally via Tesseract. Confidential data
  never leaves your server.
- **Encrypted at rest** — uploaded files are Fernet-encrypted on disk and served
  only through the authenticated admin.
- **One-time links** — each client link works once, then locks.
- **Link expiry** — links expire after a configurable window (`LINK_EXPIRY_DAYS`,
  default 7; override per-link when generating). `0` = never expires.
- **Hardened** — CSP + security headers, uploads never rendered inline in the
  admin origin, server-side type/size validation, encrypted file storage.

## Document extraction

| Document the client uploads | Fields it fills |
|---|---|
| **PAN** | First/Last name, Father's first/last name, PAN number, Date of Birth |
| **Aadhaar** | Present address |
| **Bank statement** | Permanent address *(best-effort)* |
| **Utility bill** (electricity/telephone) | Registered office address, name on bill *(best-effort)* |

PAN and Aadhaar have fixed layouts and extract reliably. Bank statements and
utility bills vary by provider, so those are best-effort — always shown to the
client to confirm/edit, and to you with the raw OCR text.

### Upgrading the "messy" documents to local AI later

Extractors are pluggable. `backend/app/ocr/extractor.py` has an `EXTRACTORS`
registry mapping each document type to a parser. To boost accuracy for
`bank_statement` / `utility_bill`, replace those entries with a callable that
runs a **local** vision/LLM model (keeping data on your VPS) and returns the
same `{field_key: value}` dict. Nothing else changes.

## How it works

1. Log into `/admin`, generate a link, and send it to your client.
2. Client opens `/f/<token>`; as they upload each document it's read and the
   matching fields pre-fill. They review, correct anything, and submit.
3. You review everything — grouped by section, with extracted fields, raw OCR
   text, and the original encrypted file — in the dashboard.

## Setup

```bash
cp .env.example .env
```

Generate the secrets and paste them into `.env`:

```bash
# SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"

# FILE_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set `ADMIN_USERNAME`, `ADMIN_PASSWORD`, a strong `POSTGRES_PASSWORD`, and
`BASE_URL` (your public https URL in production).

Then:

```bash
docker compose up -d --build
```

The app listens on `127.0.0.1:8000`. Put a reverse proxy in front for HTTPS
(see `Caddyfile.example`).

## Changing the form

Everything about the form lives in `backend/app/form_config.yaml`:

- `partner_template` — the field/upload set repeated for each partner. Add a
  partner by adding one line under `partners:`.
- `sections` — the non-repeating sections (LLP names, capital, office, …), in
  render order. `position: top` renders before the partner blocks.
- A field's `fill_from: <upload_key>` links it to an upload; the upload's
  `extractor` (`pan`/`aadhaar`/`bank_statement`/`utility_bill`) decides how it's
  read. Extractor output keys must match the field `key` (see the comments at
  the top of the file).
- `hide_if_din: true` hides + un-requires a field once that partner enters a DIN.

After editing, restart the app:

```bash
docker compose restart app
```

## Security notes

- `.env` holds all secrets — never commit it.
- Postgres is not exposed to the host; only the app talks to it.
- The app binds to localhost; terminate TLS at the reverse proxy.
- Files on disk are encrypted with `FILE_ENCRYPTION_KEY`. **Back this key up** —
  losing it means the stored files can't be decrypted.

## Tests

An end-to-end suite (parsers, live-extract endpoint, submission, conditional
validation, security headers) runs without Docker/Tesseract:

```bash
python backend/tests/test_e2e.py
```

## Project layout

```
backend/
  app/
    form_config.yaml     <- edit me for fields / uploads / extraction mapping
    config_loader.py     <- expands the config into render-ready sections
    main.py              <- FastAPI app + security headers
    models.py            <- DB tables
    ocr/
      extractor.py       <- Tesseract OCR + EXTRACTORS registry (AI swap-point)
      parsers.py         <- PAN / Aadhaar / bank / utility field parsing
    routers/
      public.py          <- client form + live /extract autofill endpoint
      admin.py           <- dashboard + auth + secure file serving
    templates/  static/
  tests/test_e2e.py
docker-compose.yml
```
