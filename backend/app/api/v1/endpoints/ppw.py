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
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, field_validator

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program
from app.models.course import Course
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.ppw import Ppw, PpwCourse, PPW_CLASSIFICATIONS, PPW_CLASSIFICATION_LABELS, PPW_REQUIRED_CREDITS
# Course-availability task — reuse the SAME visibility rule and student-scope
# resolver as GET /courses, rather than duplicating the ownership-OR-
# availability logic a second time (explicit instruction).
from app.api.v1.endpoints.courses import _resolve_student_scope, _course_visibility_condition

router = APIRouter(prefix="/ppw", tags=["PPW"])

_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)

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


def _require_owner_and_draft(p: Ppw, user: User) -> None:
    if p.student_id != user.id:
        raise HTTPException(404, "PPW not found.")
    if p.status != "draft":
        raise HTTPException(400, "This PPW has been submitted and can no longer be edited.")


async def _committee_rows(student_id: UUID, db: AsyncSession) -> list[dict]:
    """Reads the student's EXISTING AdvisoryCommittee/CommitteeMember rows
    live — no committee data is duplicated into PPW (Section 6 instruction)."""
    result = await db.execute(
        select(AdvisoryCommittee)
        .options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty).selectinload(User.department))
        .where(AdvisoryCommittee.student_id == student_id)
    )
    committee = result.scalar_one_or_none()
    members_by_role = {}
    if committee:
        for m in committee.members:
            members_by_role.setdefault(m.role, m)

    rows = []
    for label, role_value in _ENDORSEMENT_ROWS:
        m = members_by_role.get(role_value)
        rows.append({
            "category": label,
            "faculty_name": m.faculty.full_name if m and m.faculty else None,
            "designation": m.faculty.designation if m and m.faculty else None,
            "department_name": (m.faculty.department.name if m and m.faculty and m.faculty.department else None),
            "signature_status": "pending",  # Phase 1: no signing implemented — always "pending", never fabricated
        })
    return {"committee_found": committee is not None, "rows": rows}


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


@router.get("/me")
async def get_my_ppw(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    result = await db.execute(select(Ppw).options(*_PPW_LOAD_OPTIONS).where(Ppw.student_id == user.id))
    p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "No PPW found. Create one first.")
    data = _ppw_dict(p)
    data["committee"] = await _committee_rows(user.id, db)
    data["signatures"] = {"head": "pending", "dpgs": "pending"}  # Phase 1: static, no signing implemented (Section 7)
    return data


@router.get("/{ppw_id}")
async def get_ppw(ppw_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    p = await _get_owned_ppw(ppw_id, user, db)
    data = _ppw_dict(p)
    data["committee"] = await _committee_rows(p.student_id, db)
    data["signatures"] = {"head": "pending", "dpgs": "pending"}
    return data


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
    — selected-vs-required credits are informational only in Phase 1 (see
    `remaining_credits` in _ppw_dict). Flagged in the implementation report as
    an open business-rule question, not decided here."""
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

    p.status = "submitted"
    p.submitted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "PPW submitted.", "status": p.status, "submitted_at": p.submitted_at.isoformat()}
