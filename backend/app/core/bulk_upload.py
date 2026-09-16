"""Generic Excel/CSV bulk-upload structural parsing helpers (Bulk Faculty/
User Excel Upload task, this revision).

Deliberately a NEW, generalized module — not a refactor of
`app/api/v1/endpoints/orientation.py`'s own private bulk-upload helpers,
which are left completely untouched so that already-shipped feature carries
zero risk from this one. The two implementations independently follow the
same proven shape (structural file parse -> per-row business validation ->
all-or-nothing insert) rather than sharing a code path — this module exists
so THIS feature's two upload endpoints (HOD Faculty, Super Admin User) don't
duplicate their OWN parsing logic between each other.

Only structural concerns live here: file extension, encoding, header
matching, blank-row skipping, cell-to-string normalization. Row-level
business validation (required fields, department/college/designation/role
resolution, duplicate detection) is the caller's responsibility, exactly
like orientation.py's own `_validate_bulk_rows` is separate from its
`_parse_bulk_upload_file`.
"""
import csv
import io
from typing import Optional

import openpyxl
from fastapi import HTTPException


def bulk_cell_to_str(v) -> str:
    """Cell -> trimmed string. Excel stores whole numbers (e.g. a Mobile
    number typed as digits) as floats like 9999999999.0 — normalized back to
    a plain integer string rather than passed through with a misleading
    '.0' suffix. Never executes formulas: openpyxl (data_only=True) only
    ever returns a cached literal value or None, never evaluates one."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse_bulk_headers(raw_headers: list, columns: list[str]) -> dict[str, int]:
    """Maps each canonical column name -> its column index. Exact match only
    (after trimming surrounding whitespace) — a misspelled or renamed
    required column is never silently accepted. `columns` is the caller's
    own canonical column list (every one of them required to be present as
    a header, regardless of whether every row must fill it in)."""
    seen: dict[str, int] = {}
    unknown: list[str] = []
    for idx, h in enumerate(raw_headers):
        name = bulk_cell_to_str(h)
        if not name:
            continue
        if name not in columns:
            unknown.append(name)
            continue
        if name in seen:
            raise HTTPException(400, f"Duplicate column header: '{name}'.")
        seen[name] = idx
    if unknown:
        raise HTTPException(
            400,
            f"Unrecognized column header(s): {', '.join(unknown)}. "
            f"Expected exactly: {', '.join(columns)}.",
        )
    missing = [c for c in columns if c not in seen]
    if missing:
        raise HTTPException(400, f"Missing required column(s): {', '.join(missing)}.")
    return seen


def parse_bulk_upload_file(filename: Optional[str], content: bytes, columns: list[str]) -> list[dict]:
    """Structural file parsing only (extension, encoding, headers, blank-row
    handling) — raises HTTPException(400, ...) for anything that means the
    file itself cannot be read at all. Row-level BUSINESS validation happens
    separately in the caller so every row's errors can be collected
    together rather than failing fast on the first bad row."""
    ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if ext not in ("xlsx", "csv"):
        raise HTTPException(400, "Unsupported file type. Please upload a .xlsx or .csv file.")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")

    if ext == "xlsx":
        try:
            # data_only=True reads each cell's last-saved literal value,
            # never a formula string, and openpyxl never evaluates formulas
            # anyway — no spreadsheet content is ever executed.
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            raw_rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "Could not read the uploaded file. Please ensure it is a valid .xlsx file.")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(400, "Could not read the uploaded file. Please ensure it is a UTF-8 encoded .csv file.")
        raw_rows = list(csv.reader(io.StringIO(text)))

    if not raw_rows:
        raise HTTPException(400, "The uploaded file has no data rows.")

    col_index = parse_bulk_headers(raw_rows[0], columns)

    parsed: list[dict] = []
    for i, r in enumerate(raw_rows[1:], start=2):  # row 1 is the header row
        if not any(bulk_cell_to_str(c) for c in r):
            continue  # blank row — skipped safely, not an error
        values = {name: bulk_cell_to_str(r[idx]) if idx < len(r) else "" for name, idx in col_index.items()}
        parsed.append({"row": i, "values": values})

    if not parsed:
        raise HTTPException(400, "The uploaded file has no data rows.")
    return parsed
