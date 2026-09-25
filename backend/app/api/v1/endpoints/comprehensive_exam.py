"""Comprehensive Examination (PG "Internal Oral Comprehensive" / PhD "Pre Oral Comprehensive").

Modelled directly on `thesis.py`'s `FinalCertificate` (PG-25(A)/Viva Voce Certificate) chain
architecture and `external_examiner.py`'s reusable-identity/proposal/selection shape — see
`app/models/comprehensive_exam.py` for the full design rationale and why the PhD External Panel
uses PARALLEL tables rather than the existing Thesis External Examiner tables.

Eligibility (confirmed rule): Major completed credits >= 20, Minor completed credits >= 8, where
"completed" means `StudentEnrollment.status == "approved"` AND the enrollment's grade (if any) is
not "F" AND `StudentEnrollment.classification != "research"` (the PPW-style classification field
is the authoritative research-exclusion signal for THIS module — never `Course.category`).
Credits/course-list are snapshotted onto the application at submission time and never
recomputed — the generated document reflects the student's state at submission, permanently.

No OTP/digital-signature step exists anywhere in this module (mirrors `FinalCertificate`'s own
plain approve/revert chain).

Student identity is ALWAYS derived from the authenticated session (`user.id`) — no endpoint here
accepts a `student_id` from the request body/query as the authority for a student-owned action.
HOD department scope is ALWAYS derived from `user.active_department_id` — no endpoint accepts a
client-supplied `department_id` as HOD's authority.
"""
import hashlib
import logging
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.api.v1.endpoints.synopsis import UNIVERSITY_NAME
from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.email import enqueue_email
from app.core.major_advisor import resolve_accepted_major_advisor
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.comprehensive_exam import (
    ComprehensiveExamApplication, ComprehensiveExamApplicationCourse, ComprehensiveExamViva,
    ComprehensiveExamVivaReport, ComprehensiveExamVivaReportSignature,
    ComprehensiveExamExternalPanelSelection, ComprehensiveExamExternalPanelCycle,
    ComprehensiveExamExternalPanelProposal, ComprehensiveExamExternalPanelResult,
    ComprehensiveExamExternalVivaReport, ComprehensiveExamExternalReportSignature,
    COMPREHENSIVE_EXAM_EXTERNAL_PANEL_SIZE, COMPREHENSIVE_EXAM_EXTERNAL_SELECTION_COUNT,
    _OPEN_APPLICATION_STATUSES, _OPEN_REPORT_STATUSES, _OPEN_EXTERNAL_REPORT_STATUSES,
)
from app.models.course import Course, CourseOffering
from app.models.enrollment import StudentEnrollment
from app.models.external_examiner import ExternalExaminer
from app.models.grading import GradeEntry
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import Program, User, UserRole
from app.utils.pdf import ChromiumRenderFailed, ChromiumUnavailable, get_logo_data_uri, render_html_documents
from app.utils.pdf_merge import InvalidPdf, inspect_pdf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/comprehensive-exam", tags=["Comprehensive Examination"])

MAJOR_CREDITS_REQUIRED = 20
MINOR_CREDITS_REQUIRED = 8
_APPROVER_ROLES = (UserRole.SUPER_ADMIN, UserRole.FACULTY, UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)
_SUBTITLES = {"PG": "( Internal Oral Comprehensive )", "PhD": "( Pre Oral Comprehensive )"}
_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
_ALLOWED_PDF_CONTENT_TYPES = {"", "application/pdf", "application/x-pdf", "application/octet-stream"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _not_found() -> HTTPException:
    return HTTPException(404, "Comprehensive Examination record not found.")


# ── Credit calculation (server-authoritative; never trusts client-supplied values) ───────────

async def _completed_credits_by_classification(student_id: UUID, db: AsyncSession) -> dict:
    """Returns {"major": {"total": int, "courses": [...]}, "minor": {...}} for every
    `StudentEnrollment` that is `status == "approved"`, `classification` in ("major", "minor")
    (never "research" — the confirmed authoritative exclusion field for this module is
    `StudentEnrollment.classification`, NOT `Course.category`), and whose grade (if any exists)
    is not "F". Live-computed, never cached — callers that need a frozen figure must snapshot
    the result themselves (see `ComprehensiveExamApplication`/`ComprehensiveExamApplicationCourse`)."""
    result = await db.execute(
        select(StudentEnrollment)
        .options(selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course))
        .where(
            StudentEnrollment.student_id == student_id,
            StudentEnrollment.status == "approved",
            StudentEnrollment.classification.in_(("major", "minor")),
        )
    )
    enrollments = result.scalars().all()
    if not enrollments:
        return {"major": {"total": 0, "courses": []}, "minor": {"total": 0, "courses": []}}

    grades = await db.execute(
        select(GradeEntry.enrollment_id, GradeEntry.grade_letter).where(
            GradeEntry.enrollment_id.in_([e.id for e in enrollments])
        )
    )
    failing_enrollment_ids = {row[0] for row in grades.all() if row[1] == "F"}

    buckets = {"major": {"total": 0, "courses": []}, "minor": {"total": 0, "courses": []}}
    for e in enrollments:
        if e.id in failing_enrollment_ids:
            continue
        offering = e.offering
        course = offering.course if offering else None
        if not offering or not course:
            continue
        bucket = buckets[e.classification]
        bucket["total"] += course.total_credits
        bucket["courses"].append({"course_number": course.course_number, "course_title": course.title, "credits": course.total_credits})
    for key in buckets:
        buckets[key]["courses"].sort(key=lambda c: c["course_number"])
    return buckets


# ── Document rendering / storage (Playwright/Chromium — SAME infra as thesis.py, no new engine) ──

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.pdf$")
_jinja_env = None


def _jinja():
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    return _jinja_env


class _DotDict(dict):
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
        logger.error("Comprehensive Exam document generation unavailable: %s", exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("Comprehensive Exam document render failed: %s", exc)
        raise HTTPException(422, "Unable to generate the document.")


def _storage_dir(subdir: str, record_id: UUID) -> Path:
    return Path(settings.UPLOAD_DIR) / "comprehensive-exam" / subdir / str(record_id)


def _write_stored(subdir: str, record_id: UUID, stored_filename: str, data: bytes) -> None:
    directory = _storage_dir(subdir, record_id)
    directory.mkdir(parents=True, exist_ok=True)
    if not _STORED_NAME_RE.match(stored_filename):
        raise HTTPException(500, "Invalid stored file reference.")
    with open(directory / stored_filename, "xb") as f:
        f.write(data)


def _read_stored(subdir: str, record_id: UUID, stored_filename: str) -> bytes:
    if not _STORED_NAME_RE.match(stored_filename or ""):
        raise HTTPException(500, "Invalid stored file reference.")
    path = _storage_dir(subdir, record_id) / stored_filename
    if not path.is_file():
        raise HTTPException(404, "The stored file is missing on the server.")
    return path.read_bytes()


def _download_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"},
    )


# ── Authorization helpers (reusing the repository's established sources of truth) ────────────

async def _is_accepted_ma(student_id: UUID, user: User, db: AsyncSession) -> bool:
    """Direct authorization check (not resolution) — never raises; used to decide whether the
    CALLER is authorized, not to pick a canonical MA to assign."""
    if user.active_role not in (UserRole.FACULTY, UserRole.HOD):
        return False
    result = await db.execute(
        select(CommitteeMember.id).join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .where(
            AdvisoryCommittee.student_id == student_id, CommitteeMember.faculty_id == user.id,
            CommitteeMember.role == "major_advisor", CommitteeMember.accepted == True,  # noqa: E712
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _committee_members_excluding_ma(student_id: UUID, db: AsyncSession) -> list[CommitteeMember]:
    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == student_id)
    )).scalar_one_or_none()
    if not committee:
        return []
    return [m for m in committee.members if m.accepted is True and m.role != "major_advisor"]


