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
revision — both remain undefined future AVFU roles under the current
four-role architecture (SUPER_ADMIN/HOD/FACULTY/STUDENT only; ACADEMIC_ADMIN
was a dummy/testing role, removed entirely). No demo substitution is invented
here (BUSINESS_LOGIC.md Open Question 11, STUDENT_SIDE_IMPLEMENTATION_PLAN.md
Section 32 Phase F-5). A committee's workflow terminates at `hod_approved` in
this revision — a documented, honest P0 stopping point, not a silent omission.

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
from app.models.user import User, UserRole, Department
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.synopsis import Synopsis, SynopsisApprovalCycle
from app.models.external_examiner import ExternalExaminerSelection, ExternalExaminerApprovalCycle
from app.models.thesis import Thesis, ThesisApprovalCycle
# Programme<->Department many-to-many redesign — single shared implementation
# in app/core/student_scope.py, re-exported under this file's existing
# private name (also imported from here by ppw.py, unchanged).
from app.core.student_scope import resolve_student_department_id as _student_department_id

router = APIRouter(prefix="/research", tags=["Research"])

_ADMIN_ROLES = (UserRole.SUPER_ADMIN,)
# Incharge Academic Cell / DPGS task (this revision) — global VIEW access
# only (Section 25/26: "view committees across all departments"). Deliberately
# a SEPARATE tuple from `_ADMIN_ROLES`, never used by
# `_authorize_propose_major_advisor`/`_authorize_manage_members`/
# `can_manage_members` — global roles must never gain Major-Advisor/member
# ownership merely because they can see everything (Section 29's explicit
# "do not bypass Major Advisor ownership").
_GLOBAL_VIEW_ROLES = (UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)

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
    # Incharge Academic Cell / DPGS task (this revision) — HOD approval no
    # longer terminates the workflow (Section 23); it continues through the
    # two global roles above HOD. Neither is a document signatory (Section
    # 24) — both are plain approval-state labels, same as HOD's own.
    "incharge_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending",
    "dpgs_approved": "DPGS Approved (Final)",
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


