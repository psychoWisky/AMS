"""AMS-owned headless-Chromium HTML -> PDF rendering (PPW Phase 3).

This is an independent implementation, specific to AMS. It does not import,
extend, or depend on anything from eFMS (eFMS/backend/app/utils/html_pdf.py
and doc_convert.py are a separate project's own files and are never
referenced here). AMS has no LibreOffice-based conversion path at all —
Playwright/Chromium is the only PDF mechanism this module provides.

Requires, on this AMS server (see requirements/requirements.txt):
    pip install -r requirements/requirements.txt   # installs playwright
    python -m playwright install chromium
    # on Linux, additionally if the OS is missing shared libraries:
    python -m playwright install-deps chromium

If Playwright or its Chromium binary is missing, render_ppw_pdf raises
ChromiumUnavailable; callers should turn that into a 503, not a raw 500.
"""
from __future__ import annotations

import base64
import functools
from pathlib import Path

# The AVFU logo lives at the AMS project root (AMS/avfu_logo.png), one level
# above backend/ — added there directly (not under backend/ or frontend/),
# so this is the one stable, AMS-owned anchor for it. Never sourced from
# eFMS's own copy (eFMS/frontend/public/avfu_logo.png) — a separate project's
# asset that must not become an AMS dependency.
_LOGO_PATH = Path(__file__).resolve().parents[3] / "avfu_logo.png"


@functools.lru_cache(maxsize=1)
def get_logo_data_uri() -> str | None:
    """Base64 data URI for the AVFU logo, embedded directly in the rendered
    HTML so Chromium never needs an HTTP round-trip to a static-file route
    (AMS has no StaticFiles mount at all today) — set_content() with a data:
    URI works entirely offline inside the headless browser.

    The source file is a large print-resolution PNG (3456x5184, ~2.7MB);
    resized once here (cached for the process lifetime, not per-request) to
    a header-appropriate width so the HTML payload stays small and Chromium
    doesn't spend time decoding a multi-megapixel image for a ~100px header
    logo. Returns None (never raises) if the asset is missing or unreadable,
    so a moved/deleted logo degrades to "no logo" rather than failing PDF
    generation entirely.
    """
    if not _LOGO_PATH.is_file():
        return None
    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(_LOGO_PATH) as im:
            im = im.convert("RGBA")
            target_width = 180
            ratio = target_width / im.width
            im = im.resize((target_width, round(im.height * ratio)), Image.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="PNG", optimize=True)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


class ChromiumUnavailable(Exception):
    """Playwright and/or its Chromium browser is not installed on this server."""


class ChromiumRenderFailed(Exception):
    """Chromium launched but did not produce a usable PDF."""


def render_ppw_pdf(html: str) -> bytes:
    """Render a full HTML document string to PDF bytes using headless
    Chromium. Page size/margins come from the document's own @page CSS
    (prefer_css_page_size=True); print_background=True so table borders and
    shaded header cells show up in the output.

    Synchronous on purpose: Playwright's sync API drives its own
    subprocess-backed browser, so this must be called from a threadpool
    (starlette.concurrency.run_in_threadpool) rather than awaited directly,
    to avoid blocking the FastAPI event loop.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright._impl._errors import Error as PlaywrightError
    except ImportError as exc:  # playwright package not installed at all
        raise ChromiumUnavailable(f"playwright not importable: {exc}") from exc

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except PlaywrightError as exc:
                # Almost always: chromium binary not downloaded yet
                # ("playwright install chromium") or a missing OS shared
                # library on a fresh Linux host.
                raise ChromiumUnavailable(f"chromium launch failed: {exc}") from exc

            try:
                page = browser.new_page()
                page.set_content(html, wait_until="networkidle")
                pdf_bytes = page.pdf(
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()
    except ChromiumUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any other launch/render failure
        raise ChromiumRenderFailed(str(exc)) from exc

    if not pdf_bytes:
        raise ChromiumRenderFailed("Chromium produced an empty PDF.")

    return pdf_bytes