async def _hod_dept_matches(student_id: UUID, user: User, db: AsyncSession) -> bool:
    """True for SUPER_ADMIN unconditionally, or for HOD when the CALLER's own active department
    (never a client-supplied value) matches the student's real department. Callers translate a
    False result into 404 — this repository's established convention of not confirming a
    foreign-department record's existence."""
    if user.active_role == UserRole.SUPER_ADMIN:
        return True
    if user.active_role != UserRole.HOD:
        return False
    dept_id = await resolve_student_department_id(student_id, db)
    return bool(dept_id and user.active_department_id and dept_id == user.active_department_id)


async def _load_student(student_id: UUID, db: AsyncSession) -> Optional[User]:
    """`db.get(User, id, options=[...])` is unsafe here: if `User` row is already in the
    session's identity map (e.g. loaded earlier in this same request by `get_current_user`),
    `.get()` returns the cached instance AS-IS and silently ignores the new eager-load
    options — the resulting `MissingGreenlet` only surfaces later, wherever the relationship is
    actually accessed. An explicit `select(...).options(...)` always (re-)applies the requested
    eager loads regardless of identity-map state."""
    result = await db.execute(
        select(User).options(selectinload(User.department), selectinload(User.college), selectinload(User.program))
        .where(User.id == student_id)
    )
    return result.scalar_one_or_none()


async def _get_application(application_id: UUID, db: AsyncSession) -> ComprehensiveExamApplication:
    app_row = await db.get(
        ComprehensiveExamApplication, application_id,
        options=[selectinload(ComprehensiveExamApplication.courses), selectinload(ComprehensiveExamApplication.vivas)],
    )
    if not app_row:
        raise _not_found()
    return app_row


async def _authorize_view_application(app_row: ComprehensiveExamApplication, user: User, db: AsyncSession) -> None:
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return
    if role == UserRole.STUDENT:
        if app_row.student_id == user.id:
            return
        raise _not_found()
    if role == UserRole.FACULTY:
        if app_row.ma_id == user.id or await _is_accepted_ma(app_row.student_id, user, db):
            return
        # Advisory Committee members (excluding MA) may also view once relevant (viva report stage).
        members = await _committee_members_excluding_ma(app_row.student_id, db)
        if any(m.faculty_id == user.id for m in members):
            return
        raise _not_found()
    if role == UserRole.HOD:
        if await _hod_dept_matches(app_row.student_id, user, db):
            return
        raise _not_found()
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.VICE_CHANCELLOR):
        return
    raise _not_found()


# ── Schemas ───────────────────────────────────────────────────────────────────────────────────

class RevertIn(BaseModel):
    remark: str

    @field_validator("remark")
    @classmethod
    def _remark_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("A remark is required when reverting.")
        return v.strip()


class VivaScheduleIn(BaseModel):
    viva_date: datetime


class VivaResultIn(BaseModel):
    result: str

    @field_validator("result")
    @classmethod
    def _valid_result(cls, v: str) -> str:
        if v not in ("satisfactory", "unsatisfactory"):
            raise ValueError("result must be 'satisfactory' or 'unsatisfactory'.")
        return v


class ExternalProposalIn(BaseModel):
    name: str
    specialization: str
    designation: str
    email: str
    phone: str
    institution: str


class ExternalPanelProposeIn(BaseModel):
    application_id: UUID
    proposals: list[ExternalProposalIn]

    @field_validator("proposals")
    @classmethod
    def _exactly_five(cls, v: list[ExternalProposalIn]) -> list[ExternalProposalIn]:
        if len(v) != COMPREHENSIVE_EXAM_EXTERNAL_PANEL_SIZE:
            raise ValueError(f"Exactly {COMPREHENSIVE_EXAM_EXTERNAL_PANEL_SIZE} external examiners must be proposed.")
        return v


class VcSelectionIn(BaseModel):
    proposal_id: UUID


# ── Number-to-words (no external dependency added for this) ──────────────────────────────────

_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _number_in_words(n: int) -> str:
    """0-999 only (sufficient for a credit-completion percentage, which is never negative and
    realistically never reaches four digits) — a tiny inline implementation rather than adding a
    new third-party dependency for one sentence of document text."""
    if n < 0:
        return str(n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rem = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[rem]}" if rem else "")
    hundreds, rem = divmod(n, 100)
    return _ONES[hundreds] + " hundred" + (f" {_number_in_words(rem)}" if rem else "")


def _application_pdf_context(app_row: ComprehensiveExamApplication, student: User, ma_user: Optional[User]) -> dict:
    major_pct = round(app_row.major_credits_completed / app_row.major_credits_required * 100)
    minor_pct = round(app_row.minor_credits_completed / app_row.minor_credits_required * 100)
    # The document body states a single completion percentage ("...completed X percent of the
    # Major and Minor courses separately..."); since eligibility already requires BOTH to be at
    # least their own requirement, the binding/guaranteed figure for that sentence is the LOWER
    # of the two percentages (never invented above what is actually true of the weaker of the
    # two) — the course table below always shows both real figures separately regardless.
    body_pct = min(major_pct, minor_pct)
    rows = []
    for classification, required in (("major", app_row.major_credits_required), ("minor", app_row.minor_credits_required)):
        courses = [c for c in app_row.courses if c.classification == classification]
        completed = sum(c.credits for c in courses)
        pct = round(completed / required * 100) if required else 0
        rows.append({
            "classification_label": "Major course" if classification == "major" else "Minor course",
            "courses_text": ", ".join(f"{c.course_number} ({c.credits})" for c in courses) or "—",
            "required": required, "completed": completed, "percent": pct,
        })
    return {
        "university": UNIVERSITY_NAME,
        "college_name": student.college.name if student.college else None,
        "subtitle": _SUBTITLES.get(app_row.degree_level, ""),
        "degree_level": app_row.degree_level,
        "student_name": student.full_name,
        "student_roll": student.student_roll,
        "department_name": student.department.name if student.department else None,
        "body_percent": body_pct,
        "body_percent_words": _number_in_words(body_pct),
        "course_rows": rows,
        "student_signed": bool(app_row.student_signed_at),
        "student_signed_at": app_row.student_signed_at.strftime("%d/%m/%Y") if app_row.student_signed_at else None,
        "ma_name": ma_user.full_name if ma_user else None,
        "ma_signed": bool(app_row.ma_acted_at),
        "ma_signed_at": app_row.ma_acted_at.strftime("%d/%m/%Y") if app_row.ma_acted_at else None,
        "hod_approved": bool(app_row.hod_approved_at),
        "hod_approved_at": app_row.hod_approved_at.strftime("%d/%m/%Y") if app_row.hod_approved_at else None,
        "dpgs_approved": bool(app_row.dpgs_approved_at),
        "dpgs_approved_at": app_row.dpgs_approved_at.strftime("%d/%m/%Y") if app_row.dpgs_approved_at else None,
        "status": app_row.status,
        "generated_at": app_row.generated_at.strftime("%d/%m/%Y") if app_row.generated_at else None,
    }


# ── Application ───────────────────────────────────────────────────────────────────────────────

