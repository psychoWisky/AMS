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
import logging
import urllib.parse
from pathlib import Path
from typing import Optional, List
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, model_validator

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles, student_user_clause
from app.core.faculty_scope import (
    faculty_has_department, faculty_has_other_department, faculty_department_ids,
    faculty_department_names, list_faculty_with_departments,
)
from app.models.user import User, UserRole, Department
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.synopsis import Synopsis, SynopsisApprovalCycle
from app.models.external_examiner import ExternalExaminerSelection, ExternalExaminerApprovalCycle
from app.models.thesis import Thesis, ThesisApprovalCycle
from app.models.progress_report import ProgressReport, ProgressReportApprovalCycle
# Programme<->Department many-to-many redesign — single shared implementation
# in app/core/student_scope.py, re-exported under this file's existing
# private name (also imported from here by ppw.py, unchanged).
from app.core.student_scope import resolve_student_department_id as _student_department_id

router = APIRouter(prefix="/research", tags=["Research"])
logger = logging.getLogger(__name__)

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
# Rule 29), PLUS `co_major_advisor` — now confirmed OPTIONAL and selectable
# (Advisory Committee department-eligibility task, this revision). "major_advisor"
# itself is set only by create_committee/reassign — never a valid value here.
_MEMBER_ROLES = ("member_major", "member_minor", "supporting", "member_of_others", "co_major_advisor")
# Department-eligibility task (this revision) — confirmed rule per role:
#   member_major: current FACULTY assignment in the STUDENT's OWN department
#   member_minor: current FACULTY assignment in AT LEAST ONE OTHER department
#   supporting:   no department restriction at all
#   member_of_others: external, no department concept (no faculty_id at all)
# "major_advisor" follows the same rule as member_major (BUSINESS_LOGIC.md D.1
# step 1: "Major Advisor must belong to the student's department").
#
# co_major_advisor: CONFIRMED optional; AVFU has NOT confirmed a cardinality
# limit or a department rule for it (open question — see BUSINESS_LOGIC.md).
# Rather than invent either, it is deliberately grouped with `_NO_DEPARTMENT_ROLES`
# (no department restriction, same as Supporting — the least-invented default,
# since restricting it would require guessing which restriction) and no
# cardinality cap is enforced anywhere in this file (same as member_major/
# member_minor/supporting today, all of which already permit any number). If
# AVFU later confirms either constraint, add it here and to `add_member` alone.
_SAME_DEPARTMENT_ROLES = ("major_advisor", "member_major")
_OTHER_DEPARTMENT_ROLES = ("member_minor",)
_NO_DEPARTMENT_ROLES = ("supporting", "co_major_advisor")
_EXTERNAL_ROLES = ("member_of_others",)

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

# Official Advisory Committee document (this revision) — display labels for
# the generated PDF, deliberately mirroring the existing, already-confirmed
# frontend vocabulary (`COMMITTEE_ROLE_LABELS`/`LEVEL_LABELS`,
# frontend/lib/utils.ts) rather than inventing new wording. `LEVEL_LABELS`
# there is itself a confirmed UI-only rename (PG -> "Masters" display text;
# the stored `Program.level` value is never touched) — reused here for the
# same document-facing purpose, never written back to the database.
_COMMITTEE_DOCUMENT_ROLE_LABELS = {
    "major_advisor": "Major Advisor",
    "member_major": "Member Major",
    "member_minor": "Member Minor",
    "supporting": "Supporting",
    "member_of_others": "Member of Others",
    "co_major_advisor": "Co-Major Advisor",
}
_PROGRAM_LEVEL_DOCUMENT_LABELS = {"UG": "UG", "PG": "Masters", "PhD": "PhD"}


class CommitteeIn(BaseModel):
    student_id: UUID
    major_advisor_id: UUID
    research_title: Optional[str] = None
    research_area: Optional[str] = None