async def _authorize_propose_major_advisor(student_id: UUID, user: User, db: AsyncSession) -> None:
    """Stage 1: only HOD (of the student's department) or admins may propose a
    Major Advisor. This is the ONLY committee-management action HOD may take —
    fixes the confirmed conflict where HOD could previously also add members."""
    if user.active_role in _ADMIN_ROLES:
        return
    if user.active_role == UserRole.HOD:
        dept_id = await _student_department_id(student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
        raise HTTPException(403, "You can only propose a Major Advisor for students in your own department.")
    raise HTTPException(403, "Only HOD (or an administrator) may propose a Major Advisor.")


async def _assert_no_synopsis_under_approval(student_id: UUID, db: AsyncSession, action: str, until: str) -> None:
    """The student's committee (its Major Advisor and members) must not change while their Synopsis has an ACTIVE
    approval cycle: every Synopsis approval stage is bound to a specific committee member (and the Major Advisor),
    so removing or replacing one mid-workflow would strand the approval. "Active" means an in-progress cycle only —
    a draft that was never submitted, a reverted cycle, a completed (approved) Synopsis, or no Synopsis at all does
    not lock anything. Called AFTER the caller's existing authorization, so it only ever adds a restriction; it never
    grants anyone permission and is never reached by an unauthorized caller."""
    active = await db.execute(
        select(SynopsisApprovalCycle.id).join(Synopsis, Synopsis.id == SynopsisApprovalCycle.synopsis_id)
        .where(Synopsis.student_id == student_id, SynopsisApprovalCycle.status == "active").limit(1)
    )
    if active.scalar_one_or_none():
        raise HTTPException(
            409,
            f"Cannot {action} because this student has a Synopsis currently under approval. "
            f"The Synopsis approval workflow must be completed or reverted before {until}.",
        )



async def _assert_no_external_examiner_under_approval(student_id: UUID, db: AsyncSession, action: str, until: str) -> None:
    """Sibling of `_assert_no_synopsis_under_approval`, same mechanism, same reasoning: each External
    Examiner Selection approval stage is bound to the exact accepted Major Advisor `CommitteeMember` row,
    so changing the committee mid-approval would strand it. Only an ACTIVE cycle blocks anything — a
    draft never submitted, a reverted selection, a fully approved one, or no selection at all, never do."""
    active = await db.execute(
        select(ExternalExaminerApprovalCycle.id).join(ExternalExaminerSelection, ExternalExaminerSelection.id == ExternalExaminerApprovalCycle.selection_id)
        .where(ExternalExaminerSelection.student_id == student_id, ExternalExaminerApprovalCycle.status == "active").limit(1)
    )
    if active.scalar_one_or_none():
        raise HTTPException(409, f"Cannot {action} because this student's External Examiner Selection is currently under approval. The approval workflow must be completed or reverted before {until}.")


async def _assert_no_thesis_under_approval(student_id: UUID, db: AsyncSession, action: str, until: str) -> None:
    """Sibling of `_assert_no_synopsis_under_approval`/`_assert_no_external_examiner_under_approval`,
    same mechanism, same reasoning: the Initial Thesis's Major Advisor stage is bound to the exact
    accepted `CommitteeMember` row, so changing the committee mid-approval would strand it."""
    active = await db.execute(
        select(ThesisApprovalCycle.id).join(Thesis, Thesis.id == ThesisApprovalCycle.thesis_id)
        .where(Thesis.student_id == student_id, ThesisApprovalCycle.status == "active").limit(1)
    )
    if active.scalar_one_or_none():
        raise HTTPException(409, f"Cannot {action} because this student's Initial Thesis is currently under approval. The approval workflow must be completed or reverted before {until}.")


async def _get_major_advisor_member(committee: AdvisoryCommittee, db: AsyncSession) -> Optional[CommitteeMember]:
    result = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee.id, CommitteeMember.role == "major_advisor",
    ))
    return result.scalar_one_or_none()


async def _authorize_manage_members(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> None:
    """Stage 2: only the committee's own ACCEPTED Major Advisor (or an admin) may
    add/remove other members. HOD is deliberately NOT included here (Rule 28)."""
    if user.active_role in _ADMIN_ROLES:
        return
    ma = await _get_major_advisor_member(committee, db)
    if ma and ma.faculty_id == user.id and ma.accepted is True:
        return
    raise HTTPException(403, "Only this committee's accepted Major Advisor (or an administrator) can manage its other members.")


async def _authorize_committee_view(committee: AdvisoryCommittee, user: User, db: AsyncSession) -> None:
    if user.active_role in _GLOBAL_VIEW_ROLES:
        return
    if user.active_role == UserRole.HOD:
        dept_id = await _student_department_id(committee.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
        raise HTTPException(403, "You can only view committees within your own department.")
    if user.active_role == UserRole.FACULTY:
        result = await db.execute(select(CommitteeMember).where(
            CommitteeMember.committee_id == committee.id, CommitteeMember.faculty_id == user.id,
        ))
        if result.scalar_one_or_none():
            return
        raise HTTPException(403, "You are not a member of this committee.")
    if user.active_role == UserRole.STUDENT:
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
    # Programme<->Department many-to-many redesign — read directly from the
    # student's own department, not via Program.
    department = student.department if student else None
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
    selectinload(AdvisoryCommittee.student).selectinload(User.program),
    # Programme<->Department many-to-many redesign — student.department is
    # loaded directly (User.department_id), not via student.program.department.
    selectinload(AdvisoryCommittee.student).selectinload(User.department),
    selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty).selectinload(User.department),
)


# ── Stage 1: HOD proposes Major Advisor ─────────────────────────────────────────

@router.post("/committees", status_code=201)
async def create_committee(
    body: CommitteeIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
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
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD)),
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
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_propose_major_advisor(c.student_id, user, db)
    await _assert_no_synopsis_under_approval(c.student_id, db, "change the Major Advisor", "the Major Advisor can be changed")
    await _assert_no_external_examiner_under_approval(c.student_id, db, "change the Major Advisor", "the Major Advisor can be changed")
    await _assert_no_thesis_under_approval(c.student_id, db, "change the Major Advisor", "the Major Advisor can be changed")
    ma = await _get_major_advisor_member(c, db)
    # Incharge Academic Cell / DPGS task (Section 28) — HOD may now also
    # change the Major Advisor when the committee has been returned to
    # `hod_pending` by an Incharge/DPGS revert (distinguished from the
    # NORMAL first-pass arrival at `hod_pending`, via `accept_membership`,
    # by `reverted_at` being set — a revert always sets it, the normal path
    # never does). This is IN ADDITION TO the pre-existing path (Stage 1
    # Major Advisor decline, `status == "reverted"`), never a replacement of it.
    declined_flow = c.status == "reverted" and ma and ma.accepted is False
    returned_from_higher_revert = c.status == "hod_pending" and c.reverted_at is not None
    if not (declined_flow or returned_from_higher_revert):
        raise HTTPException(400, "This committee is not awaiting Major Advisor reassignment.")
    await _check_advisor_capacity(body.major_advisor_id, db)

    await db.delete(ma)
    db.add(CommitteeMember(committee_id=c.id, faculty_id=body.major_advisor_id, role="major_advisor"))
    c.status = "major_advisor_pending"; c.revert_remark = None; c.reverted_at = None
    await db.commit()
    return {"message": "New Major Advisor proposed. Awaiting their response."}


# ── Stage 2: Major Advisor selects other members ────────────────────────────────

@router.get("/committees/{committee_id}/eligible-faculty")
async def list_eligible_faculty(
    committee_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Faculty directory for the Add Member modal, scoped to whoever is
    actually allowed to manage THIS committee's members (admin or this
    committee's own accepted Major Advisor) — reuses `_authorize_manage_members`
    unchanged. Fixes the Major Advisor (role=FACULTY) being unable to see a
    faculty list at all, since the general-purpose `GET /auth/users`
    directory is intentionally admin/HOD-only and is not being widened here."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_manage_members(c, user, db)
    result = await db.execute(
        select(User).options(selectinload(User.department))
        .where(User.role.in_([UserRole.FACULTY, UserRole.HOD]), User.is_active == True)
        .order_by(User.first_name)
    )
    return [{
        "id": str(u.id), "full_name": u.full_name, "role": u.role.value,
        "designation": u.designation, "department_id": str(u.department_id) if u.department_id else None,
    } for u in result.scalars().all()]


@router.post("/committees/{committee_id}/members", status_code=201)
async def add_member(
    committee_id: UUID, body: MemberIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_manage_members(c, user, db)
    await _assert_no_synopsis_under_approval(c.student_id, db, "add a committee member", "the committee can be changed")
    await _assert_no_external_examiner_under_approval(c.student_id, db, "add a committee member", "the committee can be changed")
    await _assert_no_thesis_under_approval(c.student_id, db, "add a committee member", "the committee can be changed")
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
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    """Corrective action after a member decline (Stage 3 revert) — lets the Major
    Advisor remove the declined member and add a replacement via add_member."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_manage_members(c, user, db)
    await _assert_no_synopsis_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
    await _assert_no_external_examiner_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
    await _assert_no_thesis_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
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
    user: User = Depends(require_roles(UserRole.FACULTY)),
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
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_propose_major_advisor(c.student_id, user, db)  # same HOD-department-match rule
    if c.status != "hod_pending":
        raise HTTPException(400, "This committee is not awaiting HOD approval.")

    if body.approved:
        # Incharge Academic Cell / DPGS task — HOD approval no longer
        # terminates the workflow (Section 23); it continues to Incharge
        # Academic Cell. `formed_at` is still set here (unchanged meaning —
        # "the committee, as HOD-approved, was formed at this time").
        c.status = "incharge_pending"; c.formed_at = datetime.now(timezone.utc)
        c.revert_remark = None; c.reverted_at = None
        await db.commit()
        return {"message": "Committee approved by HOD.", "stage": c.status}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    c.status = "members_pending"  # revert to the immediately previous level (Rule 2)
    c.revert_remark = body.remark; c.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Committee reverted to the member stage.", "stage": c.status}


# ── Incharge Academic Cell stage (global — Incharge Academic Cell/DPGS task) ─

@router.patch("/committees/{committee_id}/incharge-approval")
async def incharge_committee_approval(
    committee_id: UUID, body: HodApprovalIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL)),
):
    """Global role — no department match (Section 25). Approval only, never
    a signature (Advisory Committee has no downloadable document at all,
    Section 24) — no separate signing mechanism is introduced."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    if c.status != "incharge_pending":
        raise HTTPException(400, "This committee is not awaiting Incharge Academic Cell approval.")

    if body.approved:
        c.status = "dpgs_pending"
        c.revert_remark = None; c.reverted_at = None
        await db.commit()
        return {"message": "Committee approved by Incharge Academic Cell.", "stage": c.status}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    # Section 27 — Incharge/DPGS revert goes back to HOD, NEVER to the
    # student, and NEVER "one level back" to members_pending — this is a
    # deliberately different revert target from every earlier stage in this
    # workflow (and from Course Registration/PPW's revert-to-student rule).
    c.status = "hod_pending"
    c.revert_remark = body.remark; c.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Committee reverted to HOD.", "stage": c.status}


# ── DPGS stage (global, final approval — Incharge Academic Cell/DPGS task) ───

@router.patch("/committees/{committee_id}/dpgs-approval")
async def dpgs_committee_approval(
    committee_id: UUID, body: HodApprovalIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.DPGS)),
):
    """Final approval — Advisory Committee has no downloadable/signable
    document (Section 24), so unlike Course Registration/PPW's DPGS stage,
    NO signature/approver-identity field is added here; this is a plain
    approval-state transition, same shape as every other committee stage."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    if c.status != "dpgs_pending":
        raise HTTPException(400, "This committee is not awaiting DPGS approval.")

    if body.approved:
        c.status = "dpgs_approved"
        c.revert_remark = None; c.reverted_at = None
        await db.commit()
        return {"message": "Committee given final approval by DPGS.", "stage": c.status}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    c.status = "hod_pending"
    c.revert_remark = body.remark; c.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Committee reverted to HOD.", "stage": c.status}


# ── Listing / detail / lock (largely unchanged) ─────────────────────────────────

@router.get("/committees")
async def list_committees(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    q = select(AdvisoryCommittee).options(*_COMMITTEE_LOAD_OPTIONS)

    if user.active_role in _GLOBAL_VIEW_ROLES:
        pass
    elif user.active_role == UserRole.HOD:
        if not user.active_department_id:
            return []
        # Programme<->Department many-to-many redesign — the student's OWN
        # department_id is authoritative now, never inferred via their
        # Programme's department (a Programme can have many Departments).
        q = (
            q.join(User, AdvisoryCommittee.student_id == User.id)
             .where(User.department_id == user.active_department_id)
        )
    elif user.active_role == UserRole.FACULTY:
        q = q.where(AdvisoryCommittee.id.in_(
            select(CommitteeMember.committee_id).where(CommitteeMember.faculty_id == user.id)
        ))
    elif user.active_role == UserRole.STUDENT:
        q = q.where(AdvisoryCommittee.student_id == user.id)
    else:
        return []

    result = await db.execute(q)
    committees = result.scalars().all()
    data = [_committee_dict(c) for c in committees]

    if user.active_role == UserRole.FACULTY:
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
    data["can_manage_members"] = bool(ma and ma.faculty_id == user.id and ma.accepted is True) or user.active_role in _ADMIN_ROLES
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
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    if c.status != "hod_approved":
        raise HTTPException(400, "Only an HOD-approved committee can be locked.")
    c.is_locked = True
    await db.commit()
    return {"message": "Committee locked."}
