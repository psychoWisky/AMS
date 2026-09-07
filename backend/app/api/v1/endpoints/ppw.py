"""PPW — Post-Graduate Programme of Work (Phase 1 foundation).

Scope, per this task's explicit instruction: student-owned draft/submit
lifecycle, the 4 required text fields, the 6-classification course plan with
informational credit totals, and a read-only Advisory-Committee/signature
preview. NO approval workflow, NO signing, NO notifications are implemented
here — see the model file's docstring and this task's investigation report
for the full reasoning.

RBAC: student self-only (create/view/edit/submit their own PPW); admin roles
(SUPER_ADMIN/ACADEMIC_ADMIN) get unrestricted read, consistent with every
other module's existing convention. No HOD/Faculty/committee endpoints exist
yet — Phase 1 explicitly excludes the approval chain.
"""
import logging
import random, string
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, field_validator

from app.db.base import get_db
from app.core.config import settings
from app.core.email import send_email
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program
from app.models.course import Course
from app.models.academic import AcademicCalendar, Semester
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.ppw import (
    Ppw, PpwCourse, PPW_CLASSIFICATIONS, PPW_CLASSIFICATION_LABELS, PPW_REQUIRED_CREDITS,
    PpwApprovalCycle, PpwApprovalStage, PpwSignature, PPW_COMMITTEE_STAGE_ROLES,
)
# Course-availability task — reuse the SAME visibility rule and student-scope
# resolver as GET /courses, rather than duplicating the ownership-OR-
# availability logic a second time (explicit instruction).
from app.api.v1.endpoints.courses import _resolve_student_scope, _course_visibility_condition
# Reuse the existing student->department resolver rather than duplicating it a
# third time (research.py, grading.py already each have their own copy of this
# exact one-liner join) — read-only import, research.py is not modified.
from app.api.v1.endpoints.research import _student_department_id

router = APIRouter(prefix="/ppw", tags=["PPW"])
logger = logging.getLogger(__name__)

_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)
_APPROVER_ROLES = (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR, UserRole.HOD)

# Dev/test-only OTP convenience (this task's explicit requirement) — accepted
# ONLY when settings.ENVIRONMENT != "production" (see _is_dev_mode below).
# Never a production bypass: _is_dev_mode() is re-checked at verification
# time, not just when the OTP was issued, so a value cached before a
# production deploy can never grant approval after one.
_DEV_OTP = "987317"


def _is_dev_mode() -> bool:
    return settings.ENVIRONMENT.strip().lower() != "production"


# PPW-level statuses corresponding to which stage_type is currently actionable
# (Section 6). Kept as a plain dict (not a class) — mirrors this file's existing
# preference for simple, inspectable constants over enums.
_PHASE_FOR_STAGE_TYPE = {
    "major_advisor": "major_advisor_pending",
    "committee_member": "committee_pending",
    "hod": "hod_pending",
}

# Fixed Advisory Committee endorsement rows for the PPW document (this task's
# confirmed document structure) -> CommitteeMember.role values. "Co-Major
# Advisor" is intentionally mapped to the LEGACY `co_major_advisor` value
# (BUSINESS_LOGIC.md M.5/Rule 29 confirms only 5 current member types —
# major_advisor, member_major, member_minor, supporting, member_of_others —
# "Co-Major Advisor" is not one of them, but research.py's own model docstring
# notes legacy rows may still carry it). This row will simply show "Not
# assigned" for any committee that (correctly, per current business rules)
# has no such member — never fabricated.
_ENDORSEMENT_ROWS = [
    ("Major Advisor", "major_advisor"),
    ("Co-Major Advisor", "co_major_advisor"),
    ("Member Major", "member_major"),
    ("Member Minor", "member_minor"),
    ("Supporting", "supporting"),
    ("Members from Others", "member_of_others"),
]


class PpwIn(BaseModel):
    """All 4 fields optional at creation — a draft may start empty (Section 1
    lists these as PPW-required, but Section 8 confirms drafts are editable/
    incremental; non-emptiness is enforced only at submit time, see submit_ppw)."""
    field_of_investigation: Optional[str] = None
    minor_field: Optional[str] = None
    supporting_field: Optional[str] = None
    research_title: Optional[str] = None


class PpwCourseIn(BaseModel):
    course_id: UUID
    classification: str

    @field_validator("classification")
    @classmethod
    def _valid_classification(cls, v: str) -> str:
        if v not in PPW_CLASSIFICATIONS:
            raise ValueError(f"classification must be one of: {', '.join(PPW_CLASSIFICATIONS)}")
        return v


_EDITABLE_STATUSES = ("draft", "reverted")


def _require_owner_and_draft(p: Ppw, user: User) -> None:
    """Name kept from Phase 1 (still called with require_draft=True at every
    existing call site) — now also permits the "reverted" status, per this
    task's explicit requirement that a student may edit again after any
    approver reverts their submission."""
    if p.student_id != user.id:
        raise HTTPException(404, "PPW not found.")
    if p.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This PPW is under approval and can no longer be edited.")


