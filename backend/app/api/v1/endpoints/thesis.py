"""Initial Thesis Management (BUSINESS_LOGIC.md section AD). Final Thesis is OUT OF SCOPE.

Workflow (per submission cycle):

    Student submits -> Major Advisor -> HOD -> Librarian -> Incharge Academic Cell -> DPGS
        (sends for evaluation) -> External Examiner(s) evaluate -> DPGS (final approval)

* Major Advisor / HOD / Librarian / DPGS sign with an email OTP (same mechanism as PPW/
  Synopsis/External Examiner Selection); the Incharge Academic Cell is a workflow approver
  only — no OTP, no signature, matching the established convention across every prior
  multi-stage workflow in this repository. DPGS's final approval (of the external
  evaluation) also signs with an OTP.
* A revert from Major Advisor/HOD/Librarian/Incharge/DPGS ALWAYS returns the Thesis to the
  STUDENT (Synopsis's convention, not External Examiner Selection's "always to Major
  Advisor" one — there is no student in that other workflow). There is deliberately no
  revert path once the Thesis has moved past DPGS into external evaluation (see the
  implementation report's open questions).
* Authorization is derived on the server from the caller's ACTIVE session role/department
  and the student's real Advisory Committee / External Examiner Assignment rows — never
  from an id in the request (`_find_my_stage`, `_authorize_view`).
* The Thesis Title is never typed by the student: it is copied ONCE, at creation, from the
  student's own `Ppw.research_title` (`title_snapshot`) — never resynced afterward.
* Examiner identity is never duplicated here: External Examiner evaluation rows point at the
  existing `ams_external_examiner_assignments` row created by the already-implemented
  External Examiner Selection module.
* Only ONE Initial Thesis per student, enforced by a partial unique index in the database.
"""
import hashlib
import logging
import os
import re
import secrets
import string
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.api.v1.endpoints.synopsis import UNIVERSITY_NAME
from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.email import send_email
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.enrollment import CourseRegistration
from app.models.external_examiner import ExternalExaminer, ExternalExaminerAssignment
from app.models.ppw import Ppw
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.thesis import (
    CONFIDENTIAL_DOCUMENT_TYPES, FINAL_CERTIFICATE_KINDS, FINAL_THESIS_DOCUMENT_TYPES, INITIAL_THESIS_DOCUMENT_TYPES,
    SYSTEM_GENERATED_DOCUMENT_TYPES, THESIS_DOCUMENT_TYPES,
    FinalCertificate, FinalCertificateSignature,
    Thesis, ThesisApprovalCycle, ThesisApprovalStage, ThesisDocument, ThesisExternalEvaluation,
    ThesisSeminarCertificate, ThesisSeminarCertificateSignature, ThesisSignature,
)
from app.models.user import Department, Program, User, UserRole
from app.utils.pdf import ChromiumRenderFailed, ChromiumUnavailable, get_logo_data_uri, render_html_documents
from app.utils.pdf_merge import InvalidPdf, inspect_pdf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/thesis", tags=["Initial Thesis"])

_PG_PHD_LEVELS = ("PG", "PhD")
_EDITABLE_STATUSES = ("draft", "reverted")
_APPROVER_ROLES = (UserRole.FACULTY, UserRole.HOD, UserRole.LIBRARIAN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)
_SIGNATORY_STAGES = ("major_advisor", "hod", "librarian", "dpgs", "dpgs_final")  # OTP-verified; incharge_academic_cell is workflow-only
_REVERTIBLE_STAGES = ("major_advisor", "hod", "librarian", "incharge_academic_cell", "dpgs")  # dpgs_final has no revert path this phase

_PHASE_FOR_STAGE = {
    "major_advisor": "major_advisor_pending",
    "hod": "hod_pending",
    "librarian": "librarian_pending",
    "incharge_academic_cell": "incharge_pending",
    "dpgs": "dpgs_pending",
    "dpgs_final": "dpgs_final_pending",
}
_STATUS_LABELS = {
    "draft": "Draft",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "librarian_pending": "Library Approval Pending",
    "incharge_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending (Send for Evaluation)",
    "external_examiner_pending": "Pending External Examiner Evaluation",
    "dpgs_final_pending": "Pending DPGS Final Approval",
    "approved": "Approved",
    "reverted": "Reverted",
}
# Business-document terminology (BUSINESS_LOGIC.md AD) — the role stays LIBRARIAN in the
# data model; only the printed/displayed label differs, exactly like External Examiner
# Selection's role_label convention.
_STAGE_ROLE_LABELS = {
    "major_advisor": "Major Advisor", "hod": "Head of the Department", "librarian": "Chief Librarian",
    "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "dpgs_final": "DPGS (Final Approval)",
}
_ROLE_DISPLAY = {
    "faculty": "Faculty", "hod": "HOD", "librarian": "Librarian",
    "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "super_admin": "Super Admin",
}
# Which document types the STUDENT may upload/replace themselves via the generic upload
# endpoint; the library plagiarism report is entered only by a Librarian (see the dedicated
# library-plagiarism endpoints), and PG25/Certificate I are SYSTEM-GENERATED ONLY (Section 4/6
# of the confirmed rules) — a student can never upload either, even by guessing the URL.
_STUDENT_DOCUMENT_TYPES = tuple(
    t for t in THESIS_DOCUMENT_TYPES if t not in CONFIDENTIAL_DOCUMENT_TYPES and t not in SYSTEM_GENERATED_DOCUMENT_TYPES
)
# The two document types an assigned External Examiner may read, once the Thesis has been
# sent for evaluation — never the purely-administrative student documents.
_EXAMINER_DOCUMENT_TYPES = {"thesis_file", "plagiarism_student_report", "plagiarism_library_report"}
# Document-format rule (Section 5 of the confirmed rules): thesis_file stays .docx; the
# remaining student-owned business-form documents are PDF (validated with the same
# `inspect_pdf`/`InvalidPdf` infrastructure Migration/Progress Report already use — never by
# filename/extension alone). Every other student document type not listed here (currently only
# `plagiarism_student_report`) keeps the original, unrelated .docx validation this phase.
_PDF_DOCUMENT_TYPES = {"payment_receipt", "seminar_proceedings", "clearance", "declaration_annexure1", "pg05a", "pg05b"}

_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(docx|pdf)$")
_ALLOWED_DOCX_CONTENT_TYPES = {"", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/octet-stream"}
_ALLOWED_PDF_CONTENT_TYPES = {"", "application/pdf", "application/x-pdf", "application/octet-stream"}
_OTP_TTL_MINUTES = 10
_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
_IST = timezone(timedelta(hours=5, minutes=30))
_jinja_env = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _not_found() -> HTTPException:
    return HTTPException(404, "Thesis not found.")


def _person_name(u: Optional[User]) -> Optional[str]:
    return u.full_name if u else None


def _name_designation(u: Optional[User]) -> str:
    if not u:
        return "—"
    return f"{u.full_name} ({u.designation})" if u.designation else u.full_name


def _is_dev_environment() -> bool:
    return settings.ENVIRONMENT == "development"


# ── Schemas ───────────────────────────────────────────────────────────────────

class ThesisCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = None


class ThesisUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plagiarism_student_percent: Optional[float] = None
    plagiarism_software_name: Optional[str] = None
    abstract: Optional[str] = None


class LibraryPlagiarismIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plagiarism_library_percent: float


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp: Optional[str] = None


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: str


# ── Load / lookup helpers ─────────────────────────────────────────────────────

_LOAD_OPTIONS = (
    selectinload(Thesis.student).selectinload(User.program),
    selectinload(Thesis.student).selectinload(User.department),
    selectinload(Thesis.student).selectinload(User.college),
    selectinload(Thesis.documents),
    selectinload(Thesis.cycles).selectinload(ThesisApprovalCycle.stages).selectinload(ThesisApprovalStage.assignee),
    selectinload(Thesis.cycles).selectinload(ThesisApprovalCycle.stages).selectinload(ThesisApprovalStage.approver),
    selectinload(Thesis.cycles).selectinload(ThesisApprovalCycle.stages).selectinload(ThesisApprovalStage.committee_member),
    selectinload(Thesis.evaluations).selectinload(ThesisExternalEvaluation.assignment).selectinload(ExternalExaminerAssignment.examiner).selectinload(ExternalExaminer.user),
)


async def _get_thesis(thesis_id: UUID, db: AsyncSession) -> Thesis:
    t = (await db.execute(select(Thesis).options(*_LOAD_OPTIONS).where(Thesis.id == thesis_id))).scalar_one_or_none()
    if not t:
        raise _not_found()
    return t


def _latest_cycle(t: Thesis) -> Optional[ThesisApprovalCycle]:
    return max(t.cycles, key=lambda c: c.cycle_number) if t.cycles else None


def _active_cycle(t: Thesis) -> Optional[ThesisApprovalCycle]:
    c = _latest_cycle(t)
    return c if c and c.status == "active" else None


def _latest_document(t: Thesis, document_type: str) -> Optional[ThesisDocument]:
    versions = [d for d in t.documents if d.document_type == document_type]
    return max(versions, key=lambda d: d.version_number) if versions else None


# ── File storage (server-generated names; the client never sees a path) ──────

def _thesis_dir(thesis_id: UUID) -> Path:
    return Path(settings.UPLOAD_DIR) / "thesis" / str(thesis_id)


def _stored_path(thesis_id: UUID, stored_filename: str) -> Path:
    if not _STORED_NAME_RE.match(stored_filename or ""):
        raise HTTPException(500, "Invalid stored file reference.")
    return _thesis_dir(thesis_id) / stored_filename


def _write_stored(thesis_id: UUID, stored_filename: str, data: bytes) -> None:
    directory = _thesis_dir(thesis_id)
    directory.mkdir(parents=True, exist_ok=True)
    with open(_stored_path(thesis_id, stored_filename), "xb") as f:
        f.write(data)


def _read_stored(thesis_id: UUID, stored_filename: str) -> bytes:
    path = _stored_path(thesis_id, stored_filename)
    if not path.is_file():
        raise HTTPException(404, "The stored file is missing on the server.")
    return path.read_bytes()


async def _validate_docx_upload(upload: UploadFile) -> tuple[bytes, str]:
    """.docx ONLY: extension, declared content type, real ZIP/OOXML signature and a real parse
    (docx is a ZIP archive containing `word/document.xml`) — the same rigor as Synopsis's
    `_validate_pdf_upload`, never trusting the filename alone. Returns (bytes, sha256)."""
    name = (upload.filename or "").strip()
    if os.path.splitext(name)[1].lower() != ".docx":
        raise HTTPException(400, "Only .docx files are accepted.")
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared not in _ALLOWED_DOCX_CONTENT_TYPES:
        raise HTTPException(400, "Only .docx files are accepted.")
    data = await upload.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, f"The file exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if not data.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The file is not a valid .docx document.")
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = zf.namelist()
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise HTTPException(400, "The file is not a valid .docx document.")
    except zipfile.BadZipFile:
        raise HTTPException(400, "The file is not a valid .docx document.")
    return data, hashlib.sha256(data).hexdigest()


async def _validate_pdf_upload(upload: UploadFile) -> tuple[bytes, str]:
    """PDF ONLY — the same rigor Migration/Progress Report already established: extension,
    declared content type, the real `%PDF-` signature, the size limit, and a genuine structural
    parse via the existing generic `app.utils.pdf_merge.inspect_pdf` (never re-implemented
    here, never a filename-only check). Returns (bytes, sha256)."""
    name = (upload.filename or "").strip()
    if os.path.splitext(name)[1].lower() != ".pdf":
        raise HTTPException(400, "Only PDF files are accepted.")
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared not in _ALLOWED_PDF_CONTENT_TYPES:
        raise HTTPException(400, "Only PDF files are accepted.")
    data = await upload.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, f"The file exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "The file is not a PDF document.")
    try:
        inspect_pdf(data)
    except InvalidPdf as exc:
        raise HTTPException(400, str(exc)) from exc
    return data, hashlib.sha256(data).hexdigest()


async def _validate_upload_for_type(document_type: str, upload: UploadFile) -> tuple[bytes, str, str]:
    """Dispatches to the correct validator by document type and returns
    (bytes, sha256, content_type) so callers never have to re-derive the stored content type."""
    if document_type in _PDF_DOCUMENT_TYPES:
        data, digest = await _validate_pdf_upload(upload)
        return data, digest, "application/pdf"
    data, digest = await _validate_docx_upload(upload)
    return data, digest, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _stored_ext_for(document_type: str) -> str:
    return "pdf" if document_type in _PDF_DOCUMENT_TYPES or document_type in SYSTEM_GENERATED_DOCUMENT_TYPES else "docx"


# ── Generated-document rendering (Playwright/Chromium — Section 6/15/18 of the confirmed
# rules; the SAME infrastructure Synopsis already uses, never a new engine/LibreOffice) ──────

def _jinja():
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    return _jinja_env


class _DotDict(dict):
    """Attribute access for the template (`d.university`) over a plain JSON-serializable dict."""
    def __getattr__(self, name):
        value = self.get(name)
        return _DotDict(value) if isinstance(value, dict) else value


def _render_html(template_name: str, context: dict) -> str:
    return _jinja().get_template(template_name).render(d=_DotDict(context), logo_data_uri=get_logo_data_uri())


def _render_single_pdf(template_name: str, context: dict) -> bytes:
    return render_html_documents([_render_html(template_name, context)])[0]


async def _render_or_503(template_name: str, context: dict) -> bytes:
    try:
        return await run_in_threadpool(_render_single_pdf, template_name, context)
    except ChromiumUnavailable as exc:
        logger.error("Thesis document generation unavailable: %s", exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("Thesis document render failed: %s", exc)
        raise HTTPException(422, "Unable to generate the document.")


def _ist_str(dt: Optional[datetime]) -> Optional[str]:
    """IST-presented instant (Section 9: seminar date/time "represented as IST consistently
    with the application") — stored as a normal tz-aware UTC instant like every other
    timestamp in this schema; the IST rendering is a presentation-only conversion done here."""
    if not dt:
        return None
    return dt.astimezone(_IST).strftime("%d-%b-%Y %I:%M %p IST")


async def _store_generated_pdf(
    t: Thesis, document_type: str, pdf_bytes: bytes, user_id: UUID, db: AsyncSession, cycle_id: Optional[UUID] = None,
) -> ThesisDocument:
    """Stores one generated PDF as a new, immutable `ThesisDocument` version — exactly the same
    versioning mechanism every uploaded document already uses (Section 24: generated documents
    must not be silently overwritten). Does NOT commit; the caller commits once, atomically,
    alongside whatever else it changed."""
    next_version = max((d.version_number for d in t.documents if d.document_type == document_type), default=0) + 1
    stored_name = f"{secrets.token_hex(16)}.pdf"
    _write_stored(t.id, stored_name, pdf_bytes)
    doc = ThesisDocument(
        thesis_id=t.id, document_type=document_type, version_number=next_version,
        original_filename=f"{document_type}.pdf", stored_filename=stored_name, content_type="application/pdf",
        size_bytes=len(pdf_bytes), sha256=hashlib.sha256(pdf_bytes).hexdigest(), is_confidential=False,
        uploaded_by=user_id, cycle_id=cycle_id,
    )
    db.add(doc)
    t.documents.append(doc)
    return doc


# ── Authorization ────────────────────────────────────────────────────────────

async def _committee_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    return (await db.execute(select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id == student_id))).scalar_one_or_none()