class MemberIn(BaseModel):
    role: str  # one of _MEMBER_ROLES
    # Internal member (member_major/member_minor/supporting): faculty_id required,
    # external fields must be omitted. External member (member_of_others): all
    # three external fields required, faculty_id must be omitted — mirrors the
    # DB's `ck_committee_member_internal_xor_external` CHECK constraint exactly,
    # so a malformed request is rejected with a clear 422 before it ever reaches
    # the database. See `_validate_member_shape` for the actual role-aware check
    # (this validator only enforces the unconditional internal/external shape).
    faculty_id: Optional[UUID] = None
    external_name: Optional[str] = None
    external_designation: Optional[str] = None
    external_institute: Optional[str] = None

    @model_validator(mode="after")
    def _internal_xor_external(self) -> "MemberIn":
        external_fields = (self.external_name, self.external_designation, self.external_institute)
        has_external = any(f is not None and f.strip() for f in external_fields)
        has_all_external = all(f is not None and f.strip() for f in external_fields)
        if self.faculty_id is not None and has_external:
            raise ValueError("Provide either faculty_id (internal member) or external name/designation/institute (external member), never both.")
        if has_external and not has_all_external:
            raise ValueError("An external committee member requires name, designation and institute together.")
        if self.faculty_id is None and not has_all_external:
            raise ValueError("Provide faculty_id for an internal committee member, or name/designation/institute for an external one.")
        return self

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


async def _assert_no_progress_report_under_approval(student_id: UUID, db: AsyncSession, action: str, until: str) -> None:
    """Sibling of `_assert_no_synopsis_under_approval`/`_assert_no_external_examiner_under_approval`/
    `_assert_no_thesis_under_approval`, same mechanism, same reasoning: every Progress Report
    approval stage (Major Advisor and each committee member) is bound to the exact
    `CommitteeMember` row, so changing the committee mid-approval would strand it. Note that a
    Progress Report cycle here can survive several PARTIAL reverts (Committee->MA, HOD->Committee,
    etc.) without ever leaving `status == "active"` — the lock applies throughout, exactly as
    intended, since the workflow is still genuinely ongoing in every one of those states."""
    active = await db.execute(
        select(ProgressReportApprovalCycle.id).join(ProgressReport, ProgressReport.id == ProgressReportApprovalCycle.report_id)
        .where(ProgressReport.student_id == student_id, ProgressReportApprovalCycle.status == "active").limit(1)
    )
    if active.scalar_one_or_none():
        raise HTTPException(409, f"Cannot {action} because this student's Progress Report is currently under approval. The approval workflow must be completed or reverted before {until}.")


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


# Post-DPGS-approval committee-change task (this revision) — AVFU confirmed
# an Advisory Committee remains editable even after `dpgs_approved`: the
# Major Advisor may still add/remove/replace a member, and an authorized HOD
# may still replace the Major Advisor, in both cases restarting the HOD ->
# Incharge Academic Cell -> DPGS chain. `dpgs_approved` is therefore now a
# MODIFIABLE status, not a terminal lock — reused here as the new entry
# point into the SAME membership-management/reassignment endpoints that
# already existed, rather than inventing a parallel "revision" workflow.
# `reverted` is included because `remove_member`'s pre-existing, documented
# use case (removing a member who declined at Stage 3) already runs while
# the committee is in that exact status. `hod_approved` is included because
# it is a WIDELY used legacy/fixture status value across this repository's
# own test suite (`test_progress_report.py`, `test_comprehensive_exam.py`,
# `test_synopsis.py`, `test_cleanup_production_db.py` — confirmed by a
# repository-wide search, not assumed) representing "this committee is
# already fully approved," predating the Incharge Academic Cell/DPGS task
# that introduced `dpgs_approved` as the real terminal status; it must be
# treated identically to `dpgs_approved` here so committee modification
# behaves consistently regardless of which "approved" status label a given
# committee happens to carry. `major_advisor_pending`, `incharge_pending`
# and `dpgs_pending` are deliberately EXCLUDED — while a revision is already
# awaiting the new Major Advisor's response, or is with Incharge Academic
# Cell/DPGS for re-review, a second concurrent membership change is blocked
# by this same status gate (see module docstring's concurrency note) rather
# than by a separate version counter.
_MEMBER_MODIFIABLE_STATUSES = ("member_selection", "members_pending", "hod_pending", "dpgs_approved", "hod_approved", "reverted")