@router.post("/applications", status_code=201)
async def create_application(
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Student creates their own Comprehensive Exam application. `student_id` is ALWAYS
    `user.id` — never accepted from the request (there is no request body at all). Every value
    on the created row is computed/resolved server-side: Program.level, the accepted Major
    Advisor, and the completed-credit totals — the client supplies nothing but the click."""
    existing = await db.execute(select(ComprehensiveExamApplication.id).where(ComprehensiveExamApplication.student_id == user.id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "You already have a Comprehensive Examination application.")

    student = await _load_student(user.id, db)
    if not student or not student.program_id:
        raise HTTPException(403, "Your academic programme is not configured; contact administration.")
    program = await db.get(Program, student.program_id)
    if not program or program.level not in ("PG", "PhD"):
        raise HTTPException(403, "Comprehensive Examination applies only to PG and PhD students.")

    ma_id = await resolve_accepted_major_advisor(user.id, db)

    credits = await _completed_credits_by_classification(user.id, db)
    major_total, minor_total = credits["major"]["total"], credits["minor"]["total"]
    if major_total < MAJOR_CREDITS_REQUIRED or minor_total < MINOR_CREDITS_REQUIRED:
        raise HTTPException(
            400,
            f"You do not yet meet the Comprehensive Examination credit requirement "
            f"(Major: {major_total}/{MAJOR_CREDITS_REQUIRED}, Minor: {minor_total}/{MINOR_CREDITS_REQUIRED}).",
        )

    now = _now()
    app_row = ComprehensiveExamApplication(
        student_id=user.id, degree_level=program.level, status="ma_pending",
        major_credits_completed=major_total, minor_credits_completed=minor_total,
        generated_at=now, student_signed_at=now, ma_id=ma_id,
    )
    db.add(app_row)
    await db.flush()
    for classification in ("major", "minor"):
        for idx, c in enumerate(credits[classification]["courses"]):
            db.add(ComprehensiveExamApplicationCourse(
                application_id=app_row.id, classification=classification,
                course_number=c["course_number"], course_title=c["course_title"], credits=c["credits"], order_index=idx,
            ))
    await db.flush()
    await db.refresh(app_row, attribute_names=["courses"])

    ma_user = await db.get(User, ma_id)
    pdf_bytes = await _render_or_503("comprehensive_exam_application.html", _application_pdf_context(app_row, student, ma_user))
    stored_name = f"{secrets.token_hex(16)}.pdf"
    _write_stored("applications", app_row.id, stored_name, pdf_bytes)
    app_row.stored_filename = stored_name
    app_row.original_filename = "Comprehensive_Exam_Application.pdf"
    app_row.content_type = "application/pdf"
    await db.commit()
    return {"id": str(app_row.id), "message": "Application submitted.", "status": app_row.status}


@router.get("/applications")
async def list_applications(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Role-scoped list: STUDENT sees only their own (0 or 1 rows — this endpoint doubles as
    both "my application" and, for every other role, an approver-relevant view). Never accepts
    a student_id/department_id filter from the client."""
    q = select(ComprehensiveExamApplication).options(selectinload(ComprehensiveExamApplication.student))
    role = user.active_role
    if role == UserRole.STUDENT:
        q = q.where(ComprehensiveExamApplication.student_id == user.id)
    elif role == UserRole.FACULTY:
        q = q.where(ComprehensiveExamApplication.ma_id == user.id)
    elif role == UserRole.HOD:
        if not user.active_department_id:
            return []
        q = q.join(User, User.id == ComprehensiveExamApplication.student_id).where(User.department_id == user.active_department_id)
    elif role == UserRole.INCHARGE_ACADEMIC_CELL:
        q = q.where(ComprehensiveExamApplication.status.in_(("incharge_pending", "dpgs_pending", "approved")))
    elif role == UserRole.DPGS:
        q = q.where(ComprehensiveExamApplication.status.in_(("dpgs_pending", "approved")))
    elif role != UserRole.SUPER_ADMIN:
        return []
    result = await db.execute(q.order_by(ComprehensiveExamApplication.created_at.desc()))
    rows = result.scalars().all()
    return [{
        "id": str(a.id), "application_label": "APPLICATION FOR HOLDING COMPREHENSIVE EXAMINATION",
        "degree_level": a.degree_level, "status": a.status,
        "student_name": a.student.full_name if a.student else None,
        "student_roll": a.student.student_roll if a.student else None,
    } for a in rows]


@router.get("/applications/{application_id}")
async def get_application(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    app_row = await _get_application(application_id, db)
    await _authorize_view_application(app_row, user, db)
    vivas = sorted(app_row.vivas, key=lambda v: v.attempt_number)
    latest_viva = vivas[-1] if vivas else None
    report = None
    if latest_viva:
        report_row = (await db.execute(
            select(ComprehensiveExamVivaReport).options(selectinload(ComprehensiveExamVivaReport.signatures))
            .where(ComprehensiveExamVivaReport.viva_id == latest_viva.id)
            .order_by(ComprehensiveExamVivaReport.version_number.desc()).limit(1)
        )).scalar_one_or_none()
        if report_row:
            report = {
                "id": str(report_row.id), "version_number": report_row.version_number, "status": report_row.status,
                "signatures": [{"faculty_id": str(s.faculty_id), "role_snapshot": s.role_snapshot, "status": s.status} for s in report_row.signatures],
            }
    panel_selection = (await db.execute(
        select(ComprehensiveExamExternalPanelSelection).where(ComprehensiveExamExternalPanelSelection.application_id == application_id)
    )).scalar_one_or_none()
    panel_info = None
    if panel_selection:
        # `panel_selection.id` is NOT the id the `/external-panel/{cycle_id}/*` endpoints accept
        # — those key off the CYCLE, not the selection. Resolve and expose the actual latest
        # cycle id here so the frontend never has to guess/construct it another way.
        latest_cycle = (await db.execute(
            select(ComprehensiveExamExternalPanelCycle).options(selectinload(ComprehensiveExamExternalPanelCycle.result))
            .where(ComprehensiveExamExternalPanelCycle.selection_id == panel_selection.id)
            .order_by(ComprehensiveExamExternalPanelCycle.cycle_number.desc()).limit(1)
        )).scalar_one_or_none()
        panel_info = {
            "selection_status": panel_selection.status,
            "cycle_id": str(latest_cycle.id) if latest_cycle else None,
            "cycle_status": latest_cycle.status if latest_cycle else None,
            "current_stage": _panel_current_stage(latest_cycle) if latest_cycle and latest_cycle.status == "active" else None,
            "selected_proposal_id": str(latest_cycle.result.proposal_id) if latest_cycle and latest_cycle.result else None,
        }
    external_report = (await db.execute(
        select(ComprehensiveExamExternalVivaReport).options(selectinload(ComprehensiveExamExternalVivaReport.signatures))
        .where(ComprehensiveExamExternalVivaReport.application_id == application_id)
        .order_by(ComprehensiveExamExternalVivaReport.version_number.desc()).limit(1)
    )).scalar_one_or_none()
    external_report_info = None
    if external_report:
        er = external_report
        # Additive enrichment (same reasoning as `latest_viva_report`/`external_panel` above) —
        # the frontend needs each stage's own approval detail to gate the committee AND-gate
        # button and render an approval-history timeline; none of this changes the approval
        # logic itself, only what is exposed for display.
        external_report_info = {
            "id": str(er.id), "version_number": er.version_number, "status": er.status,
            "uploaded_at": er.uploaded_at.isoformat() if er.uploaded_at else None,
            "submitted_at": er.submitted_at.isoformat() if er.submitted_at else None,
            "signatures": [{"faculty_id": str(s.faculty_id), "role_snapshot": s.role_snapshot, "status": s.status, "signed_at": s.signed_at.isoformat() if s.signed_at else None} for s in er.signatures],
            "hod_approved": bool(er.hod_approved_at), "hod_approved_at": er.hod_approved_at.isoformat() if er.hod_approved_at else None,
            "incharge_approved": bool(er.incharge_approved_at), "incharge_approved_at": er.incharge_approved_at.isoformat() if er.incharge_approved_at else None,
            "dpgs_approved": bool(er.dpgs_approved_at), "dpgs_approved_at": er.dpgs_approved_at.isoformat() if er.dpgs_approved_at else None,
            "approved_at": er.approved_at.isoformat() if er.approved_at else None,
            "reverted_at": er.reverted_at.isoformat() if er.reverted_at else None,
            "revert_remark": er.revert_remark if er.status == "reverted" else None,
        }
    return {
        "id": str(app_row.id), "student_id": str(app_row.student_id), "degree_level": app_row.degree_level,
        "status": app_row.status, "application_label": "APPLICATION FOR HOLDING COMPREHENSIVE EXAMINATION",
        "major_credits_required": app_row.major_credits_required, "minor_credits_required": app_row.minor_credits_required,
        "major_credits_completed": app_row.major_credits_completed, "minor_credits_completed": app_row.minor_credits_completed,
        "ma_id": str(app_row.ma_id) if app_row.ma_id else None,
        "revert_remark": app_row.revert_remark if app_row.status == "reverted" else None,
        "vivas": [{
            "id": str(v.id), "attempt_number": v.attempt_number, "viva_date": v.viva_date.isoformat(),
            "result": v.result, "has_report": v.id == (latest_viva.id if latest_viva else None) and report is not None,
        } for v in vivas],
        "latest_viva_report": report,
        "external_panel": panel_info,
        "external_viva_report": external_report_info,
    }


@router.get("/applications/{application_id}/document")
async def download_application_document(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    app_row = await _get_application(application_id, db)
    await _authorize_view_application(app_row, user, db)
    if not app_row.stored_filename:
        raise HTTPException(404, "Document not found.")
    data = _read_stored("applications", app_row.id, app_row.stored_filename)
    return _download_response(data, app_row.original_filename or "Comprehensive_Exam_Application.pdf")


@router.post("/applications/{application_id}/approve")
async def approve_application(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    app_row = await _get_application(application_id, db)
    now = _now()
    if app_row.status == "ma_pending":
        if not (app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN):
            raise _not_found()
        app_row.ma_acted_at = now
        app_row.status = "hod_pending"
    elif app_row.status == "hod_pending":
        if not await _hod_dept_matches(app_row.student_id, user, db):
            raise _not_found()
        app_row.hod_approved_by = user.id
        app_row.hod_approved_at = now
        app_row.status = "incharge_pending"
    elif app_row.status == "incharge_pending":
        if user.active_role not in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        app_row.incharge_approved_by = user.id
        app_row.incharge_approved_at = now
        app_row.status = "dpgs_pending"
    elif app_row.status == "dpgs_pending":
        if user.active_role not in (UserRole.DPGS, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        app_row.dpgs_approved_by = user.id
        app_row.dpgs_approved_at = now
        app_row.status = "approved"
        app_row.approved_at = now
    else:
        raise HTTPException(400, f"This application is not awaiting approval (current status: {app_row.status}).")
    await db.commit()
    return {"message": "Approved.", "status": app_row.status}


@router.post("/applications/{application_id}/revert")
async def revert_application(application_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    app_row = await _get_application(application_id, db)
    if app_row.status not in _OPEN_APPLICATION_STATUSES:
        raise HTTPException(400, "This application is not currently awaiting any approval.")
    authorized = False
    if app_row.status == "ma_pending":
        authorized = app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN
    elif app_row.status == "hod_pending":
        authorized = await _hod_dept_matches(app_row.student_id, user, db)
    elif app_row.status == "incharge_pending":
        authorized = user.active_role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN)
    elif app_row.status == "dpgs_pending":
        authorized = user.active_role in (UserRole.DPGS, UserRole.SUPER_ADMIN)
    if not authorized:
        raise _not_found()
    app_row.status = "reverted"
    app_row.reverted_by = user.id
    app_row.reverted_at = _now()
    app_row.revert_remark = body.remark
    await db.commit()
    return {"message": "Application reverted.", "status": app_row.status}


# ── Viva scheduling / result ─────────────────────────────────────────────────────────────────

_ROLE_LABELS = {
    "major_advisor": "Major Advisor", "co_major_advisor": "Co-Major Advisor", "member_major": "Member Major",
    "member_minor": "Member Minor", "supporting": "Supporting", "member_of_others": "Member of Others", "member": "Member",
}


@router.post("/applications/{application_id}/viva", status_code=201)
async def schedule_viva(application_id: UUID, body: VivaScheduleIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD, UserRole.SUPER_ADMIN))):
    """HOD fixes the viva date — the SOLE authority for this date; it is never replaced by an
    upload/generation/current-date timestamp later. Only reachable once the application is fully
    approved (first attempt) or the previous attempt was Unsatisfactory (a new attempt) —
    unlimited attempts, no cap enforced (confirmed rule, this revision)."""
    app_row = await _get_application(application_id, db)
    if not await _hod_dept_matches(app_row.student_id, user, db):
        raise _not_found()
    vivas = sorted(app_row.vivas, key=lambda v: v.attempt_number)
    if not vivas:
        if app_row.status != "approved":
            raise HTTPException(400, "The application must be fully approved before a Viva can be scheduled.")
        attempt_number = 1
    else:
        latest = vivas[-1]
        if latest.result != "unsatisfactory":
            raise HTTPException(400, "A Viva is already scheduled or pending a result for this application.")
        attempt_number = latest.attempt_number + 1
    viva = ComprehensiveExamViva(application_id=app_row.id, attempt_number=attempt_number, viva_date=body.viva_date, scheduled_by=user.id)
    db.add(viva)
    await db.commit()
    return {"id": str(viva.id), "attempt_number": attempt_number, "message": "Viva scheduled."}


@router.post("/vivas/{viva_id}/result")
async def record_viva_result(viva_id: UUID, body: VivaResultIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    """Only the application's actual accepted Major Advisor (never a bare FACULTY check) may
    record a result. Satisfactory generates the Viva Report immediately (auto-signed by the same
    MA, mirroring FinalCertificate's Viva kind); Unsatisfactory records the outcome and leaves
    the door open for the HOD to schedule a new attempt — no report is generated, no maximum
    attempt count is enforced."""
    viva = await db.get(ComprehensiveExamViva, viva_id)
    if not viva:
        raise _not_found()
    app_row = await _get_application(viva.application_id, db)
    if not (app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN):
        raise _not_found()
    if viva.result != "pending":
        raise HTTPException(400, "A result has already been recorded for this Viva attempt.")

    now = _now()
    viva.result = body.result
    viva.result_by = user.id
    viva.result_at = now

    if body.result == "unsatisfactory":
        await db.commit()
        return {"message": "Result recorded. The HOD must schedule a new Viva attempt.", "result": viva.result}

    student = await _load_student(app_row.student_id, db)
    members = await _committee_members_excluding_ma(app_row.student_id, db)
    report = ComprehensiveExamVivaReport(viva_id=viva.id, status="generated", generated_by_id=user.id, generated_at=now)
    db.add(report)
    await db.flush()
    for m in members:
        db.add(ComprehensiveExamVivaReportSignature(report_id=report.id, committee_member_id=m.id, faculty_id=m.faculty_id, role_snapshot=m.role))
    await db.flush()
    await db.refresh(report, attribute_names=["signatures"])

    ma_user = await db.get(User, app_row.ma_id)
    context = _viva_report_pdf_context(app_row, viva, report, student, ma_user, members)
    pdf_bytes = await _render_or_503("comprehensive_exam_viva_report.html", context)
    stored_name = f"{secrets.token_hex(16)}.pdf"
    _write_stored("viva-reports", report.id, stored_name, pdf_bytes)
    report.stored_filename = stored_name
    report.original_filename = "Comprehensive_Exam_Internal_Viva_Report.pdf"
    report.content_type = "application/pdf"
    await db.commit()
    return {"message": "Result recorded. Viva Report generated.", "result": viva.result, "report_id": str(report.id)}


def _viva_report_pdf_context(app_row, viva, report, student: User, ma_user: Optional[User], members: list[CommitteeMember]) -> dict:
    return {
        "university": UNIVERSITY_NAME,
        "college_name": student.college.name if student.college else None,
        "degree_level": app_row.degree_level,
        "student_name": student.full_name, "student_roll": student.student_roll,
        "department_name": student.department.name if student.department else None,
        "viva_date": viva.viva_date.strftime("%d/%m/%Y"),
        "attempt_number": viva.attempt_number,
        "advisory_rows": [{
            "role_label": "Major Advisor", "name": ma_user.full_name if ma_user else "—",
            "designation": ma_user.designation if ma_user else None, "signed": bool(report.ma_acted_at),
        }] + [{
            "role_label": _ROLE_LABELS.get(m.role, m.role.replace("_", " ").title()), "name": m.faculty.full_name if m.faculty else "—",
            "designation": m.faculty.designation if m.faculty else None,
            "signed": next((s.status == "signed" for s in report.signatures if s.committee_member_id == m.id), False),
        } for m in members],
        "hod_approved": bool(report.hod_approved_at), "hod_approved_at": report.hod_approved_at.strftime("%d/%m/%Y") if report.hod_approved_at else None,
        "dpgs_approved": bool(report.dpgs_approved_at), "dpgs_approved_at": report.dpgs_approved_at.strftime("%d/%m/%Y") if report.dpgs_approved_at else None,
        "status": report.status, "version_number": report.version_number,
        "generated_at": report.generated_at.strftime("%d/%m/%Y") if report.generated_at else None,
        "external_examiner_name": None,  # internal report only — see the external report context builder
    }


# ── Viva Report workflow (mirrors FinalCertificate's committee -> HOD -> Incharge -> DPGS tail) ──

async def _get_viva_report(report_id: UUID, db: AsyncSession) -> ComprehensiveExamVivaReport:
    report = await db.get(
        ComprehensiveExamVivaReport, report_id,
        options=[selectinload(ComprehensiveExamVivaReport.signatures), selectinload(ComprehensiveExamVivaReport.viva)],
    )
    if not report:
        raise _not_found()
    return report


@router.post("/viva-reports/{report_id}/submit")
async def submit_viva_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    report = await _get_viva_report(report_id, db)
    app_row = await _get_application(report.viva.application_id, db)
    if not (app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN):
        raise _not_found()
    if report.status != "generated":
        raise HTTPException(400, "This report is not awaiting submission.")
    report.ma_acted_at = _now()
    report.status = "committee_pending" if report.signatures else "hod_pending"
    await db.commit()
    return {"message": "Submitted.", "status": report.status}


@router.post("/viva-reports/{report_id}/committee-sign")
async def sign_viva_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    """AND-gate: every accepted non-MA committee member must individually sign before the stage
    advances — one signature never completes it if others remain pending."""
    report = await _get_viva_report(report_id, db)
    if report.status != "committee_pending":
        raise HTTPException(400, "This report is not awaiting committee signatures.")
    sig = next((s for s in report.signatures if s.faculty_id == user.id), None)
    if not sig and user.active_role != UserRole.SUPER_ADMIN:
        raise _not_found()
    if sig:
        sig.status = "signed"
        sig.signed_at = _now()
    if all(s.status == "signed" for s in report.signatures):
        report.status = "hod_pending"
    await db.commit()
    return {"message": "Signed.", "status": report.status}


@router.post("/viva-reports/{report_id}/approve")
async def approve_viva_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    report = await _get_viva_report(report_id, db)
    app_row = await _get_application(report.viva.application_id, db)
    now = _now()
    if report.status == "hod_pending":
        if not await _hod_dept_matches(app_row.student_id, user, db):
            raise _not_found()
        report.hod_approved_by = user.id
        report.hod_approved_at = now
        report.status = "incharge_pending"
    elif report.status == "incharge_pending":
        if user.active_role not in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        report.incharge_approved_by = user.id
        report.incharge_approved_at = now
        report.status = "dpgs_pending"
    elif report.status == "dpgs_pending":
        if user.active_role not in (UserRole.DPGS, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        report.dpgs_approved_by = user.id
        report.dpgs_approved_at = now
        report.status = "approved"
        report.approved_at = now
    else:
        raise HTTPException(400, f"This report is not awaiting approval (current status: {report.status}).")
    await db.commit()
    return {"message": "Approved.", "status": report.status}


@router.post("/viva-reports/{report_id}/revert")
async def revert_viva_report(report_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    report = await _get_viva_report(report_id, db)
    app_row = await _get_application(report.viva.application_id, db)
    if report.status not in _OPEN_REPORT_STATUSES:
        raise HTTPException(400, "This report is not currently awaiting any approval.")
    authorized = False
    if report.status in ("generated", "ma_pending"):
        authorized = app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN
    elif report.status == "committee_pending":
        authorized = any(s.faculty_id == user.id for s in report.signatures) or user.active_role == UserRole.SUPER_ADMIN
    elif report.status == "hod_pending":
        authorized = await _hod_dept_matches(app_row.student_id, user, db)
    elif report.status == "incharge_pending":
        authorized = user.active_role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN)
    elif report.status == "dpgs_pending":
        authorized = user.active_role in (UserRole.DPGS, UserRole.SUPER_ADMIN)
    if not authorized:
        raise _not_found()
    report.status = "reverted"
    report.reverted_by = user.id
    report.reverted_at = _now()
    report.revert_remark = body.remark
    await db.commit()
    return {"message": "Reverted.", "status": report.status}


@router.get("/viva-reports/{report_id}/document")
async def download_viva_report_document(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    report = await _get_viva_report(report_id, db)
    app_row = await _get_application(report.viva.application_id, db)
    await _authorize_view_application(app_row, user, db)
    if not report.stored_filename:
        raise HTTPException(404, "Document not found.")
    data = _read_stored("viva-reports", report.id, report.stored_filename)
    return _download_response(data, report.original_filename or "Comprehensive_Exam_Internal_Viva_Report.pdf")


# ── PhD External Panel (parallel tables — never the Thesis External Examiner tables) ─────────

async def _first_approved_viva_report_exists(application_id: UUID, db: AsyncSession) -> bool:
    result = await db.execute(
        select(ComprehensiveExamVivaReport.id)
        .join(ComprehensiveExamViva, ComprehensiveExamViva.id == ComprehensiveExamVivaReport.viva_id)
        .where(ComprehensiveExamViva.application_id == application_id, ComprehensiveExamVivaReport.status == "approved")
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


@router.get("/external-panel/eligible")
async def list_external_panel_eligible(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD, UserRole.SUPER_ADMIN))):
    """PhD students whose FIRST Internal Viva Report has been fully DPGS-approved, and who do
    not already have an in-progress/approved External Panel. Scoped to the caller: a Major
    Advisor sees only their own advisees; an HOD sees only their own department; Super Admin
    sees all."""
    q = (
        select(ComprehensiveExamApplication).options(selectinload(ComprehensiveExamApplication.student))
        .where(ComprehensiveExamApplication.degree_level == "PhD")
    )
    if user.active_role == UserRole.FACULTY:
        q = q.where(ComprehensiveExamApplication.ma_id == user.id)
    elif user.active_role == UserRole.HOD:
        if not user.active_department_id:
            return []
        q = q.join(User, User.id == ComprehensiveExamApplication.student_id).where(User.department_id == user.active_department_id)
    apps = (await db.execute(q)).scalars().all()
    eligible = []
    for a in apps:
        if not await _first_approved_viva_report_exists(a.id, db):
            continue
        panel = (await db.execute(select(ComprehensiveExamExternalPanelSelection).where(ComprehensiveExamExternalPanelSelection.application_id == a.id))).scalar_one_or_none()
        if panel and panel.status == "approved":
            continue
        eligible.append({
            "application_id": str(a.id), "student_name": a.student.full_name if a.student else None,
            "student_roll": a.student.student_roll if a.student else None,
            "panel_status": panel.status if panel else None,
        })
    return eligible


@router.post("/external-panel", status_code=201)
async def propose_external_panel(body: ExternalPanelProposeIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    """Major Advisor proposes exactly 5 external examiners for a PhD student's Comprehensive
    Exam. Authorization is the real accepted Major Advisor relationship — never a bare FACULTY
    role check. `application_id` is client-supplied but only ever used to look up the target;
    the caller's actual authority over it is independently re-verified below."""
    app_row = await _get_application(body.application_id, db)
    if not (await _is_accepted_ma(app_row.student_id, user, db) or user.active_role == UserRole.SUPER_ADMIN):
        raise _not_found()
    if app_row.degree_level != "PhD":
        raise HTTPException(400, "The External Examiner Panel applies only to PhD students.")
    if not await _first_approved_viva_report_exists(app_row.id, db):
        raise HTTPException(400, "The student's first Internal Viva Report must be fully approved before proposing an External Panel.")

    selection = (await db.execute(select(ComprehensiveExamExternalPanelSelection).where(ComprehensiveExamExternalPanelSelection.application_id == app_row.id))).scalar_one_or_none()
    if selection:
        active_cycle = (await db.execute(select(ComprehensiveExamExternalPanelCycle).where(ComprehensiveExamExternalPanelCycle.selection_id == selection.id, ComprehensiveExamExternalPanelCycle.status == "active"))).scalar_one_or_none()
        if active_cycle or selection.status == "approved":
            raise HTTPException(409, "An External Panel already exists for this student.")
    else:
        selection = ComprehensiveExamExternalPanelSelection(application_id=app_row.id, status="in_progress")
        db.add(selection)
        await db.flush()

    cycle_count = (await db.execute(select(ComprehensiveExamExternalPanelCycle.id).where(ComprehensiveExamExternalPanelCycle.selection_id == selection.id))).all()
    cycle = ComprehensiveExamExternalPanelCycle(selection_id=selection.id, cycle_number=len(cycle_count) + 1, status="active", ma_id=user.id)
    db.add(cycle)
    selection.status = "in_progress"
    await db.flush()
    for idx, p in enumerate(body.proposals, start=1):
        normalized_email = p.email.strip().lower()
        examiner = (await db.execute(select(ExternalExaminer.id).where(ExternalExaminer.email == normalized_email))).scalar_one_or_none()
        db.add(ComprehensiveExamExternalPanelProposal(
            cycle_id=cycle.id, slot_number=idx, examiner_id=examiner,
            name_snapshot=p.name.strip(), specialization_snapshot=p.specialization.strip(), designation_snapshot=p.designation.strip(),
            email_snapshot=normalized_email, phone_snapshot=p.phone.strip(), institution_snapshot=p.institution.strip(),
        ))
    await db.commit()
    return {"id": str(cycle.id), "message": "External Panel proposed.", "status": cycle.status}


async def _get_panel_cycle(cycle_id: UUID, db: AsyncSession) -> ComprehensiveExamExternalPanelCycle:
    cycle = await db.get(
        ComprehensiveExamExternalPanelCycle, cycle_id,
        options=[selectinload(ComprehensiveExamExternalPanelCycle.proposals), selectinload(ComprehensiveExamExternalPanelCycle.selection), selectinload(ComprehensiveExamExternalPanelCycle.result)],
    )
    if not cycle:
        raise _not_found()
    return cycle


def _panel_current_stage(cycle: ComprehensiveExamExternalPanelCycle) -> str:
    """The single pending-stage indicator the frontend needs to show the right role's actions
    and status label — derived from the same fields `approve_external_panel`/
    `revert_external_panel` already gate on, never a second parallel state machine. Only
    meaningful while `cycle.status == "active"`; a terminal cycle (`approved`/`reverted`) has no
    "current" stage."""
    if not cycle.hod_approved_at:
        return "hod"
    if not cycle.incharge_approved_at:
        return "incharge_academic_cell"
    if not cycle.dpgs_approved_at:
        return "dpgs"
    if not cycle.vc_selection_completed_at:
        return "vc"
    return "approved"


@router.get("/external-panel/pending-approvals")
async def list_external_panel_pending_approvals(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.VICE_CHANCELLOR, UserRole.SUPER_ADMIN)),
):
    """"My External Panel approvals" inbox — mirrors the established
    `GET /external-examiners/pending-approvals` pattern exactly. Without this endpoint, HOD/
    Incharge Academic Cell/DPGS/Vice Chancellor have no way to discover which panel (and which
    `cycle_id`) is awaiting their action at all — `GET /external-panel/eligible` is deliberately
    MA/HOD-proposal-scoped only (a different concern: "who may propose", not "what awaits my
    approval") and does not serve this purpose. Never trusts a client-supplied department id —
    HOD is scoped to `user.active_department_id` exactly like every other HOD-facing list in
    this module. Declared BEFORE `/external-panel/{cycle_id}` below so this literal path segment
    is never swallowed by that route's `{cycle_id}` UUID parameter."""
    q = (
        select(ComprehensiveExamExternalPanelCycle)
        .options(
            selectinload(ComprehensiveExamExternalPanelCycle.selection)
            .selectinload(ComprehensiveExamExternalPanelSelection.application)
            .selectinload(ComprehensiveExamApplication.student)
            .selectinload(User.department),
        )
        .where(ComprehensiveExamExternalPanelCycle.status == "active")
    )
    cycles = (await db.execute(q)).scalars().all()
    rows = []
    for c in cycles:
        stage = _panel_current_stage(c)
        app_row = c.selection.application
        if user.active_role == UserRole.HOD:
            if stage != "hod":
                continue
            if not await _hod_dept_matches(app_row.student_id, user, db):
                continue
        elif user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
            if stage != "incharge_academic_cell":
                continue
        elif user.active_role == UserRole.DPGS:
            if stage != "dpgs":
                continue
        elif user.active_role == UserRole.VICE_CHANCELLOR:
            if stage != "vc":
                continue
        # SUPER_ADMIN: unrestricted, sees every active cycle regardless of stage.
        student = app_row.student
        rows.append({
            "cycle_id": str(c.id), "application_id": str(app_row.id),
            "student_name": student.full_name if student else None,
            "student_roll": student.student_roll if student else None,
            "department_name": student.department.name if student and student.department else None,
            "degree_level": app_row.degree_level, "current_stage": stage,
            "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
        })
    rows.sort(key=lambda r: r["submitted_at"] or "")
    return rows


@router.get("/external-panel/{cycle_id}")
async def get_external_panel(cycle_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    cycle = await _get_panel_cycle(cycle_id, db)
    app_row = await _get_application(cycle.selection.application_id, db)
    await _authorize_view_application(app_row, user, db)
    student = await _load_student(app_row.student_id, db)
    return {
        "id": str(cycle.id), "status": cycle.status, "application_id": str(app_row.id),
        "degree_level": app_row.degree_level,
        "student_name": student.full_name if student else None,
        "student_roll": student.student_roll if student else None,
        "department_name": student.department.name if student and student.department else None,
        # Stage-approval detail — additive, needed by the frontend to know whose turn it is and
        # to render each approver's own timestamp; never a second workflow, just exposing the
        # same fields the approve/revert endpoints already gate on.
        "current_stage": _panel_current_stage(cycle) if cycle.status == "active" else None,
        "hod_approved": bool(cycle.hod_approved_at), "hod_approved_at": cycle.hod_approved_at.isoformat() if cycle.hod_approved_at else None,
        "incharge_approved": bool(cycle.incharge_approved_at), "incharge_approved_at": cycle.incharge_approved_at.isoformat() if cycle.incharge_approved_at else None,
        "dpgs_approved": bool(cycle.dpgs_approved_at), "dpgs_approved_at": cycle.dpgs_approved_at.isoformat() if cycle.dpgs_approved_at else None,
        "reverted_at": cycle.reverted_at.isoformat() if cycle.reverted_at else None,
        "revert_remark": cycle.revert_remark if cycle.status == "reverted" else None,
        "proposals": [{
            "id": str(p.id), "slot_number": p.slot_number, "name": p.name_snapshot, "specialization": p.specialization_snapshot,
            "designation": p.designation_snapshot, "email": p.email_snapshot, "phone": p.phone_snapshot, "institution": p.institution_snapshot,
        } for p in cycle.proposals],
        "selected_proposal_id": str(cycle.result.proposal_id) if cycle.result else None,
        "vc_selection_completed": bool(cycle.vc_selection_completed_at),
        "email_sent_at": cycle.result.email_sent_at.isoformat() if cycle.result and cycle.result.email_sent_at else None,
        "required_selection_count": COMPREHENSIVE_EXAM_EXTERNAL_SELECTION_COUNT,
    }


@router.post("/external-panel/{cycle_id}/approve")
async def approve_external_panel(cycle_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.SUPER_ADMIN))):
    cycle = await _get_panel_cycle(cycle_id, db)
    app_row = await _get_application(cycle.selection.application_id, db)
    now = _now()
    if cycle.status != "active":
        raise HTTPException(400, "This panel is not currently awaiting approval.")
    if not cycle.hod_approved_at:
        if not await _hod_dept_matches(app_row.student_id, user, db):
            raise _not_found()
        cycle.hod_approved_by = user.id
        cycle.hod_approved_at = now
        return await _commit_and_return(db, {"message": "HOD approved.", "stage": "hod"})
    if not cycle.incharge_approved_at:
        if user.active_role not in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        cycle.incharge_approved_by = user.id
        cycle.incharge_approved_at = now
        return await _commit_and_return(db, {"message": "Incharge approved.", "stage": "incharge_academic_cell"})
    if not cycle.dpgs_approved_at:
        if user.active_role not in (UserRole.DPGS, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        cycle.dpgs_approved_by = user.id
        cycle.dpgs_approved_at = now
        return await _commit_and_return(db, {"message": "DPGS approved. Awaiting Vice Chancellor selection.", "stage": "dpgs"})
    raise HTTPException(400, "Use the External Panel selection endpoint to complete the Vice Chancellor stage.")


async def _commit_and_return(db: AsyncSession, payload: dict) -> dict:
    await db.commit()
    return payload


@router.post("/external-panel/{cycle_id}/revert")
async def revert_external_panel(cycle_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.VICE_CHANCELLOR, UserRole.SUPER_ADMIN))):
    cycle = await _get_panel_cycle(cycle_id, db)
    app_row = await _get_application(cycle.selection.application_id, db)
    if cycle.status != "active":
        raise HTTPException(400, "This panel is not currently awaiting any approval.")
    authorized = False
    if not cycle.hod_approved_at:
        authorized = await _hod_dept_matches(app_row.student_id, user, db)
    elif not cycle.incharge_approved_at:
        authorized = user.active_role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN)
    elif not cycle.dpgs_approved_at:
        authorized = user.active_role in (UserRole.DPGS, UserRole.SUPER_ADMIN)
    elif not cycle.vc_selection_completed_at:
        authorized = user.active_role in (UserRole.VICE_CHANCELLOR, UserRole.SUPER_ADMIN)
    if not authorized:
        raise _not_found()
    cycle.status = "reverted"
    cycle.reverted_at = _now()
    cycle.revert_remark = body.remark
    await db.commit()
    return {"message": "External Panel reverted. The Major Advisor may propose a new panel.", "status": cycle.status}


@router.post("/external-panel/{cycle_id}/vc-selection")
async def select_external_examiner(cycle_id: UUID, body: VcSelectionIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.VICE_CHANCELLOR, UserRole.SUPER_ADMIN))):
    """VC selects exactly ONE of the five proposed examiners. Idempotency-protected via
    `vc_selection_completed_at` (checked before writing) — a repeated call never re-selects or
    creates a second result. CRITICAL: this NEVER creates a `User` account for the selected
    examiner — `ExternalExaminer.user_id` is left NULL, permanently, unlike the existing Thesis
    External Examiner flow (`_resolve_or_create_examiner_account`), which must NOT be copied
    here."""
    cycle = await _get_panel_cycle(cycle_id, db)
    app_row = await _get_application(cycle.selection.application_id, db)
    if cycle.vc_selection_completed_at:
        # Already completed — idempotent replay, never a second selection. Checked BEFORE the
        # "still active" guard below, since a successful selection already advances
        # cycle.status to "approved" — that must never be mistaken for "not yet reachable".
        return {"message": "Selection already completed.", "selected_proposal_id": str(cycle.result.proposal_id) if cycle.result else None}
    if cycle.status != "active" or not cycle.dpgs_approved_at:
        raise HTTPException(400, "This panel is not yet awaiting Vice Chancellor selection.")

    proposal = next((p for p in cycle.proposals if p.id == body.proposal_id), None)
    if not proposal:
        raise HTTPException(404, "Proposal not found in this panel.")

    now = _now()
    examiner = (await db.execute(select(ExternalExaminer).where(ExternalExaminer.email == proposal.email_snapshot))).scalar_one_or_none()
    if not examiner:
        examiner = ExternalExaminer(email=proposal.email_snapshot)  # user_id intentionally left NULL — no AMS account, ever.
        db.add(examiner)
        await db.flush()
    proposal.examiner_id = examiner.id

    result_row = ComprehensiveExamExternalPanelResult(cycle_id=cycle.id, proposal_id=proposal.id, selected_by=user.id, selected_at=now)
    db.add(result_row)
    cycle.vc_selection_completed_at = now
    cycle.status = "approved"
    cycle.completed_at = now
    cycle.selection.status = "approved"

    student = await db.get(User, app_row.student_id)
    subject = "Selection as External Examiner for Comprehensive Examination"
    body_text = (
        f"Dear {proposal.name_snapshot},\n\n"
        f"We are pleased to inform you that you have been selected as the External Examiner for the "
        f"Comprehensive Examination of {student.full_name if student else '—'}, {app_row.degree_level} programme, "
        f"at Assam Veterinary and Fishery University.\n\n"
        f"The examination will be conducted offline. Further examination-related coordination will be "
        f"communicated through the concerned authority.\n\n"
        f"Regards,\nAssam Veterinary and Fishery University"
    )
    enqueue_email(db, proposal.email_snapshot, subject, body_text)
    result_row.email_sent_at = now
    await db.commit()
    return {"message": "External Examiner selected and notified.", "selected_proposal_id": str(proposal.id)}