_CERT_LOAD_OPTIONS = (
    selectinload(ThesisSeminarCertificate.signatures).selectinload(ThesisSeminarCertificateSignature.faculty),
    selectinload(ThesisSeminarCertificate.ma),
    selectinload(ThesisSeminarCertificate.hod_approver),
)


async def _get_certificate(thesis_id: UUID, db: AsyncSession) -> Optional[ThesisSeminarCertificate]:
    """The CURRENT valid PG25 row for this Thesis — the highest `(attempt_number,
    version_number)` — never merely "an approved row exists" (Section 11/13 of the corrected
    rules): an older, superseded attempt/version must never be mistaken for the live one."""
    return (await db.execute(
        select(ThesisSeminarCertificate).options(*_CERT_LOAD_OPTIONS)
        .where(ThesisSeminarCertificate.thesis_id == thesis_id)
        .order_by(ThesisSeminarCertificate.attempt_number.desc(), ThesisSeminarCertificate.version_number.desc())
        .limit(1)
    )).scalar_one_or_none()


async def _get_certificate_by_id(certificate_id: UUID, db: AsyncSession) -> Optional[ThesisSeminarCertificate]:
    return (await db.execute(
        select(ThesisSeminarCertificate).options(
            *_CERT_LOAD_OPTIONS, selectinload(ThesisSeminarCertificate.thesis).selectinload(Thesis.student),
        ).where(ThesisSeminarCertificate.id == certificate_id)
    )).scalar_one_or_none()


async def _all_certificates(thesis_id: UUID, db: AsyncSession) -> list[ThesisSeminarCertificate]:
    """Every attempt/version row for this Thesis, oldest first — for history/audit views only;
    business logic must always use `_get_certificate` (the current one), never this list."""
    return list((await db.execute(
        select(ThesisSeminarCertificate).options(*_CERT_LOAD_OPTIONS).where(ThesisSeminarCertificate.thesis_id == thesis_id)
        .order_by(ThesisSeminarCertificate.attempt_number, ThesisSeminarCertificate.version_number)
    )).scalars().all())


# ── Final Thesis: PG-25(A) / Viva Voce Certificate helpers ────────────────────────────────────

_FINAL_CERT_LOAD_OPTIONS = (
    selectinload(FinalCertificate.signatures).selectinload(FinalCertificateSignature.faculty),
    selectinload(FinalCertificate.generator), selectinload(FinalCertificate.ma),
    selectinload(FinalCertificate.hod_approver), selectinload(FinalCertificate.incharge_approver),
    selectinload(FinalCertificate.dpgs_approver),
)


async def _get_final_certificate(thesis_id: UUID, kind: str, db: AsyncSession) -> Optional[FinalCertificate]:
    """The CURRENT valid `kind` certificate for this Thesis — the highest `(attempt_number,
    version_number)` — never merely "an approved row exists somewhere"."""
    return (await db.execute(
        select(FinalCertificate).options(*_FINAL_CERT_LOAD_OPTIONS)
        .where(FinalCertificate.thesis_id == thesis_id, FinalCertificate.kind == kind)
        .order_by(FinalCertificate.attempt_number.desc(), FinalCertificate.version_number.desc())
        .limit(1)
    )).scalar_one_or_none()


async def _get_final_certificate_by_id(certificate_id: UUID, db: AsyncSession) -> Optional[FinalCertificate]:
    return (await db.execute(
        select(FinalCertificate).options(
            *_FINAL_CERT_LOAD_OPTIONS, selectinload(FinalCertificate.thesis).selectinload(Thesis.student),
        ).where(FinalCertificate.id == certificate_id)
    )).scalar_one_or_none()


_FINAL_CERT_STATUS_LABELS = {
    "unsatisfactory": "Unsatisfactory", "generated": "Generated", "ma_pending": "Awaiting Major Advisor",
    "committee_pending": "Awaiting Advisory Committee", "hod_pending": "Awaiting HOD Approval",
    "incharge_pending": "Awaiting Incharge Academic Cell Approval", "dpgs_pending": "Awaiting DPGS Approval",
    "approved": "Approved", "reverted": "Reverted",
}


def _final_cert_summary(cert: Optional[FinalCertificate]) -> Optional[dict]:
    if not cert:
        return None
    return {
        "id": str(cert.id), "kind": cert.kind, "status": cert.status,
        "status_label": _FINAL_CERT_STATUS_LABELS.get(cert.status, cert.status),
        "attempt_number": cert.attempt_number, "version_number": cert.version_number,
        "event_at": _iso(cert.event_at), "event_at_ist": _ist_str(cert.event_at),
        "student_signed_at": _iso(cert.student_signed_at), "ma_acted_at": _iso(cert.ma_acted_at),
        "approved_at": _iso(cert.approved_at),
        "signatures_completed": sum(1 for s in cert.signatures if s.status == "signed"),
        "signatures_required": len(cert.signatures),
    }


def _my_final_cert_signature(cert: FinalCertificate, user: User) -> Optional[FinalCertificateSignature]:
    return next((s for s in cert.signatures if s.faculty_id == user.id), None)


async def _my_examiner_evaluation(t: Thesis, user: User) -> Optional[ThesisExternalEvaluation]:
    """The caller's OWN evaluation row on THIS thesis, or None — resolved from the actual
    ExternalExaminerAssignment chain (assignment.examiner.user_id == caller), never from an
    id in the request. An examiner assigned to a DIFFERENT student never matches here, even
    if they hold another, unrelated assignment for this same student's evaluation module."""
    if user.active_role != UserRole.EXTERNAL_EXAMINER:
        return None
    for ev in t.evaluations:
        examiner = ev.assignment.examiner if ev.assignment else None
        if examiner and examiner.user_id == user.id and ev.assignment.student_id == t.student_id:
            return ev
    return None


