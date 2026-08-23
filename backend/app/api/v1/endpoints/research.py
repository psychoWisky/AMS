"""PG/Research Management (Module 10)."""
from typing import Optional, List
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program
from app.models.research import AdvisoryCommittee, CommitteeMember

router = APIRouter(prefix="/research", tags=["Research"])

_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)


class CommitteeIn(BaseModel):
    student_id: UUID
    research_title: Optional[str] = None
    research_area: Optional[str] = None

class MemberIn(BaseModel):
    faculty_id: UUID
    role: str = "member"  # major_advisor / co_major_advisor / member


# ── Authorization helpers ───────────────────────────────────────────────────────
# Mirrors the style of _authorize_offering_management / _resolve_student_scope
# (enrollment.py, courses.py): admin roles unrestricted, HOD via department match,
# assignment-based access for everyone else. A student's department is derived
# through Program, exactly like Course Categorisation's student eligibility.

async def _student_department_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    """Resolve a student's department via User.program_id -> Program.department_id.
    Returns None if unresolvable (fail closed)."""
    student = await db.get(User, student_id)
    if not student or not student.program_id:
        return None
    program = await db.get(Program, student.program_id)
    return program.department_id if program else None


async def _can_manage_committee(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> bool:
    """Who may add/manage members: admins, the HOD of the student's department,
    or the committee's own Major Advisor (a CommitteeMember row with role='major_advisor')."""
    if user.role in _ADMIN_ROLES:
        return True
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(committee.student_id, db)
        return bool(dept_id and user.department_id and dept_id == user.department_id)
    result = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee.id,
        CommitteeMember.faculty_id == user.id,
        CommitteeMember.role == "major_advisor",
    ))
    return result.scalar_one_or_none() is not None


