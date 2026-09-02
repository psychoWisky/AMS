"""PG/Research Management (Module 10).

Advisory Committee formation workflow (BUSINESS_LOGIC.md D.1/M.5-M.7,
STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 32 Phase F-1):

    HOD proposes Major Advisor -> Major Advisor accepts -> Major Advisor selects
    other members -> members individually accept (mandatory remark on decline) ->
    HOD approves -> [I/C Academic Cell -> DPGS: NOT implemented, see below]

RBAC NOTE (fixes a confirmed conflict — BUSINESS_LOGIC.md D.1 Repository Conflict,
Rule 28): HOD may ONLY propose the Major Advisor (create_committee). Only the
committee's own accepted Major Advisor may add/remove other members. HOD is
deliberately excluded from `_can_manage_members` — the prior implementation let
HOD do both steps, which the confirmed business rule does not permit.

SCOPE NOTE: I/C Academic Cell and DPGS stages are NOT implemented in this
revision. Orientation's Incharge-Academic-Cell demo substitution
(SUPER_ADMIN/ACADEMIC_ADMIN) is specific to that module's documented mitigation;
no equivalent demo mitigation is documented anywhere for Advisory Committee's
Incharge/DPGS stages, and none is invented here (BUSINESS_LOGIC.md Open Question
11, STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 32 Phase F-5). A committee's
workflow terminates at `hod_approved` in this revision — a documented, honest
P0 stopping point, not a silent omission.

CAPACITY ASSUMPTION (BUSINESS_LOGIC.md Rule 30, Open Question 38 — "passes out"
has no confirmed system trigger): this implementation treats a committee's
`status` value `reverted`/`dissolved` as excluded from an advisor's active
advisee count, and otherwise counts every non-reverted committee where the
student is `is_active`. This is an explicit, documented assumption, not a
confirmed business rule — record any correction to it in BUSINESS_LOGIC.md's
Open Questions, not silently in code.
"""
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
from app.models.user import User, UserRole, Program, Department
from app.models.research import AdvisoryCommittee, CommitteeMember

router = APIRouter(prefix="/research", tags=["Research"])

_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)

# The 5 confirmed PG/PhD Research Committee member types (BUSINESS_LOGIC.md M.5,
# Rule 29). "major_advisor" is set only by create_committee/reassign — never a
# valid value for the member-add endpoint below.
_MEMBER_ROLES = ("member_major", "member_minor", "supporting", "member_of_others")

_DEFAULT_ADVISOR_CAPACITY = 10
_BIOSTATISTICS_ADVISOR_CAPACITY = 15  # Rule 30 — department-specific exception

# BUSINESS_LOGIC.md C.8 status vocabulary, mapped from the internal stage code.
_STAGE_LABELS = {
    "major_advisor_pending": "Major Advisor Approval Pending",
    "member_selection": "Member Selection Pending",
    "members_pending": "Member Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "hod_approved": "HOD Approved",
    "reverted": "Reverted",
}


class CommitteeIn(BaseModel):
    student_id: UUID
    major_advisor_id: UUID
    research_title: Optional[str] = None
    research_area: Optional[str] = None

class MemberIn(BaseModel):
    faculty_id: UUID
    role: str  # one of _MEMBER_ROLES

class MajorAdvisorResponse(BaseModel):
    accepted: bool
    remark: Optional[str] = None

class HodApprovalIn(BaseModel):
    approved: bool
    remark: Optional[str] = None

class ReassignMajorAdvisorIn(BaseModel):
    major_advisor_id: UUID


# ── Authorization helpers ───────────────────────────────────────────────────────
# Mirrors the style of _authorize_offering_management / _resolve_student_scope
# (enrollment.py, courses.py): admin roles unrestricted, HOD via department match,
# assignment-based access for everyone else.

async def _student_department_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    student = await db.get(User, student_id)
    if not student or not student.program_id:
        return None
    program = await db.get(Program, student.program_id)
    return program.department_id if program else None


async def _authorize_propose_major_advisor(student_id: UUID, user: User, db: AsyncSession) -> None:
    """Stage 1: only HOD (of the student's department) or admins may propose a
    Major Advisor. This is the ONLY committee-management action HOD may take —
    fixes the confirmed conflict where HOD could previously also add members."""
    if user.role in _ADMIN_ROLES:
        return
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(student_id, db)
        if dept_id and user.department_id and dept_id == user.department_id:
            return
        raise HTTPException(403, "You can only propose a Major Advisor for students in your own department.")
    raise HTTPException(403, "Only HOD (or an administrator) may propose a Major Advisor.")


async def _get_major_advisor_member(committee: AdvisoryCommittee, db: AsyncSession) -> Optional[CommitteeMember]:
    result = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee.id, CommitteeMember.role == "major_advisor",
    ))
    return result.scalar_one_or_none()


async def _authorize_manage_members(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> None:
    """Stage 2: only the committee's own ACCEPTED Major Advisor (or an admin) may
    add/remove other members. HOD is deliberately NOT included here (Rule 28)."""
    if user.role in _ADMIN_ROLES:
        return
    ma = await _get_major_advisor_member(committee, db)
    if ma and ma.faculty_id == user.id and ma.accepted is True:
        return
    raise HTTPException(403, "Only this committee's accepted Major Advisor (or an administrator) can manage its other members.")


async def _authorize_committee_view(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> None:
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


async def _advisor_capacity(faculty_id: UUID, db: AsyncSession) -> int:
    faculty = await db.get(User, faculty_id)
    if faculty and faculty.department_id:
        dept = await db.get(Department, faculty.department_id)
        if dept and "biostat" in dept.name.lower():
            return _BIOSTATISTICS_ADVISOR_CAPACITY
    return _DEFAULT_ADVISOR_CAPACITY


async def _current_advisee_count(faculty_id: UUID, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(CommitteeMember)
        .join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .join(User, User.id == AdvisoryCommittee.student_id)
        .where(
            CommitteeMember.role == "major_advisor",
            CommitteeMember.faculty_id == faculty_id,
            AdvisoryCommittee.status.notin_(["reverted", "dissolved"]),
            User.is_active == True,
        )
    )
    return result.scalar() or 0


async def _check_advisor_capacity(faculty_id: UUID, db: AsyncSession) -> None:
    capacity = await _advisor_capacity(faculty_id, db)
    current = await _current_advisee_count(faculty_id, db)
    if current >= capacity:
        raise HTTPException(400, f"This faculty member already advises {current} students, at their capacity of {capacity}.")


def _committee_dict(c: AdvisoryCommittee) -> dict:
    student = c.student
    program = student.program if student else None
    department = program.department if program else None
    return {
        "id": str(c.id),
        "student_id": str(c.student_id),
        "student_name": student.full_name if student else None,
        "student_roll": student.student_roll if student else None,
        "student_session": (str(student.admission_year) if student and student.admission_year else None),
        "program_name": program.name if program else None,
        "program_level": program.level if program else None,
        "department_name": department.name if department else None,
        "college_name": department.stream if department else None,  # BUSINESS_LOGIC.md Open Question 28 — "College" not confirmed mapped; department.stream used as the closest existing analogue, flagged not asserted equivalent
        "research_title": c.research_title, "research_area": c.research_area,
        "stage": c.status,
        "status_label": _STAGE_LABELS.get(c.status, c.status),
        "is_locked": c.is_locked,
        "revert_remark": c.revert_remark,
        "reverted_at": c.reverted_at.isoformat() if c.reverted_at else None,
        "members": [{
            "id": str(m.id), "faculty_id": str(m.faculty_id),
            "faculty_name": m.faculty.full_name if m.faculty else None,
            "designation": m.faculty.designation if m.faculty else None,
            "department_name": m.faculty.department.name if m.faculty and m.faculty.department else None,
            "role": m.role, "accepted": m.accepted, "remark": m.remark,
        } for m in c.members],
    }


async def _member_stats(faculty_ids: List[UUID], db: AsyncSession) -> dict:
    """Committee counts per faculty (unchanged from the prior revision — still
    correct against the new role vocabulary, since it only distinguishes
    role == 'major_advisor' from everything else)."""
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


# ── Stage 1: HOD proposes Major Advisor ─────────────────────────────────────────

@router.post("/committees", status_code=201)
async def create_committee(
    body: CommitteeIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    await _authorize_propose_major_advisor(body.student_id, user, db)
    existing = await db.execute(select(AdvisoryCommittee).where(AdvisoryCommittee.student_id == body.student_id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Committee already exists for this student.")
    await _check_advisor_capacity(body.major_advisor_id, db)

    c = AdvisoryCommittee(
        student_id=body.student_id, research_title=body.research_title,
        research_area=body.research_area, status="major_advisor_pending",
    )
    db.add(c); await db.flush()
    db.add(CommitteeMember(committee_id=c.id, faculty_id=body.major_advisor_id, role="major_advisor"))
    await db.commit(); await db.refresh(c)
    return {"id": str(c.id), "message": "Major Advisor proposed. Awaiting their response."}


# ── Stage 1 response: Major Advisor accepts/declines ────────────────────────────

@router.patch("/committees/{committee_id}/major-advisor-response")
async def major_advisor_response(
    committee_id: UUID, body: MajorAdvisorResponse, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR, UserRole.HOD)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    if c.status != "major_advisor_pending":
        raise HTTPException(400, "This committee is not awaiting a Major Advisor response.")
    ma = await _get_major_advisor_member(c, db)
    if not ma or ma.faculty_id != user.id:
        raise HTTPException(403, "Only the proposed Major Advisor may respond to this proposal.")

    now = datetime.now(timezone.utc)
    if body.accepted:
        ma.accepted = True; ma.accepted_at = now
        c.status = "member_selection"
        await db.commit()
        return {"message": "Major Advisor role accepted. You may now select other committee members."}

    if not body.remark:
        raise HTTPException(400, "A remark is required when declining.")
    ma.accepted = False; ma.accepted_at = now; ma.remark = body.remark
    c.status = "reverted"; c.revert_remark = body.remark; c.reverted_at = now
    await db.commit()
    return {"message": "Major Advisor proposal declined. HOD must propose a different Major Advisor."}


# ── HOD corrective action: reassign after a Stage-1 decline ────────────────────

@router.post("/committees/{committee_id}/reassign-major-advisor")
async def reassign_major_advisor(
    committee_id: UUID, body: ReassignMajorAdvisorIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_propose_major_advisor(c.student_id, user, db)
    ma = await _get_major_advisor_member(c, db)
    if c.status != "reverted" or not ma or ma.accepted is not False:
        raise HTTPException(400, "This committee is not awaiting Major Advisor reassignment.")
    await _check_advisor_capacity(body.major_advisor_id, db)

    await db.delete(ma)
    db.add(CommitteeMember(committee_id=c.id, faculty_id=body.major_advisor_id, role="major_advisor"))
    c.status = "major_advisor_pending"; c.revert_remark = None; c.reverted_at = None
    await db.commit()
    return {"message": "New Major Advisor proposed. Awaiting their response."}


# ── Stage 2: Major Advisor selects other members ────────────────────────────────

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
    await _authorize_manage_members(c, user, db)
    if c.status not in ("member_selection", "members_pending"):
        raise HTTPException(400, "Members can only be added while the committee is in member-selection stage.")
    if body.role not in _MEMBER_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(_MEMBER_ROLES)}.")
    if c.is_locked: raise HTTPException(400, "Committee is locked.")

    existing = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee_id, CommitteeMember.faculty_id == body.faculty_id,
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "This faculty member is already on the committee.")

    m = CommitteeMember(committee_id=committee_id, faculty_id=body.faculty_id, role=body.role)
    db.add(m)
    c.status = "members_pending"
    await db.commit()
    return {"message": "Member added."}


@router.delete("/committees/{committee_id}/members/{member_id}", status_code=204)
async def remove_member(
    committee_id: UUID, member_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR,
        UserRole.HOD, UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR,
    )),
):
    """Corrective action after a member decline (Stage 3 revert) — lets the Major
    Advisor remove the declined member and add a replacement via add_member."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_manage_members(c, user, db)
    m = await db.get(CommitteeMember, member_id)
    if not m or m.committee_id != committee_id or m.role == "major_advisor":
        raise HTTPException(404, "Member not found.")

    await db.delete(m)
    await db.flush()
    remaining = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee_id, CommitteeMember.role != "major_advisor",
    ))
    if remaining.scalars().first():
        c.status = "members_pending"
    else:
        c.status = "member_selection"
    c.revert_remark = None; c.reverted_at = None
    await db.commit()


# ── Stage 3: selected members respond ───────────────────────────────────────────

@router.patch("/committees/{committee_id}/members/{member_id}/accept")
async def accept_membership(
    committee_id: UUID, member_id: UUID,
    accepted: bool, remark: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR)),
):
    m = await db.get(CommitteeMember, member_id)
    if not m or m.committee_id != committee_id or m.faculty_id != user.id:
        raise HTTPException(404, "Membership not found.")
    if m.role == "major_advisor":
        raise HTTPException(400, "Use the Major Advisor response endpoint for this role.")
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c or c.status != "members_pending":
        raise HTTPException(400, "This committee is not awaiting member responses.")

    now = datetime.now(timezone.utc)
    if not accepted and not remark:
        raise HTTPException(400, "A remark is required when declining.")
    m.accepted = accepted; m.accepted_at = now; m.remark = remark

    if not accepted:
        c.status = "reverted"; c.revert_remark = remark; c.reverted_at = now
        await db.commit()
        return {"message": "Membership declined. The Major Advisor must select a replacement."}

    # Auto-advance once every non-Major-Advisor member has accepted.
    others = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee_id, CommitteeMember.role != "major_advisor",
    ))
    all_members = others.scalars().all()
    if all_members and all(x.accepted is True for x in all_members):
        c.status = "hod_pending"
    await db.commit()
    return {"message": "Response recorded."}


# ── Stage 4: HOD approval ───────────────────────────────────────────────────────

@router.patch("/committees/{committee_id}/hod-approval")
async def hod_approval(
    committee_id: UUID, body: HodApprovalIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_propose_major_advisor(c.student_id, user, db)  # same HOD-department-match rule
    if c.status != "hod_pending":
        raise HTTPException(400, "This committee is not awaiting HOD approval.")

    if body.approved:
        c.status = "hod_approved"; c.formed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"message": "Committee approved by HOD.", "stage": c.status}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    c.status = "members_pending"  # revert to the immediately previous level (Rule 2)
    c.revert_remark = body.remark; c.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Committee reverted to the member stage.", "stage": c.status}


# ── Listing / detail / lock (largely unchanged) ─────────────────────────────────

@router.get("/committees")
async def list_committees(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    q = select(AdvisoryCommittee).options(*_COMMITTEE_LOAD_OPTIONS)

    if user.role in _ADMIN_ROLES:
        pass
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
        return []

    result = await db.execute(q)
    committees = result.scalars().all()
    data = [_committee_dict(c) for c in committees]

    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        for d, c in zip(data, committees):
            mine = next((m for m in c.members if m.faculty_id == user.id), None)
            d["my_role"] = mine.role if mine else None
    return data


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
    ma = await _get_major_advisor_member(c, db)
    data["can_manage_members"] = bool(ma and ma.faculty_id == user.id and ma.accepted is True) or user.role in _ADMIN_ROLES
    data["can_propose_major_advisor"] = False
    try:
        await _authorize_propose_major_advisor(student_id, user, db)
        data["can_propose_major_advisor"] = True
    except HTTPException:
        pass
    return data


@router.get("/committees/faculty/{faculty_id}/capacity")
async def get_advisor_capacity(faculty_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    capacity = await _advisor_capacity(faculty_id, db)
    current = await _current_advisee_count(faculty_id, db)
    return {"faculty_id": str(faculty_id), "capacity": capacity, "current": current, "available": max(capacity - current, 0)}


@router.patch("/committees/{committee_id}/lock")
async def lock_committee(
    committee_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    if c.status != "hod_approved":
        raise HTTPException(400, "Only an HOD-approved committee can be locked.")
    c.is_locked = True
    await db.commit()
    return {"message": "Committee locked."}