async def _authorize_view(t: Thesis, user: User, db: AsyncSession) -> None:
    """Who may READ a Thesis. Anything else is a 404 so the existence of another student's
    Thesis is never revealed. A draft is private to the student (and Super Admin)."""
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return
    if role == UserRole.STUDENT:
        if t.student_id == user.id:
            return
        raise _not_found()
    if role == UserRole.EXTERNAL_EXAMINER:
        if await _my_examiner_evaluation(t, user):
            return
        raise _not_found()
    # PG25/PG-25(A)/Viva all happen BEFORE (or independent of) main submission — while
    # `t.status` is still "draft" — so the Major Advisor driving them, the Advisory Committee
    # members signing them, and (once each reaches its own later stage) the student's own-
    # department HOD / Incharge Academic Cell / DPGS all need to see this Thesis before the
    # normal post-submission visibility rules (below) would otherwise apply.
    if role in (UserRole.HOD, UserRole.FACULTY, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
        pre_certs: list = []
        if t.thesis_type == "initial":
            c = await _get_certificate(t.id, db)
            if c:
                pre_certs.append(c)
        else:
            for kind in ("pg25a", "viva"):
                c = await _get_final_certificate(t.id, kind, db)
                if c:
                    pre_certs.append(c)
        for cert in pre_certs:
            if getattr(cert, "ma_id", None) == user.id:
                return
            if any(sig.faculty_id == user.id for sig in cert.signatures):
                return
            if role == UserRole.HOD and cert.status in ("hod_pending", "incharge_pending", "dpgs_pending", "approved"):
                dept_id = await resolve_student_department_id(t.student_id, db)
                if dept_id and user.active_department_id and dept_id == user.active_department_id:
                    return
            if role == UserRole.INCHARGE_ACADEMIC_CELL and cert.status in ("incharge_pending", "dpgs_pending", "approved"):
                return
            if role == UserRole.DPGS and cert.status in ("dpgs_pending", "approved"):
                return
    if t.status == "draft":
        raise _not_found()
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.LIBRARIAN):
        return
    if role in (UserRole.FACULTY, UserRole.HOD):
        assigned = await db.execute(
            select(ThesisApprovalStage.id)
            .join(ThesisApprovalCycle, ThesisApprovalCycle.id == ThesisApprovalStage.cycle_id)
            .where(ThesisApprovalCycle.thesis_id == t.id, ThesisApprovalStage.assignee_id == user.id)
            .limit(1)
        )
        if assigned.scalar_one_or_none():
            return
    if role == UserRole.HOD:
        dept_id = await resolve_student_department_id(t.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
    raise _not_found()


async def _find_my_stage(t: Thesis, user: User, db: AsyncSession) -> Optional[ThesisApprovalStage]:
    """The SOLE authorization check for every approval action: the caller's own currently-
    actionable stage in the ACTIVE cycle, or None. Never a bare role match and never an id
    from the request. Librarian is explicitly NOT scoped to any department/library — ANY
    active Librarian may act on the pending Librarian stage (Section 12 of the confirmed
    business rules; no evidence anywhere in the repository of a library/department scoping
    requirement, so none was invented)."""
    cycle = _active_cycle(t)
    if not cycle:
        return None
    committee_id = None
    for stage in cycle.stages:
        if stage.status != "pending" or _PHASE_FOR_STAGE.get(stage.stage_type) != t.status:
            continue
        if stage.stage_type == "major_advisor":
            if user.active_role not in (UserRole.FACULTY, UserRole.HOD):
                continue
            member = stage.committee_member
            if not member or member.faculty_id != user.id or member.role != "major_advisor":
                continue
            if committee_id is None:
                committee_id = await _committee_id(t.student_id, db)
            if committee_id and member.committee_id == committee_id:
                return stage
        elif stage.stage_type == "hod":
            if user.active_role == UserRole.HOD and user.active_department_id:
                dept_id = await resolve_student_department_id(t.student_id, db)
                if dept_id and dept_id == user.active_department_id:
                    return stage
        elif stage.stage_type == "librarian":
            if user.active_role == UserRole.LIBRARIAN:
                return stage
        elif stage.stage_type == "incharge_academic_cell":
            if user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
                return stage
        elif stage.stage_type in ("dpgs", "dpgs_final"):
            if user.active_role == UserRole.DPGS:
                return stage
    return None


async def _require_my_stage(t: Thesis, user: User, db: AsyncSession) -> ThesisApprovalStage:
    stage = await _find_my_stage(t, user, db)
    if not stage:
        raise HTTPException(403, "You have no pending Initial Thesis approval action.")
    return stage


# ── Serialization ─────────────────────────────────────────────────────────────

async def _department_names(db: AsyncSession) -> dict:
    return {i: n for i, n in (await db.execute(select(Department.id, Department.name))).all()}


def _document_dict(d: Optional[ThesisDocument]) -> Optional[dict]:
    if not d:
        return None
    return {
        "id": str(d.id), "version": d.version_number, "original_filename": d.original_filename,
        "size_bytes": d.size_bytes, "uploaded_at": _iso(d.uploaded_at),
        "uploaded_by": _person_name(d.uploader),
    }


def _stage_dict(stage: ThesisApprovalStage, dept_names: dict) -> dict:
    return {
        "sequence": stage.sequence, "stage_type": stage.stage_type, "role_label": stage.role_label,
        "assigned_to": _name_designation(stage.assignee) if stage.assignee else None,
        "status": stage.status,
        "acted_by": _name_designation(stage.approver) if stage.approver else None,
        "acted_role": _ROLE_DISPLAY.get(stage.acted_role or "", stage.acted_role),
        "acted_department": dept_names.get(stage.acted_department_id),
        "acted_at": _iso(stage.acted_at), "remark": stage.remark,
        "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
    }


def _signature_row(cycle: Optional[ThesisApprovalCycle], stage_type: str, label: str) -> dict:
    stage = next((st for st in cycle.stages if st.stage_type == stage_type), None) if cycle else None
    return {
        "role_label": label,
        "status": "Signed" if (stage and stage.status == "approved") else "Pending",
        "acted_at": _iso(stage.acted_at) if stage else None,
    }


def _signature_table(t: Thesis, cycle: Optional[ThesisApprovalCycle]) -> list[dict]:
    """The printed Signature table — exactly the 6 rows confirmed in the business
    requirement (Student, Major Advisor, HOD, Chief Librarian, Incharge Academic Cell,
    DPGS). "Student Signature" is derived from the submission act itself (no OTP, no
    separate stage/signature row — a formal student e-signature is not a mechanism that
    exists anywhere else in AMS, so none was invented here); its date is the cycle's
    `submitted_at`. DPGS's SECOND, later action (approving the external evaluation) is
    tracked as its own `dpgs_final` stage for audit history, but is deliberately NOT a
    7th row here — it is not part of the business form's confirmed table."""
    return [
        {"role_label": "Student Signature", "status": "Signed" if (cycle and cycle.submitted_at) else "Pending",
         "acted_at": _iso(cycle.submitted_at) if cycle else None},
        _signature_row(cycle, "major_advisor", "Major Advisor"),
        _signature_row(cycle, "hod", "HOD"),
        _signature_row(cycle, "librarian", "Chief Librarian"),
        _signature_row(cycle, "incharge_academic_cell", "Incharge Academic Cell"),
        _signature_row(cycle, "dpgs", "DPGS"),
    ]


def _evaluation_row(ev: ThesisExternalEvaluation) -> dict:
    examiner_user = ev.assignment.examiner.user if (ev.assignment and ev.assignment.examiner) else None
    return {
        "id": str(ev.id),
        "name": _person_name(examiner_user) or "External Examiner",
        "status": ev.status,
        "report_available": ev.status in ("submitted", "approved"),
        "submitted_at": _iso(ev.submitted_at),
        "dpgs_approved_at": _iso(ev.dpgs_approved_at),
    }


def _external_report_section(t: Thesis, viewer: User) -> dict:
    """Confidentiality-by-omission (External Examiner Selection's own established pattern):
    a Student sees ONLY evaluations DPGS has approved (Section 22/23 — before that, the field
    is genuinely absent, never an empty/pending placeholder). An assigned Examiner sees only
    their OWN row. DPGS/Super Admin see every row, any status. Every other approver
    (HOD/Librarian/Incharge/Major Advisor) gets no examiner-identifying detail at all — only
    a plain completion boolean — mirroring the confidentiality boundary the External Examiner
    Selection module already enforces for the very same VC decision."""
    role = viewer.active_role
    any_approved = any(ev.status == "approved" for ev in t.evaluations)
    if role == UserRole.STUDENT:
        rows = [_evaluation_row(ev) for ev in t.evaluations if ev.status == "approved"]
        if rows:
            return {"external_report": rows}
        return {"external_evaluation_completed": False}
    if role == UserRole.EXTERNAL_EXAMINER:
        return {"external_evaluation_completed": any_approved}
    if role in (UserRole.DPGS, UserRole.SUPER_ADMIN):
        return {"external_report": [_evaluation_row(ev) for ev in t.evaluations]}
    return {"external_evaluation_completed": any_approved}


_PG25_STATUS_LABELS = {
    "unsatisfactory": "Unsatisfactory", "generated": "Generated (Major Advisor)",
    "committee_pending": "Awaiting Advisory Committee", "hod_pending": "Awaiting HOD Approval",
    "approved": "Approved", "reverted": "Reverted",
}


def _pg25_summary(cert: Optional[ThesisSeminarCertificate]) -> Optional[dict]:
    if not cert:
        return None
    return {
        "id": str(cert.id), "status": cert.status, "status_label": _PG25_STATUS_LABELS.get(cert.status, cert.status),
        "attempt_number": cert.attempt_number, "version_number": cert.version_number,
        "seminar_at": _iso(cert.seminar_at), "seminar_at_ist": _ist_str(cert.seminar_at),
        "ma_signed_at": _iso(cert.ma_signed_at), "approved_at": _iso(cert.approved_at),
        "signatures_completed": sum(1 for s in cert.signatures if s.status == "signed"),
        "signatures_required": len(cert.signatures),
    }


async def _thesis_dict(t: Thesis, viewer: User, db: AsyncSession) -> dict:
    dept_names = await _department_names(db)
    cycle = _latest_cycle(t)
    my_stage = await _find_my_stage(t, viewer, db) if viewer.active_role in _APPROVER_ROLES else None
    is_examiner = viewer.active_role == UserRole.EXTERNAL_EXAMINER
    my_evaluation = await _my_examiner_evaluation(t, viewer) if is_examiner else None
    is_final = t.thesis_type == "final"
    cert = None if is_final else await _get_certificate(t.id, db)
    pg25a_cert = await _get_final_certificate(t.id, "pg25a", db) if is_final else None
    viva_cert = await _get_final_certificate(t.id, "viva", db) if is_final else None

    revert_info = None
    if t.status == "reverted" and cycle and cycle.status == "reverted":
        stage = next((st for st in cycle.stages if st.status == "reverted"), None)
        revert_info = {
            "reverted_by": _name_designation(stage.approver) if stage and stage.approver else None,
            "role": stage.role_label if stage else None,
            "department": dept_names.get(stage.acted_department_id) if stage else None,
            "reverted_at": _iso(cycle.reverted_at), "remark": cycle.revert_remark,
        }

    if is_examiner:
        doc_types = _EXAMINER_DOCUMENT_TYPES
    else:
        doc_types = FINAL_THESIS_DOCUMENT_TYPES if is_final else INITIAL_THESIS_DOCUMENT_TYPES
    documents = {}
    for dt in doc_types:
        # The student never sees a generated approval-workflow document as an available Student
        # Document until the CURRENT attempt/version is fully approved — enforced HERE, server-
        # side, never by frontend hiding; every other viewer (MA/HOD/committee/Incharge/DPGS/
        # Super Admin) sees it as soon as it exists, since they are actively working on it.
        if dt == "seminar_certificate_pg25" and viewer.active_role == UserRole.STUDENT and (not cert or cert.status != "approved"):
            documents[dt] = None
            continue
        if dt == "pg25a_certificate" and viewer.active_role == UserRole.STUDENT and (not pg25a_cert or pg25a_cert.status != "approved"):
            documents[dt] = None
            continue
        if dt == "viva_voce_certificate" and viewer.active_role == UserRole.STUDENT and (not viva_cert or viva_cert.status != "approved"):
            documents[dt] = None
            continue
        documents[dt] = _document_dict(_latest_document(t, dt))

    body = {
        "id": str(t.id), "thesis_type": t.thesis_type,
        "status": t.status, "status_label": _STATUS_LABELS.get(t.status, t.status),
        "title": t.title_snapshot,
        "student": {
            "name": t.student.full_name, "roll_no": t.student.student_roll,
            "degree_name": t.student.program.name if t.student.program else None,
            "department_name": t.student.department.name if t.student.department else None,
            "college_name": t.student.college.name if t.student.college else None,
        },
        "plagiarism_student_percent": t.plagiarism_student_percent,
        "plagiarism_software_name": t.plagiarism_software_name,
        "plagiarism_library_percent": t.plagiarism_library_percent,
        "abstract": t.abstract if not is_examiner else None,
        "documents": documents,
        "pg25": _pg25_summary(cert) if not is_final else None,
        "pg25a": _final_cert_summary(pg25a_cert) if is_final else None,
        "viva": _final_cert_summary(viva_cert) if is_final else None,
        "is_owner": viewer.active_role == UserRole.STUDENT and t.student_id == viewer.id,
        "can_edit": viewer.active_role == UserRole.STUDENT and t.student_id == viewer.id and t.status in _EDITABLE_STATUSES,
        "revert_info": revert_info,
        "my_pending_stage": (
            {"stage_type": my_stage.stage_type, "role_label": my_stage.role_label, "requires_otp": my_stage.stage_type in _SIGNATORY_STAGES}
            if my_stage else None
        ),
        "my_evaluation": _evaluation_row(my_evaluation) if my_evaluation else None,
    }
    if not is_examiner:
        body["stages"] = [_stage_dict(st, dept_names) for st in cycle.stages] if cycle else []
        body["signature_table"] = _signature_table(t, cycle)
        body.update(_external_report_section(t, viewer))
    return body


# ── Create / read ─────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_thesis(
    body: ThesisCreateIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """A student creates their Thesis. The student is always the caller — no student id is
    accepted anywhere, and `thesis_type` is NEVER accepted from the client (Section 40 of the
    confirmed Final Thesis rules — `ThesisCreateIn` has no such field at all, so a client body
    of `{"thesis_type": "final"}` is simply ignored/rejected by the schema, never trusted).

    **Server-determined classification** (this revision): the FIRST call ever for a student
    creates `thesis_type="initial"`. A SECOND call is only accepted once that Initial Thesis has
    reached `status == "approved"` — pending, reverted, or otherwise incomplete Initial Thesis
    blocks Final Thesis creation entirely — and creates a genuinely SEPARATE
    `thesis_type="final"` row (never a version/cycle of the Initial one), so Final Thesis gets
    its own independent documents/plagiarism-data/approval-history for free. A THIRD call is
    always rejected (`409`) — one Initial + one Final is the maximum, each independently
    database-enforced (`uq_thesis_initial_per_student`/`uq_thesis_final_per_student`); the
    checks below only give a friendly message.

    Thesis Title fallback (unchanged for Initial): the student's own PPW `research_title` is
    used when non-blank; `body.title` is a prefill/override. For a Final Thesis, the title
    instead defaults to the Initial Thesis's own `title_snapshot` (the same research, now in its
    final form) — `body.title` may still override it. Whichever title is used is captured ONCE
    into `title_snapshot` and never resynced afterward, exactly as before."""
    program = await db.get(Program, user.program_id) if user.program_id else None
    if not program or program.level not in _PG_PHD_LEVELS:
        raise HTTPException(403, "The Thesis is available to postgraduate students only.")
    existing_final = (await db.execute(select(Thesis.id).where(Thesis.student_id == user.id, Thesis.thesis_type == "final"))).scalar_one_or_none()
    if existing_final:
        raise HTTPException(409, "You already have a Final Thesis. A student can have only one.")
    existing_initial = (await db.execute(select(Thesis).where(Thesis.student_id == user.id, Thesis.thesis_type == "initial"))).scalar_one_or_none()

    if existing_initial:
        if existing_initial.status != "approved":
            raise HTTPException(409, "Your Initial Thesis must be fully approved before you can start your Final Thesis.")
        thesis_type = "final"
        ppw_id = existing_initial.ppw_id
        default_title = (existing_initial.title_snapshot or "").strip()
        message = "Final Thesis draft created."
    else:
        thesis_type = "initial"
        ppw = (await db.execute(select(Ppw).where(Ppw.student_id == user.id))).scalar_one_or_none()
        ppw_id = ppw.id if ppw else None
        default_title = (ppw.research_title or "").strip() if ppw else ""
        message = "Initial Thesis draft created."

    title = (body.title or "").strip() or default_title
    if not title:
        raise HTTPException(400, "Enter a Thesis Title — either fill in your Research Title in your PPW, or provide one here directly.")
    t = Thesis(student_id=user.id, thesis_type=thesis_type, ppw_id=ppw_id, title_snapshot=title, status="draft")
    db.add(t)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"You already have a {'Final' if thesis_type == 'final' else 'Initial'} Thesis. A student can have only one.")
    return {"id": str(t.id), "message": message, "title": title, "thesis_type": thesis_type}


@router.get("/mine")
async def list_my_theses(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """The Thesis Applications table — for now, at most one row (Initial Submission only)."""
    rows = (await db.execute(select(Thesis).where(Thesis.student_id == user.id))).scalars().all()
    return [{
        "id": str(t.id), "title": t.title_snapshot,
        "thesis_type_label": "Initial Submission" if t.thesis_type == "initial" else "Final Submission",
        "status": t.status, "status_label": _STATUS_LABELS.get(t.status, t.status),
    } for t in rows]


@router.get("/my-evaluations")
async def list_my_evaluations(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.EXTERNAL_EXAMINER))):
    """Every Thesis this examiner actually holds an `ExternalExaminerAssignment` for — resolved
    entirely from that existing chain, never from a client-supplied student/thesis id. A thesis
    only appears once it has actually been sent for evaluation (`external_examiner_pending` or
    later); an examiner never sees a thesis before DPGS releases it (Section 20)."""
    my_assignment_ids = (await db.execute(
        select(ExternalExaminerAssignment.id).join(ExternalExaminer, ExternalExaminer.id == ExternalExaminerAssignment.examiner_id)
        .where(ExternalExaminer.user_id == user.id, ExternalExaminerAssignment.status == "active")
    )).scalars().all()
    if not my_assignment_ids:
        return []
    rows = []
    theses = (await db.execute(
        select(Thesis).options(*_LOAD_OPTIONS)
        .join(ThesisExternalEvaluation, ThesisExternalEvaluation.thesis_id == Thesis.id)
        .where(ThesisExternalEvaluation.assignment_id.in_(my_assignment_ids))
    )).unique().scalars().all()
    for t in theses:
        ev = await _my_examiner_evaluation(t, user)
        if not ev:
            continue
        rows.append({
            "thesis_id": str(t.id), "evaluation_id": str(ev.id), "student_name": t.student.full_name,
            "title": t.title_snapshot, "status": ev.status, "submitted_at": _iso(ev.submitted_at),
        })
    return rows


@router.get("/pending-approvals")
async def list_pending_approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """"My Thesis approvals" inbox — every Thesis whose CURRENT stage the caller (in their
    active session role) can act on."""
    ids: set[UUID] = set()
    if user.active_role == UserRole.FACULTY:
        ids.update((await db.execute(
            select(Thesis.id)
            .join(ThesisApprovalCycle, ThesisApprovalCycle.thesis_id == Thesis.id)
            .join(ThesisApprovalStage, ThesisApprovalStage.cycle_id == ThesisApprovalCycle.id)
            .where(ThesisApprovalCycle.status == "active", ThesisApprovalStage.status == "pending",
                   ThesisApprovalStage.stage_type == "major_advisor", ThesisApprovalStage.assignee_id == user.id)
        )).scalars().all())
    phase = {
        UserRole.HOD: "hod_pending", UserRole.LIBRARIAN: "librarian_pending",
        UserRole.INCHARGE_ACADEMIC_CELL: "incharge_pending",
    }.get(user.active_role)
    if user.active_role == UserRole.DPGS:
        ids.update((await db.execute(select(Thesis.id).where(Thesis.status.in_(("dpgs_pending", "dpgs_final_pending"))))).scalars().all())
    elif phase:
        q = select(Thesis.id).where(Thesis.status == phase)
        if user.active_role == UserRole.HOD:
            if not user.active_department_id:
                return []
            q = q.join(User, User.id == Thesis.student_id).where(User.department_id == user.active_department_id)
        ids.update((await db.execute(q)).scalars().all())

    rows = []
    for tid in ids:
        t = await _get_thesis(tid, db)
        stage = await _find_my_stage(t, user, db)
        if not stage:
            continue
        cycle = _active_cycle(t)
        rows.append({
            "thesis_id": str(t.id), "student_name": t.student.full_name, "student_roll": t.student.student_roll,
            "degree_name": t.student.program.name if t.student.program else None,
            "department_name": t.student.department.name if t.student.department else None,
            "title": t.title_snapshot, "status": t.status, "status_label": _STATUS_LABELS.get(t.status, t.status),
            "acting_as": stage.role_label, "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
            "cycle_number": cycle.cycle_number if cycle else None, "submitted_at": _iso(cycle.submitted_at) if cycle else None,
        })
    rows.sort(key=lambda r: r["submitted_at"] or "")
    return rows


