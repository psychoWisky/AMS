"""Shared Jinja -> HTML -> Chromium PDF response helpers for the Gradesheet/Result
documents (course-wise Gradesheet, Grade Card). Uses the existing AMS PDF
architecture (`app.utils.pdf`, Playwright/Chromium) — no other PDF engine."""
import logging
import urllib.parse
from pathlib import Path

from fastapi import HTTPException, Response

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
_jinja_env = None


def render_template(name: str, context: dict) -> str:
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    return _jinja_env.get_template(name).render(**context)


async def pdf_response(html: str, filename: str, label: str) -> Response:
    """Render `html` to PDF (in a worker thread) and return an attachment response.
    Chromium missing -> 503; render failure -> 422 (never a raw 500)."""
    from starlette.concurrency import run_in_threadpool
    from app.utils.pdf import render_ppw_pdf, ChromiumUnavailable, ChromiumRenderFailed
    try:
        pdf_bytes = await run_in_threadpool(render_ppw_pdf, html)
    except ChromiumUnavailable as exc:
        logger.error("%s generation unavailable: %s", label, exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("%s render failed: %s", label, exc)
        raise HTTPException(422, f"Unable to generate the {label}.")
    encoded = urllib.parse.quote(filename, safe="")
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"})
