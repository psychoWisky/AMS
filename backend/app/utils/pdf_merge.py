"""PDF validation and merging for the Synopsis module (pypdf, pure Python).

Chromium (app/utils/pdf.py) renders the AMS-generated pages; it cannot embed an
existing PDF. This module validates a student's uploaded PDF and merges it,
scaled to fit A4, between the AMS-rendered front and approval pages so the final
Synopsis package is one A4 PDF. No LibreOffice, no Word conversion.
"""
from __future__ import annotations

import io

from pypdf import PageObject, PdfReader, PdfWriter, Transformation

A4_WIDTH_PT = 595.276   # 210 mm
A4_HEIGHT_PT = 841.890  # 297 mm
_TOLERANCE_PT = 1.5
MAX_PAGES = 200


class InvalidPdf(Exception):
    """The bytes are not a usable, unencrypted PDF."""


def inspect_pdf(data: bytes) -> int:
    """Validate `data` as a real, readable, unencrypted PDF and return its page
    count. The magic bytes are checked by the caller; this proves the structure
    actually parses (a file that merely starts with `%PDF-` is rejected)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InvalidPdf("Password-protected or encrypted PDFs are not accepted.")
        count = len(reader.pages)
        if count < 1:
            raise InvalidPdf("The PDF has no pages.")
        # Force real parsing of the first page's geometry; a truncated/garbage file fails here.
        first = reader.pages[0]
        float(first.mediabox.width), float(first.mediabox.height)
    except InvalidPdf:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many concrete types for malformed input
        raise InvalidPdf("The file is not a valid PDF document.") from exc
    if count > MAX_PAGES:
        raise InvalidPdf(f"The PDF has {count} pages; the limit is {MAX_PAGES}.")
    return count


def page_sizes(data: bytes) -> list[tuple[float, float]]:
    reader = PdfReader(io.BytesIO(data))
    return [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]


def is_a4(width: float, height: float) -> bool:
    """True for A4 in either orientation."""
    return any(
        abs(width - w) <= _TOLERANCE_PT and abs(height - h) <= _TOLERANCE_PT
        for w, h in ((A4_WIDTH_PT, A4_HEIGHT_PT), (A4_HEIGHT_PT, A4_WIDTH_PT))
    )


def _fit_to_a4(src: PageObject) -> PageObject:
    """Return an A4 page (same orientation as the source) showing `src` scaled
    uniformly to fit and centred; a page that is already A4 is returned as-is."""
    src.transfer_rotation_to_content()
    width, height = float(src.mediabox.width), float(src.mediabox.height)
    left, bottom = float(src.mediabox.left), float(src.mediabox.bottom)
    landscape = width > height
    target_w, target_h = (A4_HEIGHT_PT, A4_WIDTH_PT) if landscape else (A4_WIDTH_PT, A4_HEIGHT_PT)
    if is_a4(width, height) and abs(left) < _TOLERANCE_PT and abs(bottom) < _TOLERANCE_PT:
        return src
    scale = min(target_w / width, target_h / height)
    blank = PageObject.create_blank_page(width=target_w, height=target_h)
    tx = (target_w - width * scale) / 2 - left * scale
    ty = (target_h - height * scale) / 2 - bottom * scale
    blank.merge_transformed_page(src, Transformation().scale(scale, scale).translate(tx, ty))
    return blank


def merge_synopsis_package(front_pdf: bytes, uploaded_pdf: bytes, approval_pdf: bytes, title: str = "Synopsis") -> bytes:
    """front pages + the student's uploaded pages (each fitted to A4) + approval pages -> one PDF."""
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(front_pdf)).pages:
        writer.add_page(page)
    for page in PdfReader(io.BytesIO(uploaded_pdf)).pages:
        writer.add_page(_fit_to_a4(page))
    for page in PdfReader(io.BytesIO(approval_pdf)).pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": title[:200], "/Producer": "AVFU AMS"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