@router.get("/{thesis_id}")
async def get_thesis(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    t = await _get_thesis(thesis_id, db)
    await _authorize_view(t, user, db)
    return await _thesis_dict(t, user, db)


# ── Student edit (plagiarism-by-student, abstract) ────────────────────────────

def _require_owned_editable_thesis(t: Thesis, user: User) -> None:
    if t.student_id != user.id:
        raise _not_found()
    if t.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Initial Thesis is under approval or approved and can no longer be edited.")


@router.patch("/{thesis_id}")
async def update_thesis(
    thesis_id: UUID, body: ThesisUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    t = await _get_thesis(thesis_id, db)
    _require_owned_editable_thesis(t, user)
    data = body.model_dump(exclude_unset=True)
    if "plagiarism_student_percent" in data and data["plagiarism_student_percent"] is not None:
        if not (0 <= data["plagiarism_student_percent"] <= 100):
            raise HTTPException(400, "Plagiarism percentage must be between 0 and 100.")
    for k, v in data.items():
        setattr(t, k, v)
    await db.commit()
    return await _thesis_dict(await _get_thesis(thesis_id, db), user, db)


# ── Documents (student-owned types) ───────────────────────────────────────────

@router.post("/{thesis_id}/documents/{document_type}", status_code=201)
async def upload_document(
    thesis_id: UUID, document_type: str, file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Upload (or replace) one of the student's own document categories. `.docx` only for
    every category in this phase (no automatic document generation yet — Section 5 of the
    confirmed rules). Each upload is a new immutable version; nothing is ever overwritten."""
    if document_type not in _STUDENT_DOCUMENT_TYPES:
        raise HTTPException(404, "Unknown document type.")
    t = await _get_thesis(thesis_id, db)
    if t.student_id != user.id:
        raise _not_found()
    # A document type belongs to exactly one Thesis type's document set (Section 4/20 of the
    # confirmed Final Thesis rules) — e.g. PG-05(A)/(B) are Final-Thesis-only, Payment
    # Receipt/Clearance/Declaration are Initial-Thesis-only; thesis_file and both plagiarism
    # report types are shared by both. Checked BEFORE the editability check, same priority
    # ordering as every other "wrong id/type" 404 in this module — an approved/submitted
    # Thesis of the WRONG type for this document is "unknown type here," never merely "locked."
    allowed = FINAL_THESIS_DOCUMENT_TYPES if t.thesis_type == "final" else INITIAL_THESIS_DOCUMENT_TYPES
    if document_type not in allowed:
        raise HTTPException(404, "Unknown document type.")
    if t.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Thesis is under approval or approved and can no longer be edited.")
    data, digest, content_type = await _validate_upload_for_type(document_type, file)
    next_version = max((d.version_number for d in t.documents if d.document_type == document_type), default=0) + 1
    stored_name = f"{secrets.token_hex(16)}.{_stored_ext_for(document_type)}"
    _write_stored(t.id, stored_name, data)
    doc = ThesisDocument(
        thesis_id=t.id, document_type=document_type, version_number=next_version,
        original_filename=os.path.basename((file.filename or document_type).replace("\\", "/"))[:255] or document_type,
        stored_filename=stored_name, content_type=content_type,
        size_bytes=len(data), sha256=digest, is_confidential=document_type in CONFIDENTIAL_DOCUMENT_TYPES, uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    return {"message": "Document uploaded.", "document_id": str(doc.id), "version": next_version}


@router.post("/{thesis_id}/declaration/generate", status_code=201)
async def generate_declaration(
    thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """The Student Declaration (Annexure-I), system-generated from live AMS data (Section 6 of
    the confirmed rules). Generate and Upload (the endpoint above, `document_type=
    "declaration_annexure1"`) occupy the SAME logical document slot — whichever happened most
    recently is simply the latest version, exactly like any other document type; nothing
    special is invented for this pair beyond that."""
    t = await _get_thesis(thesis_id, db)
    _require_owned_editable_thesis(t, user)
    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == user.id)
    )).scalar_one_or_none()
    major = next((m for m in committee.members if m.role == "major_advisor" and m.accepted is True), None) if committee else None
    now = _now()
    context = {
        "university": UNIVERSITY_NAME, "college_name": t.student.college.name if t.student.college else None,
        "student_name": t.student.full_name, "degree_name": t.student.program.name if t.student.program else None,
        "student_roll": t.student.student_roll, "student_email": t.student.email, "student_mobile": t.student.mobile,
        "advisor_name": _person_name(major.faculty) if major else None,
        "advisor_designation": major.faculty.designation if (major and major.faculty) else None,
        "advisor_email": major.faculty.email if (major and major.faculty) else None,
        "advisor_mobile": major.faculty.mobile if (major and major.faculty) else None,
        "title": t.title_snapshot, "plagiarism_software_name": t.plagiarism_software_name,
        "generated_at": now.strftime("%d-%b-%Y %H:%M:%S"),
    }
    pdf_bytes = await _render_or_503("declaration_annexure1.html", context)
    doc = await _store_generated_pdf(t, "declaration_annexure1", pdf_bytes, user.id, db)
    await db.commit()
    return {"message": "Student Declaration generated.", "document_id": str(doc.id), "version": doc.version_number}


@router.get("/{thesis_id}/documents/{document_id}/download")
async def download_document(
    thesis_id: UUID, document_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Streamed through this authorized endpoint only — no path or public URL is ever exposed.
    `thesis_id`/`document_id` are both re-verified against the real record (a mismatched pair,
    or a document belonging to a different Thesis, is a 404, never a 403 that would confirm
    the id exists elsewhere). The Library plagiarism report is confidential from the student —
    enforced HERE, never by omitting a frontend button."""
    t = await _get_thesis(thesis_id, db)
    await _authorize_view(t, user, db)
    doc = next((d for d in t.documents if d.id == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found.")
    if user.active_role == UserRole.STUDENT and doc.is_confidential:
        raise HTTPException(404, "Document not found.")
    if user.active_role == UserRole.EXTERNAL_EXAMINER and doc.document_type not in _EXAMINER_DOCUMENT_TYPES:
        raise HTTPException(404, "Document not found.")
    # Re-verified here too, never trusted merely because it was absent from the `documents`
    # dict — a student who somehow learns a real document_id must still be blocked.
    if user.active_role == UserRole.STUDENT and doc.document_type == "seminar_certificate_pg25":
        cert = await _get_certificate(t.id, db)
        if not cert or cert.status != "approved":
            raise HTTPException(404, "Document not found.")
    if user.active_role == UserRole.STUDENT and doc.document_type in ("pg25a_certificate", "viva_voce_certificate"):
        kind = "pg25a" if doc.document_type == "pg25a_certificate" else "viva"
        fcert = await _get_final_certificate(t.id, kind, db)
        if not fcert or fcert.status != "approved":
            raise HTTPException(404, "Document not found.")
    data = _read_stored(t.id, doc.stored_filename)
    return Response(
        content=data, media_type=doc.content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(doc.original_filename, safe='')}"},
    )


# ── Librarian workflow (plagiarism entry is SEPARATE from approval) ──────────

def _require_librarian_stage(t: Thesis) -> ThesisApprovalCycle:
    cycle = _active_cycle(t)
    if not cycle or t.status != "librarian_pending":
        raise HTTPException(400, "This Initial Thesis is not currently at the Librarian stage.")
    return cycle


@router.patch("/{thesis_id}/library-plagiarism")
async def set_library_plagiarism(
    thesis_id: UUID, body: LibraryPlagiarismIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.LIBRARIAN)),
):
    t = await _get_thesis(thesis_id, db)
    _require_librarian_stage(t)
    if not (0 <= body.plagiarism_library_percent <= 100):
        raise HTTPException(400, "Plagiarism percentage must be between 0 and 100.")
    t.plagiarism_library_percent = body.plagiarism_library_percent
    await db.commit()
    return {"message": "Library plagiarism percentage recorded."}


@router.post("/{thesis_id}/library-plagiarism/report", status_code=201)
async def upload_library_plagiarism_report(
    thesis_id: UUID, file: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.LIBRARIAN)),
):
    t = await _get_thesis(thesis_id, db)
    _require_librarian_stage(t)
    data, digest = await _validate_docx_upload(file)
    document_type = "plagiarism_library_report"
    next_version = max((d.version_number for d in t.documents if d.document_type == document_type), default=0) + 1
    stored_name = f"{secrets.token_hex(16)}.docx"
    _write_stored(t.id, stored_name, data)
    doc = ThesisDocument(
        thesis_id=t.id, document_type=document_type, version_number=next_version,
        original_filename=os.path.basename((file.filename or document_type).replace("\\", "/"))[:255] or document_type,
        stored_filename=stored_name, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(data), sha256=digest, is_confidential=True, uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    return {"message": "Library plagiarism report uploaded.", "document_id": str(doc.id), "version": next_version}


# ── Submission ────────────────────────────────────────────────────────────────

@router.post("/{thesis_id}/submit")
async def submit_thesis(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """Submit (or RE-submit after a revert): revalidates everything server-side (never trusts
    that the frontend already checked), then seeds a brand-new approval cycle starting at the
    Major Advisor. Nothing from an earlier cycle carries over."""
    t = await _get_thesis(thesis_id, db)
    if t.student_id != user.id:
        raise _not_found()
    is_final = t.thesis_type == "final"
    label = "Final Thesis" if is_final else "Initial Thesis"
    if t.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, f"This {label} is already under approval or approved.")

    if is_final:
        # Final Thesis prerequisites (Sections 4/20 of the confirmed rules): PG-05(A)/(B)
        # uploaded, PG-25(A) and Viva Voce Certificate both fully approved for the CURRENT
        # attempt/version (never a stale/reverted one, never Initial Thesis's own PG25). Neither
        # Certificate I nor the old PG25 workflow apply to Final Thesis at all.
        if not _latest_document(t, "pg05a"):
            raise HTTPException(400, "Upload Form No. PG-05(A) before submitting.")
        if not _latest_document(t, "pg05b"):
            raise HTTPException(400, "Upload Form No. PG-05(B) before submitting.")
        pg25a = await _get_final_certificate(t.id, "pg25a", db)
        if not pg25a or pg25a.status != "approved":
            raise HTTPException(400, "Form No. PG-25(A) must be fully approved — generated and signed by you, approved by your Major Advisor, your Advisory Committee, your HOD, Incharge Academic Cell, and DPGS — before you can submit your Final Thesis.")
        viva = await _get_final_certificate(t.id, "viva", db)
        if not viva or viva.status != "approved":
            raise HTTPException(400, "Your Viva Voce Certificate must be fully approved — generated by your Major Advisor and approved by your Advisory Committee, HOD, Incharge Academic Cell, and DPGS — before you can submit your Final Thesis.")
    else:
        # PG25 gate (Section 11 of the corrected rules): `_get_certificate` always resolves the
        # CURRENT attempt/version (highest attempt_number/version_number) — an old, superseded,
        # or reverted certificate can never satisfy this, even if an earlier version was once
        # approved.
        cert = await _get_certificate(t.id, db)
        if not cert or cert.status != "approved":
            raise HTTPException(400, "Your Thesis Seminar Certificate (PG 25) must be fully approved — generated and signed by your Major Advisor, approved by your Advisory Committee, and finally approved by your HOD — before you can submit your Initial Thesis.")

    if not _latest_document(t, "thesis_file"):
        raise HTTPException(400, "Upload your Thesis File (.docx) before submitting.")
    if t.plagiarism_student_percent is None:
        raise HTTPException(400, "Enter your plagiarism percentage before submitting.")
    if not (t.plagiarism_software_name or "").strip():
        raise HTTPException(400, "Enter the plagiarism-check software name before submitting.")
    # A Final Thesis is its own, separate `Thesis` row (`thesis_id` differs from Initial's) —
    # `_latest_document(t, ...)` is already scoped to THIS row, so Initial Thesis's own
    # plagiarism report can never satisfy this check for Final Thesis (Section 26 of the
    # confirmed rules: never query by `student_id` alone for this reason).
    if not _latest_document(t, "plagiarism_student_report"):
        raise HTTPException(400, "Upload your plagiarism report before submitting.")
    if not (t.abstract or "").strip():
        raise HTTPException(400, "Enter your Thesis Abstract before submitting.")

    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == user.id)
    )).scalar_one_or_none()
    major = next((m for m in committee.members if m.role == "major_advisor"), None) if committee else None
    if not major or major.accepted is not True:
        raise HTTPException(400, f"Your Major Advisor must be assigned and must have accepted before you can submit your {label}.")
    if not major.faculty or not major.faculty.is_active:
        raise HTTPException(400, "Your Major Advisor's account is inactive. Ask your HOD to assign a replacement.")

    next_number = (await db.execute(select(ThesisApprovalCycle.cycle_number).where(ThesisApprovalCycle.thesis_id == t.id).order_by(ThesisApprovalCycle.cycle_number.desc()).limit(1))).scalar_one_or_none()
    next_number = (next_number or 0) + 1
    now = _now()
    cycle = ThesisApprovalCycle(thesis_id=t.id, cycle_number=next_number, status="active", submitted_at=now)
    db.add(cycle)
    await db.flush()
    seq = 1
    db.add(ThesisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type="major_advisor", role_label=_STAGE_ROLE_LABELS["major_advisor"],
                               committee_member_id=major.id, assignee_id=major.faculty_id))
    # Final Thesis chain (Section 21/27): major_advisor -> hod -> librarian -> incharge -> dpgs,
    # NO External Examiner, NO second DPGS-final stage. Initial Thesis chain is unchanged.
    remaining_stages = ("hod", "librarian", "incharge_academic_cell", "dpgs") if is_final else ("hod", "librarian", "incharge_academic_cell", "dpgs", "dpgs_final")
    for stage_type in remaining_stages:
        seq += 1
        db.add(ThesisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type=stage_type, role_label=_STAGE_ROLE_LABELS[stage_type]))
    t.status = "major_advisor_pending"
    t.submitted_at = now
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, f"This {label} already has an active approval cycle.")
    return {"message": f"{label} submitted for approval.", "status": t.status, "cycle_number": next_number}


# ── OTP / approve / revert ────────────────────────────────────────────────────

def _send_otp_email(to: str, code: str) -> None:
    send_email(
        to, "AVFU AMS — Initial Thesis Approval OTP",
        f"Your OTP for Initial Thesis approval is: {code}\n\nThis code is valid for {_OTP_TTL_MINUTES} minutes. "
        "If you did not request this, you can safely ignore this email.",
    )