def _recompute_committee_member_stage(all_members: list[CommitteeMember]) -> str:
    """Single source of truth for the non-Major-Advisor-member completeness
    check, used by every place that adds/removes a member or records a
    Major Advisor's acceptance (`add_member`, `remove_member`,
    `major_advisor_response`'s accept branch). Three-way, not a simple
    boolean: an EMPTY list is `member_selection` (nothing to accept yet — the
    normal state right after a Major Advisor's own first acceptance, with no
    other members added), a NON-empty list with at least one member whose
    `accepted` is not `True` is `members_pending` (something still needs a
    response), and a NON-empty, fully `accepted is True` list is
    `hod_pending` — ready for the next approval stage regardless of how it
    got there: first-time formation, a post-DPGS-approval member change
    (where only the newly added/remaining members matter), or a replaced
    Major Advisor whose OTHER members were already accepted and are left
    untouched (the confirmed requirement: no member re-acceptance for a
    Major-Advisor-only change)."""
    if not all_members:
        return "member_selection"
    if all(m.accepted is True for m in all_members):
        return "hod_pending"
    return "members_pending"


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


# ── Department eligibility (Advisory Committee roles, NEVER RBAC roles) ─────────
# Committee-member roles (major_advisor, member_major, member_minor, supporting,
# member_of_others) are CommitteeMember.role values only — they are never written
# to UserRole/UserRoleAssignment and never affect a person's system authorization.
# The only RBAC fact consulted here is whether the candidate CURRENTLY holds a
# FACULTY UserRoleAssignment in the relevant department(s) — see
# app.core.faculty_scope, the single source of truth for that question. This is
# authoritative, server-side validation: the frontend's dropdown filtering is a
# convenience only and is never trusted on its own.

_ROLE_LABELS = {"major_advisor": "The Major Advisor", "member_major": "Member Major"}


async def _validate_faculty_department_eligibility(role: str, faculty_id: UUID, student_department_id: Optional[UUID], db: AsyncSession) -> None:
    """Raises 400 if `faculty_id` is not eligible for `role` against the
    student's department, per the confirmed rules:
      major_advisor / member_major -> must hold FACULTY in the student's OWN department
      member_minor                 -> must hold FACULTY in at least one OTHER department
      supporting                   -> no department restriction (any current FACULTY member)
    A faculty member with FACULTY assignments in BOTH the student's department and
    another one is correctly eligible for BOTH member_major/major_advisor AND
    member_minor simultaneously — the two checks are independent, not mutually
    exclusive (confirmed multi-department example)."""
    faculty = await db.get(User, faculty_id)
    if not faculty or not faculty.is_active or faculty.role not in (UserRole.FACULTY, UserRole.HOD):
        raise HTTPException(400, "The selected person is not an active AVFU faculty member.")

    if role in _SAME_DEPARTMENT_ROLES:
        if not await faculty_has_department(faculty_id, student_department_id, db):
            raise HTTPException(400, f"{_ROLE_LABELS.get(role, role)} must currently hold a FACULTY assignment in the student's own department.")
    elif role in _OTHER_DEPARTMENT_ROLES:
        if not await faculty_has_other_department(faculty_id, student_department_id, db):
            raise HTTPException(400, "Member Minor must currently hold a FACULTY assignment in a department other than the student's own.")
    elif role in _NO_DEPARTMENT_ROLES:
        departments = await faculty_department_ids(faculty_id, db)
        if not departments:
            raise HTTPException(400, "The selected person does not currently hold a FACULTY assignment in any department.")
    # _EXTERNAL_ROLES never reach this function — see add_member/create_committee.


async def _advisor_capacity(faculty_id: UUID, db: AsyncSession) -> int:
    """Department-eligibility task — reads the advisor's CURRENT FACULTY
    department memberships (`faculty_scope`), never the legacy `User.department_id`
    scalar, consistent with every other faculty-department decision in this file.
    A multi-department advisor gets the higher (Biostatistics) capacity if EITHER
    of their departments is a Biostatistics one — capacity is a property of the
    person's advising workload, not tied to exactly one of their departments."""
    department_ids = await faculty_department_ids(faculty_id, db)
    if department_ids:
        depts = await db.execute(select(Department.name).where(Department.id.in_(department_ids)))
        if any("biostat" in name.lower() for name in depts.scalars().all()):
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


