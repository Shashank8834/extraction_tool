"""Admin exports: submissions as Excel, uploaded documents as a ZIP.

The per-submission sheet deliberately mirrors the layout of the intake
spreadsheet the office already works from — Particular / Details / Notes, with
a heading row per section — so a completed submission drops straight into the
existing process.
"""
import io
import zipfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .security import sanitize_filename
from .storage import load_decrypted

_FONT = "Arial"
_HEAD = Font(name=_FONT, size=11, bold=True, color="FFFFFF")
_SECTION = Font(name=_FONT, size=11, bold=True)
_LABEL = Font(name=_FONT, size=10, bold=True)
_BODY = Font(name=_FONT, size=10)
_NOTE = Font(name=_FONT, size=9, italic=True, color="666666")
_HEAD_FILL = PatternFill("solid", fgColor="1A1A1A")
_SECTION_FILL = PatternFill("solid", fgColor="EDEDED")
_THIN = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WRAP = Alignment(vertical="top", wrap_text=True)


def _autosize(ws, widths: dict[int, int]) -> None:
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width


def _write_row(ws, row: int, cells: list, font: Font, fill: PatternFill | None = None) -> None:
    for col, value in enumerate(cells, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = font
        cell.alignment = _WRAP
        cell.border = _BORDER
        if fill is not None:
            cell.fill = fill


def submission_workbook(cfg: dict, link, submission, uploads_by_slot: dict) -> bytes:
    """One submission, laid out section by section."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Details"

    _write_row(ws, 1, ["Particular", "Details", "Notes"], _HEAD, _HEAD_FILL)
    ws.freeze_panes = "A2"

    row = 2
    meta = [
        ("Client / reference", link.label or "—"),
        ("Submitted (UTC)", submission.submitted_at.strftime("%Y-%m-%d %H:%M")),
        ("Submitted from IP", submission.client_ip or "—"),
    ]
    for label, value in meta:
        _write_row(ws, row, [label, value, ""], _BODY)
        ws.cell(row=row, column=1).font = _LABEL
        row += 1
    row += 1

    for section in cfg["sections"]:
        _write_row(ws, row, [section["title"], "", section.get("note", "")], _SECTION, _SECTION_FILL)
        row += 1

        for up in section["uploads"]:
            upload = uploads_by_slot.get(up["name"])
            _write_row(
                ws, row,
                [up["label"], upload.original_filename if upload else "Not uploaded",
                 "Attached document"],
                _BODY,
            )
            ws.cell(row=row, column=1).font = _LABEL
            ws.cell(row=row, column=3).font = _NOTE
            row += 1

        for field in section["fields"]:
            value = (submission.form_data or {}).get(field["name"]) or ""
            _write_row(ws, row, [field["label"], value, field.get("help", "")], _BODY)
            ws.cell(row=row, column=1).font = _LABEL
            ws.cell(row=row, column=3).font = _NOTE
            row += 1

        row += 1

    _autosize(ws, {1: 42, 2: 52, 3: 46})
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def all_submissions_workbook(cfg: dict, records: list[tuple]) -> bytes:
    """Every submission as one row, for bulk review. `records` is a list of
    (link, submission) pairs, newest first."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Submissions"

    fields = [f for s in cfg["sections"] for f in s["fields"]]
    header = ["Client / reference", "Submitted (UTC)"] + [
        f"{s['title']} — {f['label']}" for s in cfg["sections"] for f in s["fields"]
    ]
    _write_row(ws, 1, header, _HEAD, _HEAD_FILL)
    ws.freeze_panes = "C2"

    for row, (link, submission) in enumerate(records, start=2):
        data = submission.form_data or {}
        values = [link.label or "—", submission.submitted_at.strftime("%Y-%m-%d %H:%M")]
        values += [data.get(f["name"], "") for f in fields]
        _write_row(ws, row, values, _BODY)

    _autosize(ws, {1: 26, 2: 18, **{i: 30 for i in range(3, len(header) + 1)}})
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def attachments_zip(cfg: dict, link, submission) -> bytes:
    """Every uploaded document for one submission, decrypted, in a single ZIP.

    Names are prefixed with the slot they were uploaded against, so two files
    called scan.jpg from different partners stay distinguishable.
    """
    labels = {up["name"]: up["label"] for s in cfg["sections"] for up in s["uploads"]}
    folder = sanitize_filename(link.label or "submission") or "submission"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = [
            f"Client / reference : {link.label or '—'}",
            f"Submitted (UTC)    : {submission.submitted_at.strftime('%Y-%m-%d %H:%M')}",
            f"Exported (UTC)     : {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            "",
            "Files:",
        ]
        seen: set[str] = set()
        for upload in submission.uploads:
            label = labels.get(upload.field_key, upload.field_key)
            name = f"{sanitize_filename(label)}__{sanitize_filename(upload.original_filename)}"
            # a duplicate name would silently overwrite inside the archive
            base, n = name, 2
            while name in seen:
                name, n = f"{n}_{base}", n + 1
            seen.add(name)
            try:
                zf.writestr(f"{folder}/{name}", load_decrypted(upload.stored_filename))
                manifest.append(f"  {name}")
            except FileNotFoundError:
                # A missing blob must not sink the whole download.
                manifest.append(f"  {name}  [MISSING ON DISK]")
        zf.writestr(f"{folder}/manifest.txt", "\n".join(manifest) + "\n")

    return buf.getvalue()