@router.get("/{thesis_id}/approval/otp")
async def request_approval_otp(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    if stage.stage_type not in _SIGNATORY_STAGES:
        raise HTTPException(400, "The Incharge Academic Cell approval is a workflow action and does not need an OTP.")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    db.add(ThesisSignature(approval_stage_id=stage.id, user_id=user.id, otp_code=code, otp_expires_at=_now() + timedelta(minutes=_OTP_TTL_MINUTES)))
    await db.commit()
    _send_otp_email(user.email, code)
    response = {"message": "OTP sent to your email."}
    if _is_dev_environment():
        response["dev_otp"] = code
    return response


async def _consume_otp(stage: ThesisApprovalStage, user: User, otp: str, request: Request, db: AsyncSession) -> None:
    sig = (await db.execute(
        select(ThesisSignature).where(
            ThesisSignature.approval_stage_id == stage.id, ThesisSignature.user_id == user.id,
            ThesisSignature.otp_used == False, ThesisSignature.otp_expires_at > _now(),  # noqa: E712
        ).order_by(ThesisSignature.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sig:
        raise HTTPException(400, "No valid OTP found. Please request a new OTP.")
    if not otp or not secrets.compare_digest(otp, sig.otp_code or ""):
        raise HTTPException(400, "Invalid OTP.")
    sig.otp_used = True
    sig.verified_at = _now()
    sig.ip_address = request.client.host if request.client else None
    sig.user_agent = request.headers.get("user-agent")


async def _lock_and_reload(t: Thesis, stage: ThesisApprovalStage, cycle: ThesisApprovalCycle, db: AsyncSession) -> list[ThesisApprovalStage]:
    await db.execute(select(Thesis.id).where(Thesis.id == t.id).with_for_update())
    current_status = (await db.execute(select(Thesis.status).where(Thesis.id == t.id))).scalar_one()
    stages = list(cycle.stages)
    for st in stages:
        await db.refresh(st, attribute_names=["status"])
    mine = next((x for x in stages if x.id == stage.id), None)
    if not mine or mine.status != "pending" or _PHASE_FOR_STAGE.get(mine.stage_type) != current_status:
        raise HTTPException(409, "This Initial Thesis has already moved on; please reload.")
    t.status = current_status
    return stages


def _record_action(stage: ThesisApprovalStage, user: User, status: str, now: datetime, remark: Optional[str] = None) -> None:
    stage.status = status
    stage.approver = user
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id
    stage.acted_at = now
    if remark is not None:
        stage.remark = remark


@router.post("/{thesis_id}/certificate-i/generate", status_code=201)
async def generate_certificate_i(
    thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD)),
):
    """Certificate I (Form No. PG 27) — Section 18-23 of the confirmed rules. Authorization is
    NOT "any Major Advisor" and NOT "supply a thesis_id" — it is exactly the same live,
    server-resolved check every other Major-Advisor action in this module already uses
    (`_require_my_stage`, stage_type == "major_advisor"): the caller must currently hold the
    ACTIVE cycle's pending Major Advisor stage for THIS student. Scoped to `cycle_id` so a
    Certificate I generated for an earlier, since-reverted submission can never satisfy a later
    resubmission's requirement (Section 21) — generating again for a fresh cycle is always
    allowed and always creates a NEW document version tied to the NEW cycle, never mistaken for
    the old one."""
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    if stage.stage_type != "major_advisor":
        raise HTTPException(400, "Certificate I can only be generated while this Initial Thesis is at the Major Advisor stage.")
    cycle = _active_cycle(t)
    if any(d.document_type == "certificate_i_pg27" and d.cycle_id == cycle.id for d in t.documents):
        raise HTTPException(409, "Certificate I has already been generated for this submission.")
    now = _now()
    context = {
        "university": UNIVERSITY_NAME, "college_name": t.student.college.name if t.student.college else None,
        "student_name": t.student.full_name, "student_roll": t.student.student_roll,
        "degree_name": t.student.program.name if t.student.program else None,
        "department_name": t.student.department.name if t.student.department else None,
        "title": t.title_snapshot,
        "advisor_name": _person_name(user), "advisor_designation": user.designation,
        "generated_at": now.strftime("%d-%b-%Y %H:%M:%S"),
    }
    pdf_bytes = await _render_or_503("certificate_i.html", context)
    doc = await _store_generated_pdf(t, "certificate_i_pg27", pdf_bytes, user.id, db, cycle_id=cycle.id)
    await db.commit()
    return {"message": "Certificate I generated and signed by the Major Advisor.", "document_id": str(doc.id), "version": doc.version_number}


# ── PG25 (Thesis Seminar Certificate) — CORRECTED, Major-Advisor-driven workflow ──────────────
# BUSINESS_LOGIC.md AD.15. The confirmed AVFU workflow is:
#   Major Advisor: Satisfactory/Unsatisfactory -> Generate -> Submit (MA signs)
#     -> Advisory Committee approve -> HOD final approval -> APPROVED
# Unsatisfactory starts a fresh seminar ATTEMPT later; a committee/HOD Revert requires the
# Major Advisor to REGENERATE a fresh VERSION of the same attempt. See
# `ThesisSeminarCertificate`'s own docstring in app/models/thesis.py for the exact
# attempt/version semantics this endpoint layer relies on.

async def _my_ma_membership(user: User, student_id: UUID, db: AsyncSession) -> Optional[CommitteeMember]:
    """The authenticated caller's OWN accepted Major-Advisor membership on `student_id`'s REAL
    Advisory Committee, or None — resolved entirely server-side (Section 15 of the corrected
    rules); never trusts a client-supplied id. A faculty member who is the Major Advisor for
    OTHER students, but not this one, gets None here, exactly like a Major Advisor who was
    removed/never accepted."""
    if user.active_role not in (UserRole.FACULTY, UserRole.HOD):
        return None
    return (await db.execute(
        select(CommitteeMember).options(selectinload(CommitteeMember.faculty))
        .join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .where(
            AdvisoryCommittee.student_id == student_id, CommitteeMember.faculty_id == user.id,
            CommitteeMember.role == "major_advisor", CommitteeMember.accepted == True,  # noqa: E712
        )
    )).scalar_one_or_none()


def _pg25_row(t: Thesis, cert: Optional[ThesisSeminarCertificate], sl_no: int) -> dict:
    status = cert.status if cert else "not_started"
    return {
        "sl_no": sl_no, "thesis_id": str(t.id),
        "student_roll": t.student.student_roll, "student_name": t.student.full_name,
        "seminar_at": _iso(cert.seminar_at) if cert else None, "seminar_at_ist": _ist_str(cert.seminar_at) if cert else None,
        "attempt_number": cert.attempt_number if cert else None, "version_number": cert.version_number if cert else None,
        "status": status, "status_label": _PG25_STATUS_LABELS.get(status, "Not Started"),
        "ma_signed": bool(cert and cert.ma_signed_at),
        "can_satisfactory": cert is None or cert.status == "unsatisfactory",
        "can_unsatisfactory": cert is None or cert.status == "unsatisfactory",
        "can_submit": bool(cert and cert.status == "generated"),
        "can_regenerate": bool(cert and cert.status == "reverted"),
    }


@router.get("/pg25/ma")
async def list_pg25_ma(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """The Major Advisor's own advisee list (Section A/2 of the corrected rules) — every student
    whose REAL, accepted Advisory Committee names the caller as `major_advisor`, and who already
    has an Initial Thesis draft. Never derived from a client-supplied faculty id; the
    authenticated user is authoritative (`_my_ma_membership`'s own query, keyed on
    `CommitteeMember.faculty_id == user.id`)."""
    student_ids = (await db.execute(
        select(AdvisoryCommittee.student_id).join(CommitteeMember, CommitteeMember.committee_id == AdvisoryCommittee.id)
        .where(CommitteeMember.faculty_id == user.id, CommitteeMember.role == "major_advisor", CommitteeMember.accepted == True)  # noqa: E712
    )).scalars().all()
    if not student_ids:
        return []
    theses = (await db.execute(
        select(Thesis).options(*_LOAD_OPTIONS).where(Thesis.student_id.in_(student_ids), Thesis.thesis_type == "initial")
    )).unique().scalars().all()
    rows = []
    for i, t in enumerate(theses, start=1):
        cert = await _get_certificate(t.id, db)
        rows.append(_pg25_row(t, cert, i))
    return rows


@router.post("/{thesis_id}/pg25/satisfactory", status_code=201)
async def pg25_satisfactory(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """The Major Advisor records a satisfactory seminar attempt: stamps seminar date/time and
    generates the PG25 PDF — NOT yet signed/submitted (Submit is a separate, later action,
    Section 5/6). A new attempt (`attempt_number` incremented, `version_number` reset to 1) may
    only be started when no attempt is currently "in flight" for this Thesis (the partial unique
    index `uq_pg25_one_open_per_thesis` is the schema-level guarantee; the pre-check here just
    gives a friendly message) — i.e. this is the first-ever attempt, or the latest one resolved
    to `unsatisfactory`."""
    t = await _get_thesis(thesis_id, db)
    if t.thesis_type != "initial":
        raise _not_found()
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    latest = await _get_certificate(t.id, db)
    if latest and latest.status != "unsatisfactory":
        raise HTTPException(409, "A Thesis Seminar Certificate attempt is already in progress or approved for this student.")
    now = _now()
    cert = ThesisSeminarCertificate(
        thesis_id=t.id, attempt_number=(latest.attempt_number + 1 if latest else 1), version_number=1,
        status="generated", seminar_at=now, ma_id=user.id, recorded_at=now,
    )
    db.add(cert)
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures", "ma"])
    pdf_bytes = await _render_or_503("pg25_certificate.html", await _pg25_pdf_context(t, cert, db))
    await _store_generated_pdf(t, "seminar_certificate_pg25", pdf_bytes, user.id, db)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A Thesis Seminar Certificate attempt is already in progress or approved for this student.")
    return {"message": "Seminar recorded as satisfactory. The Thesis Seminar Certificate (PG 25) has been generated — click Submit to sign and route it to the Advisory Committee.", "certificate_id": str(cert.id)}


@router.post("/{thesis_id}/pg25/unsatisfactory", status_code=201)
async def pg25_unsatisfactory(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """A REAL, state-changing action (Section 4 of the corrected rules — NOT a no-op): the
    Major Advisor records this seminar attempt as unsatisfactory. No PG25 document is generated,
    no approval workflow starts, and the Initial Thesis submission gate stays blocked. The
    attempt is preserved forever as audit history; a fresh offline seminar produces a NEW
    attempt (`attempt_number + 1`) the next time the Major Advisor calls Satisfactory or
    Unsatisfactory for this student."""
    t = await _get_thesis(thesis_id, db)
    if t.thesis_type != "initial":
        raise _not_found()
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    latest = await _get_certificate(t.id, db)
    if latest and latest.status != "unsatisfactory":
        raise HTTPException(409, "A Thesis Seminar Certificate attempt is already in progress or approved for this student.")
    now = _now()
    cert = ThesisSeminarCertificate(
        thesis_id=t.id, attempt_number=(latest.attempt_number + 1 if latest else 1), version_number=1,
        status="unsatisfactory", seminar_at=now, ma_id=user.id, recorded_at=now,
    )
    db.add(cert)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A Thesis Seminar Certificate attempt is already in progress or approved for this student.")
    return {"message": "Seminar attempt recorded as unsatisfactory. Conduct the seminar again offline, then record the new attempt here.", "certificate_id": str(cert.id)}


@router.post("/{thesis_id}/pg25/submit")
async def pg25_submit(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Section 6: the Major Advisor's Submit action — validates the caller is THIS student's
    actual Major Advisor (never a client-supplied id), applies the MA signature
    (`ma_signed_at`), and routes the certificate to the student's REAL, accepted Advisory
    Committee members (every accepted `CommitteeMember`, EXCLUDING the Major Advisor's own row
    — the MA already just signed here, and never signs twice)."""
    t = await _get_thesis(thesis_id, db)
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    cert = await _get_certificate(t.id, db)
    if not cert or cert.status != "generated":
        raise HTTPException(400, "Generate the Thesis Seminar Certificate (Satisfactory) before submitting it.")
    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == t.student_id)
    )).scalar_one_or_none()
    required = [m for m in (committee.members if committee else []) if m.accepted is True and m.role != "major_advisor"]
    now = _now()
    cert.ma_signed_at = now
    for m in required:
        db.add(ThesisSeminarCertificateSignature(certificate_id=cert.id, committee_member_id=m.id, faculty_id=m.faculty_id, role_snapshot=m.role, status="pending"))
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures"])
    cert.status = "hod_pending" if not required else "committee_pending"
    pdf_bytes = await _render_or_503("pg25_certificate.html", await _pg25_pdf_context(t, cert, db))
    await _store_generated_pdf(t, "seminar_certificate_pg25", pdf_bytes, user.id, db)
    await db.commit()
    return {"message": "Certificate signed and submitted.", "status": cert.status}


@router.post("/{thesis_id}/pg25/regenerate", status_code=201)
async def pg25_regenerate(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Section 12/13: after a committee/HOD Revert, the Major Advisor regenerates — a NEW
    `version_number` row for the SAME `attempt_number`, never reusing or resurrecting the
    reverted row (which stays forever as immutable history). The regenerated version restarts
    the whole MA-sign -> Committee -> HOD cycle from scratch."""
    t = await _get_thesis(thesis_id, db)
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    latest = await _get_certificate(t.id, db)
    if not latest or latest.status != "reverted":
        raise HTTPException(400, "There is no reverted Thesis Seminar Certificate to regenerate.")
    now = _now()
    cert = ThesisSeminarCertificate(
        thesis_id=t.id, attempt_number=latest.attempt_number, version_number=latest.version_number + 1,
        status="generated", seminar_at=latest.seminar_at, ma_id=user.id, recorded_at=now,
    )
    db.add(cert)
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures", "ma"])
    pdf_bytes = await _render_or_503("pg25_certificate.html", await _pg25_pdf_context(t, cert, db))
    await _store_generated_pdf(t, "seminar_certificate_pg25", pdf_bytes, user.id, db)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A Thesis Seminar Certificate attempt is already in progress or approved for this student.")
    return {"message": "Certificate regenerated — click Submit to sign and re-route it.", "certificate_id": str(cert.id)}


def _my_pg25_signature(cert: ThesisSeminarCertificate, user: User) -> Optional[ThesisSeminarCertificateSignature]:
    return next((s for s in cert.signatures if s.faculty_id == user.id), None)


@router.get("/pg25/hod")
async def list_pg25_hod(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD))):
    """Section 9: the HOD's final-approval inbox — every Thesis in the HOD's OWN active
    department (never a client-supplied department id) whose CURRENT PG25 attempt/version has
    completed Advisory Committee approval and is awaiting the HOD's final sign-off
    (`status == "hod_pending"`)."""
    if not user.active_department_id:
        return []
    theses = (await db.execute(
        select(Thesis).options(*_LOAD_OPTIONS).join(User, User.id == Thesis.student_id)
        .where(User.department_id == user.active_department_id, Thesis.thesis_type == "initial")
    )).unique().scalars().all()
    rows = []
    for t in theses:
        cert = await _get_certificate(t.id, db)
        if cert and cert.status == "hod_pending":
            rows.append(cert)
    rows.sort(key=lambda c: c.seminar_at)
    return [{
        "sl_no": i, "certificate_id": str(c.id), "thesis_id": str(c.thesis_id),
        "student_roll": c.thesis.student.student_roll if c.thesis.student else None,
        "seminar_at": _iso(c.seminar_at), "seminar_at_ist": _ist_str(c.seminar_at),
        "status": c.status, "status_label": _PG25_STATUS_LABELS.get(c.status, c.status),
    } for i, c in enumerate(rows, start=1)]


@router.post("/pg25/{certificate_id}/hod-approve")
async def pg25_hod_approve(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD))):
    """Section 9/10: the HOD's REAL final approval (NOT a no-op) — only the student's OWN
    department HOD, resolved server-side, may approve, and only once Advisory Committee approval
    is actually complete (`status == "hod_pending"`). This is the ONLY action that flips PG25 to
    `approved`, which is the ONLY moment it becomes visible to the student and satisfies the
    Initial Thesis submission gate."""
    cert = await _get_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    dept_id = await resolve_student_department_id(cert.thesis.student_id, db)
    if not dept_id or not user.active_department_id or dept_id != user.active_department_id:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "hod_pending":
        raise HTTPException(400, "This certificate is not awaiting HOD approval.")
    now = _now()
    cert.hod_approved_by = user.id
    cert.hod_approved_at = now
    cert.approved_at = now
    cert.status = "approved"
    t = await _get_thesis(cert.thesis_id, db)
    pdf_bytes = await _render_or_503("pg25_certificate.html", await _pg25_pdf_context(t, cert, db))
    await _store_generated_pdf(t, "seminar_certificate_pg25", pdf_bytes, user.id, db)
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.get("/pg25/mine/home")
async def pg25_home(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Home tab: fully-APPROVED certificates the caller is an actual required Advisory Committee
    signer on — resolved entirely from the real `ThesisSeminarCertificateSignature.faculty_id`,
    never from a client-supplied id."""
    certs = (await db.execute(
        select(ThesisSeminarCertificate).options(
            selectinload(ThesisSeminarCertificate.signatures), selectinload(ThesisSeminarCertificate.thesis).selectinload(Thesis.student),
        ).join(ThesisSeminarCertificateSignature, ThesisSeminarCertificateSignature.certificate_id == ThesisSeminarCertificate.id)
        .where(ThesisSeminarCertificateSignature.faculty_id == user.id, ThesisSeminarCertificate.status == "approved")
    )).unique().scalars().all()
    return [{
        "sl_no": i, "certificate_id": str(c.id), "thesis_id": str(c.thesis_id),
        "student_name": c.thesis.student.full_name, "student_roll": c.thesis.student.student_roll,
        "status": c.status, "status_label": "Approved",
    } for i, c in enumerate(certs, start=1)]


@router.get("/pg25/mine/pending")
async def pg25_pending(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Pending tab: certificates awaiting the caller's OWN required Advisory Committee
    signature — the certificate itself must still be `committee_pending` (an already-reverted
    or already-approved certificate's stale `pending` signature row, if any, never counts)."""
    sigs = (await db.execute(
        select(ThesisSeminarCertificateSignature).options(
            selectinload(ThesisSeminarCertificateSignature.certificate).selectinload(ThesisSeminarCertificate.thesis).selectinload(Thesis.student),
        ).join(ThesisSeminarCertificate, ThesisSeminarCertificate.id == ThesisSeminarCertificateSignature.certificate_id)
        .where(
            ThesisSeminarCertificateSignature.faculty_id == user.id, ThesisSeminarCertificateSignature.status == "pending",
            ThesisSeminarCertificate.status == "committee_pending",
        )
    )).unique().scalars().all()
    rows = []
    for i, sig in enumerate(sigs, start=1):
        c = sig.certificate
        rows.append({
            "sl_no": i, "certificate_id": str(c.id), "thesis_id": str(c.thesis_id),
            "student_roll": c.thesis.student.student_roll, "student_name": c.thesis.student.full_name,
            "seminar_at": _iso(c.seminar_at), "seminar_at_ist": _ist_str(c.seminar_at),
            "status": c.status, "status_label": _PG25_STATUS_LABELS.get(c.status, c.status),
        })
    return rows


@router.post("/pg25/{certificate_id}/sign")
async def pg25_committee_sign(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Only the authenticated committee member's OWN required signature may ever be performed by
    this call — resolved from the real `faculty_id` on their OWN signature row, never from any
    id in the request. A caller with no matching row (not a member of THIS student's committee,
    or already signed) gets a 404, never a 403 that would confirm the certificate exists."""
    cert = await _get_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    sig = _my_pg25_signature(cert, user)
    if not sig:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "committee_pending":
        raise HTTPException(400, "This certificate is not currently awaiting Advisory Committee approval.")
    if sig.status == "signed":
        raise HTTPException(400, "You have already approved this certificate.")
    sig.status = "signed"
    sig.signed_at = _now()
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures"])
    if all(s.status == "signed" for s in cert.signatures):
        cert.status = "hod_pending"
    t = await _get_thesis(cert.thesis_id, db)
    pdf_bytes = await _render_or_503("pg25_certificate.html", await _pg25_pdf_context(t, cert, db))
    await _store_generated_pdf(t, "seminar_certificate_pg25", pdf_bytes, user.id, db)
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.post("/pg25/{certificate_id}/revert")
async def pg25_revert(certificate_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """A REAL revert (Section 12 — no longer a no-op): either an authorized Advisory Committee
    member (while `committee_pending`) or the student's own-department HOD (while `hod_pending`)
    may single-handedly revert. Terminal for this row; the Major Advisor must call `/pg25/
    regenerate` to try again — a reverted row can never again satisfy anything."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    cert = await _get_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    is_committee = cert.status == "committee_pending" and _my_pg25_signature(cert, user) is not None
    is_hod = False
    if not is_committee and user.active_role == UserRole.HOD and cert.status == "hod_pending":
        dept_id = await resolve_student_department_id(cert.thesis.student_id, db)
        is_hod = bool(dept_id and user.active_department_id and dept_id == user.active_department_id)
    if not is_committee and not is_hod:
        raise HTTPException(404, "Certificate not found.")
    cert.status = "reverted"
    cert.reverted_by = user.id
    cert.reverted_at = _now()
    cert.revert_remark = remark
    await db.commit()
    return {"message": "Reverted to the Major Advisor for correction.", "status": cert.status}


@router.get("/pg25/{certificate_id}")
async def get_pg25_detail(
    certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD, UserRole.FACULTY, UserRole.SUPER_ADMIN)),
):
    """Detail view behind a "View" action — authorization is the SAME real-relationship check as
    everywhere else in this sub-module (the actual Major Advisor, an actual required committee
    signer, or the student's own-department HOD), never a bare role check."""
    cert = await _get_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    authorized = user.active_role == UserRole.SUPER_ADMIN or cert.ma_id == user.id or _my_pg25_signature(cert, user) is not None
    if not authorized and user.active_role == UserRole.HOD:
        dept_id = await resolve_student_department_id(cert.thesis.student_id, db)
        authorized = bool(dept_id and user.active_department_id and dept_id == user.active_department_id)
    if not authorized:
        raise HTTPException(404, "Certificate not found.")
    return {
        "id": str(cert.id), "thesis_id": str(cert.thesis_id), "status": cert.status,
        "status_label": _PG25_STATUS_LABELS.get(cert.status, cert.status),
        "attempt_number": cert.attempt_number, "version_number": cert.version_number,
        "student_name": cert.thesis.student.full_name, "student_roll": cert.thesis.student.student_roll,
        "seminar_at": _iso(cert.seminar_at), "seminar_at_ist": _ist_str(cert.seminar_at),
        "ma_name": _person_name(cert.ma), "ma_signed_at": _iso(cert.ma_signed_at),
        "hod_approved_by": _person_name(cert.hod_approver), "approved_at": _iso(cert.approved_at),
        "revert_remark": cert.revert_remark, "reverted_at": _iso(cert.reverted_at),
        "signatures": [{
            "role_label": s.role_snapshot, "name": _person_name(s.faculty), "status": s.status,
            "signed_at": _iso(s.signed_at), "is_me": s.faculty_id == user.id,
        } for s in cert.signatures],
    }


async def _pg25_pdf_context(t: Thesis, cert: ThesisSeminarCertificate, db: AsyncSession) -> dict:
    program = t.student.program
    committee_rows = []
    for s in cert.signatures:
        faculty = (await db.execute(select(User).where(User.id == s.faculty_id))).scalar_one_or_none()
        committee_rows.append({
            "role_label": s.role_snapshot, "name": _person_name(faculty) or "—",
            "designation": faculty.designation if faculty else None, "status": s.status,
        })
    ma_user = cert.ma
    return {
        "university": UNIVERSITY_NAME, "college_name": t.student.college.name if t.student.college else None,
        "student_name": t.student.full_name, "student_roll": t.student.student_roll,
        "degree_name": t.student.program.name if t.student.program else None,
        "degree_level": program.level if program else None,
        "department_name": t.student.department.name if t.student.department else None,
        "title": t.title_snapshot,
        "seminar_at": _ist_str(cert.seminar_at),
        "attempt_number": cert.attempt_number, "version_number": cert.version_number,
        "ma_name": _person_name(ma_user), "ma_designation": ma_user.designation if ma_user else None,
        "ma_signed": bool(cert.ma_signed_at),
        "committee_rows": committee_rows, "status": cert.status,
        "hod_approved": cert.status == "approved",
        "generated_at": _now().strftime("%d-%b-%Y %H:%M:%S"),
    }


async def _require_final_thesis(t: Thesis) -> None:
    if t.thesis_type != "final":
        raise _not_found()


async def _resolve_committee_and_ma(student_id: UUID, db: AsyncSession):
    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == student_id)
    )).scalar_one_or_none()
    major = next((m for m in committee.members if m.role == "major_advisor" and m.accepted is True), None) if committee else None
    return committee, major