async def _authorize_committee_manage(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> None:
    if await _can_manage_committee(committee, user, db):
        return
    raise HTTPException(403, "Only the committee's Major Advisor or an authorized administrator can manage members.")


async def _authorize_committee_view(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> None:
    """Who may view a specific committee: admins, HOD of the student's department,
    a CommitteeMember (cross-department allowed), or the student themselves."""
    if user.role in _ADMIN_ROLES:
        return
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(committee.student_id, db)
        if dept_id and user.department_id and dept_id == user.department_id:
            return
        raise HTTPException(403, "You can only view committees within your own department.")
    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        result = await db.execute(select(CommitteeMember).where(
            CommitteeMember.committee_id == committee.id, CommitteeMember.faculty_id == user.id,
        ))
        if result.scalar_one_or_none():
            return
        raise HTTPException(403, "You are not a member of this committee.")
    if user.role == UserRole.STUDENT:
        if committee.student_id == user.id:
            return
        raise HTTPException(403, "You can only view your own committee.")
    raise HTTPException(403, "Insufficient permissions.")


def _committee_dict(c: AdvisoryCommittee) -> dict:
    student = c.student
    program = student.program if student else None
    department = program.department if program else None
    return {
        "id": str(c.id),
        "student_id": str(c.student_id),
        "student_name": student.full_name if student else None,
        "student_roll": student.student_roll if student else None,
        "program_name": program.name if program else None,
        "program_level": program.level if program else None,
        "department_name": department.name if department else None,
        "research_title": c.research_title, "research_area": c.research_area,
        "status": c.status, "is_locked": c.is_locked,
        "members": [{
            "id": str(m.id), "faculty_id": str(m.faculty_id),
            "faculty_name": m.faculty.full_name if m.faculty else None,
            "designation": m.faculty.designation if m.faculty else None,
            "department_name": m.faculty.department.name if m.faculty and m.faculty.department else None,
            "role": m.role, "accepted": m.accepted,
        } for m in c.members],
    }


async def _member_stats(faculty_ids: List[UUID], db: AsyncSession) -> dict:
    """Committee counts per faculty, in one aggregate query (no N+1).
    'major_advisor_count' = rows with role == major_advisor; 'member_count' = everything else,
    per the explicitly given Phase A formula (co_major_advisor counts as a member for now)."""
    if not faculty_ids:
        return {}
    result = await db.execute(
        select(CommitteeMember.faculty_id, CommitteeMember.role, func.count())
        .where(CommitteeMember.faculty_id.in_(faculty_ids))
        .group_by(CommitteeMember.faculty_id, CommitteeMember.role)
    )
    stats: dict = {}
    for faculty_id, role, count in result.all():
        entry = stats.setdefault(faculty_id, {"major_advisor_count": 0, "member_count": 0})
        if role == "major_advisor":
            entry["major_advisor_count"] += count
        else:
            entry["member_count"] += count
    return stats


_COMMITTEE_LOAD_OPTIONS = (
    selectinload(AdvisoryCommittee.student).selectinload(User.program).selectinload(Program.department),
    selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty).selectinload(User.department),
)


@router.post("/committees", status_code=201)
async def create_committee(
    body: CommitteeIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(body.student_id, db)
        if not dept_id or not user.department_id or dept_id != user.department_id:
            raise HTTPException(403, "You can only form committees for students in your own department.")
    existing = await db.execute(select(AdvisoryCommittee).where(AdvisoryCommittee.student_id == body.student_id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Committee already exists for this student.")
    c = AdvisoryCommittee(**body.model_dump())
    db.add(c); await db.commit(); await db.refresh(c)
    return {"id": str(c.id), "message": "Committee created."}


@router.get("/committees")
async def list_committees(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    q = select(AdvisoryCommittee).options(*_COMMITTEE_LOAD_OPTIONS)

    if user.role in _ADMIN_ROLES:
        pass  # unrestricted
    elif user.role == UserRole.HOD:
        if not user.department_id:
            return []
        q = (
            q.join(User, AdvisoryCommittee.student_id == User.id)
             .join(Program, User.program_id == Program.id)
             .where(Program.department_id == user.department_id)
        )
    elif user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        q = q.where(AdvisoryCommittee.id.in_(
            select(CommitteeMember.committee_id).where(CommitteeMember.faculty_id == user.id)
        ))
    elif user.role == UserRole.STUDENT:
        q = q.where(AdvisoryCommittee.student_id == user.id)
    else:
        return []  # fail closed for roles with no defined committee visibility

    result = await db.execute(q)
    return [_committee_dict(c) for c in result.scalars().all()]


@router.get("/committees/student/{student_id}")
async def get_student_committee(student_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(AdvisoryCommittee).options(*_COMMITTEE_LOAD_OPTIONS).where(AdvisoryCommittee.student_id == student_id)
    )
    c = result.scalar_one_or_none()
    if not c: raise HTTPException(404, "No committee found.")
    await _authorize_committee_view(c, user, db)

    data = _committee_dict(c)
    stats = await _member_stats([m.faculty_id for m in c.members], db)
    for m_out, m in zip(data["members"], c.members):
        s = stats.get(m.faculty_id, {"major_advisor_count": 0, "member_count": 0})
        m_out["major_advisor_count"] = s["major_advisor_count"]
        m_out["member_count"] = s["member_count"]
    data["can_manage"] = await _can_manage_committee(c, user, db)
    return data


@router.post("/committees/{committee_id}/members", status_code=201)
async def add_member(
    committee_id: UUID, body: MemberIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR,
        UserRole.HOD, UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR,
    )),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_committee_manage(c, user, db)
    if c.is_locked: raise HTTPException(400, "Committee is locked.")

    existing = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee_id, CommitteeMember.faculty_id == body.faculty_id,
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "This faculty member is already on the committee.")

    m = CommitteeMember(committee_id=committee_id, **body.model_dump())
    db.add(m); await db.commit()
    return {"message": "Member added."}


@router.patch("/committees/{committee_id}/members/{member_id}/accept")
async def accept_membership(
    committee_id: UUID, member_id: UUID,
    accepted: bool, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR)),
):
    m = await db.get(CommitteeMember, member_id)
    if not m or m.committee_id != committee_id or m.faculty_id != user.id:
        raise HTTPException(404, "Membership not found.")
    m.accepted = accepted; m.accepted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Acceptance recorded."}


@router.patch("/committees/{committee_id}/lock")
async def lock_committee(
    committee_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    c.is_locked = True; c.status = "locked"; c.formed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Committee locked."}