async def _committee_dict(c: AdvisoryCommittee, db: AsyncSession) -> dict:
    student = c.student
    program = student.program if student else None
    # Programme<->Department many-to-many redesign — read directly from the
    # student's own department, not via Program.
    department = student.department if student else None

    members = []
    for m in c.members:
        is_external = m.faculty_id is None
        if is_external:
            members.append({
                "id": str(m.id), "faculty_id": None, "is_external": True,
                "faculty_name": m.external_name, "designation": m.external_designation,
                "institute": m.external_institute, "department_name": None, "department_names": [],
                "role": m.role, "accepted": m.accepted, "remark": m.remark,
            })
        else:
            # Department-eligibility task — shows EVERY department this member
            # currently holds FACULTY in (multi-department aware), never just the
            # single legacy `User.department_id` `m.faculty.department` would give.
            dept_names = await faculty_department_names(m.faculty_id, db)
            members.append({
                "id": str(m.id), "faculty_id": str(m.faculty_id), "is_external": False,
                "faculty_name": m.faculty.full_name if m.faculty else None,
                "designation": m.faculty.designation if m.faculty else None,
                "institute": None,
                "department_name": dept_names[0] if dept_names else None,
                "department_names": dept_names,
                "role": m.role, "accepted": m.accepted, "remark": m.remark,
            })

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
        "members": members,
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

@router.get("/committees/eligible-students")
async def list_eligible_students(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    """Student directory for the Propose Major Advisor modal (`create_committee`
    below) — the general-purpose `GET /auth/users` directory intentionally
    excludes every student account (`staff_user_clause()`), and that exclusion
    is not weakened here or anywhere else; this is a narrow, purpose-built
    replacement for this one picker, mirroring `list_eligible_faculty`'s same
    reasoning for the Add Member modal.

    Department scope is ALWAYS server-derived from the caller's own session —
    never a request parameter (there is deliberately no `department_id` query
    param on this endpoint at all, so there is nothing for a client to
    override). HOD sees only students in their own active department
    (`user.active_department_id`, resolved from their own active
    `UserRoleAssignment` at login/role-switch — never trusted from the
    request); Super Admin is unrestricted, consistent with every other
    department-scoped endpoint in this file.

    Student department membership is read from `User.department_id` — the
    authoritative field for a STUDENT's own department since the
    Programme<->Department many-to-many redesign (see
    `app/core/student_scope.py`) — never `UserRoleAssignment.department_id`,
    which students do not use, and never the pre-redesign Program-join
    inference."""
    q = select(User).where(student_user_clause(), User.is_active == True)
    if user.active_role == UserRole.HOD:
        if not user.active_department_id:
            return []
        q = q.where(User.department_id == user.active_department_id)
    result = await db.execute(q.order_by(User.first_name))
    return [{
        "id": str(u.id), "full_name": u.full_name,
        "department_id": str(u.department_id) if u.department_id else None,
    } for u in result.scalars().all()]


@router.get("/committees/eligible-major-advisors")
async def list_eligible_major_advisors(
    student_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    """Faculty directory for the Major-Advisor picker — needed in TWO places that
    both happen BEFORE any accepted Major Advisor exists on the committee (so
    `list_eligible_faculty`/`_authorize_manage_members` below, which requires an
    already-ACCEPTED Major Advisor or admin, cannot be reused here):
      1. `create_committee` (Stage 1 propose) — no committee row exists yet at all.
      2. `reassign_major_advisor` (after a decline/higher-stage revert) — a
         committee exists, but by definition has no accepted Major Advisor right
         now, so only its `student_id` is usable, never its `committee_id`.
    Authorization therefore mirrors `_authorize_propose_major_advisor` exactly
    (same HOD-own-department-or-admin rule as both callers above), and eligibility
    reuses the same department-membership source as everywhere else in this file —
    current FACULTY `UserRoleAssignment`s (`app.core.faculty_scope`), never
    `User.department_id`. Multi-department faculty are eligible here whenever the
    student's department is ONE of their current FACULTY departments."""
    await _authorize_propose_major_advisor(student_id, user, db)
    student_department_id = await _student_department_id(student_id, db)
    candidates = await list_faculty_with_departments(db)
    if not student_department_id:
        return []
    return [f for f in candidates if str(student_department_id) in f["department_ids"]]


@router.post("/committees", status_code=201)
async def create_committee(
    body: CommitteeIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    await _authorize_propose_major_advisor(body.student_id, user, db)
    existing = await db.execute(select(AdvisoryCommittee).where(AdvisoryCommittee.student_id == body.student_id))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Committee already exists for this student.")
    student_department_id = await _student_department_id(body.student_id, db)
    await _validate_faculty_department_eligibility("major_advisor", body.major_advisor_id, student_department_id, db)
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
        # Post-DPGS-approval Major Advisor change task (this revision) —
        # reuses the SAME 3-way recompute as add_member/remove_member rather
        # than unconditionally landing on "member_selection": a FIRST-time
        # Major Advisor acceptance has no other members yet (empty list ->
        # still correctly "member_selection", unchanged), but a REPLACEMENT
        # Major Advisor's committee already has its other members intact and
        # accepted — this lets it skip straight to "hod_pending" without
        # asking already-accepted members to re-accept, matching the
        # confirmed requirement (no member re-acceptance for a Major-Advisor-
        # only change). The HOD must still separately, explicitly approve —
        # reaching "hod_pending" here is not itself an approval.
        others = await db.execute(select(CommitteeMember).where(
            CommitteeMember.committee_id == committee_id, CommitteeMember.role != "major_advisor",
        ))
        c.status = _recompute_committee_member_stage(others.scalars().all())
        c.revert_remark = None; c.reverted_at = None
        await db.commit()
        return {"message": "Major Advisor role accepted.", "stage": c.status}

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
    await _assert_no_progress_report_under_approval(c.student_id, db, "change the Major Advisor", "the Major Advisor can be changed")
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
    # Post-DPGS-approval Major Advisor change task (this revision) — the ONLY
    # new trigger added here: an authorized HOD may replace the Major Advisor
    # of an already fully approved committee (`dpgs_approved`, or the legacy
    # `hod_approved` fixture convention — see `_MEMBER_MODIFIABLE_STATUSES`'s
    # comment for why both are treated identically). Setting status to
    # `major_advisor_pending` below (unchanged for every trigger) means the
    # HOD's initiation here does NOT itself count as approval of the revised
    # committee — the new Major Advisor must still accept, and the HOD must
    # still separately, explicitly approve via `hod_approval` afterwards.
    post_approval_change = c.status in ("dpgs_approved", "hod_approved")
    if not (declined_flow or returned_from_higher_revert or post_approval_change):
        raise HTTPException(400, "This committee is not awaiting Major Advisor reassignment.")
    student_department_id = await _student_department_id(c.student_id, db)
    await _validate_faculty_department_eligibility("major_advisor", body.major_advisor_id, student_department_id, db)
    await _check_advisor_capacity(body.major_advisor_id, db)

    await db.delete(ma)
    db.add(CommitteeMember(committee_id=c.id, faculty_id=body.major_advisor_id, role="major_advisor"))
    c.status = "major_advisor_pending"; c.revert_remark = None; c.reverted_at = None
    await db.commit()
    return {"message": "New Major Advisor proposed. Awaiting their response."}


# ── Stage 2: Major Advisor selects other members ────────────────────────────────

@router.get("/committees/{committee_id}/eligible-faculty")
async def list_eligible_faculty(
    committee_id: UUID, role: Optional[str] = None, department_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Faculty directory for the Add Member modal, scoped to whoever is
    actually allowed to manage THIS committee's members (admin or this
    committee's own accepted Major Advisor) — reuses `_authorize_manage_members`
    unchanged. Fixes the Major Advisor (role=FACULTY) being unable to see a
    faculty list at all, since the general-purpose `GET /auth/users`
    directory is intentionally admin/HOD-only and is not being widened here.

    Department-eligibility task (this revision) — now ROLE-AWARE and based on
    CURRENT FACULTY `UserRoleAssignment` membership (`app.core.faculty_scope`),
    never `User.department_id`. `role` is optional (an unfiltered list is still
    returned if omitted, for backward compatibility); when given, only
    candidates eligible for THAT committee role against this student's
    department are returned — matching exactly what `add_member` will itself
    accept, so the dropdown can never offer a choice the backend would reject.
    `department_id` is an OPTIONAL narrowing filter for the caller's own
    convenience (e.g. Member Minor picking one particular other department) —
    it is never trusted as authorization; `add_member` re-validates
    independently of whatever the client claims here. A multi-department
    faculty member (e.g. FACULTY @ A + @ B) appears exactly ONCE, with every
    department they hold FACULTY in listed, per `list_faculty_with_departments`."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_manage_members(c, user, db)
    student_department_id = await _student_department_id(c.student_id, db)
    candidates = await list_faculty_with_departments(db)

    if role in _SAME_DEPARTMENT_ROLES:
        candidates = [f for f in candidates if student_department_id and str(student_department_id) in f["department_ids"]]
    elif role in _OTHER_DEPARTMENT_ROLES:
        candidates = [f for f in candidates if any(d != str(student_department_id) for d in f["department_ids"])]
    # role in _NO_DEPARTMENT_ROLES (supporting), or role omitted/unrecognized -> every current-FACULTY candidate, unfiltered.

    if department_id is not None:
        candidates = [f for f in candidates if str(department_id) in f["department_ids"]]

    return candidates


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
    await _assert_no_progress_report_under_approval(c.student_id, db, "add a committee member", "the committee can be changed")
    if c.status not in _MEMBER_MODIFIABLE_STATUSES:
        raise HTTPException(400, "Members can only be added while the committee is in member-selection stage, awaiting HOD approval, or after DPGS final approval (which reopens the approval chain).")
    if body.role not in _MEMBER_ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(_MEMBER_ROLES)}.")

    # Shape must match the role — the MemberIn validator only enforces "exactly one
    # of internal/external", not which one a given ROLE requires.
    is_external = body.faculty_id is None
    if body.role in _EXTERNAL_ROLES and not is_external:
        raise HTTPException(400, "Member of Others must be entered as an external person (name/designation/institute), not an internal faculty_id.")
    if body.role not in _EXTERNAL_ROLES and is_external:
        raise HTTPException(400, f"{body.role.replace('_', ' ').title()} requires an internal faculty_id.")

    if is_external:
        # External members are "involved offline only" — there is no invitation/
        # accept-decline flow for them, so they are marked accepted immediately and
        # never block the other members' all-must-accept auto-advance gate (see
        # accept_membership below and the CommitteeMember model docstring).
        now = datetime.now(timezone.utc)
        m = CommitteeMember(
            committee_id=committee_id, role=body.role,
            external_name=body.external_name.strip(), external_designation=body.external_designation.strip(),
            external_institute=body.external_institute.strip(),
            accepted=True, accepted_at=now,
        )
    else:
        student_department_id = await _student_department_id(c.student_id, db)
        await _validate_faculty_department_eligibility(body.role, body.faculty_id, student_department_id, db)
        existing = await db.execute(select(CommitteeMember).where(
            CommitteeMember.committee_id == committee_id, CommitteeMember.faculty_id == body.faculty_id,
        ))
        if existing.scalar_one_or_none():
            raise HTTPException(409, "This faculty member is already on the committee.")
        m = CommitteeMember(committee_id=committee_id, faculty_id=body.faculty_id, role=body.role)

    db.add(m)
    await db.flush()
    # Mirrors accept_membership's own auto-advance check: if this newly-added
    # member happens to be the last one still needing a response (e.g. an
    # external member, always pre-accepted, added after every internal member
    # already accepted), the committee must not be stranded in members_pending.
    # Post-DPGS-approval member-change task (this revision) — reuses the
    # shared 3-way recompute (`_recompute_committee_member_stage`) so adding a
    # member to an ALREADY-`dpgs_approved` committee correctly reopens the
    # chain: the new member is never pre-accepted (internal) or is
    # pre-accepted (external), and the recompute decides `members_pending` vs
    # `hod_pending` the same way it always has. Any stale revert remark from
    # a prior cycle is cleared once the Major Advisor acts — mirrors
    # `remove_member`'s existing, unconditional clear below for consistency
    # between these two sibling endpoints.
    others = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee_id, CommitteeMember.role != "major_advisor",
    ))
    c.status = _recompute_committee_member_stage(others.scalars().all())
    c.revert_remark = None; c.reverted_at = None
    await db.commit()
    return {"message": "Member added.", "stage": c.status}


@router.delete("/committees/{committee_id}/members/{member_id}", status_code=204)
async def remove_member(
    committee_id: UUID, member_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    """Corrective action after a member decline (Stage 3 revert) — lets the Major
    Advisor remove the declined member and add a replacement via add_member.
    Post-DPGS-approval member-change task (this revision) — also the
    endpoint for removing a member WITHOUT replacement from an
    already-`dpgs_approved` committee, which restarts the approval chain at
    HOD approval (see `_recompute_committee_member_stage` below: the
    remaining members are all still `accepted is True` from before, so the
    recompute lands directly on `hod_pending`, with no acceptance step
    needed since nothing new was added)."""
    c = await db.get(AdvisoryCommittee, committee_id)
    if not c: raise HTTPException(404, "Committee not found.")
    await _authorize_manage_members(c, user, db)
    await _assert_no_synopsis_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
    await _assert_no_external_examiner_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
    await _assert_no_thesis_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
    await _assert_no_progress_report_under_approval(c.student_id, db, "remove a committee member", "the committee can be changed")
    if c.status not in _MEMBER_MODIFIABLE_STATUSES:
        raise HTTPException(400, "Members can only be removed while the committee is in member-selection stage, awaiting HOD approval, after a member decline, or after DPGS final approval (which reopens the approval chain).")
    m = await db.get(CommitteeMember, member_id)
    if not m or m.committee_id != committee_id or m.role == "major_advisor":
        raise HTTPException(404, "Member not found.")

    await db.delete(m)
    await db.flush()
    remaining = await db.execute(select(CommitteeMember).where(
        CommitteeMember.committee_id == committee_id, CommitteeMember.role != "major_advisor",
    ))
    c.status = _recompute_committee_member_stage(remaining.scalars().all())
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
    a signature — no separate signing/OTP mechanism is introduced (unlike
    Course Registration/PPW's DPGS stage). Advisory Committee document task
    (this revision): the committee now HAS a downloadable document once
    `dpgs_approved` (see `get_committee_document` below), but this stage
    itself still records no approver identity — the document reports the
    workflow's recorded status, nothing new is persisted here."""
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
    """Final approval. Unlike Course Registration/PPW's DPGS stage, NO
    signature/approver-identity field is added here; this is a plain
    approval-state transition, same shape as every other committee stage.
    Advisory Committee document task (this revision): reaching
    `dpgs_approved` is what makes `GET /committees/{id}/document` start
    serving the official document (see below) — a read-only consequence of
    this status, not a new persisted field on this model."""
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


# ── Official Advisory Committee document (this revision) ────────────────────────
# AVFU-confirmed requirement: once DPGS gives final approval, the system must
# generate an official Advisory Committee document and make it downloadable
# by the associated student. Reuses the exact PPW architecture (on-demand
# Jinja2 -> Playwright/Chromium render, same `render_ppw_pdf` utility, same
# Content-Disposition pattern) — no new PDF library, no new storage system.
#
# On-demand, never frozen/stored (deliberate, not a shortcut): `remove_member`
# above has NO `c.status` gate — the committee's own accepted Major Advisor can
# remove a member at ANY stage, including after `dpgs_approved`, and doing so
# always resets `c.status` to `members_pending`/`member_selection` as a side
# effect of the existing code (see `remove_member`). Re-checking
# `c.status == "dpgs_approved"` on every request therefore already guarantees
# the document is refused the instant the approved composition changes — no
# separate frozen-copy/invalidation mechanism is needed to keep the document
# honest, unlike Synopsis's frozen-PDF pattern (which exists because Synopsis
# documents are merged with a separately-uploaded file, a different problem).

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
_committee_jinja_env = None


def _render_committee_document_html(context: dict) -> str:
    """Renders the Advisory Committee document template. Jinja2 environment is
    created lazily and cached at module level, same pattern as ppw.py."""
    global _committee_jinja_env
    if _committee_jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _committee_jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )
    template = _committee_jinja_env.get_template("advisory_committee_document.html")
    return template.render(**context)


async def _build_committee_document_context(c: AdvisoryCommittee, db: AsyncSession) -> dict:
    data = await _committee_dict(c, db)
    # Major Advisor first, other members in their existing order — the
    # `members` relationship has no `order_by`, so this is made explicit here
    # rather than relying on incidental DB return order (sample's own layout
    # always lists the Major Advisor first).
    data["members"].sort(key=lambda m: 0 if m["role"] == "major_advisor" else 1)
    for m in data["members"]:
        m["role_label"] = _COMMITTEE_DOCUMENT_ROLE_LABELS.get(m["role"], m["role"].replace("_", " ").title())
    major_advisor = next((m for m in data["members"] if m["role"] == "major_advisor"), None)

    from app.utils.pdf import get_logo_data_uri
    return {
        "committee": data,
        "program_level_label": _PROGRAM_LEVEL_DOCUMENT_LABELS.get(data["program_level"] or "", None),
        "major_advisor_name": major_advisor["faculty_name"] if major_advisor else None,
        "logo_data_uri": get_logo_data_uri(),
        "generated_at": datetime.now(timezone.utc),
    }


@router.get("/committees/{committee_id}/document")
async def get_committee_document(
    committee_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Official Advisory Committee document as a PDF — reuses `_authorize_committee_view`
    unchanged (the same viewer rules as GET /committees/student/{id}: a student
    sees only their own committee, HOD only their own department, Faculty only
    a committee they belong to, Super Admin/Incharge Academic Cell/DPGS see
    all). Only ever served once the committee has reached DPGS final
    approval — every other stage (pending, reverted) is refused with 400, so
    no partial/unapproved composition is ever exposed as an official document."""
    result = await db.execute(
        select(AdvisoryCommittee).options(*_COMMITTEE_LOAD_OPTIONS).where(AdvisoryCommittee.id == committee_id)
    )
    c = result.scalar_one_or_none()
    if not c:
        raise HTTPException(404, "Committee not found.")
    await _authorize_committee_view(c, user, db)
    if c.status != "dpgs_approved":
        raise HTTPException(400, "The Advisory Committee document is only available after DPGS final approval.")

    context = await _build_committee_document_context(c, db)
    html = _render_committee_document_html(context)

    from starlette.concurrency import run_in_threadpool
    from app.utils.pdf import render_ppw_pdf, ChromiumUnavailable, ChromiumRenderFailed
    try:
        pdf_bytes = await run_in_threadpool(render_ppw_pdf, html)
    except ChromiumUnavailable as exc:
        logger.error("Advisory Committee document generation unavailable (committee_id=%s): %s", committee_id, exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("Advisory Committee document render failed (committee_id=%s): %s", committee_id, exc)
        raise HTTPException(422, "Unable to generate Advisory Committee document.")

    roll_or_id = (c.student.student_roll or str(c.student_id)) if c.student else str(c.student_id)
    safe_roll = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in roll_or_id)
    filename = f"Advisory-Committee-{safe_roll}.pdf"
    encoded_name = urllib.parse.quote(filename, safe="")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


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
    data = [await _committee_dict(c, db) for c in committees]

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

    data = await _committee_dict(c, db)
    stats = await _member_stats([m.faculty_id for m in c.members if m.faculty_id], db)
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


# `PATCH /committees/{id}/lock` was REMOVED, this revision (post-DPGS-approval
# committee-change task). It required `c.status == "hod_approved"`, a status
# value no endpoint has set since the Incharge Academic Cell/DPGS task
# extended the chain past HOD (HOD approval now advances to
# `incharge_pending`, never `hod_approved` — confirmed by inspection: no
# `c.status = "hod_approved"` assignment exists anywhere in this file). The
# endpoint was therefore already permanently unreachable dead code — no test
# and no frontend code ever called it (confirmed by repository-wide search) —
# and even if it had been reachable, a PERMANENT lock would now directly
# contradict the newly confirmed business rule that a committee remains
# editable after `dpgs_approved`. `AdvisoryCommittee.is_locked` (the column)
# was deliberately LEFT IN PLACE rather than dropped via a migration — see
# the model's own field comment — since removing a column is a materially
# higher-risk, higher-ceremony action than leaving an already-inert one, and
# no functional requirement of this task needed the schema changed.