async def _final_cert_pdf_context(t: Thesis, cert: FinalCertificate, db: AsyncSession) -> dict:
    program = t.student.program
    committee_rows = []
    for s in cert.signatures:
        faculty = (await db.execute(select(User).where(User.id == s.faculty_id))).scalar_one_or_none()
        committee_rows.append({
            "role_label": s.role_snapshot, "name": _person_name(faculty) or "—",
            "designation": faculty.designation if faculty else None, "status": s.status,
        })
    ma_user = cert.ma
    return {
        "university": UNIVERSITY_NAME, "college_name": t.student.college.name if t.student.college else None,
        "student_name": t.student.full_name, "student_roll": t.student.student_roll,
        "degree_name": t.student.program.name if t.student.program else None,
        "degree_level": program.level if program else None,
        "department_name": t.student.department.name if t.student.department else None,
        "title": t.title_snapshot,
        "viva_at": _ist_str(cert.event_at), "attempt_number": cert.attempt_number, "version_number": cert.version_number,
        "ma_name": _person_name(ma_user), "ma_designation": ma_user.designation if ma_user else None,
        "ma_signed": bool(cert.ma_acted_at), "student_signed": bool(cert.student_signed_at),
        "committee_rows": committee_rows, "status": cert.status, "status_label": _FINAL_CERT_STATUS_LABELS.get(cert.status, cert.status),
        "hod_approved": cert.hod_approved_at is not None, "dpgs_approved": cert.dpgs_approved_at is not None,
        "generated_at": _now().strftime("%d-%b-%Y %H:%M:%S"),
    }


async def _render_final_cert_pdf(t: Thesis, cert: FinalCertificate, user_id: UUID, db: AsyncSession) -> None:
    template = "pg25a_certificate.html" if cert.kind == "pg25a" else "viva_voce_certificate.html"
    document_type = "pg25a_certificate" if cert.kind == "pg25a" else "viva_voce_certificate"
    pdf_bytes = await _render_or_503(template, await _final_cert_pdf_context(t, cert, db))
    await _store_generated_pdf(t, document_type, pdf_bytes, user_id, db)


# ── Final Thesis: PG-25(A) — student-generated ────────────────────────────────────────────────

@router.post("/{thesis_id}/pg25a/generate", status_code=201)
async def pg25a_generate(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """Section 7: the student generates PG-25(A) — auto-signed by the student immediately
    (`student_signed_at`), NOT yet submitted to the Major Advisor (a separate, later action)."""
    t = await _get_thesis(thesis_id, db)
    if t.student_id != user.id:
        raise _not_found()
    await _require_final_thesis(t)
    latest = await _get_final_certificate(t.id, "pg25a", db)
    if latest and latest.status != "reverted":
        raise HTTPException(409, "A PG-25(A) attempt is already in progress or approved for this Final Thesis.")
    if latest:
        raise HTTPException(400, "A reverted PG-25(A) exists — use Regenerate, not Generate.")
    now = _now()
    cert = FinalCertificate(
        thesis_id=t.id, kind="pg25a", attempt_number=1, version_number=1, status="generated",
        event_at=now, generated_by_id=user.id, generated_at=now, student_signed_at=now,
    )
    db.add(cert)
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures", "generator", "ma"])
    await _render_final_cert_pdf(t, cert, user.id, db)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A PG-25(A) attempt is already in progress or approved for this Final Thesis.")
    return {"message": "PG-25(A) generated and signed. Click Submit to route it to your Major Advisor.", "certificate_id": str(cert.id)}


@router.post("/{thesis_id}/pg25a/submit")
async def pg25a_submit(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """Section 8: routes the generated, student-signed PG-25(A) to the student's actual,
    accepted Major Advisor — resolved server-side, never a client-supplied id."""
    t = await _get_thesis(thesis_id, db)
    if t.student_id != user.id:
        raise _not_found()
    await _require_final_thesis(t)
    cert = await _get_final_certificate(t.id, "pg25a", db)
    if not cert or cert.status != "generated":
        raise HTTPException(400, "Generate PG-25(A) before submitting it.")
    _, major = await _resolve_committee_and_ma(t.student_id, db)
    if not major or not major.faculty:
        raise HTTPException(400, "Your Major Advisor must be assigned and must have accepted before you can submit PG-25(A).")
    cert.ma_id = major.faculty_id
    cert.status = "ma_pending"
    await db.commit()
    return {"message": "PG-25(A) submitted to your Major Advisor.", "status": cert.status}


@router.post("/{thesis_id}/pg25a/regenerate", status_code=201)
async def pg25a_regenerate(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """Section 10: after any approver reverts, the STUDENT regenerates — a new `version_number`
    row for the same attempt; the reverted row is kept forever as history, never reused."""
    t = await _get_thesis(thesis_id, db)
    if t.student_id != user.id:
        raise _not_found()
    await _require_final_thesis(t)
    latest = await _get_final_certificate(t.id, "pg25a", db)
    if not latest or latest.status != "reverted":
        raise HTTPException(400, "There is no reverted PG-25(A) to regenerate.")
    now = _now()
    cert = FinalCertificate(
        thesis_id=t.id, kind="pg25a", attempt_number=latest.attempt_number, version_number=latest.version_number + 1,
        status="generated", event_at=now, generated_by_id=user.id, generated_at=now, student_signed_at=now,
    )
    db.add(cert)
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures", "generator", "ma"])
    await _render_final_cert_pdf(t, cert, user.id, db)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A PG-25(A) attempt is already in progress or approved for this Final Thesis.")
    return {"message": "PG-25(A) regenerated — click Submit to sign and re-route it.", "certificate_id": str(cert.id)}


# ── Final Thesis: Viva Voce Certificate — Major-Advisor-generated ─────────────────────────────

@router.get("/viva/ma")
async def list_viva_ma(
    academic_year_id: Optional[UUID] = None, semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD)),
):
    """Section 14: the Major Advisor's own advisee list for Viva — every student whose REAL,
    accepted Advisory Committee names the caller as `major_advisor`, and who already has a
    Final Thesis draft. **Correction (this audit)**: an earlier pass claimed no per-student
    Semester field exists anywhere in AMS — that was wrong. `CourseRegistration` already carries
    a genuine, unique `(student_id, semester_id)` record (`calendar_id` for the Academic Year),
    the same authoritative source Progress Report itself already relies on
    (`uq_progress_report_student_semester`). Both filters are real and server-side: a student is
    only included when they have an actual `CourseRegistration` row matching the requested
    Academic Year/Semester — never a client-asserted label."""
    student_ids = (await db.execute(
        select(AdvisoryCommittee.student_id).join(CommitteeMember, CommitteeMember.committee_id == AdvisoryCommittee.id)
        .where(CommitteeMember.faculty_id == user.id, CommitteeMember.role == "major_advisor", CommitteeMember.accepted == True)  # noqa: E712
    )).scalars().all()
    if not student_ids:
        return []
    if academic_year_id or semester_id:
        reg_q = select(CourseRegistration.student_id).where(CourseRegistration.student_id.in_(student_ids))
        if academic_year_id:
            reg_q = reg_q.where(CourseRegistration.calendar_id == academic_year_id)
        if semester_id:
            reg_q = reg_q.where(CourseRegistration.semester_id == semester_id)
        student_ids = (await db.execute(reg_q)).scalars().all()
        if not student_ids:
            return []
    theses = (await db.execute(
        select(Thesis).options(*_LOAD_OPTIONS).where(Thesis.student_id.in_(student_ids), Thesis.thesis_type == "final")
    )).unique().scalars().all()
    rows = []
    for i, t in enumerate(theses, start=1):
        cert = await _get_final_certificate(t.id, "viva", db)
        status = cert.status if cert else "not_started"
        rows.append({
            "sl_no": i, "thesis_id": str(t.id), "student_roll": t.student.student_roll, "student_name": t.student.full_name,
            "event_at": _iso(cert.event_at) if cert else None, "event_at_ist": _ist_str(cert.event_at) if cert else None,
            "status": status, "status_label": _FINAL_CERT_STATUS_LABELS.get(status, "Not Started"),
            "ma_signed": bool(cert and cert.ma_acted_at),
            "can_satisfactory": cert is None or cert.status == "unsatisfactory",
            "can_unsatisfactory": cert is None or cert.status == "unsatisfactory",
            "can_submit": bool(cert and cert.status == "generated"),
            "can_regenerate": bool(cert and cert.status == "reverted"),
        })
    return rows


@router.post("/{thesis_id}/viva/satisfactory", status_code=201)
async def viva_satisfactory(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Section 15: the Major Advisor records a satisfactory viva — generates the certificate
    (NOT yet submitted/signed; Submit is a separate, later action)."""
    t = await _get_thesis(thesis_id, db)
    await _require_final_thesis(t)
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    latest = await _get_final_certificate(t.id, "viva", db)
    if latest and latest.status != "unsatisfactory":
        raise HTTPException(409, "A Viva attempt is already in progress or approved for this student.")
    now = _now()
    cert = FinalCertificate(
        thesis_id=t.id, kind="viva", attempt_number=(latest.attempt_number + 1 if latest else 1), version_number=1,
        status="generated", event_at=now, generated_by_id=user.id, generated_at=now, ma_id=user.id,
    )
    db.add(cert)
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures", "generator", "ma"])
    await _render_final_cert_pdf(t, cert, user.id, db)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A Viva attempt is already in progress or approved for this student.")
    return {"message": "Viva recorded as satisfactory. Certificate generated — click Submit to sign and route it.", "certificate_id": str(cert.id)}


@router.post("/{thesis_id}/viva/unsatisfactory", status_code=201)
async def viva_unsatisfactory(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Section 16: a REAL, terminal action — no certificate, no approval chain, submission gate
    stays blocked. The student must undergo another viva; the Major Advisor can then start a
    fresh attempt (`attempt_number + 1`) with Satisfactory."""
    t = await _get_thesis(thesis_id, db)
    await _require_final_thesis(t)
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    latest = await _get_final_certificate(t.id, "viva", db)
    if latest and latest.status != "unsatisfactory":
        raise HTTPException(409, "A Viva attempt is already in progress or approved for this student.")
    now = _now()
    cert = FinalCertificate(
        thesis_id=t.id, kind="viva", attempt_number=(latest.attempt_number + 1 if latest else 1), version_number=1,
        status="unsatisfactory", event_at=now, generated_by_id=user.id, generated_at=now, ma_id=user.id,
    )
    db.add(cert)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A Viva attempt is already in progress or approved for this student.")
    return {"message": "Viva recorded as unsatisfactory. The student must undergo another viva; record the new attempt here.", "certificate_id": str(cert.id)}


@router.post("/{thesis_id}/viva/submit")
async def viva_submit(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Section 15: the Major Advisor's Submit — applies their own signature (`ma_acted_at`) and
    resolves the student's REAL, accepted Advisory Committee (excluding the Major Advisor's own
    row) into required signatures."""
    t = await _get_thesis(thesis_id, db)
    await _require_final_thesis(t)
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    cert = await _get_final_certificate(t.id, "viva", db)
    if not cert or cert.status != "generated":
        raise HTTPException(400, "Generate the Viva Voce Certificate (Satisfactory) before submitting it.")
    committee, _ = await _resolve_committee_and_ma(t.student_id, db)
    required = [m for m in (committee.members if committee else []) if m.accepted is True and m.role != "major_advisor"]
    now = _now()
    cert.ma_acted_at = now
    for m in required:
        db.add(FinalCertificateSignature(certificate_id=cert.id, committee_member_id=m.id, faculty_id=m.faculty_id, role_snapshot=m.role, status="pending"))
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures"])
    cert.status = "hod_pending" if not required else "committee_pending"
    await _render_final_cert_pdf(t, cert, user.id, db)
    await db.commit()
    return {"message": "Viva Voce Certificate signed and submitted.", "status": cert.status}


@router.post("/{thesis_id}/viva/regenerate", status_code=201)
async def viva_regenerate(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Section 17: after any approver reverts, the Major Advisor regenerates — a new
    `version_number` row for the same attempt; the reverted row is kept forever as history."""
    t = await _get_thesis(thesis_id, db)
    await _require_final_thesis(t)
    if not await _my_ma_membership(user, t.student_id, db):
        raise _not_found()
    latest = await _get_final_certificate(t.id, "viva", db)
    if not latest or latest.status != "reverted":
        raise HTTPException(400, "There is no reverted Viva Voce Certificate to regenerate.")
    now = _now()
    cert = FinalCertificate(
        thesis_id=t.id, kind="viva", attempt_number=latest.attempt_number, version_number=latest.version_number + 1,
        status="generated", event_at=latest.event_at, generated_by_id=user.id, generated_at=now, ma_id=user.id,
    )
    db.add(cert)
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures", "generator", "ma"])
    await _render_final_cert_pdf(t, cert, user.id, db)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A Viva attempt is already in progress or approved for this student.")
    return {"message": "Viva Voce Certificate regenerated — click Submit to sign and re-route it.", "certificate_id": str(cert.id)}


# ── Final Thesis certificates: shared MA/Committee/HOD/Incharge/DPGS actions (both kinds) ─────

@router.post("/finalcert/{certificate_id}/ma-approve")
async def finalcert_ma_approve(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """PG-25(A) only (Section 8): the Major Advisor's approval after the student's Submit. Viva
    has no separate post-generation MA-approval phase (the Major Advisor's own generate+Submit
    already IS their contribution), so this always 400s for a `viva` certificate."""
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert or cert.ma_id != user.id:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "ma_pending":
        raise HTTPException(400, "This certificate is not currently awaiting Major Advisor approval.")
    committee, _ = await _resolve_committee_and_ma(cert.thesis.student_id, db)
    required = [m for m in (committee.members if committee else []) if m.accepted is True and m.role != "major_advisor"]
    cert.ma_acted_at = _now()
    for m in required:
        db.add(FinalCertificateSignature(certificate_id=cert.id, committee_member_id=m.id, faculty_id=m.faculty_id, role_snapshot=m.role, status="pending"))
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures"])
    cert.status = "hod_pending" if not required else "committee_pending"
    t = await _get_thesis(cert.thesis_id, db)
    await _render_final_cert_pdf(t, cert, user.id, db)
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.post("/finalcert/{certificate_id}/sign")
async def finalcert_committee_sign(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Only the authenticated committee member's OWN required signature — resolved from the real
    `faculty_id` on their OWN row, never any id in the request."""
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    sig = _my_final_cert_signature(cert, user)
    if not sig:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "committee_pending":
        raise HTTPException(400, "This certificate is not currently awaiting Advisory Committee approval.")
    if sig.status == "signed":
        raise HTTPException(400, "You have already approved this certificate.")
    sig.status = "signed"
    sig.signed_at = _now()
    await db.flush()
    await db.refresh(cert, attribute_names=["signatures"])
    if all(s.status == "signed" for s in cert.signatures):
        cert.status = "hod_pending"
    t = await _get_thesis(cert.thesis_id, db)
    await _render_final_cert_pdf(t, cert, user.id, db)
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.post("/finalcert/{certificate_id}/hod-approve")
async def finalcert_hod_approve(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD))):
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    dept_id = await resolve_student_department_id(cert.thesis.student_id, db)
    if not dept_id or not user.active_department_id or dept_id != user.active_department_id:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "hod_pending":
        raise HTTPException(400, "This certificate is not currently awaiting HOD approval.")
    cert.hod_approved_by = user.id
    cert.hod_approved_at = _now()
    cert.status = "incharge_pending"
    t = await _get_thesis(cert.thesis_id, db)
    await _render_final_cert_pdf(t, cert, user.id, db)
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.post("/finalcert/{certificate_id}/incharge-approve")
async def finalcert_incharge_approve(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.INCHARGE_ACADEMIC_CELL))):
    """Incharge Academic Cell is a role-only, single-holder approval stage — no department/
    committee scoping exists anywhere else in this repository for this role either. Incharge
    NEVER appears as a named signatory on the generated PDF (Section 6/13/23 of the confirmed
    rules) — `incharge_approved_by/at` exist purely for internal audit/authorization state."""
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "incharge_pending":
        raise HTTPException(400, "This certificate is not currently awaiting Incharge Academic Cell approval.")
    cert.incharge_approved_by = user.id
    cert.incharge_approved_at = _now()
    cert.status = "dpgs_pending"
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.post("/finalcert/{certificate_id}/dpgs-approve")
async def finalcert_dpgs_approve(certificate_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.DPGS))):
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    if cert.status != "dpgs_pending":
        raise HTTPException(400, "This certificate is not currently awaiting DPGS approval.")
    now = _now()
    cert.dpgs_approved_by = user.id
    cert.dpgs_approved_at = now
    cert.approved_at = now
    cert.status = "approved"
    t = await _get_thesis(cert.thesis_id, db)
    await _render_final_cert_pdf(t, cert, user.id, db)
    await db.commit()
    return {"message": "Approved.", "status": cert.status}


@router.post("/finalcert/{certificate_id}/revert")
async def finalcert_revert(certificate_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """Any one currently-authorized approver (Major Advisor at `ma_pending`, a real committee
    signer at `committee_pending`, the student's own-department HOD at `hod_pending`, Incharge
    at `incharge_pending`, or DPGS at `dpgs_pending`) may single-handedly revert, mirroring every
    other revert convention in this repository. Terminal for that row — the driving role
    (student for PG-25(A), Major Advisor for Viva) must regenerate to try again."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    authorized = False
    if cert.status == "ma_pending" and cert.ma_id == user.id:
        authorized = True
    elif cert.status == "committee_pending" and _my_final_cert_signature(cert, user) is not None:
        authorized = True
    elif cert.status == "hod_pending" and user.active_role == UserRole.HOD:
        dept_id = await resolve_student_department_id(cert.thesis.student_id, db)
        authorized = bool(dept_id and user.active_department_id and dept_id == user.active_department_id)
    elif cert.status == "incharge_pending" and user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
        authorized = True
    elif cert.status == "dpgs_pending" and user.active_role == UserRole.DPGS:
        authorized = True
    if not authorized:
        raise HTTPException(404, "Certificate not found.")
    cert.status = "reverted"
    cert.reverted_by = user.id
    cert.reverted_at = _now()
    cert.revert_remark = remark
    await db.commit()
    return {"message": "Reverted for regeneration.", "status": cert.status}


@router.get("/finalcert/mine/home")
async def finalcert_home(kind: Optional[str] = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Approved certificates (either kind, or filtered via `?kind=pg25a`/`?kind=viva`) the
    caller is an actual required Advisory Committee signer on."""
    q = (
        select(FinalCertificate).options(
            selectinload(FinalCertificate.signatures), selectinload(FinalCertificate.thesis).selectinload(Thesis.student),
        ).join(FinalCertificateSignature, FinalCertificateSignature.certificate_id == FinalCertificate.id)
        .where(FinalCertificateSignature.faculty_id == user.id, FinalCertificate.status == "approved")
    )
    if kind in FINAL_CERTIFICATE_KINDS:
        q = q.where(FinalCertificate.kind == kind)
    certs = (await db.execute(q)).unique().scalars().all()
    return [{
        "sl_no": i, "certificate_id": str(c.id), "kind": c.kind, "thesis_id": str(c.thesis_id),
        "student_name": c.thesis.student.full_name, "student_roll": c.thesis.student.student_roll,
        "status": c.status, "status_label": "Approved",
    } for i, c in enumerate(certs, start=1)]


@router.get("/finalcert/mine/pending")
async def finalcert_pending(kind: Optional[str] = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Certificates awaiting the caller's OWN required Advisory Committee signature."""
    q = (
        select(FinalCertificateSignature).options(
            selectinload(FinalCertificateSignature.certificate).selectinload(FinalCertificate.thesis).selectinload(Thesis.student),
        ).join(FinalCertificate, FinalCertificate.id == FinalCertificateSignature.certificate_id)
        .where(FinalCertificateSignature.faculty_id == user.id, FinalCertificateSignature.status == "pending", FinalCertificate.status == "committee_pending")
    )
    if kind in FINAL_CERTIFICATE_KINDS:
        q = q.where(FinalCertificate.kind == kind)
    sigs = (await db.execute(q)).unique().scalars().all()
    rows = []
    for i, sig in enumerate(sigs, start=1):
        c = sig.certificate
        rows.append({
            "sl_no": i, "certificate_id": str(c.id), "kind": c.kind, "thesis_id": str(c.thesis_id),
            "student_roll": c.thesis.student.student_roll, "student_name": c.thesis.student.full_name,
            "event_at": _iso(c.event_at), "event_at_ist": _ist_str(c.event_at),
            "status": c.status, "status_label": _FINAL_CERT_STATUS_LABELS.get(c.status, c.status),
        })
    return rows


@router.get("/finalcert/ma-pending")
async def finalcert_ma_pending(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """PG-25(A) certificates awaiting the caller's OWN Major Advisor approval."""
    certs = (await db.execute(
        select(FinalCertificate).options(
            selectinload(FinalCertificate.thesis).selectinload(Thesis.student),
        ).where(FinalCertificate.ma_id == user.id, FinalCertificate.status == "ma_pending", FinalCertificate.kind == "pg25a")
    )).unique().scalars().all()
    return [{
        "sl_no": i, "certificate_id": str(c.id), "thesis_id": str(c.thesis_id),
        "student_roll": c.thesis.student.student_roll, "student_name": c.thesis.student.full_name,
        "status": c.status, "status_label": _FINAL_CERT_STATUS_LABELS.get(c.status, c.status),
    } for i, c in enumerate(certs, start=1)]


@router.get("/finalcert/hod")
async def finalcert_hod_inbox(kind: Optional[str] = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD))):
    """HOD's final-inbox for PG-25(A)/Viva — own department only, `hod_pending` only."""
    if not user.active_department_id:
        return []
    theses = (await db.execute(
        select(Thesis).options(*_LOAD_OPTIONS).join(User, User.id == Thesis.student_id)
        .where(User.department_id == user.active_department_id, Thesis.thesis_type == "final")
    )).unique().scalars().all()
    kinds = (kind,) if kind in FINAL_CERTIFICATE_KINDS else FINAL_CERTIFICATE_KINDS
    rows = []
    for t in theses:
        for k in kinds:
            c = await _get_final_certificate(t.id, k, db)
            if c and c.status == "hod_pending":
                rows.append(c)
    rows.sort(key=lambda c: c.event_at)
    return [{
        "sl_no": i, "certificate_id": str(c.id), "kind": c.kind, "thesis_id": str(c.thesis_id),
        "student_roll": c.thesis.student.student_roll if c.thesis.student else None,
        "event_at": _iso(c.event_at), "event_at_ist": _ist_str(c.event_at),
        "status": c.status, "status_label": _FINAL_CERT_STATUS_LABELS.get(c.status, c.status),
    } for i, c in enumerate(rows, start=1)]


@router.get("/finalcert/incharge")
async def finalcert_incharge_inbox(kind: Optional[str] = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.INCHARGE_ACADEMIC_CELL))):
    """Incharge Academic Cell's inbox — role-only (single-holder), no department scoping."""
    q = select(FinalCertificate).options(selectinload(FinalCertificate.thesis).selectinload(Thesis.student)).where(FinalCertificate.status == "incharge_pending")
    if kind in FINAL_CERTIFICATE_KINDS:
        q = q.where(FinalCertificate.kind == kind)
    rows = (await db.execute(q)).unique().scalars().all()
    return [{
        "sl_no": i, "certificate_id": str(c.id), "kind": c.kind, "thesis_id": str(c.thesis_id),
        "student_roll": c.thesis.student.student_roll, "student_name": c.thesis.student.full_name,
        "status": c.status, "status_label": _FINAL_CERT_STATUS_LABELS.get(c.status, c.status),
    } for i, c in enumerate(rows, start=1)]


@router.get("/finalcert/dpgs")
async def finalcert_dpgs_inbox(kind: Optional[str] = None, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.DPGS))):
    """DPGS's inbox — role-only (single-holder), no department scoping."""
    q = select(FinalCertificate).options(selectinload(FinalCertificate.thesis).selectinload(Thesis.student)).where(FinalCertificate.status == "dpgs_pending")
    if kind in FINAL_CERTIFICATE_KINDS:
        q = q.where(FinalCertificate.kind == kind)
    rows = (await db.execute(q)).unique().scalars().all()
    return [{
        "sl_no": i, "certificate_id": str(c.id), "kind": c.kind, "thesis_id": str(c.thesis_id),
        "student_roll": c.thesis.student.student_roll, "student_name": c.thesis.student.full_name,
        "status": c.status, "status_label": _FINAL_CERT_STATUS_LABELS.get(c.status, c.status),
    } for i, c in enumerate(rows, start=1)]