_CYCLE_LOAD_OPTIONS = (
    selectinload(PpwApprovalCycle.stages).selectinload(PpwApprovalStage.committee_member).selectinload(CommitteeMember.faculty).selectinload(User.department),
    selectinload(PpwApprovalCycle.stages).selectinload(PpwApprovalStage.approver).selectinload(User.department),
)


async def _latest_cycle(ppw_id: UUID, db: AsyncSession) -> Optional[PpwApprovalCycle]:
    """The most recent cycle (active, or the last historical one if none is
    active) — used purely for DISPLAY. Action endpoints use `_get_active_cycle`
    instead, which requires status=='active' and 404s otherwise."""
    result = await db.execute(
        select(PpwApprovalCycle).options(*_CYCLE_LOAD_OPTIONS)
        .where(PpwApprovalCycle.ppw_id == ppw_id)
        .order_by(PpwApprovalCycle.cycle_number.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _get_active_cycle(ppw_id: UUID, db: AsyncSession) -> PpwApprovalCycle:
    result = await db.execute(
        select(PpwApprovalCycle).options(*_CYCLE_LOAD_OPTIONS)
        .where(PpwApprovalCycle.ppw_id == ppw_id, PpwApprovalCycle.status == "active")
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(400, "This PPW has no active approval cycle.")
    return c


async def _resolve_my_stage(p: Ppw, cycle: PpwApprovalCycle, user: User, db: AsyncSession) -> PpwApprovalStage:
    """The SOLE authorization check for every approval action (Section 12) —
    the caller's own currently-eligible stage (status=='pending' AND matching
    the PPW's current phase) within the given ACTIVE cycle. Never a bare role
    match: Major Advisor/committee-member stages require
    CommitteeMember.faculty_id == the caller's own id; the HOD stage requires
    a LIVE department match, re-verified on every call (never trusting
    approver_id, which is only a seed-time hint)."""
    for stage in cycle.stages:
        if stage.status != "pending":
            continue
        if _PHASE_FOR_STAGE_TYPE.get(stage.stage_type) != p.status:
            continue
        if stage.stage_type in ("major_advisor", "committee_member"):
            if stage.committee_member and stage.committee_member.faculty_id == user.id:
                return stage
        elif stage.stage_type == "hod":
            if user.role == UserRole.HOD:
                dept_id = await _student_department_id(p.student_id, db)
                if dept_id and user.department_id and dept_id == user.department_id:
                    return stage
    raise HTTPException(403, "You have no pending PPW approval action.")


async def _authorize_ppw_view(p: Ppw, user: User, db: AsyncSession) -> None:
    """Who may READ a PPW: admins (unrestricted, existing convention); the
    owning student; any faculty/research-supervisor who is the exact
    CommitteeMember behind ANY approval stage ever created for this PPW
    (so past-cycle approvers retain read access to what they signed); the
    student's current-department HOD. Mirrors research.py's
    `_authorize_committee_view` join-based style — never a bare role check."""
    if user.role in _ADMIN_ROLES:
        return
    if user.role == UserRole.STUDENT:
        if p.student_id == user.id:
            return
        raise HTTPException(404, "PPW not found.")
    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        result = await db.execute(
            select(PpwApprovalStage.id)
            .join(PpwApprovalCycle, PpwApprovalCycle.id == PpwApprovalStage.cycle_id)
            .join(CommitteeMember, CommitteeMember.id == PpwApprovalStage.committee_member_id)
            .where(PpwApprovalCycle.ppw_id == p.id, CommitteeMember.faculty_id == user.id)
            .limit(1)
        )
        if result.scalar_one_or_none():
            return
        raise HTTPException(404, "PPW not found.")
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(p.student_id, db)
        if dept_id and user.department_id and dept_id == user.department_id:
            return
        raise HTTPException(404, "PPW not found.")
    raise HTTPException(403, "Insufficient permissions.")


async def _committee_rows(p: Ppw, cycle: Optional[PpwApprovalCycle], db: AsyncSession) -> dict:
    """Reads the student's EXISTING AdvisoryCommittee/CommitteeMember rows
    live — no committee data is duplicated into PPW (Phase 1 instruction,
    unchanged). Overlays real PpwApprovalStage status from the most recent
    cycle (Phase 2) instead of Phase 1's hardcoded "pending" placeholder.
    Co-Major Advisor always reports "not_applicable" — that role is
    explicitly not implemented this phase, even if a legacy committee row
    exists (Section 10)."""
    result = await db.execute(
        select(AdvisoryCommittee)
        .options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty).selectinload(User.department))
        .where(AdvisoryCommittee.student_id == p.student_id)
    )
    committee = result.scalar_one_or_none()
    members_by_role = {}
    if committee:
        for m in committee.members:
            members_by_role.setdefault(m.role, m)

    stage_by_committee_member_id = {}
    if cycle:
        for s in cycle.stages:
            if s.committee_member_id:
                stage_by_committee_member_id[s.committee_member_id] = s

    rows = []
    for label, role_value in _ENDORSEMENT_ROWS:
        m = members_by_role.get(role_value)
        if role_value == "co_major_advisor":
            sig_status, signed_at, is_current, remark = "not_applicable", None, False, None
        else:
            stage = stage_by_committee_member_id.get(m.id) if m else None
            if stage:
                sig_status = stage.status
                signed_at = stage.signed_at.isoformat() if stage.signed_at else None
                is_current = stage.status == "pending" and _PHASE_FOR_STAGE_TYPE.get(stage.stage_type) == p.status
                remark = stage.remark
            elif m:
                sig_status, signed_at, is_current, remark = "not_submitted", None, False, None
            else:
                sig_status, signed_at, is_current, remark = "not_applicable", None, False, None
        rows.append({
            "category": label,
            "faculty_name": m.faculty.full_name if m and m.faculty else None,
            "designation": m.faculty.designation if m and m.faculty else None,
            "department_name": (m.faculty.department.name if m and m.faculty and m.faculty.department else None),
            "signature_status": sig_status,
            "signed_at": signed_at,
            "is_current_stage": is_current,
            "remark": remark,
        })
    return {
        "committee_found": committee is not None,
        "cycle_number": cycle.cycle_number if cycle else None,
        "cycle_status": cycle.status if cycle else None,
        "revert_remark": cycle.revert_remark if cycle else None,
        "reverted_at": cycle.reverted_at.isoformat() if cycle and cycle.reverted_at else None,
        "rows": rows,
    }


def _hod_status(p: Ppw, cycle: Optional[PpwApprovalCycle]) -> dict:
    """HOD is not a CommitteeMember, so it is reported separately from the
    committee `rows` above (Section 10 — "HOD status dynamically... no longer
    pretend HOD is static")."""
    stage = next((s for s in cycle.stages if s.stage_type == "hod"), None) if cycle else None
    if not stage:
        return {"status": "not_submitted", "approver_name": None, "department_name": None, "signed_at": None, "is_current_stage": False, "remark": None}
    return {
        "status": stage.status,
        "approver_name": stage.approver.full_name if stage.approver else None,
        "department_name": stage.approver.department.name if stage.approver and stage.approver.department else None,
        "signed_at": stage.signed_at.isoformat() if stage.signed_at else None,
        "is_current_stage": stage.status == "pending" and _PHASE_FOR_STAGE_TYPE.get(stage.stage_type) == p.status,
        "remark": stage.remark,
    }


def _student_header(student: User) -> dict:
    program = student.program if student else None
    department = program.department if program else None
    return {
        "student_name": student.full_name,
        "student_roll": student.student_roll,
        "program_name": program.name if program else None,
        "program_level": program.level if program else None,
        "department_name": department.name if department else None,
        # BUSINESS_LOGIC.md Open Question 28 precedent (research.py/courses.py) —
        # "College" has no confirmed data-model mapping; department.stream is
        # reused here as the same existing best-analogue, not asserted equivalent.
        "college_name": department.stream if department else None,
        "admission_year": student.admission_year,
    }


def _ppw_dict(p: Ppw) -> dict:
    by_class: dict[str, list[dict]] = {c: [] for c in PPW_CLASSIFICATIONS}
    for pc in sorted(p.courses, key=lambda x: (x.classification, x.sl_no)):
        by_class.setdefault(pc.classification, []).append({
            "id": str(pc.id),
            "sl_no": pc.sl_no,
            "course_id": str(pc.course_id),
            "course_number": pc.course.course_number if pc.course else None,
            "course_title": pc.course.title if pc.course else None,
            "credit_structure": pc.course.credit_structure if pc.course else None,
            "credits": pc.course.total_credits if pc.course else 0,
            "department_name": pc.course.department.name if pc.course and pc.course.department else None,
        })
    classification_summary = []
    for c in PPW_CLASSIFICATIONS:
        selected = sum(item["credits"] for item in by_class.get(c, []))
        required = PPW_REQUIRED_CREDITS[c]
        classification_summary.append({
            "classification": c,
            "label": PPW_CLASSIFICATION_LABELS[c],
            "required_credits": required,
            "selected_credits": selected,
            "remaining_credits": required - selected,  # informational only — see submit_ppw docstring
            "courses": by_class.get(c, []),
        })

    return {
        "id": str(p.id),
        "student_id": str(p.student_id),
        "status": p.status,
        "field_of_investigation": p.field_of_investigation,
        "minor_field": p.minor_field,
        "supporting_field": p.supporting_field,
        "research_title": p.research_title,
        "submitted_at": p.submitted_at.isoformat() if p.submitted_at else None,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
        "classifications": classification_summary,
        "header": _student_header(p.student),
    }


_PPW_LOAD_OPTIONS = (
    selectinload(Ppw.student).selectinload(User.program).selectinload(Program.department),
    selectinload(Ppw.courses).selectinload(PpwCourse.course).selectinload(Course.department),
)


async def _get_owned_ppw(ppw_id: UUID, user: User, db: AsyncSession, require_draft: bool = False) -> Ppw:
    result = await db.execute(select(Ppw).options(*_PPW_LOAD_OPTIONS).where(Ppw.id == ppw_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "PPW not found.")
    if user.role in _ADMIN_ROLES:
        return p
    if require_draft:
        _require_owner_and_draft(p, user)  # raises 404 (not owner) or 400 (not draft)
    elif p.student_id != user.id:
        raise HTTPException(404, "PPW not found.")
    return p


@router.get("/available-courses")
async def list_available_courses(
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Course-availability task — SUPERSEDES the Phase 1 "all active courses"
    behavior (previously flagged as an over-broad assumption). Now scoped to
    exactly: courses OWNED by the student's own department, PLUS courses
    explicitly made AVAILABLE to it via CourseAvailability — the identical
    rule GET /courses uses, via the shared `_course_visibility_condition`
    helper (courses.py), so this can never silently drift from it. The
    student's own department is always resolved server-side via
    `_resolve_student_scope`, exactly as GET /courses already does — never
    client-supplied. Classification is still chosen by the student when
    adding a course to a PPW section — Course.category is unrelated (see
    ppw.py model docstring) and is unmodified by this change."""
    scope = await _resolve_student_scope(user, db)
    if not scope:
        return []
    q = (
        select(Course)
        .options(selectinload(Course.department))
        .where(Course.status == "active", _course_visibility_condition(scope["department_id"]))
    )
    result = await db.execute(q.order_by(Course.course_number))
    return [{
        "id": str(c.id), "course_number": c.course_number, "title": c.title,
        "credit_structure": c.credit_structure, "credits": c.total_credits,
        "department_id": str(c.department_id) if c.department_id else None,
        "department_name": c.department.name if c.department else None,
    } for c in result.scalars().all()]


@router.post("", status_code=201)
async def create_ppw(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    existing = await db.execute(select(Ppw).where(Ppw.student_id == user.id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "You already have a PPW.")
    p = Ppw(student_id=user.id)
    db.add(p)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You already have a PPW.")
    await db.refresh(p)
    return {"id": str(p.id), "message": "PPW draft created."}


async def _full_ppw_dict(p: Ppw, db: AsyncSession, viewer: User) -> dict:
    data = _ppw_dict(p)
    cycle = await _latest_cycle(p.id, db)
    data["committee"] = await _committee_rows(p, cycle, db)
    data["hod_approval"] = _hod_status(p, cycle)
    # Section 10: the old static {"head": "pending", "dpgs": "pending"} placeholder
    # no longer pretends HOD is static — "head"/HOD now reflects real state.
    # DPGS remains explicitly not-implemented this phase (not fabricated).
    data["signatures"] = {"head": data["hod_approval"]["status"], "dpgs": "not_implemented"}
    data["is_editable"] = p.status in _EDITABLE_STATUSES and p.student_id == viewer.id
    return data


@router.get("/me")
async def get_my_ppw(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    result = await db.execute(select(Ppw).options(*_PPW_LOAD_OPTIONS).where(Ppw.student_id == user.id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "No PPW found. Create one first.")
    return await _full_ppw_dict(p, db, user)


@router.get("/pending-approvals")
async def list_pending_approvals(
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    """"My Approvals" inbox (Section 9). Faculty/research-supervisor: PPWs
    where the caller is the exact CommitteeMember behind their own currently-
    pending, currently-eligible stage in the ACTIVE cycle. HOD: PPWs in
    hod_pending whose student belongs to the caller's own department — the
    same live department-match rule used everywhere else in this module,
    never a stored value alone. Registered BEFORE the dynamic GET /{ppw_id}
    route below (fixed-path-before-dynamic-path convention, courses.py) so
    "/ppw/pending-approvals" is never swallowed by the {ppw_id}: UUID matcher."""
    rows: list[dict] = []
    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        result = await db.execute(
            select(PpwApprovalStage, PpwApprovalCycle, Ppw)
            .join(PpwApprovalCycle, PpwApprovalCycle.id == PpwApprovalStage.cycle_id)
            .join(Ppw, Ppw.id == PpwApprovalCycle.ppw_id)
            .join(CommitteeMember, CommitteeMember.id == PpwApprovalStage.committee_member_id)
            .options(selectinload(Ppw.student).selectinload(User.program).selectinload(Program.department))
            .where(
                PpwApprovalCycle.status == "active",
                PpwApprovalStage.status == "pending",
                CommitteeMember.faculty_id == user.id,
            )
        )
        for stage, cycle, p in result.all():
            if _PHASE_FOR_STAGE_TYPE.get(stage.stage_type) != p.status:
                continue  # not yet this stage's turn (e.g. a committee-member stage before MA has approved)
            student = p.student
            program = student.program if student else None
            department = program.department if program else None
            rows.append({
                "ppw_id": str(p.id), "student_name": student.full_name if student else None,
                "student_roll": student.student_roll if student else None,
                "student_email": student.email if student else None,
                "research_title": p.research_title, "status": p.status,
                "stage_type": stage.stage_type, "submitted_at": cycle.submitted_at.isoformat(),
                "department_name": department.name if department else None,
                "program_name": program.name if program else None,
            })
    elif user.role == UserRole.HOD:
        if not user.department_id:
            return []
        result = await db.execute(
            select(PpwApprovalStage, PpwApprovalCycle, Ppw)
            .join(PpwApprovalCycle, PpwApprovalCycle.id == PpwApprovalStage.cycle_id)
            .join(Ppw, Ppw.id == PpwApprovalCycle.ppw_id)
            .join(User, User.id == Ppw.student_id)
            .join(Program, Program.id == User.program_id)
            .options(selectinload(Ppw.student).selectinload(User.program).selectinload(Program.department))
            .where(
                PpwApprovalCycle.status == "active",
                PpwApprovalStage.status == "pending",
                PpwApprovalStage.stage_type == "hod",
                Ppw.status == "hod_pending",
                Program.department_id == user.department_id,
            )
        )
        for stage, cycle, p in result.all():
            student = p.student
            program = student.program if student else None
            department = program.department if program else None
            rows.append({
                "ppw_id": str(p.id), "student_name": student.full_name if student else None,
                "student_roll": student.student_roll if student else None,
                "student_email": student.email if student else None,
                "research_title": p.research_title, "status": p.status,
                "stage_type": stage.stage_type, "submitted_at": cycle.submitted_at.isoformat(),
                "department_name": department.name if department else None,
                "program_name": program.name if program else None,
            })
    return rows


@router.get("/{ppw_id}")
async def get_ppw(ppw_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(select(Ppw).options(*_PPW_LOAD_OPTIONS).where(Ppw.id == ppw_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "PPW not found.")
    await _authorize_ppw_view(p, user, db)
    return await _full_ppw_dict(p, db, user)


@router.patch("/{ppw_id}")
async def update_ppw(
    ppw_id: UUID, body: PpwIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    p = await _get_owned_ppw(ppw_id, user, db, require_draft=True)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    await db.commit()
    return {"message": "PPW updated."}


@router.post("/{ppw_id}/courses", status_code=201)
async def add_ppw_course(
    ppw_id: UUID, body: PpwCourseIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    p = await _get_owned_ppw(ppw_id, user, db, require_draft=True)
    course = await db.get(Course, body.course_id)
    if not course:
        raise HTTPException(404, "Course not found.")

    count_result = await db.execute(select(PpwCourse).where(PpwCourse.ppw_id == ppw_id, PpwCourse.classification == body.classification))
    next_sl_no = len(count_result.scalars().all()) + 1

    pc = PpwCourse(ppw_id=ppw_id, course_id=body.course_id, classification=body.classification, sl_no=next_sl_no)
    db.add(pc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This course has already been added to your PPW.")
    return {"id": str(pc.id), "message": "Course added."}


@router.delete("/{ppw_id}/courses/{ppw_course_id}", status_code=204)
async def remove_ppw_course(
    ppw_id: UUID, ppw_course_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    p = await _get_owned_ppw(ppw_id, user, db, require_draft=True)
    pc = await db.get(PpwCourse, ppw_course_id)
    if not pc or pc.ppw_id != ppw_id:
        raise HTTPException(404, "Selected course not found on this PPW.")
    await db.delete(pc)
    await db.commit()


@router.patch("/{ppw_id}/submit")
async def submit_ppw(
    ppw_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """HARD validation (submission-blocking): the 4 required text fields must
    be non-empty — these are explicitly confirmed "required fields" (Section 1).

    NOT hard-validated: exact credit totals. No business rule found stating
    whether a PPW must exactly meet/may exceed its target credits before
    submission (this task's Section 5 explicitly forbids inventing that rule)
    — selected-vs-required credits are informational only (see
    `remaining_credits` in _ppw_dict). Flagged in the implementation report as
    an open business-rule question, not decided here. Course selections
    themselves are otherwise unvalidated beyond Phase 1's existing rules (no
    new course-selection rule is introduced by this phase).

    Phase 2 (this task): also seeds a brand-new PpwApprovalCycle + its stages
    (Major Advisor, one per actually-accepted applicable committee member,
    HOD) and advances Ppw.status to "major_advisor_pending". Works
    identically whether this is a first submission (status=='draft') or a
    resubmission after a revert (status=='reverted') — both produce a new
    cycle_number, and the prior cycle (if any) is left untouched in the
    database for audit (explicit instruction: never delete approval history).
    Everything below is added to the session but not committed until the
    single `db.commit()` at the end, so a failure partway through leaves NO
    partial cycle/stage rows behind (Section 7's atomicity requirement) —
    consistent with this file's existing add-then-commit-once convention."""
    p = await _get_owned_ppw(ppw_id, user, db, require_draft=True)

    missing = [
        label for field, label in [
            (p.field_of_investigation, "Field of investigation"),
            (p.minor_field, "Minor field"),
            (p.supporting_field, "Supporting field"),
            (p.research_title, "Research title"),
        ] if not (field and field.strip())
    ]
    if missing:
        raise HTTPException(400, f"Please complete the following before submitting: {', '.join(missing)}.")

    committee_result = await db.execute(
        select(AdvisoryCommittee)
        .options(selectinload(AdvisoryCommittee.members))
        .where(AdvisoryCommittee.student_id == user.id)
    )
    committee = committee_result.scalar_one_or_none()
    ma = next((m for m in committee.members if m.role == "major_advisor"), None) if committee else None
    # "Major Advisor having accepted" — the same interpretation already
    # established for Course Registration eligibility
    # (core/dependencies.py::require_advisory_committee_established), reused
    # here rather than inventing a second, possibly-inconsistent rule for
    # what "available" means (Section 7.9's explicit safe-rejection instruction).
    if not ma or ma.accepted is not True:
        raise HTTPException(
            400,
            "Your Advisory Committee's Major Advisor must be assigned and must have accepted before you can submit your PPW.",
        )

    applicable_members = [m for m in committee.members if m.role in PPW_COMMITTEE_STAGE_ROLES and m.accepted is True]

    dept_id = await _student_department_id(user.id, db)
    hod_id = None
    if dept_id:
        hod_result = await db.execute(
            select(User.id).where(User.role == UserRole.HOD, User.department_id == dept_id, User.is_active == True).limit(1)
        )
        hod_id = hod_result.scalar_one_or_none()

    last_cycle_result = await db.execute(
        select(func.max(PpwApprovalCycle.cycle_number)).where(PpwApprovalCycle.ppw_id == p.id)
    )
    next_cycle_number = (last_cycle_result.scalar() or 0) + 1

    now = datetime.now(timezone.utc)
    cycle = PpwApprovalCycle(ppw_id=p.id, cycle_number=next_cycle_number, status="active", submitted_at=now)
    db.add(cycle)
    await db.flush()  # need cycle.id for the stages below

    sequence = 1
    db.add(PpwApprovalStage(cycle_id=cycle.id, sequence=sequence, stage_type="major_advisor", committee_member_id=ma.id))
    sequence += 1
    for m in applicable_members:
        db.add(PpwApprovalStage(cycle_id=cycle.id, sequence=sequence, stage_type="committee_member", committee_member_id=m.id))
        sequence += 1
    db.add(PpwApprovalStage(cycle_id=cycle.id, sequence=sequence, stage_type="hod", approver_id=hod_id))

    p.status = "major_advisor_pending"
    p.submitted_at = now
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This PPW already has an active approval cycle.")
    return {"message": "PPW submitted for approval.", "status": p.status, "submitted_at": p.submitted_at.isoformat()}


# ── Phase 2: Advisory Committee approval workflow ───────────────────────────

class ApproveIn(BaseModel):
    otp: str

class RevertIn(BaseModel):
    remark: str


def _send_ppw_otp_email(to: str, otp_code: str) -> None:
    send_email(
        to, "AVFU AMS — PPW Approval OTP",
        f"Your OTP for Programme of Work (PPW) approval is: {otp_code}\n\nThis code is valid for 10 minutes. "
        "If you did not request this, you can safely ignore this email.",
    )


@router.get("/{ppw_id}/approval/otp")
async def request_ppw_approval_otp(
    ppw_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    """Students may never reach this endpoint — require_roles excludes
    UserRole.STUDENT entirely (Section 12's explicit requirement), independent
    of any stage-matching logic below."""
    p = await db.get(Ppw, ppw_id)
    if not p:
        raise HTTPException(404, "PPW not found.")
    cycle = await _get_active_cycle(p.id, db)
    stage = await _resolve_my_stage(p, cycle, user, db)

    otp_code = "".join(random.choices(string.digits, k=6))
    sig = PpwSignature(
        approval_stage_id=stage.id, user_id=user.id, otp_code=otp_code,
        otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(sig)
    await db.commit()
    _send_ppw_otp_email(user.email, otp_code)

    response = {"message": "OTP sent to your email."}
    if _is_dev_mode():
        # Dev/test convenience ONLY (Section 5) — never included when
        # settings.ENVIRONMENT == "production". The real, emailed `otp_code`
        # above remains valid too; this is an ADDITIONAL accepted value, not a
        # replacement, and is re-checked at verification time, not just here.
        response["dev_otp"] = _DEV_OTP
    return response


async def _verify_and_consume_otp(stage: PpwApprovalStage, user: User, otp: str, request: Request, db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    sig_result = await db.execute(
        select(PpwSignature).where(
            PpwSignature.approval_stage_id == stage.id,
            PpwSignature.user_id == user.id,
            PpwSignature.otp_used == False,
            PpwSignature.otp_expires_at > now,
        ).order_by(PpwSignature.created_at.desc()).limit(1)
    )
    sig = sig_result.scalar_one_or_none()
    if not sig:
        raise HTTPException(400, "No valid OTP found. Please request a new OTP.")
    # The dev bypass is re-checked live (settings can change between OTP
    # issuance and verification) and still requires an unexpired, unused,
    # correctly-scoped PpwSignature row to exist — it is not a standalone
    # bypass that works without ever requesting an OTP (Section 5's explicit
    # single-use/expiry/per-stage/per-user requirements still apply).
    if not (otp == sig.otp_code or (_is_dev_mode() and otp == _DEV_OTP)):
        raise HTTPException(400, "Invalid OTP.")
    sig.otp_used = True
    sig.verified_at = now
    sig.ip_address = request.client.host if request.client else None


@router.post("/{ppw_id}/approval/approve")
async def approve_ppw_stage(
    ppw_id: UUID, body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    p = await db.get(Ppw, ppw_id)
    if not p:
        raise HTTPException(404, "PPW not found.")
    cycle = await _get_active_cycle(p.id, db)
    stage = await _resolve_my_stage(p, cycle, user, db)

    await _verify_and_consume_otp(stage, user, body.otp, request, db)

    now = datetime.now(timezone.utc)
    stage.status = "approved"
    stage.approver_id = user.id
    stage.signed_at = now
    await db.flush()

    # Lock the cycle row for the remainder of this transaction so a
    # concurrently-committing sibling approval (e.g. two committee members
    # finishing at nearly the same instant) cannot both observe "not all
    # committee stages done yet" and neither triggers the hod_pending
    # transition (Section 13's explicit concurrency requirement). Re-read
    # stage statuses AFTER acquiring the lock, not from the possibly-stale
    # `cycle` object loaded before this request began.
    await db.execute(select(PpwApprovalCycle.id).where(PpwApprovalCycle.id == cycle.id).with_for_update())
    fresh_result = await db.execute(select(PpwApprovalStage).where(PpwApprovalStage.cycle_id == cycle.id))
    fresh_stages = fresh_result.scalars().all()

    if stage.stage_type == "major_advisor":
        committee_stages = [s for s in fresh_stages if s.stage_type == "committee_member"]
        p.status = "hod_pending" if not committee_stages else "committee_pending"
    elif stage.stage_type == "committee_member":
        committee_stages = [s for s in fresh_stages if s.stage_type == "committee_member"]
        if all(s.status == "approved" for s in committee_stages):
            p.status = "hod_pending"
    elif stage.stage_type == "hod":
        p.status = "hod_approved"
        cycle_row = await db.get(PpwApprovalCycle, cycle.id)
        cycle_row.status = "approved"
        cycle_row.completed_at = now

    await db.commit()
    return {"message": f"{stage.stage_type.replace('_', ' ').title()} stage approved.", "ppw_status": p.status}


@router.post("/{ppw_id}/approval/revert")
async def revert_ppw_stage(
    ppw_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    if not body.remark or not body.remark.strip():
        raise HTTPException(400, "A remark is required when reverting.")

    p = await db.get(Ppw, ppw_id)
    if not p:
        raise HTTPException(404, "PPW not found.")
    cycle = await _get_active_cycle(p.id, db)
    stage = await _resolve_my_stage(p, cycle, user, db)

    now = datetime.now(timezone.utc)
    # Lock the cycle for the same reason as approve_ppw_stage — prevents a
    # revert racing a concurrent approval on a sibling stage.
    await db.execute(select(PpwApprovalCycle.id).where(PpwApprovalCycle.id == cycle.id).with_for_update())

    stage.status = "reverted"
    stage.remark = body.remark
    stage.signed_at = now

    cycle_row = await db.get(PpwApprovalCycle, cycle.id)
    cycle_row.status = "reverted"
    cycle_row.reverted_at = now
    cycle_row.revert_remark = body.remark

    p.status = "reverted"
    await db.commit()
    return {"message": "PPW reverted to the student for correction.", "ppw_status": p.status}


# ── Phase 3: Official PPW Document / PDF generation ─────────────────────────
# AMS-owned Jinja2 + Playwright/Chromium pipeline (app/utils/pdf.py). No eFMS
# code is imported anywhere in this section — eFMS's own html_pdf.py/
# doc_convert.py are a separate project's files and are not referenced.
#
# Deliberately reuses _full_ppw_dict/_committee_rows/_hod_status as-is rather
# than re-querying/re-deriving approval state a second time — the document
# must always show exactly what GET /ppw/{id} already shows (same latest
# cycle, same signature statuses), never a second, potentially-diverging
# computation.

# Official document classification letters (this task's confirmed A-F
# mapping) — presentation-only, deliberately NOT written back onto
# PpwCourse.classification (which stays the plain word values it already
# used before this phase; see PPW_CLASSIFICATIONS in models/ppw.py).
_DOCUMENT_CLASSIFICATION_LETTERS = {
    "major": "A", "minor": "B", "supporting": "C",
    "research": "D", "seminar": "E", "compulsory": "F",
}

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
_jinja_env = None  # lazily constructed — see _render_document_html


def _render_document_html(context: dict) -> str:
    """Renders the PPW document template. Jinja2 environment is created
    lazily and cached at module level (import cost only, not per-request)."""
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )
    template = _jinja_env.get_template("ppw_document.html")
    return template.render(**context)


async def _current_academic_context(db: AsyncSession) -> dict:
    """Academic year/semester for the official document header.

    AMS has no per-student semester/academic-year assignment anywhere in the
    schema (no FK from User/Ppw to AcademicCalendar or Semester) — the only
    authoritative academic-year data in AMS is the admin-managed
    AcademicCalendar/Semester tables (academic_calendar.py), each with a
    `status` field the admin sets via PATCH .../status. This reads whichever
    calendar/semester the admin has marked "active" — never derived from
    today's date, never hardcoded, and never a per-student value (none
    exists to read).

    AMS's existing data was found (during this phase's testing) to allow
    MULTIPLE AcademicCalendar rows to be simultaneously "active" — nothing in
    academic_calendar.py enforces "at most one active calendar" today. Per
    explicit product decision for this phase: when more than one is active,
    the most recently STARTING one (max start_date) is treated as current —
    deterministic, but does not fix the underlying data-quality gap (multiple
    active calendars remain possible and are not validated against here).
    Same tie-break applied to Semester within the chosen calendar. If no
    calendar is currently marked active, both fields are None and the
    document renders "-" rather than fabricating a value.
    """
    cal_result = await db.execute(
        select(AcademicCalendar).where(AcademicCalendar.status == "active").order_by(AcademicCalendar.start_date.desc()).limit(1)
    )
    calendar = cal_result.scalar_one_or_none()
    semester_name = None
    if calendar:
        sem_result = await db.execute(
            select(Semester).where(Semester.calendar_id == calendar.id, Semester.status == "active")
            .order_by(Semester.start_date.desc()).limit(1)
        )
        sem = sem_result.scalar_one_or_none()
        semester_name = sem.name if sem else None
    return {"academic_year": calendar.academic_year if calendar else None, "semester_name": semester_name}


def _pretty_timestamp(iso_value: str | None) -> str | None:
    if not iso_value:
        return None
    try:
        return datetime.fromisoformat(iso_value).strftime("%d-%b-%Y %H:%M")
    except ValueError:
        return iso_value


async def _build_document_context(p: Ppw, db: AsyncSession, viewer: User) -> dict:
    data = await _full_ppw_dict(p, db, viewer)
    for c in data["classifications"]:
        c["letter"] = _DOCUMENT_CLASSIFICATION_LETTERS[c["classification"]]
    for row in data["committee"]["rows"]:
        row["signed_at"] = _pretty_timestamp(row["signed_at"])
    data["hod_approval"]["signed_at"] = _pretty_timestamp(data["hod_approval"]["signed_at"])
    academic = await _current_academic_context(db)

    from app.utils.pdf import get_logo_data_uri
    return {
        "ppw": data,
        "academic_year": academic["academic_year"],
        "semester_name": academic["semester_name"],
        "logo_data_uri": get_logo_data_uri(),
        "generated_at": datetime.now(timezone.utc),
    }


@router.get("/{ppw_id}/document")
async def get_ppw_document(
    ppw_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Official PPW document as a PDF — reuses `_authorize_ppw_view` (the
    same viewer rules as GET /ppw/{id}) and the same latest-cycle approval
    data every other PPW response already exposes. A draft PPW has no
    approval state worth documenting yet, so generation is refused (400)
    until at least one submission has happened."""
    result = await db.execute(select(Ppw).options(*_PPW_LOAD_OPTIONS).where(Ppw.id == ppw_id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "PPW not found.")
    await _authorize_ppw_view(p, user, db)
    if p.status == "draft":
        raise HTTPException(400, "PPW must be submitted before the official document can be generated.")

    context = await _build_document_context(p, db, user)
    html = _render_document_html(context)

    from starlette.concurrency import run_in_threadpool
    from app.utils.pdf import render_ppw_pdf, ChromiumUnavailable, ChromiumRenderFailed
    try:
        pdf_bytes = await run_in_threadpool(render_ppw_pdf, html)
    except ChromiumUnavailable as exc:
        logger.error("PPW document generation unavailable (ppw_id=%s): %s", ppw_id, exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("PPW document render failed (ppw_id=%s): %s", ppw_id, exc)
        raise HTTPException(422, "Unable to generate PPW document.")

    roll_or_id = (p.student.student_roll or str(p.student_id)) if p.student else str(p.student_id)
    safe_roll = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in roll_or_id)
    filename = f"PPW-{safe_roll}-{p.status}.pdf"
    encoded_name = urllib.parse.quote(filename, safe="")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )
