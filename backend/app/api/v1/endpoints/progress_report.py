"""Student Progress Report (BUSINESS_LOGIC.md section AF).

Workflow (per submission cycle):

    Student submits -> Major Advisor -> ALL other Advisory Committee members (any
    order, all must approve) -> HOD -> Incharge Academic Cell -> DPGS -> APPROVED

* Major Advisor has TWO separate operations: upload Proceedings, and approve — uploading
  never auto-approves.
* Revert destinations are STAGE-SPECIFIC (unlike Synopsis's uniform "always to student"):
  Major Advisor reverts to the STUDENT (ends the cycle, exactly like Synopsis); every other
  reverting stage goes back exactly ONE step (Committee -> Major Advisor; HOD -> Committee;
  Incharge -> HOD; DPGS -> Incharge) WITHOUT ending the cycle — a fresh "round" of stage(s)
  is opened with new stage rows (old, decided rows are never mutated, so full history
  survives every partial revert). See `_live_stages`/`_open_round`.
* Authorization is derived on the server from the caller's ACTIVE session role/department
  and the student's real Advisory Committee rows — never from an id in the request.
* Research Title is independently entered on this module — never sourced from PPW,
  Synopsis, or the Advisory Committee (a confirmed, deliberate divergence from Thesis).
* Session Year / Session Semester are pure functions of `semester_completed`, computed only
  at serialization time — never stored, never client-suppliable.
* One Progress Report per student per (academic year, semester), enforced by a database
  UNIQUE constraint — a reverted report is resubmitted in place, never duplicated.
"""
import hashlib
import math
import os
import secrets
import string
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.email import send_email
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.academic import AcademicCalendar, Semester
from app.models.progress_report import (
    ProgressReport, ProgressReportApprovalCycle, ProgressReportApprovalStage, ProgressReportProceedings, ProgressReportSignature,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import Department, Program, User, UserRole
from app.utils.pdf_merge import InvalidPdf, inspect_pdf

router = APIRouter(prefix="/progress-reports", tags=["Progress Report"])

_EDITABLE_STATUSES = ("draft", "reverted")
_APPROVER_ROLES = (UserRole.FACULTY, UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)
_COMMITTEE_ACTING_ROLES = (UserRole.FACULTY, UserRole.HOD)
_SIGNATORY_STAGES = ("major_advisor", "committee_member", "hod", "dpgs")  # OTP; incharge is workflow-only
_STAGE_ORDER = ("major_advisor", "committee_member", "hod", "incharge_academic_cell", "dpgs")

_PHASE_FOR_STAGE = {
    "major_advisor": "major_advisor_pending",
    "committee_member": "committee_pending",
    "hod": "hod_pending",
    "incharge_academic_cell": "incharge_academic_cell_pending",
    "dpgs": "dpgs_pending",
}
# Stage-specific revert destination — the report status a revert of THIS stage type lands
# on. Major Advisor is intentionally absent here: its revert ends the cycle entirely (see
# `revert_stage`), it does not simply move to a "previous" status within an active cycle.
_REVERT_DESTINATION_STATUS = {
    "committee_member": "major_advisor_pending",
    "hod": "committee_pending",
    "incharge_academic_cell": "hod_pending",
    "dpgs": "incharge_academic_cell_pending",
}
_STATUS_LABELS = {
    "draft": "Draft",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "committee_pending": "Advisory Committee Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "incharge_academic_cell_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending",
    "approved": "Approved",
    "reverted": "Reverted",
}
_STAGE_ROLE_LABELS = {
    "major_advisor": "Major Advisor", "hod": "Head of the Department",
    "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS",
}
_ADVISORY_LABELS = {
    "major_advisor": "Major Advisor", "co_major_advisor": "Co-Major Advisor", "member_major": "Member Major",
    "member_minor": "Member Minor", "supporting": "Supporting", "member_of_others": "Members from Others", "member": "Member",
}
_ROLE_DISPLAY = {"faculty": "Faculty", "hod": "HOD", "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "super_admin": "Super Admin"}

_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
_ALLOWED_PDF_CONTENT_TYPES = {"", "application/pdf", "application/x-pdf", "application/octet-stream"}
_OTP_TTL_MINUTES = 10

_SESSION_YEAR_LABELS = {1: "First Year", 2: "Second Year", 3: "Third Year", 4: "Fourth Year"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _not_found() -> HTTPException:
    return HTTPException(404, "Progress Report not found.")


def _advisory_label(role: str) -> str:
    return _ADVISORY_LABELS.get(role, role.replace("_", " ").title())


def _person_name(u: Optional[User]) -> Optional[str]:
    return u.full_name if u else None


def _name_designation(u: Optional[User]) -> str:
    if not u:
        return "—"
    return f"{u.full_name} ({u.designation})" if u.designation else u.full_name


def _is_dev_environment() -> bool:
    return settings.ENVIRONMENT == "development"


def _session_labels(semester_completed: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """Pure function of `semester_completed` — never stored, never client-suppliable.
    session_year = ceil(n / 2); odd n -> First Semester, even n -> Second Semester."""
    if not semester_completed or semester_completed < 1:
        return None, None
    year_number = math.ceil(semester_completed / 2)
    year_label = _SESSION_YEAR_LABELS.get(year_number, f"Year {year_number}")
    sem_label = "First Semester" if semester_completed % 2 == 1 else "Second Semester"
    return year_label, sem_label


def _completion_wire(value: Optional[bool]) -> Optional[str]:
    """DB Boolean -> wire "Yes"/"No" (or None if never answered) — the closed two-value vocabulary
    the confirmed requirement's own examples use on the wire, backed by a plain Boolean column
    (the established repository convention for a closed two-value field), never a free-text value."""
    if value is None:
        return None
    return "Yes" if value else "No"


def _apply_completion_fields(target: ProgressReport, data: dict) -> None:
    """Student-entered completion fields (CORRECTED — Section 2 of the confirmed rules).
    `data` is the caller's `model_dump(exclude_unset=True)` — only fields the client actually
    sent are considered, so an incremental edit that doesn't touch these fields never disturbs
    them. Enforced here, not only by the frontend:
      * "Yes" always clears any reason (a stale reason must never remain an active explanation
        once the student says everything is on track — the chosen, documented behavior for the
        Section 10 "stale reason while Yes" case).
      * "No" requires a non-blank reason, sourced from THIS request if given, otherwise from
        whatever is already stored (so a later, unrelated edit doesn't have to keep re-sending it).
      * After applying, the row is re-checked as a whole: `expected_completion is False` can never
        end up paired with a blank `completion_delay_reason`, regardless of which fields were in
        this particular request — a bad partial edit is rejected just as firmly as a bad full one.
    """
    if "expected_completion" in data:
        wire = data["expected_completion"]
        target.expected_completion = (wire == "Yes")
        if wire == "Yes":
            target.completion_delay_reason = None
        else:
            reason = (data.get("completion_delay_reason") if "completion_delay_reason" in data else target.completion_delay_reason) or ""
            reason = reason.strip()
            if not reason:
                raise HTTPException(400, "Please explain the reason, since the courses/research works are not expected to be completed in time.")
            target.completion_delay_reason = reason
    elif "completion_delay_reason" in data:
        # A reason is only ever "active" while expected_completion is No — an incremental edit
        # that sends a reason while No is already stored updates it normally; sent while Yes (or
        # never yet answered) is stored, it is simply not a meaningful value yet, so it is ignored
        # here rather than accepted as a dangling, inactive explanation (same "clear/ignore, don't
        # let a stale value become active" choice as the "Yes" branch above).
        if target.expected_completion is False:
            target.completion_delay_reason = (data["completion_delay_reason"] or "").strip() or None

    if target.expected_completion is False and not (target.completion_delay_reason or "").strip():
        raise HTTPException(400, "Please explain the reason, since the courses/research works are not expected to be completed in time.")


# ── Schemas (extra="forbid" — identity/status/session fields are never client-supplied) ──

_EDITABLE_FIELDS = (
    "period_from", "period_to", "semester_completed",
    "total_courses", "total_credits_programme", "current_semester_courses", "current_semester_credits",
    "courses_completed_till_date", "credits_completed_till_date",
    "research_title", "research_progress", "leave_availed", "fellowship_stipend",
)


class ProgressReportCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    academic_year_id: UUID
    semester_id: UUID
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    semester_completed: Optional[int] = None
    total_courses: Optional[int] = None
    total_credits_programme: Optional[int] = None
    current_semester_courses: Optional[int] = None
    current_semester_credits: Optional[int] = None
    courses_completed_till_date: Optional[int] = None
    credits_completed_till_date: Optional[int] = None
    research_title: Optional[str] = None
    research_progress: Optional[str] = None
    leave_availed: Optional[str] = None
    fellowship_stipend: Optional[str] = None
    # Student-entered (CORRECTED — Section 1/2 of the confirmed rules): a closed Yes/No choice,
    # never free text; any other value is rejected by the `Literal` type itself (422).
    expected_completion: Optional[Literal["Yes", "No"]] = None
    completion_delay_reason: Optional[str] = None


class ProgressReportUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    semester_completed: Optional[int] = None
    total_courses: Optional[int] = None
    total_credits_programme: Optional[int] = None
    current_semester_courses: Optional[int] = None
    current_semester_credits: Optional[int] = None
    courses_completed_till_date: Optional[int] = None
    credits_completed_till_date: Optional[int] = None
    research_title: Optional[str] = None
    research_progress: Optional[str] = None
    leave_availed: Optional[str] = None
    fellowship_stipend: Optional[str] = None
    expected_completion: Optional[Literal["Yes", "No"]] = None
    completion_delay_reason: Optional[str] = None


class AdvisorFieldsIn(BaseModel):
    """CORRECTED — these are the actual Major Advisor fields (Section 3/4 of the confirmed
    rules); the Yes/No completion fields moved to the student's own schemas above. `extra="forbid"`
    means a Student (or anyone else) attempting to submit these three fields through the STUDENT
    create/update schemas gets a `422` — they are not even valid keys there — never a silent
    ignore, and this endpoint itself remains gated to the live Major Advisor only."""
    model_config = ConfigDict(extra="forbid")
    advisory_remark: Optional[str] = None
    overall_progress: Optional[str] = None
    student_conduct: Optional[str] = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp: Optional[str] = None


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: str


# ── Load / lookup helpers ─────────────────────────────────────────────────────

_LOAD_OPTIONS = (
    selectinload(ProgressReport.student).selectinload(User.program),
    selectinload(ProgressReport.academic_year), selectinload(ProgressReport.semester),
    selectinload(ProgressReport.cycles).selectinload(ProgressReportApprovalCycle.stages).selectinload(ProgressReportApprovalStage.committee_member).selectinload(CommitteeMember.faculty),
    selectinload(ProgressReport.cycles).selectinload(ProgressReportApprovalCycle.stages).selectinload(ProgressReportApprovalStage.assignee),
    selectinload(ProgressReport.cycles).selectinload(ProgressReportApprovalCycle.stages).selectinload(ProgressReportApprovalStage.approver),
    selectinload(ProgressReport.cycles).selectinload(ProgressReportApprovalCycle.proceedings),
)


async def _get_report(report_id: UUID, db: AsyncSession) -> ProgressReport:
    r = (await db.execute(select(ProgressReport).options(*_LOAD_OPTIONS).where(ProgressReport.id == report_id))).scalar_one_or_none()
    if not r:
        raise _not_found()
    return r


def _latest_cycle(r: ProgressReport) -> Optional[ProgressReportApprovalCycle]:
    return max(r.cycles, key=lambda c: c.cycle_number) if r.cycles else None


def _active_cycle(r: ProgressReport) -> Optional[ProgressReportApprovalCycle]:
    c = _latest_cycle(r)
    return c if c and c.status == "active" else None


def _live_stages(cycle: ProgressReportApprovalCycle) -> list[ProgressReportApprovalStage]:
    """The one LIVE row per (stage_type, committee_member_id) — the highest-`sequence` row in
    that group. Everything else is pure, immutable history (an already-decided/superseded
    row is never re-used)."""
    best: dict[tuple[str, Optional[UUID]], ProgressReportApprovalStage] = {}
    for st in cycle.stages:
        key = (st.stage_type, st.committee_member_id)
        if key not in best or st.sequence > best[key].sequence:
            best[key] = st
    return list(best.values())


def _latest_proceedings(cycle: Optional[ProgressReportApprovalCycle]) -> Optional[ProgressReportProceedings]:
    if not cycle or not cycle.proceedings:
        return None
    return max(cycle.proceedings, key=lambda p: p.version_number)


def _ensure_live_single_stage(cycle: ProgressReportApprovalCycle, stage_type: str) -> None:
    """Guarantee that the LIVE row for this single-actor `stage_type` (hod / incharge_academic_cell
    / dpgs) is actionable ('pending') before the report transitions into the status that stage
    represents. A revert several steps further down the chain (e.g. Incharge -> HOD, or DPGS ->
    Incharge) only re-opens the ONE stage type it targets directly — an earlier round's HOD/
    Incharge/DPGS row for a type further along than that can be left `approved`/`reverted` (a
    decided, terminal row) with no fresh row ever created for it. Without this check, once the
    chain naturally arrives back at that type's phase, its live row would still be that stale,
    terminal one and nobody could ever act on it again. Open a fresh round-of-one row whenever
    the live row for this type is not already pending."""
    live = next((s for s in _live_stages(cycle) if s.stage_type == stage_type), None)
    if live and live.status == "pending":
        return
    next_seq = (max((s.sequence for s in cycle.stages), default=0)) + 1
    cycle.stages.append(ProgressReportApprovalStage(
        cycle_id=cycle.id, sequence=next_seq, stage_type=stage_type, role_label=_STAGE_ROLE_LABELS[stage_type],
    ))


# ── File storage ──────────────────────────────────────────────────────────────

def _proceedings_dir(report_id: UUID) -> Path:
    return Path(settings.UPLOAD_DIR) / "progress-report" / str(report_id)


def _stored_path(report_id: UUID, stored_filename: str) -> Path:
    import re
    if not re.match(r"^[0-9a-f]{32}\.pdf$", stored_filename or ""):
        raise HTTPException(500, "Invalid stored file reference.")
    return _proceedings_dir(report_id) / stored_filename


def _write_stored(report_id: UUID, stored_filename: str, data: bytes) -> None:
    directory = _proceedings_dir(report_id)
    directory.mkdir(parents=True, exist_ok=True)
    with open(_stored_path(report_id, stored_filename), "xb") as f:
        f.write(data)


def _read_stored(report_id: UUID, stored_filename: str) -> bytes:
    path = _stored_path(report_id, stored_filename)
    if not path.is_file():
        raise HTTPException(404, "The stored file is missing on the server.")
    return path.read_bytes()


async def _validate_pdf_upload(upload: UploadFile) -> tuple[bytes, str]:
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


# ── Authorization ────────────────────────────────────────────────────────────

async def _committee(student_id: UUID, db: AsyncSession) -> Optional[AdvisoryCommittee]:
    return (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == student_id)
    )).scalar_one_or_none()


async def _committee_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    return (await db.execute(select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id == student_id))).scalar_one_or_none()


async def _authorize_view(r: ProgressReport, user: User, db: AsyncSession) -> None:
    """Who may READ a Progress Report. Anything else is a 404 so the existence of another
    student's report is never revealed. A draft is private to the student (and Super Admin)."""
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return
    if role == UserRole.STUDENT:
        if r.student_id == user.id:
            return
        raise _not_found()
    if r.status == "draft":
        raise _not_found()
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
        return
    if role in _COMMITTEE_ACTING_ROLES:
        assigned = await db.execute(
            select(ProgressReportApprovalStage.id)
            .join(ProgressReportApprovalCycle, ProgressReportApprovalCycle.id == ProgressReportApprovalStage.cycle_id)
            .where(ProgressReportApprovalCycle.report_id == r.id, ProgressReportApprovalStage.assignee_id == user.id)
            .limit(1)
        )
        if assigned.scalar_one_or_none():
            return
    if role == UserRole.HOD:
        dept_id = await resolve_student_department_id(r.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
    raise _not_found()


async def _find_my_stage(r: ProgressReport, user: User, db: AsyncSession) -> Optional[ProgressReportApprovalStage]:
    """The SOLE authorization check for every approval action: the caller's own currently
    actionable LIVE stage in the ACTIVE cycle, or None. Never a bare role match and never an
    id from the request — mirrors Synopsis's `_find_my_stage` exactly, generalized over
    `_live_stages` so a re-opened round after a partial revert is found the same way."""
    cycle = _active_cycle(r)
    if not cycle:
        return None
    committee_id = None
    for stage in _live_stages(cycle):
        if stage.status != "pending" or _PHASE_FOR_STAGE.get(stage.stage_type) != r.status:
            continue
        if stage.stage_type in ("major_advisor", "committee_member"):
            if user.active_role not in _COMMITTEE_ACTING_ROLES:
                continue
            member = stage.committee_member
            if not member or member.faculty_id != user.id:
                continue
            is_major = member.role == "major_advisor"
            if is_major != (stage.stage_type == "major_advisor"):
                continue
            if committee_id is None:
                committee_id = await _committee_id(r.student_id, db)
            if committee_id and member.committee_id == committee_id:
                return stage
        elif stage.stage_type == "hod":
            if user.active_role == UserRole.HOD and user.active_department_id:
                dept_id = await resolve_student_department_id(r.student_id, db)
                if dept_id and dept_id == user.active_department_id:
                    return stage
        elif stage.stage_type == "incharge_academic_cell":
            if user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
                return stage
        elif stage.stage_type == "dpgs":
            if user.active_role == UserRole.DPGS:
                return stage
    return None


async def _require_my_stage(r: ProgressReport, user: User, db: AsyncSession) -> ProgressReportApprovalStage:
    stage = await _find_my_stage(r, user, db)
    if not stage:
        raise HTTPException(403, "You have no pending Progress Report approval action.")
    return stage


def _is_live_major_advisor(r: ProgressReport, cycle: Optional[ProgressReportApprovalCycle], user: User) -> bool:
    """Whether `user` currently holds the LIVE Major Advisor stage of `cycle` — used to gate
    Proceedings upload, which is separate from (and does not require) approving."""
    if not cycle or r.status != "major_advisor_pending" or user.active_role not in _COMMITTEE_ACTING_ROLES:
        return False
    for stage in _live_stages(cycle):
        if stage.stage_type == "major_advisor" and stage.status == "pending":
            member = stage.committee_member
            return bool(member and member.faculty_id == user.id)
    return False


# ── Serialization ─────────────────────────────────────────────────────────────

async def _department_names(db: AsyncSession) -> dict:
    return {i: n for i, n in (await db.execute(select(Department.id, Department.name))).all()}


def _proceedings_dict(p: Optional[ProgressReportProceedings]) -> Optional[dict]:
    if not p:
        return None
    return {"id": str(p.id), "version": p.version_number, "original_filename": p.original_filename, "uploaded_at": _iso(p.uploaded_at)}


def _stage_dict(stage: ProgressReportApprovalStage, dept_names: dict) -> dict:
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


async def _report_dict(r: ProgressReport, viewer: User, db: AsyncSession) -> dict:
    dept_names = await _department_names(db)
    cycle = _latest_cycle(r)
    my_stage = await _find_my_stage(r, viewer, db) if viewer.active_role in _APPROVER_ROLES else None
    is_advisor_viewer = viewer.active_role in _COMMITTEE_ACTING_ROLES
    session_year, session_semester = _session_labels(r.semester_completed)

    revert_info = None
    if r.status == "reverted" and cycle:
        stage = next((st for st in cycle.stages if st.status == "reverted"), None)
        if stage:
            revert_info = {
                "reverted_by": _name_designation(stage.approver) if stage.approver else None,
                "role": stage.role_label,
                "department": dept_names.get(stage.acted_department_id),
                "reverted_at": _iso(stage.acted_at), "remark": stage.remark,
            }

    all_stage_history = []
    for c in r.cycles:
        for st in c.stages:
            all_stage_history.append({"cycle_number": c.cycle_number, **_stage_dict(st, dept_names)})

    is_owner = viewer.active_role == UserRole.STUDENT and r.student_id == viewer.id
    proceedings = _latest_proceedings(cycle)

    body = {
        "id": str(r.id), "status": r.status, "status_label": _STATUS_LABELS.get(r.status, r.status),
        "student": {
            "name": r.student_name_snapshot, "roll_no": r.student_roll_snapshot, "program_name": r.program_snapshot,
        },
        "academic_year_id": str(r.academic_year_id), "academic_year": r.academic_year.academic_year if r.academic_year else None,
        "semester_id": str(r.semester_id), "semester_name": r.semester.name if r.semester else None,
        "period_from": _iso(r.period_from) if r.period_from else None, "period_to": _iso(r.period_to) if r.period_to else None,
        "semester_completed": r.semester_completed, "session_year": session_year, "session_semester": session_semester,
        "total_courses": r.total_courses, "total_credits_programme": r.total_credits_programme,
        "current_semester_courses": r.current_semester_courses, "current_semester_credits": r.current_semester_credits,
        "courses_completed_till_date": r.courses_completed_till_date, "credits_completed_till_date": r.credits_completed_till_date,
        "research_title": r.research_title, "research_progress": r.research_progress,
        "leave_availed": r.leave_availed, "fellowship_stipend": r.fellowship_stipend,
        # Student-entered (CORRECTED — see module docstring): visible to every authorized viewer,
        # editable only by the student while `can_edit`.
        "expected_completion": _completion_wire(r.expected_completion), "completion_delay_reason": r.completion_delay_reason,
        "is_owner": is_owner, "can_edit": is_owner and r.status in _EDITABLE_STATUSES,
        "revert_info": revert_info,
        "current_cycle_number": cycle.cycle_number if cycle else None,
        "stages": [_stage_dict(st, dept_names) for st in _live_stages(cycle)] if cycle else [],
        "history": all_stage_history,
        "my_pending_stage": (
            {"stage_type": my_stage.stage_type, "role_label": my_stage.role_label, "requires_otp": my_stage.stage_type in _SIGNATORY_STAGES}
            if my_stage else None
        ),
        "submitted_at": _iso(r.submitted_at), "approved_at": _iso(r.approved_at),
    }
    if viewer.active_role != UserRole.STUDENT:
        body["proceedings"] = proceedings if is_advisor_viewer or viewer.active_role in (UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.SUPER_ADMIN) else None
        # Major Advisor fields (CORRECTED — Section 6 of the confirmed rules): read-only for every
        # non-student viewer (Major Advisor, other committee members, HOD, Incharge Academic Cell,
        # DPGS, Super Admin); genuinely ABSENT from the response for the student, never null/empty,
        # matching this repository's established confidentiality-by-omission convention.
        body["advisory_remark"] = r.advisory_remark
        body["overall_progress"] = r.overall_progress
        body["student_conduct"] = r.student_conduct
        body["can_edit_advisor_fields"] = _is_live_major_advisor(r, cycle, viewer)
    return body


# ── Create / read ─────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_progress_report(
    body: ProgressReportCreateIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """A student creates their Progress Report for one (academic year, semester). No
    student id is ever accepted — Roll No/Name/Programme are snapshotted from the caller's
    own `User` record. The database's UNIQUE constraint is the final guarantee; the
    pre-check below only gives a friendly message. This is the SAME record reused across a
    revert-and-resubmit cycle — a second creation attempt for the same semester is refused
    even if the first is `reverted`."""
    calendar = await db.get(AcademicCalendar, body.academic_year_id)
    if not calendar:
        raise HTTPException(400, "The selected Academic Year does not exist.")
    semester = await db.get(Semester, body.semester_id)
    if not semester or semester.calendar_id != calendar.id:
        raise HTTPException(400, "The selected Semester does not belong to the selected Academic Year.")
    existing = (await db.execute(
        select(ProgressReport.id).where(
            ProgressReport.student_id == user.id, ProgressReport.academic_year_id == calendar.id, ProgressReport.semester_id == semester.id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "You already have a Progress Report for this Academic Year and Semester. Open it to edit or resubmit instead.")

    program = await db.get(Program, user.program_id) if user.program_id else None
    r = ProgressReport(
        student_id=user.id, academic_year_id=calendar.id, semester_id=semester.id, status="draft",
        student_name_snapshot=user.full_name, student_roll_snapshot=user.student_roll,
        program_snapshot=program.name if program else None,
        **body.model_dump(exclude={"academic_year_id", "semester_id", "expected_completion", "completion_delay_reason"}, exclude_none=True),
    )
    _apply_completion_fields(r, body.model_dump(exclude_unset=True))
    db.add(r)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You already have a Progress Report for this Academic Year and Semester.")
    return {"id": str(r.id), "message": "Progress Report draft created.", "status": r.status}


@router.get("/mine")
async def list_my_reports(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    rows = (await db.execute(
        select(ProgressReport).options(selectinload(ProgressReport.academic_year), selectinload(ProgressReport.semester))
        .where(ProgressReport.student_id == user.id).order_by(ProgressReport.created_at.desc())
    )).scalars().all()
    out = []
    for r in rows:
        session_year, session_semester = _session_labels(r.semester_completed)
        out.append({
            "id": str(r.id), "session_year": session_year, "session_semester": session_semester,
            "academic_year": r.academic_year.academic_year if r.academic_year else None,
            "status": r.status, "status_label": _STATUS_LABELS.get(r.status, r.status),
        })
    return out


@router.get("/pending-approvals")
async def list_pending_approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    ids: set[UUID] = set()
    if user.active_role in _COMMITTEE_ACTING_ROLES:
        ids.update((await db.execute(
            select(ProgressReport.id)
            .join(ProgressReportApprovalCycle, ProgressReportApprovalCycle.report_id == ProgressReport.id)
            .join(ProgressReportApprovalStage, ProgressReportApprovalStage.cycle_id == ProgressReportApprovalCycle.id)
            .where(ProgressReportApprovalCycle.status == "active", ProgressReportApprovalStage.status == "pending",
                   ProgressReportApprovalStage.assignee_id == user.id)
        )).scalars().all())
    phase = {UserRole.HOD: "hod_pending", UserRole.INCHARGE_ACADEMIC_CELL: "incharge_academic_cell_pending", UserRole.DPGS: "dpgs_pending"}.get(user.active_role)
    if phase:
        q = select(ProgressReport.id).where(ProgressReport.status == phase)
        if user.active_role == UserRole.HOD:
            if not user.active_department_id:
                return []
            q = q.join(User, User.id == ProgressReport.student_id).where(User.department_id == user.active_department_id)
        ids.update((await db.execute(q)).scalars().all())

    rows = []
    for rid in ids:
        r = await _get_report(rid, db)
        stage = await _find_my_stage(r, user, db)
        if not stage:
            continue
        cycle = _active_cycle(r)
        session_year, session_semester = _session_labels(r.semester_completed)
        rows.append({
            "report_id": str(r.id), "student_name": r.student_name_snapshot, "student_roll": r.student_roll_snapshot,
            "session_year": session_year, "session_semester": session_semester,
            "academic_year": r.academic_year.academic_year if r.academic_year else None,
            "status": r.status, "status_label": _STATUS_LABELS.get(r.status, r.status),
            "acting_as": stage.role_label, "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
            "cycle_number": cycle.cycle_number if cycle else None, "submitted_at": _iso(cycle.submitted_at) if cycle else None,
        })
    rows.sort(key=lambda x: x["submitted_at"] or "")
    return rows


@router.get("/{report_id}")
async def get_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    r = await _get_report(report_id, db)
    await _authorize_view(r, user, db)
    return await _report_dict(r, user, db)


@router.patch("/{report_id}")
async def update_report(
    report_id: UUID, body: ProgressReportUpdateIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    r = await _get_report(report_id, db)
    if r.student_id != user.id:
        raise _not_found()
    if r.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Progress Report is under approval or approved and can no longer be edited.")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k in ("expected_completion", "completion_delay_reason") or v is None:
            continue
        setattr(r, k, v)
    _apply_completion_fields(r, data)
    await db.commit()
    return await _report_dict(await _get_report(report_id, db), user, db)


# ── Major Advisor fields (separate from approval; Major-Advisor-only) ────────

@router.patch("/{report_id}/advisor-fields")
async def set_advisor_fields(
    report_id: UUID, body: AdvisorFieldsIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_COMMITTEE_ACTING_ROLES)),
):
    r = await _get_report(report_id, db)
    cycle = _active_cycle(r)
    if not cycle or r.status != "major_advisor_pending" or not _is_live_major_advisor(r, cycle, user):
        raise HTTPException(403, "Only the current Major Advisor may set these fields, and only while the Progress Report is at the Major Advisor stage.")
    r.advisory_remark = (body.advisory_remark or "").strip() or None
    r.overall_progress = (body.overall_progress or "").strip() or None
    r.student_conduct = (body.student_conduct or "").strip() or None
    await db.commit()
    return {"message": "Major Advisor assessment saved."}


# ── Proceedings (separate action from approval) ──────────────────────────────

@router.post("/{report_id}/proceedings", status_code=201)
async def upload_proceedings(
    report_id: UUID, file: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_COMMITTEE_ACTING_ROLES)),
):
    """Upload (or replace) the Proceedings PDF. Only the CURRENT Major Advisor, only while
    the report is at the Major Advisor stage. This never approves the stage — approving is a
    separate action (`POST .../approval/approve`)."""
    r = await _get_report(report_id, db)
    cycle = _active_cycle(r)
    if not cycle or r.status != "major_advisor_pending" or not _is_live_major_advisor(r, cycle, user):
        raise HTTPException(403, "Only the current Major Advisor may upload Proceedings, and only while the Progress Report is at the Major Advisor stage.")
    data, digest = await _validate_pdf_upload(file)
    next_version = max((p.version_number for p in cycle.proceedings), default=0) + 1
    stored_name = f"{secrets.token_hex(16)}.pdf"
    _write_stored(r.id, stored_name, data)
    db.add(ProgressReportProceedings(
        cycle_id=cycle.id, version_number=next_version,
        original_filename=os.path.basename((file.filename or "proceedings.pdf").replace("\\", "/"))[:255] or "proceedings.pdf",
        stored_filename=stored_name,
        content_type="application/pdf", size_bytes=len(data), sha256=digest, uploaded_by=user.id,
    ))
    await db.commit()
    return {"message": "Proceedings uploaded.", "version": next_version}


@router.get("/{report_id}/proceedings")
async def download_proceedings(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Visible to Major Advisor, Advisory Committee members, HOD, Incharge Academic Cell,
    DPGS and Super Admin — explicitly NOT the student."""
    r = await _get_report(report_id, db)
    if user.active_role == UserRole.STUDENT:
        raise _not_found()
    await _authorize_view(r, user, db)
    cycle = _latest_cycle(r)
    proceedings = _latest_proceedings(cycle)
    if not proceedings:
        raise HTTPException(404, "No Proceedings have been uploaded yet.")
    data = _read_stored(r.id, proceedings.stored_filename)
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{proceedings.original_filename}"'},
    )


# ── Submit ────────────────────────────────────────────────────────────────────

def _seed_stages(cycle: ProgressReportApprovalCycle, start_seq: int, major: CommitteeMember, others: list[CommitteeMember]) -> int:
    """Append one fresh row per required stage type, in order, starting at `start_seq`.
    Returns the next free sequence number. Used both at first submission (major + all
    other members + hod + incharge + dpgs) and when re-opening a later round."""
    seq = start_seq
    cycle.stages.append(ProgressReportApprovalStage(
        cycle_id=cycle.id, sequence=seq, stage_type="major_advisor", role_label=_advisory_label("major_advisor"),
        committee_member_id=major.id, assignee_id=major.faculty_id,
    ))
    seq += 1
    for m in others:
        cycle.stages.append(ProgressReportApprovalStage(
            cycle_id=cycle.id, sequence=seq, stage_type="committee_member", role_label=_advisory_label(m.role),
            committee_member_id=m.id, assignee_id=m.faculty_id,
        ))
        seq += 1
    for stage_type in ("hod", "incharge_academic_cell", "dpgs"):
        cycle.stages.append(ProgressReportApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type=stage_type, role_label=_STAGE_ROLE_LABELS[stage_type]))
        seq += 1
    return seq


@router.post("/{report_id}/submit")
async def submit_report(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    r = await _get_report(report_id, db)
    if r.student_id != user.id:
        raise _not_found()
    if r.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Progress Report is already under approval or approved.")
    if r.expected_completion is None:
        raise HTTPException(400, "Please answer whether the courses/research works are expected to be completed in time before submitting.")
    if r.expected_completion is False and not (r.completion_delay_reason or "").strip():
        raise HTTPException(400, "Please explain the reason, since the courses/research works are not expected to be completed in time.")

    committee = await _committee(user.id, db)
    major = next((m for m in committee.members if m.role == "major_advisor"), None) if committee else None
    if not major or major.accepted is not True:
        raise HTTPException(400, "Your Major Advisor must be assigned and must have accepted before you can submit your Progress Report.")
    if not major.faculty or not major.faculty.is_active:
        raise HTTPException(400, "Your Major Advisor's account is inactive. Ask your HOD to assign a replacement.")
    others = [m for m in committee.members if m.role != "major_advisor"]
    inactive = [_person_name(m.faculty) for m in others if not m.faculty or not m.faculty.is_active]
    if inactive:
        raise HTTPException(400, "These committee members are inactive: " + ", ".join(inactive) + ". Ask your HOD/Major Advisor to replace them.")

    next_number = (await db.execute(
        select(ProgressReportApprovalCycle.cycle_number).where(ProgressReportApprovalCycle.report_id == r.id).order_by(ProgressReportApprovalCycle.cycle_number.desc()).limit(1)
    )).scalar_one_or_none()
    next_number = (next_number or 0) + 1
    now = _now()
    cycle = ProgressReportApprovalCycle(report_id=r.id, cycle_number=next_number, status="active", submitted_at=now)
    cycle.stages = []  # a brand-new cycle has no rows yet — set explicitly so the async ORM
    # never needs to lazy-load this collection (which would raise MissingGreenlet outside an
    # awaited call) once `cycle` becomes persistent after the flush below.
    db.add(cycle)
    await db.flush()
    _seed_stages(cycle, 1, major, others)
    r.status = "major_advisor_pending"
    r.submitted_at = now
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This Progress Report already has an active approval cycle.")
    return {"message": "Progress Report submitted for approval.", "status": r.status, "cycle_number": next_number}


# ── OTP / approve / revert ────────────────────────────────────────────────────

def _send_otp_email(to: str, code: str) -> None:
    send_email(
        to, "AVFU AMS — Progress Report Approval OTP",
        f"Your OTP for Progress Report approval is: {code}\n\nThis code is valid for {_OTP_TTL_MINUTES} minutes. "
        "If you did not request this, you can safely ignore this email.",
    )


@router.get("/{report_id}/approval/otp")
async def request_approval_otp(report_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    r = await _get_report(report_id, db)
    stage = await _require_my_stage(r, user, db)
    if stage.stage_type not in _SIGNATORY_STAGES:
        raise HTTPException(400, "The Incharge Academic Cell approval is a workflow action and does not need an OTP.")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    db.add(ProgressReportSignature(approval_stage_id=stage.id, user_id=user.id, otp_code=code, otp_expires_at=_now() + timedelta(minutes=_OTP_TTL_MINUTES)))
    await db.commit()
    _send_otp_email(user.email, code)
    response = {"message": "OTP sent to your email."}
    if _is_dev_environment():
        response["dev_otp"] = code
    return response


async def _consume_otp(stage: ProgressReportApprovalStage, user: User, otp: str, request: Request, db: AsyncSession) -> None:
    sig = (await db.execute(
        select(ProgressReportSignature).where(
            ProgressReportSignature.approval_stage_id == stage.id, ProgressReportSignature.user_id == user.id,
            ProgressReportSignature.otp_used == False, ProgressReportSignature.otp_expires_at > _now(),  # noqa: E712
        ).order_by(ProgressReportSignature.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sig:
        raise HTTPException(400, "No valid OTP found. Please request a new OTP.")
    if not otp or not secrets.compare_digest(otp, sig.otp_code or ""):
        raise HTTPException(400, "Invalid OTP.")
    sig.otp_used = True
    sig.verified_at = _now()
    sig.ip_address = request.client.host if request.client else None
    sig.user_agent = request.headers.get("user-agent")


async def _lock_and_reload(r: ProgressReport, stage: ProgressReportApprovalStage, cycle: ProgressReportApprovalCycle, db: AsyncSession) -> None:
    """Serialize concurrent actions: take a row lock, then re-read the report's status and
    EVERY live stage's status from the database (never the copies loaded before the lock) —
    the committee-completion check below reads `cycle.stages` in-memory, so every row must be
    current, not just the caller's own."""
    await db.execute(select(ProgressReport.id).where(ProgressReport.id == r.id).with_for_update())
    current_status = (await db.execute(select(ProgressReport.status).where(ProgressReport.id == r.id))).scalar_one()
    for st in cycle.stages:
        await db.refresh(st, attribute_names=["status"])
    if stage.status != "pending" or _PHASE_FOR_STAGE.get(stage.stage_type) != current_status:
        raise HTTPException(409, "This Progress Report has already moved on; please reload.")
    r.status = current_status


def _record_action(stage: ProgressReportApprovalStage, user: User, status: str, now: datetime, remark: Optional[str] = None) -> None:
    stage.status = status
    stage.approver = user
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id
    stage.acted_at = now
    if remark is not None:
        stage.remark = remark


@router.post("/{report_id}/approval/approve")
async def approve_stage(
    report_id: UUID, body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    r = await _get_report(report_id, db)
    stage = await _require_my_stage(r, user, db)
    cycle = _active_cycle(r)
    await _lock_and_reload(r, stage, cycle, db)
    now = _now()

    if stage.stage_type in _SIGNATORY_STAGES:
        await _consume_otp(stage, user, body.otp or "", request, db)
    _record_action(stage, user, "approved", now)

    if stage.stage_type == "major_advisor":
        live = _live_stages(cycle)
        live_committee = [x for x in live if x.stage_type == "committee_member"]
        if not live_committee:
            _ensure_live_single_stage(cycle, "hod")
            r.status = "hod_pending"
        elif any(x.status == "pending" for x in live_committee):
            r.status = "committee_pending"
        else:
            # Re-opened round after a Committee-Member-triggered revert: every existing
            # committee-member row is already decided (approved/reverted/cancelled) from a
            # prior round, so none of them is actionable anymore. Open a fresh round for
            # EVERY non-MA committee member — exactly like the initial submission — so a
            # member who had already approved before the revert must approve again.
            next_seq = (max((s.sequence for s in cycle.stages), default=0)) + 1
            committee = await _committee(r.student_id, db)
            others = [m for m in committee.members if m.role != "major_advisor"] if committee else []
            seq = next_seq
            for m in others:
                cycle.stages.append(ProgressReportApprovalStage(
                    cycle_id=cycle.id, sequence=seq, stage_type="committee_member", role_label=_advisory_label(m.role),
                    committee_member_id=m.id, assignee_id=m.faculty_id,
                ))
                seq += 1
            r.status = "committee_pending"
    elif stage.stage_type == "committee_member":
        # `stage` is the same in-memory object already mutated to "approved" above, so this
        # naturally reflects the just-recorded approval without a second DB round-trip.
        others_pending = [x for x in _live_stages(cycle) if x.stage_type == "committee_member" and x.status == "pending"]
        if not others_pending:
            _ensure_live_single_stage(cycle, "hod")
            r.status = "hod_pending"
    elif stage.stage_type == "hod":
        _ensure_live_single_stage(cycle, "incharge_academic_cell")
        r.status = "incharge_academic_cell_pending"
    elif stage.stage_type == "incharge_academic_cell":
        _ensure_live_single_stage(cycle, "dpgs")
        r.status = "dpgs_pending"
    elif stage.stage_type == "dpgs":
        r.status = "approved"
        r.approved_at = now
        cycle.status = "approved"
        cycle.completed_at = now

    await db.commit()
    return {"message": f"{stage.role_label} approved.", "status": r.status}


@router.post("/{report_id}/approval/revert")
async def revert_stage(report_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """Stage-specific revert destination (NOT Synopsis's uniform "always to student"):
    Major Advisor -> Student (ends the cycle); every other stage -> exactly one step back,
    WITHOUT ending the cycle — a fresh round of stage(s) opens with new rows (the reverting
    row itself, and every other superseded live row, are marked and never mutated again)."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    r = await _get_report(report_id, db)
    stage = await _require_my_stage(r, user, db)
    cycle = _active_cycle(r)
    await _lock_and_reload(r, stage, cycle, db)
    now = _now()
    _record_action(stage, user, "reverted", now, remark)

    if stage.stage_type == "major_advisor":
        for other in _live_stages(cycle):
            if other.id != stage.id and other.status == "pending":
                other.status = "cancelled"
        cycle.status = "reverted"
        cycle.reverted_at = now
        cycle.revert_remark = remark
        r.status = "reverted"
        await db.commit()
        return {"message": "Progress Report reverted to the student for correction.", "status": r.status}

    # Partial (in-chain) revert — cancel any other still-pending live stages of THIS type
    # (e.g. other committee members who hadn't yet acted), then open a fresh round.
    for other in _live_stages(cycle):
        if other.id != stage.id and other.stage_type == stage.stage_type and other.status == "pending":
            other.status = "cancelled"
    await db.flush()
    next_seq = (max((s.sequence for s in cycle.stages), default=0)) + 1
    committee = await _committee(r.student_id, db)
    major = next((m for m in committee.members if m.role == "major_advisor"), None) if committee else None
    others = [m for m in committee.members if m.role != "major_advisor"] if committee else []
    if stage.stage_type == "committee_member":
        cycle.stages.append(ProgressReportApprovalStage(
            cycle_id=cycle.id, sequence=next_seq, stage_type="major_advisor", role_label=_advisory_label("major_advisor"),
            committee_member_id=major.id, assignee_id=major.faculty_id,
        ))
    elif stage.stage_type == "hod":
        seq = next_seq
        for m in others:
            cycle.stages.append(ProgressReportApprovalStage(
                cycle_id=cycle.id, sequence=seq, stage_type="committee_member", role_label=_advisory_label(m.role),
                committee_member_id=m.id, assignee_id=m.faculty_id,
            ))
            seq += 1
    elif stage.stage_type == "incharge_academic_cell":
        cycle.stages.append(ProgressReportApprovalStage(cycle_id=cycle.id, sequence=next_seq, stage_type="hod", role_label=_STAGE_ROLE_LABELS["hod"]))
    elif stage.stage_type == "dpgs":
        cycle.stages.append(ProgressReportApprovalStage(cycle_id=cycle.id, sequence=next_seq, stage_type="incharge_academic_cell", role_label=_STAGE_ROLE_LABELS["incharge_academic_cell"]))

    r.status = _REVERT_DESTINATION_STATUS[stage.stage_type]
    await db.commit()
    return {"message": f"Progress Report reverted by {stage.role_label}.", "status": r.status}