@router.get("/finalcert/{certificate_id}")
async def get_final_cert_detail(
    certificate_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT, UserRole.HOD, UserRole.FACULTY, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.SUPER_ADMIN)),
):
    cert = await _get_final_certificate_by_id(certificate_id, db)
    if not cert:
        raise HTTPException(404, "Certificate not found.")
    authorized = user.active_role == UserRole.SUPER_ADMIN or cert.ma_id == user.id or _my_final_cert_signature(cert, user) is not None
    if not authorized and user.active_role == UserRole.STUDENT:
        authorized = cert.thesis.student_id == user.id
    if not authorized and user.active_role == UserRole.HOD:
        dept_id = await resolve_student_department_id(cert.thesis.student_id, db)
        authorized = bool(dept_id and user.active_department_id and dept_id == user.active_department_id)
    if not authorized and user.active_role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
        authorized = True
    if not authorized:
        raise HTTPException(404, "Certificate not found.")
    return {
        "id": str(cert.id), "kind": cert.kind, "thesis_id": str(cert.thesis_id), "status": cert.status,
        "status_label": _FINAL_CERT_STATUS_LABELS.get(cert.status, cert.status),
        "attempt_number": cert.attempt_number, "version_number": cert.version_number,
        "event_at": _iso(cert.event_at), "event_at_ist": _ist_str(cert.event_at),
        "student_name": cert.thesis.student.full_name, "student_roll": cert.thesis.student.student_roll,
        "ma_name": _person_name(cert.ma), "ma_acted_at": _iso(cert.ma_acted_at),
        "hod_approved_by": _person_name(cert.hod_approver), "hod_approved_at": _iso(cert.hod_approved_at),
        "incharge_approved_at": _iso(cert.incharge_approved_at),
        "dpgs_approved_by": _person_name(cert.dpgs_approver), "approved_at": _iso(cert.approved_at),
        "revert_remark": cert.revert_remark, "reverted_at": _iso(cert.reverted_at),
        "signatures": [{
            "role_label": s.role_snapshot, "name": _person_name(s.faculty), "status": s.status,
            "signed_at": _iso(s.signed_at), "is_me": s.faculty_id == user.id,
        } for s in cert.signatures],
    }


@router.post("/{thesis_id}/approval/approve")
async def approve_stage(
    thesis_id: UUID, body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    cycle = _active_cycle(t)
    stages = await _lock_and_reload(t, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()

    if stage.stage_type == "librarian" and (t.plagiarism_library_percent is None or not any(d.document_type == "plagiarism_library_report" for d in t.documents)):
        raise HTTPException(400, "Enter the Library plagiarism percentage and upload the Library plagiarism report before approving.")

    # Certificate I is an INITIAL-Thesis-only requirement (Section 25 of the Final Thesis
    # rules) — it is not part of the Final Thesis document set and must never gate a Final
    # Thesis's Major Advisor stage.
    if stage.stage_type == "major_advisor" and t.thesis_type == "initial" and not any(d.document_type == "certificate_i_pg27" and d.cycle_id == cycle.id for d in t.documents):
        raise HTTPException(400, "Generate Certificate I (Form No. PG 27) for this submission before approving.")

    if stage.stage_type in _SIGNATORY_STAGES:
        await _consume_otp(stage, user, body.otp or "", request, db)
    _record_action(stage, user, "approved", now)

    if stage.stage_type == "major_advisor":
        t.status = "hod_pending"
    elif stage.stage_type == "hod":
        t.status = "librarian_pending"
    elif stage.stage_type == "librarian":
        t.status = "incharge_pending"
    elif stage.stage_type == "incharge_academic_cell":
        t.status = "dpgs_pending"
    elif stage.stage_type == "dpgs" and t.thesis_type == "final":
        # Final Thesis has NO External Examiner stage (Section 21/27 of the Final Thesis rules)
        # — DPGS approval is the last stage in the chain, full stop.
        t.status = "approved"
        t.approved_at = now
        cycle.status = "approved"
        cycle.completed_at = now
    elif stage.stage_type == "dpgs":
        assignments = (await db.execute(
            select(ExternalExaminerAssignment).where(ExternalExaminerAssignment.student_id == t.student_id, ExternalExaminerAssignment.status == "active")
        )).scalars().all()
        if not assignments:
            raise HTTPException(400, "This student has no assigned External Examiner(s) yet. Complete External Examiner Selection before sending this thesis for evaluation.")
        for a in assignments:
            exists = (await db.execute(select(ThesisExternalEvaluation.id).where(ThesisExternalEvaluation.thesis_id == t.id, ThesisExternalEvaluation.assignment_id == a.id))).scalar_one_or_none()
            if not exists:
                db.add(ThesisExternalEvaluation(thesis_id=t.id, assignment_id=a.id, status="pending"))
        t.status = "external_examiner_pending"
    elif stage.stage_type == "dpgs_final":
        for ev in t.evaluations:
            if ev.status == "submitted":
                ev.status = "approved"
                ev.dpgs_approved_at = now
                ev.dpgs_approved_by = user.id
        t.status = "approved"
        t.approved_at = now
        cycle.status = "approved"
        cycle.completed_at = now

    await db.commit()
    return {"message": f"{stage.role_label} approved.", "status": t.status}


@router.post("/{thesis_id}/approval/revert")
async def revert_stage(thesis_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """Revert to the STUDENT (Synopsis's convention — see module docstring). A remark is
    REQUIRED; the cycle's other still-pending stages are marked `cancelled`."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    if stage.stage_type not in _REVERTIBLE_STAGES:
        raise HTTPException(400, "This stage cannot be reverted.")
    cycle = _active_cycle(t)
    stages = await _lock_and_reload(t, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()
    _record_action(stage, user, "reverted", now, remark)
    for other in stages:
        if other.id != stage.id and other.status == "pending":
            other.status = "cancelled"
    cycle.status = "reverted"
    cycle.reverted_at = now
    cycle.revert_remark = remark
    t.status = "reverted"
    await db.commit()
    return {"message": "Initial Thesis reverted to the student for correction.", "status": t.status}


# ── External Examiner evaluation ─────────────────────────────────────────────

@router.post("/{thesis_id}/evaluations/{evaluation_id}/report", status_code=201)
async def upload_evaluation_report(
    thesis_id: UUID, evaluation_id: UUID, file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.EXTERNAL_EXAMINER)),
):
    """The assigned External Examiner uploads their evaluation report. Authorization is based
    entirely on the actual `ExternalExaminerAssignment` chain — never on `evaluation_id`/
    `thesis_id` alone: an examiner assigned to a DIFFERENT student is blocked even if they
    hold another, unrelated assignment for this same one (Section 20 of the confirmed rules)."""
    t = await _get_thesis(thesis_id, db)
    ev = next((e for e in t.evaluations if e.id == evaluation_id), None)
    if not ev:
        raise HTTPException(404, "Evaluation not found.")
    mine = await _my_examiner_evaluation(t, user)
    if not mine or mine.id != ev.id:
        raise HTTPException(404, "Evaluation not found.")
    if t.status != "external_examiner_pending" or ev.status != "pending":
        raise HTTPException(400, "This evaluation is not currently open for submission.")
    data, digest = await _validate_docx_upload(file)
    stored_name = f"{secrets.token_hex(16)}.docx"
    _write_stored(t.id, stored_name, data)
    ev.report_stored_filename = stored_name
    ev.report_original_filename = os.path.basename((file.filename or "evaluation_report").replace("\\", "/"))[:255]
    ev.report_content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ev.report_size_bytes = len(data)
    ev.report_sha256 = digest
    ev.status = "submitted"
    ev.submitted_at = _now()
    await db.flush()
    if all(e.status in ("submitted", "approved") for e in t.evaluations):
        t.status = "dpgs_final_pending"
    await db.commit()
    return {"message": "Evaluation report submitted.", "status": ev.status}


@router.get("/{thesis_id}/evaluations/{evaluation_id}/report")
async def download_evaluation_report(
    thesis_id: UUID, evaluation_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Visible ONLY once DPGS has approved (`status == 'approved'`) to the student and to
    every non-DPGS/Super-Admin/examiner viewer; DPGS/Super Admin/the assigned examiner may
    read it at any status (Section 21/23)."""
    t = await _get_thesis(thesis_id, db)
    await _authorize_view(t, user, db)
    ev = next((e for e in t.evaluations if e.id == evaluation_id), None)
    if not ev or not ev.report_stored_filename:
        raise HTTPException(404, "Report not found.")
    role = user.active_role
    if role == UserRole.EXTERNAL_EXAMINER:
        mine = await _my_examiner_evaluation(t, user)
        if not mine or mine.id != ev.id:
            raise HTTPException(404, "Report not found.")
    elif role not in (UserRole.DPGS, UserRole.SUPER_ADMIN):
        if ev.status != "approved":
            raise HTTPException(404, "Report not found.")
    data = _read_stored(t.id, ev.report_stored_filename)
    return Response(
        content=data, media_type=ev.report_content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(ev.report_original_filename or 'evaluation_report.docx', safe='')}"},
    )