# ── PhD External Viva Report (blank template for printing, then a signed-scan upload) ─────────

async def _selected_external_examiner_name(application_id: UUID, db: AsyncSession) -> Optional[str]:
    panel = (await db.execute(select(ComprehensiveExamExternalPanelSelection).where(ComprehensiveExamExternalPanelSelection.application_id == application_id))).scalar_one_or_none()
    if not panel:
        return None
    cycle = (await db.execute(
        select(ComprehensiveExamExternalPanelCycle).options(selectinload(ComprehensiveExamExternalPanelCycle.result).selectinload(ComprehensiveExamExternalPanelResult.proposal))
        .where(ComprehensiveExamExternalPanelCycle.selection_id == panel.id, ComprehensiveExamExternalPanelCycle.status == "approved")
        .order_by(ComprehensiveExamExternalPanelCycle.cycle_number.desc()).limit(1)
    )).scalar_one_or_none()
    if not cycle or not cycle.result:
        return None
    return cycle.result.proposal.name_snapshot


@router.get("/applications/{application_id}/external-viva-report/blank-document")
async def download_blank_external_report(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    """A NOT-stored, on-demand rendering of the printable (unsigned) External Viva Report — the
    Major Advisor prints this, collects physical signatures offline, then uploads the signed scan
    via `/external-viva-report/upload` (the only copy that becomes the authoritative record)."""
    app_row = await _get_application(application_id, db)
    if not (app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN):
        raise _not_found()
    if app_row.degree_level != "PhD":
        raise HTTPException(400, "The External Viva Report applies only to PhD students.")
    student = await _load_student(app_row.student_id, db)
    latest_viva = sorted(app_row.vivas, key=lambda v: v.attempt_number)[-1] if app_row.vivas else None
    if not latest_viva:
        raise HTTPException(400, "No Viva has been scheduled for this application.")
    ma_user = await db.get(User, app_row.ma_id)
    members = await _committee_members_excluding_ma(app_row.student_id, db)
    examiner_name = await _selected_external_examiner_name(app_row.id, db)
    context = {
        "university": UNIVERSITY_NAME, "college_name": student.college.name if student.college else None,
        "degree_level": app_row.degree_level, "student_name": student.full_name, "student_roll": student.student_roll,
        "department_name": student.department.name if student.department else None,
        "viva_date": latest_viva.viva_date.strftime("%d/%m/%Y"), "attempt_number": latest_viva.attempt_number,
        "advisory_rows": [{"role_label": "Major Advisor", "name": ma_user.full_name if ma_user else "—", "designation": ma_user.designation if ma_user else None, "signed": False}]
        + [{"role_label": _ROLE_LABELS.get(m.role, m.role.replace("_", " ").title()), "name": m.faculty.full_name if m.faculty else "—", "designation": m.faculty.designation if m.faculty else None, "signed": False} for m in members],
        "hod_approved": False, "hod_approved_at": None, "dpgs_approved": False, "dpgs_approved_at": None,
        "status": "blank", "version_number": None, "generated_at": _now().strftime("%d/%m/%Y"),
        "external_examiner_name": examiner_name,
    }
    pdf_bytes = await _render_or_503("comprehensive_exam_external_viva_report.html", context)
    return _download_response(pdf_bytes, "Comprehensive_Exam_External_Viva_Report_Blank.pdf")


@router.post("/applications/{application_id}/external-viva-report/upload", status_code=201)
async def upload_external_report(application_id: UUID, file: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    """Every upload creates a NEW version — never overwrites a prior signed report (mirrors
    `ThesisDocument`'s versioning contract exactly). Committee AND-gate signers are snapshotted
    fresh for each new version, exactly like the Internal Viva Report."""
    app_row = await _get_application(application_id, db)
    if not (app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN):
        raise _not_found()
    if app_row.degree_level != "PhD":
        raise HTTPException(400, "The External Viva Report applies only to PhD students.")
    existing = (await db.execute(
        select(ComprehensiveExamExternalVivaReport).where(ComprehensiveExamExternalVivaReport.application_id == application_id)
        .order_by(ComprehensiveExamExternalVivaReport.version_number.desc()).limit(1)
    )).scalar_one_or_none()
    if existing and existing.status in _OPEN_EXTERNAL_REPORT_STATUSES:
        raise HTTPException(409, "A signed report is already in progress for this application. Wait for it to be reverted before uploading a new version.")

    name = (file.filename or "").strip()
    if os.path.splitext(name)[1].lower() != ".pdf":
        raise HTTPException(400, "Only PDF files are accepted.")
    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared not in _ALLOWED_PDF_CONTENT_TYPES:
        raise HTTPException(400, "Only PDF files are accepted.")
    data = await file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, f"The file exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")
    if not data or not data.startswith(b"%PDF-"):
        raise HTTPException(400, "The file is not a PDF document.")
    try:
        inspect_pdf(data)
    except InvalidPdf as exc:
        raise HTTPException(400, str(exc)) from exc
    digest = hashlib.sha256(data).hexdigest()

    version_number = (existing.version_number + 1) if existing else 1
    stored_name = f"{secrets.token_hex(16)}.pdf"
    report = ComprehensiveExamExternalVivaReport(
        application_id=app_row.id, version_number=version_number, status="uploaded",
        stored_filename=stored_name, original_filename=name, content_type="application/pdf",
        size_bytes=len(data), sha256=digest, uploaded_by=user.id,
    )
    db.add(report)
    await db.flush()
    members = await _committee_members_excluding_ma(app_row.student_id, db)
    for m in members:
        db.add(ComprehensiveExamExternalReportSignature(report_id=report.id, committee_member_id=m.id, faculty_id=m.faculty_id, role_snapshot=m.role))
    _write_stored("external-viva-reports", report.id, stored_name, data)
    await db.commit()
    return {"id": str(report.id), "version_number": version_number, "message": "Signed report uploaded."}


async def _get_external_report(report_id: UUID, db: AsyncSession) -> ComprehensiveExamExternalVivaReport:
    report = await db.get(ComprehensiveExamExternalVivaReport, report_id, options=[selectinload(ComprehensiveExamExternalVivaReport.signatures)])
    if not report:
        raise _not_found()
    return report


@router.post("/external-viva-reports/{report_id}/submit")
async def submit_external_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    report = await _get_external_report(report_id, db)
    app_row = await _get_application(report.application_id, db)
    if not (app_row.ma_id == user.id or user.active_role == UserRole.SUPER_ADMIN):
        raise _not_found()
    if report.status != "uploaded":
        raise HTTPException(400, "This report is not awaiting submission.")
    report.submitted_at = _now()
    report.status = "committee_pending" if report.signatures else "hod_pending"
    await db.commit()
    return {"message": "Submitted.", "status": report.status}


@router.post("/external-viva-reports/{report_id}/committee-sign")
async def sign_external_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN))):
    report = await _get_external_report(report_id, db)
    if report.status != "committee_pending":
        raise HTTPException(400, "This report is not awaiting committee signatures.")
    sig = next((s for s in report.signatures if s.faculty_id == user.id), None)
    if not sig and user.active_role != UserRole.SUPER_ADMIN:
        raise _not_found()
    if sig:
        sig.status = "signed"
        sig.signed_at = _now()
    if all(s.status == "signed" for s in report.signatures):
        report.status = "hod_pending"
    await db.commit()
    return {"message": "Signed.", "status": report.status}


@router.post("/external-viva-reports/{report_id}/approve")
async def approve_external_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    report = await _get_external_report(report_id, db)
    app_row = await _get_application(report.application_id, db)
    now = _now()
    if report.status == "hod_pending":
        if not await _hod_dept_matches(app_row.student_id, user, db):
            raise _not_found()
        report.hod_approved_by = user.id
        report.hod_approved_at = now
        report.status = "incharge_pending"
    elif report.status == "incharge_pending":
        if user.active_role not in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        report.incharge_approved_by = user.id
        report.incharge_approved_at = now
        report.status = "dpgs_pending"
    elif report.status == "dpgs_pending":
        if user.active_role not in (UserRole.DPGS, UserRole.SUPER_ADMIN):
            raise HTTPException(403, "Insufficient permissions.")
        report.dpgs_approved_by = user.id
        report.dpgs_approved_at = now
        report.status = "approved"
        report.approved_at = now
    else:
        raise HTTPException(400, f"This report is not awaiting approval (current status: {report.status}).")
    await db.commit()
    return {"message": "Approved.", "status": report.status}


@router.post("/external-viva-reports/{report_id}/revert")
async def revert_external_report(report_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    report = await _get_external_report(report_id, db)
    app_row = await _get_application(report.application_id, db)
    if report.status not in _OPEN_EXTERNAL_REPORT_STATUSES:
        raise HTTPException(400, "This report is not currently awaiting any approval.")
    authorized = False
    if report.status == "committee_pending":
        authorized = any(s.faculty_id == user.id for s in report.signatures) or user.active_role == UserRole.SUPER_ADMIN
    elif report.status == "hod_pending":
        authorized = await _hod_dept_matches(app_row.student_id, user, db)
    elif report.status == "incharge_pending":
        authorized = user.active_role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.SUPER_ADMIN)
    elif report.status == "dpgs_pending":
        authorized = user.active_role in (UserRole.DPGS, UserRole.SUPER_ADMIN)
    if not authorized:
        raise _not_found()
    report.status = "reverted"
    report.reverted_by = user.id
    report.reverted_at = _now()
    report.revert_remark = body.remark
    await db.commit()
    return {"message": "Reverted. The Major Advisor must upload a new signed version.", "status": report.status}


@router.get("/external-viva-reports/{report_id}/document")
async def download_external_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    report = await _get_external_report(report_id, db)
    app_row = await _get_application(report.application_id, db)
    await _authorize_view_application(app_row, user, db)
    data = _read_stored("external-viva-reports", report.id, report.stored_filename)
    return _download_response(data, report.original_filename or "Comprehensive_Exam_External_Viva_Report.pdf")
