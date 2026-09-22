"""External Examiner Selection (BUSINESS_LOGIC.md section AC).

Workflow (per submission cycle):

    Major Advisor -> HOD -> Incharge Academic Cell -> DPGS -> Vice Chancellor

* There is NO student participation anywhere in this module — no student
  endpoint, no student serializer branch, never `require_roles(..., STUDENT)`.
  The Major Advisor prepares the list on the student's behalf.
* Required proposal count comes ONLY from `Program.level` (PG -> 3, PhD -> 5),
  snapshotted onto the selection at creation. VC selection count is likewise
  fixed (PG -> 1, PhD -> 2). Neither is ever accepted from the client.
* The Major Advisor is resolved server-side from the student's Advisory
  Committee (`role='major_advisor'`, `accepted=True`) — never from a
  client-supplied id, exactly the Synopsis/PPW `_find_my_stage` pattern.
* Every revert (HOD, Incharge, DPGS, or VC) returns unconditionally to the
  Major Advisor and requires a remark; a resubmission starts a brand-new
  cycle with fresh proposal rows — nothing from an earlier cycle carries over,
  and no cycle/proposal/stage is ever deleted or overwritten.
* The Incharge Academic Cell may correct the six proposal fields WHILE at
  their stage (a separate, audited endpoint — editing never approves, and
  never creates a cycle) but cannot change the proposal count, the slot
  layout, or which cycle/selection a proposal belongs to.
* Examiners are REUSABLE identities (`ExternalExaminer`, keyed by normalized
  email) — the same person proposed for different students resolves to the
  same identity, and once actually selected, the same AMS account (no
  password reset, no duplicate account, no credentials resent).
* After VC selection, WHICH examiner(s) were chosen is visible only to DPGS
  and the VC — every other authorized viewer (Major Advisor, HOD, Incharge)
  sees only `selection_completed: true/false`, with the result field entirely
  ABSENT from the JSON (never null/empty) so its presence can't leak the
  answer either.
* Examiners must be outside AVFU — enforced by rejecting any `@avfu.ac.in`
  email (case-insensitive), the same domain constant already used for AVFU
  staff/student accounts elsewhere in this codebase. Institution is free text
  and is never validated against it (no reliable master data exists for that).
"""
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, EmailStr, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.email import enqueue_email
from app.core.security import generate_temp_password, hash_password
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.audit import AuditLog
from app.models.external_examiner import (
    REQUIRED_PROPOSAL_COUNT, REQUIRED_SELECTION_COUNT,
    ExternalExaminer, ExternalExaminerApprovalCycle, ExternalExaminerApprovalStage,
    ExternalExaminerAssignment, ExternalExaminerProposal, ExternalExaminerSelection,
    ExternalExaminerSelectionResult, ExternalExaminerSignature,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import Department, Program, User, UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/external-examiners", tags=["External Examiner Selection"])

# The same AVFU domain constant that already gates staff/Orientation accounts
# elsewhere (auth.py's `_AVFU_STAFF_EMAIL_DOMAIN`, orientation.py's
# `_AVFU_EMAIL_DOMAIN`) — reused here for the OPPOSITE check: an external
# examiner must NOT be on this domain.
_AVFU_EMAIL_DOMAIN = "avfu.ac.in"

_PG_PHD_LEVELS = ("PG", "PhD")
_EDITABLE_STATUSES = ("draft", "reverted")
# Every stage past the Major Advisor is a signature (OTP) EXCEPT Incharge, which is
# workflow-approval-only — identical convention to Synopsis's `_SIGNATORY_STAGES`.
_SIGNATORY_STAGES = ("major_advisor", "hod", "dpgs", "vc")
_APPROVER_ROLES = (UserRole.FACULTY, UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.VICE_CHANCELLOR)
_OTP_TTL_MINUTES = 10

_PHASE_FOR_STAGE = {
    "major_advisor": "major_advisor_pending",
    "hod": "hod_pending",
    "incharge_academic_cell": "incharge_pending",
    "dpgs": "dpgs_pending",
    "vc": "vc_pending",
}
_STATUS_LABELS = {
    "draft": "Draft",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "incharge_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending",
    "vc_pending": "Vice Chancellor Selection Pending",
    "approved": "Approved (Final)",
    "reverted": "Reverted",
}
_ROLE_DISPLAY = {
    "hod": "HOD", "faculty": "Faculty", "incharge_academic_cell": "Incharge Academic Cell",
    "dpgs": "DPGS", "vice_chancellor": "Vice Chancellor", "super_admin": "Super Admin",
}
_STAGE_ROLE_LABELS = {
    "major_advisor": "Major Advisor", "hod": "Head of the Department",
    "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "vc": "Vice Chancellor",
}


# ── Schemas ──────────────────────────────────────────────────────────────────

class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    specialization: str
    designation: str
    email: EmailStr
    phone: str
    institution: str

    @field_validator("name", "specialization", "designation", "phone", "institution")
    @classmethod
    def _required_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("This field is required.")
        return v


class SelectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: UUID
    proposals: list[ProposalIn]


class ProposalEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    specialization: str
    designation: str
    email: EmailStr
    phone: str
    institution: str

    @field_validator("name", "specialization", "designation", "phone", "institution")
    @classmethod
    def _required_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("This field is required.")
        return v


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp: Optional[str] = None


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: str


class VcSelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_ids: list[UUID]


# ── Small helpers ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _not_found() -> HTTPException:
    return HTTPException(404, "External Examiner Selection not found.")


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _reject_avfu_email(email: str) -> None:
    if email.endswith("@" + _AVFU_EMAIL_DOMAIN):
        raise HTTPException(400, f"External examiners must be from outside Assam Veterinary and Fishery University (an @{_AVFU_EMAIL_DOMAIN} address was given).")


def _person_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return " ".join(p for p in (u.title, u.full_name) if p)


def _name_designation(u: Optional[User]) -> str:
    if not u:
        return "—"
    return f"{_person_name(u)} ({u.designation})" if u.designation else _person_name(u)


def _is_dev_environment() -> bool:
    return settings.ENVIRONMENT.strip().lower() == "development"


_LOAD_OPTIONS = (
    selectinload(ExternalExaminerSelection.student).selectinload(User.program),
    selectinload(ExternalExaminerSelection.student).selectinload(User.department),
    selectinload(ExternalExaminerSelection.student).selectinload(User.college),
    selectinload(ExternalExaminerSelection.cycles).selectinload(ExternalExaminerApprovalCycle.proposals).selectinload(ExternalExaminerProposal.examiner),
    selectinload(ExternalExaminerSelection.cycles).selectinload(ExternalExaminerApprovalCycle.stages).selectinload(ExternalExaminerApprovalStage.committee_member),
    selectinload(ExternalExaminerSelection.cycles).selectinload(ExternalExaminerApprovalCycle.stages).selectinload(ExternalExaminerApprovalStage.assignee),
    selectinload(ExternalExaminerSelection.cycles).selectinload(ExternalExaminerApprovalCycle.stages).selectinload(ExternalExaminerApprovalStage.approver),
    selectinload(ExternalExaminerSelection.cycles).selectinload(ExternalExaminerApprovalCycle.results).selectinload(ExternalExaminerSelectionResult.proposal),
)


async def _get_selection(selection_id: UUID, db: AsyncSession) -> ExternalExaminerSelection:
    result = await db.execute(
        select(ExternalExaminerSelection).options(*_LOAD_OPTIONS).where(ExternalExaminerSelection.id == selection_id)
        .execution_options(populate_existing=True)
    )
    s = result.scalar_one_or_none()
    if not s:
        raise _not_found()
    return s


def _latest_cycle(s: ExternalExaminerSelection) -> Optional[ExternalExaminerApprovalCycle]:
    return s.cycles[-1] if s.cycles else None


def _active_cycle(s: ExternalExaminerSelection) -> Optional[ExternalExaminerApprovalCycle]:
    c = _latest_cycle(s)
    return c if c and c.status == "active" else None


# ── Authorization ────────────────────────────────────────────────────────────

async def _committee_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    return (await db.execute(select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id == student_id))).scalar_one_or_none()


async def _accepted_major_advisor(student_id: UUID, db: AsyncSession) -> Optional[CommitteeMember]:
    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members))
        .where(AdvisoryCommittee.student_id == student_id)
    )).scalar_one_or_none()
    if not committee:
        return None
    return next((m for m in committee.members if m.role == "major_advisor" and m.accepted is True), None)


async def _authorize_view(s: ExternalExaminerSelection, user: User, db: AsyncSession) -> None:
    """Who may READ a selection (never a Student — Section 8/10 of the confirmed
    rules, no branch exists for it at all). Anything unauthorized is a 404 so the
    existence of another student's selection is never revealed."""
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.VICE_CHANCELLOR):
        return
    if role == UserRole.FACULTY:
        major = await _accepted_major_advisor(s.student_id, db)
        if major and major.faculty_id == user.id:
            return
        # Past-cycle approvers keep read access, exactly the Synopsis convention.
        assigned = await db.execute(
            select(ExternalExaminerApprovalStage.id)
            .join(ExternalExaminerApprovalCycle, ExternalExaminerApprovalCycle.id == ExternalExaminerApprovalStage.cycle_id)
            .where(ExternalExaminerApprovalCycle.selection_id == s.id, ExternalExaminerApprovalStage.assignee_id == user.id)
            .limit(1)
        )
        if assigned.scalar_one_or_none():
            return
        raise _not_found()
    if role == UserRole.HOD:
        dept_id = await resolve_student_department_id(s.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
        raise _not_found()
    raise _not_found()


async def _find_my_stage(s: ExternalExaminerSelection, user: User, db: AsyncSession) -> Optional[ExternalExaminerApprovalStage]:
    """The SOLE authorization check for every approval action — the caller's own
    currently-actionable stage in the ACTIVE cycle, or None. Never a bare role
    match, never an id from the request:

    * Major Advisor — active session role is faculty-type AND the stage's
      `CommitteeMember` row belongs to THIS student's committee, has
      `faculty_id == caller`, and is the Major Advisor row specifically.
    * HOD — active role HOD AND the caller's ACTIVE assignment department ==
      the student's department, re-verified live.
    * Incharge / DPGS / VC — the caller's active role only (global)."""
    cycle = _active_cycle(s)
    if not cycle:
        return None
    committee_id = None
    for stage in cycle.stages:
        if stage.status != "pending" or _PHASE_FOR_STAGE.get(stage.stage_type) != s.status:
            continue
        if stage.stage_type == "major_advisor":
            if user.active_role not in (UserRole.FACULTY, UserRole.HOD):
                continue
            member = stage.committee_member
            if not member or member.faculty_id != user.id or member.role != "major_advisor":
                continue
            if committee_id is None:
                committee_id = await _committee_id(s.student_id, db)
            if committee_id and member.committee_id == committee_id:
                return stage
        elif stage.stage_type == "hod":
            if user.active_role == UserRole.HOD and user.active_department_id:
                dept_id = await resolve_student_department_id(s.student_id, db)
                if dept_id and dept_id == user.active_department_id:
                    return stage
        elif stage.stage_type == "incharge_academic_cell":
            if user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
                return stage
        elif stage.stage_type == "dpgs":
            if user.active_role == UserRole.DPGS:
                return stage
        elif stage.stage_type == "vc":
            if user.active_role == UserRole.VICE_CHANCELLOR:
                return stage
    return None


async def _require_my_stage(s: ExternalExaminerSelection, user: User, db: AsyncSession) -> ExternalExaminerApprovalStage:
    stage = await _find_my_stage(s, user, db)
    if not stage:
        raise HTTPException(403, "You have no pending External Examiner action on this selection.")
    return stage


# ── Examiner identity resolution ─────────────────────────────────────────────

async def _resolve_examiner_id(email: str, db: AsyncSession) -> Optional[UUID]:
    """Look up the reusable `ExternalExaminer` identity by NORMALIZED email. Never
    creates one — a proposed-but-not-yet-selected examiner has no identity row
    until the VC actually selects them (Section 7 of the confirmed rules)."""
    normalized = _normalize_email(email)
    return (await db.execute(select(ExternalExaminer.id).where(ExternalExaminer.email == normalized))).scalar_one_or_none()


# ── Serialization ────────────────────────────────────────────────────────────

def _proposal_dict(p: ExternalExaminerProposal) -> dict:
    return {
        "id": str(p.id), "slot_number": p.slot_number,
        "name": p.name_snapshot, "specialization": p.specialization_snapshot, "designation": p.designation_snapshot,
        "email": p.email_snapshot, "phone": p.phone_snapshot, "institution": p.institution_snapshot,
        "is_reused_examiner": p.examiner_id is not None,
        "edited": p.edited_at is not None,
    }


def _stage_dict(stage: ExternalExaminerApprovalStage, dept_names: dict) -> dict:
    return {
        "sequence": stage.sequence,
        "stage_type": stage.stage_type,
        "role_label": stage.role_label,
        "assigned_to": _name_designation(stage.assignee) if stage.assignee else None,
        "status": stage.status,
        "acted_by": _name_designation(stage.approver) if stage.approver else None,
        "acted_role": _ROLE_DISPLAY.get(stage.acted_role or "", stage.acted_role),
        "acted_department": dept_names.get(stage.acted_department_id),
        "acted_at": _iso(stage.acted_at),
        "remark": stage.remark,
        "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
    }


async def _department_names(db: AsyncSession) -> dict:
    return {i: n for i, n in (await db.execute(select(Department.id, Department.name))).all()}


async def _selection_dict(s: ExternalExaminerSelection, viewer: User, db: AsyncSession) -> dict:
    """Role-scoped detail. The `selection_result`/`selected_proposal_ids` field is
    only ever ADDED for DPGS/VC/Super Admin — every other authorized viewer gets
    `selection_completed` alone, with the field genuinely absent (never null/empty),
    so field presence itself cannot leak the answer (Section 6/27 of the confirmed
    rules)."""
    dept_names = await _department_names(db)
    latest = _latest_cycle(s)
    my_stage = await _find_my_stage(s, viewer, db) if viewer.active_role in _APPROVER_ROLES else None

    revert_info = None
    if s.status == "reverted" and latest and latest.status == "reverted":
        stage = next((st for st in latest.stages if st.status == "reverted"), None)
        revert_info = {
            "cycle_number": latest.cycle_number,
            "reverted_by": _name_designation(stage.approver) if stage and stage.approver else None,
            "role": stage.role_label if stage else None,
            "acting_as": _ROLE_DISPLAY.get(stage.acted_role or "", stage.acted_role) if stage else None,
            "department": dept_names.get(stage.acted_department_id) if stage else None,
            "reverted_at": _iso(latest.reverted_at),
            "remark": latest.revert_remark,
        }

    proposals = [_proposal_dict(p) for p in latest.proposals] if latest else []
    stages = [_stage_dict(st, dept_names) for st in latest.stages] if latest else []
    major = await _accepted_major_advisor(s.student_id, db)
    is_major_advisor = viewer.active_role in (UserRole.FACULTY, UserRole.HOD) and major and major.faculty_id == viewer.id

    body = {
        "id": str(s.id),
        "status": s.status,
        "status_label": _STATUS_LABELS.get(s.status, s.status),
        "degree_level": s.degree_level,
        "required_proposal_count": REQUIRED_PROPOSAL_COUNT.get(s.degree_level),
        "required_selection_count": REQUIRED_SELECTION_COUNT.get(s.degree_level),
        "student": {
            "name": s.student.full_name, "roll_no": s.student.student_roll,
            "program_name": s.student.program.name if s.student.program else None,
            "department_name": s.student.department.name if s.student.department else None,
            "college_name": s.student.college.name if s.student.college else None,
        },
        "is_major_advisor": is_major_advisor,
        "can_edit": is_major_advisor and s.status in _EDITABLE_STATUSES,
        "current_cycle_number": latest.cycle_number if latest else None,
        "proposals": proposals,
        "stages": stages,
        "revert_info": revert_info,
        "selection_completed": bool(latest and latest.vc_selection_completed_at),
        "my_pending_stage": (
            {"stage_type": my_stage.stage_type, "role_label": my_stage.role_label, "requires_otp": my_stage.stage_type in _SIGNATORY_STAGES}
            if my_stage else None
        ),
        "history": [{
            "cycle_number": c.cycle_number, "status": c.status,
            "submitted_at": _iso(c.submitted_at), "completed_at": _iso(c.completed_at),
            "reverted_at": _iso(c.reverted_at), "revert_remark": c.revert_remark,
            "proposals": [_proposal_dict(p) for p in c.proposals],
            "stages": [_stage_dict(st, dept_names) for st in c.stages],
        } for c in s.cycles],
    }
    if latest and latest.vc_selection_completed_at and viewer.active_role in (UserRole.DPGS, UserRole.VICE_CHANCELLOR, UserRole.SUPER_ADMIN):
        body["selected_proposal_ids"] = sorted(str(r.proposal_id) for r in latest.results)
    return body


# ── Create / read ────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_selection(
    body: SelectionCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD)),
):
    """The Major Advisor creates and immediately submits the proposal list in one
    call (there is no separate draft-then-submit step for this module — unlike
    Synopsis/PPW, there is nothing else to fill in first). The caller must
    actually be `body.student_id`'s accepted Major Advisor; `body.student_id` is
    the only student-identifying input accepted anywhere in the request, and it
    is verified, never trusted."""
    student = await db.get(User, body.student_id)
    if not student:
        raise HTTPException(404, "Student not found.")
    major = await _accepted_major_advisor(student.id, db)
    if not major or major.faculty_id != user.id:
        raise HTTPException(403, "You are not this student's accepted Major Advisor.")

    program = await db.get(Program, student.program_id) if student.program_id else None
    if not program or program.level not in _PG_PHD_LEVELS:
        raise HTTPException(400, "External Examiner Selection applies to postgraduate (PG/PhD) students only.")
    degree_level = program.level
    required = REQUIRED_PROPOSAL_COUNT[degree_level]
    if len(body.proposals) != required:
        raise HTTPException(400, f"{degree_level} students require exactly {required} proposed examiners; {len(body.proposals)} were given.")

    emails_seen = set()
    normalized_proposals = []
    for p in body.proposals:
        normalized = _normalize_email(p.email)
        _reject_avfu_email(normalized)
        if normalized in emails_seen:
            raise HTTPException(400, f"The same examiner email ({normalized}) was proposed more than once in this list.")
        emails_seen.add(normalized)
        normalized_proposals.append((p, normalized))

    existing = (await db.execute(select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.student_id == student.id))).scalar_one_or_none()
    if existing:
        selection = await _get_selection(existing, db)
        if selection.status not in _EDITABLE_STATUSES:
            raise HTTPException(409, "This student already has an External Examiner Selection under approval or approved.")
    else:
        selection = ExternalExaminerSelection(student_id=student.id, degree_level=degree_level, status="draft")
        db.add(selection)
        await db.flush()

    # Computed via a direct query, never `selection.cycles` — for a brand-new
    # selection (the `else` branch above) that collection is unloaded on a
    # freshly-flushed object, and touching it here would trigger an implicit
    # lazy load outside of any awaited context (MissingGreenlet under async
    # SQLAlchemy) on every student's very first submission.
    next_number = (await db.execute(
        select(func.max(ExternalExaminerApprovalCycle.cycle_number)).where(ExternalExaminerApprovalCycle.selection_id == selection.id)
    )).scalar_one() or 0
    next_number += 1
    now = _now()
    cycle = ExternalExaminerApprovalCycle(selection_id=selection.id, cycle_number=next_number, status="active", submitted_at=now)
    db.add(cycle)
    await db.flush()

    for slot, (p, normalized) in enumerate(normalized_proposals, start=1):
        examiner_id = await _resolve_examiner_id(normalized, db)
        db.add(ExternalExaminerProposal(
            cycle_id=cycle.id, slot_number=slot, examiner_id=examiner_id,
            name_snapshot=p.name, specialization_snapshot=p.specialization, designation_snapshot=p.designation,
            email_snapshot=normalized, phone_snapshot=p.phone, institution_snapshot=p.institution,
        ))

    seq = 1
    db.add(ExternalExaminerApprovalStage(
        cycle_id=cycle.id, sequence=seq, stage_type="major_advisor", role_label=_STAGE_ROLE_LABELS["major_advisor"],
        committee_member_id=major.id, assignee_id=major.faculty_id,
    ))
    for stage_type in ("hod", "incharge_academic_cell", "dpgs", "vc"):
        seq += 1
        db.add(ExternalExaminerApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type=stage_type, role_label=_STAGE_ROLE_LABELS[stage_type]))

    selection.status = "major_advisor_pending"
    selection.degree_level = degree_level
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This student already has an active External Examiner approval cycle.")
    return {"id": str(selection.id), "message": "External Examiner list submitted for approval.", "status": selection.status}


@router.get("/mine")
async def list_my_selections(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD))):
    """Every External Examiner Selection where the caller is (or was, in any past
    cycle) the student's accepted Major Advisor — so the Major Advisor's own page
    can show existing status/history instead of blindly re-proposing. Mirrors
    `list_pending_approvals`'s join style, without restricting to pending stages."""
    ids = (await db.execute(
        select(ExternalExaminerSelection.id)
        .join(ExternalExaminerApprovalCycle, ExternalExaminerApprovalCycle.selection_id == ExternalExaminerSelection.id)
        .join(ExternalExaminerApprovalStage, ExternalExaminerApprovalStage.cycle_id == ExternalExaminerApprovalCycle.id)
        .join(CommitteeMember, CommitteeMember.id == ExternalExaminerApprovalStage.committee_member_id)
        .where(ExternalExaminerApprovalStage.stage_type == "major_advisor", CommitteeMember.faculty_id == user.id)
        .distinct()
    )).scalars().all()
    rows = []
    for sid in ids:
        s = await _get_selection(sid, db)
        rows.append({
            "selection_id": str(s.id), "student_id": str(s.student_id), "student_name": s.student.full_name,
            "student_roll": s.student.student_roll, "degree_level": s.degree_level,
            "status": s.status, "status_label": _STATUS_LABELS.get(s.status, s.status),
        })
    return rows


@router.get("/pending-approvals")
async def list_pending_approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """"My External Examiner approvals" inbox — every selection whose CURRENT
    stage the caller (in their active session role) can act on."""
    ids: set[UUID] = set()
    if user.active_role in (UserRole.FACULTY, UserRole.HOD):
        ids.update((await db.execute(
            select(ExternalExaminerSelection.id)
            .join(ExternalExaminerApprovalCycle, ExternalExaminerApprovalCycle.selection_id == ExternalExaminerSelection.id)
            .join(ExternalExaminerApprovalStage, ExternalExaminerApprovalStage.cycle_id == ExternalExaminerApprovalCycle.id)
            .join(CommitteeMember, CommitteeMember.id == ExternalExaminerApprovalStage.committee_member_id)
            .where(ExternalExaminerApprovalCycle.status == "active", ExternalExaminerApprovalStage.status == "pending",
                   CommitteeMember.faculty_id == user.id)
        )).scalars().all())
    phase = {
        UserRole.HOD: "hod_pending", UserRole.INCHARGE_ACADEMIC_CELL: "incharge_pending",
        UserRole.DPGS: "dpgs_pending", UserRole.VICE_CHANCELLOR: "vc_pending",
    }.get(user.active_role)
    if phase:
        q = select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.status == phase)
        if user.active_role == UserRole.HOD:
            if not user.active_department_id:
                return []
            q = q.join(User, User.id == ExternalExaminerSelection.student_id).where(User.department_id == user.active_department_id)
        ids.update((await db.execute(q)).scalars().all())

    rows = []
    for sid in ids:
        s = await _get_selection(sid, db)
        stage = await _find_my_stage(s, user, db)
        if not stage:
            continue
        cycle = _active_cycle(s)
        rows.append({
            "selection_id": str(s.id),
            "student_name": s.student.full_name,
            "student_roll": s.student.student_roll,
            "program_name": s.student.program.name if s.student.program else None,
            "department_name": s.student.department.name if s.student.department else None,
            "degree_level": s.degree_level,
            "status": s.status,
            "status_label": _STATUS_LABELS.get(s.status, s.status),
            "acting_as": stage.role_label,
            "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
            "cycle_number": cycle.cycle_number if cycle else None,
            "submitted_at": _iso(cycle.submitted_at) if cycle else None,
        })
    rows.sort(key=lambda r: r["submitted_at"] or "")
    return rows


@router.get("/examiners/{examiner_id}")
async def get_examiner(examiner_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.DPGS))):
    """DPGS/Super Admin oversight: a reusable examiner's account + assignment
    history, needed to decide whether their account is safe to deactivate."""
    examiner = (await db.execute(
        select(ExternalExaminer).options(selectinload(ExternalExaminer.user)).where(ExternalExaminer.id == examiner_id)
    )).scalar_one_or_none()
    if not examiner:
        raise HTTPException(404, "Examiner not found.")
    assignments = (await db.execute(
        select(ExternalExaminerAssignment).options(selectinload(ExternalExaminerAssignment.student))
        .where(ExternalExaminerAssignment.examiner_id == examiner_id)
    )).scalars().all()
    return {
        "id": str(examiner.id), "email": examiner.email,
        "account_user_id": str(examiner.user_id) if examiner.user_id else None,
        "account_active": examiner.user.is_active if examiner.user else None,
        "assignments": [{
            "id": str(a.id), "student_name": a.student.full_name if a.student else None,
            "student_roll": a.student.student_roll if a.student else None, "status": a.status,
            "created_at": _iso(a.created_at),
        } for a in assignments],
        "has_active_assignment": any(a.status == "active" for a in assignments),
    }


@router.get("/{selection_id}")
async def get_selection(selection_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    s = await _get_selection(selection_id, db)
    await _authorize_view(s, user, db)
    return await _selection_dict(s, user, db)


# ── Incharge edit (separate from approve — Section 15/17 of the confirmed rules) ──

@router.patch("/{selection_id}/proposals/{proposal_id}")
async def edit_proposal(
    selection_id: UUID, proposal_id: UUID, body: ProposalEditIn, request: Request,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.INCHARGE_ACADEMIC_CELL)),
):
    """Incharge-only correction of the six proposal fields, WHILE the selection is
    at the Incharge stage — never `slot_number`, `cycle_id`, `examiner_id`, or the
    proposal count, none of which this schema even accepts. Editing does NOT
    approve and does NOT create a new cycle; it is a plain in-place update of the
    current cycle's row, audited independently of the (separate) approve call."""
    s = await _get_selection(selection_id, db)
    stage = await _require_my_stage(s, user, db)
    if stage.stage_type != "incharge_academic_cell":
        raise HTTPException(403, "You may only edit a proposal while it is at the Incharge Academic Cell stage.")
    cycle = _active_cycle(s)
    proposal = next((p for p in cycle.proposals if p.id == proposal_id), None) if cycle else None
    if not proposal:
        # Deliberately 404, not 400: this also covers "belongs to another selection/cycle entirely" —
        # a foreign proposal_id must never distinguish "wrong cycle" from "doesn't exist".
        raise HTTPException(404, "Proposal not found in the current cycle.")

    old = {
        "name": proposal.name_snapshot, "specialization": proposal.specialization_snapshot,
        "designation": proposal.designation_snapshot, "email": proposal.email_snapshot,
        "phone": proposal.phone_snapshot, "institution": proposal.institution_snapshot,
    }
    new_email = _normalize_email(body.email)
    _reject_avfu_email(new_email)
    duplicate = next((p for p in cycle.proposals if p.id != proposal.id and p.email_snapshot == new_email), None)
    if duplicate:
        raise HTTPException(400, f"The same examiner email ({new_email}) is already proposal slot {duplicate.slot_number} in this cycle.")
    new = {
        "name": body.name, "specialization": body.specialization, "designation": body.designation,
        "email": new_email, "phone": body.phone, "institution": body.institution,
    }

    proposal.name_snapshot, proposal.specialization_snapshot, proposal.designation_snapshot = new["name"], new["specialization"], new["designation"]
    proposal.phone_snapshot, proposal.institution_snapshot = new["phone"], new["institution"]
    if new_email != old["email"]:
        proposal.email_snapshot = new_email
        proposal.examiner_id = await _resolve_examiner_id(new_email, db)
    proposal.edited_by = user.id
    proposal.edited_at = _now()

    db.add(AuditLog(
        user_id=user.id, action="external_examiner.incharge_edit_proposal", entity_type="external_examiner_proposal",
        entity_id=str(proposal.id), old_value=old, new_value=new,
        metadata_={"selection_id": str(s.id), "cycle_id": str(cycle.id), "slot_number": proposal.slot_number},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"), role_context=user.active_role.value,
    ))
    await db.commit()
    return await _selection_dict(await _get_selection(selection_id, db), user, db)


# ── Submit is folded into create_selection (Section 10 — no separate draft flow) ──
# ── OTP / approve / revert ───────────────────────────────────────────────────

def _send_otp_email(to: str, code: str) -> None:
    from app.core.email import send_email
    send_email(
        to, "AVFU AMS — External Examiner Approval OTP",
        f"Your OTP for External Examiner Selection approval is: {code}\n\nThis code is valid for {_OTP_TTL_MINUTES} minutes. "
        "If you did not request this, you can safely ignore this email.",
    )


@router.get("/{selection_id}/approval/otp")
async def request_approval_otp(selection_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    s = await _get_selection(selection_id, db)
    stage = await _require_my_stage(s, user, db)
    if stage.stage_type not in _SIGNATORY_STAGES:
        raise HTTPException(400, "The Incharge Academic Cell approval is a workflow action and does not need an OTP.")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    db.add(ExternalExaminerSignature(approval_stage_id=stage.id, user_id=user.id, otp_code=code, otp_expires_at=_now() + timedelta(minutes=_OTP_TTL_MINUTES)))
    await db.commit()
    _send_otp_email(user.email, code)
    response = {"message": "OTP sent to your email."}
    if _is_dev_environment():
        response["dev_otp"] = code
    return response


async def _consume_otp(stage: ExternalExaminerApprovalStage, user: User, otp: str, request: Request, db: AsyncSession) -> None:
    sig = (await db.execute(
        select(ExternalExaminerSignature).where(
            ExternalExaminerSignature.approval_stage_id == stage.id, ExternalExaminerSignature.user_id == user.id,
            ExternalExaminerSignature.otp_used == False, ExternalExaminerSignature.otp_expires_at > _now(),  # noqa: E712
        ).order_by(ExternalExaminerSignature.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sig:
        raise HTTPException(400, "No valid OTP found. Please request a new OTP.")
    if not otp or not secrets.compare_digest(otp, sig.otp_code or ""):
        raise HTTPException(400, "Invalid OTP.")
    sig.otp_used = True
    sig.verified_at = _now()
    sig.ip_address = request.client.host if request.client else None
    sig.user_agent = request.headers.get("user-agent")


async def _lock_and_reload(s: ExternalExaminerSelection, stage: ExternalExaminerApprovalStage, cycle: ExternalExaminerApprovalCycle, db: AsyncSession) -> list[ExternalExaminerApprovalStage]:
    """Serialize concurrent actions: row-lock the selection, then re-read its status
    and each stage's status from the database, and re-validate the caller's stage
    is still current — identical mechanism to Synopsis's `_lock_and_reload`."""
    await db.execute(select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.id == s.id).with_for_update())
    current_status = (await db.execute(select(ExternalExaminerSelection.status).where(ExternalExaminerSelection.id == s.id))).scalar_one()
    stages = list(cycle.stages)
    for st in stages:
        await db.refresh(st, attribute_names=["status"])
    mine = next((x for x in stages if x.id == stage.id), None)
    if not mine or mine.status != "pending" or _PHASE_FOR_STAGE.get(mine.stage_type) != current_status:
        raise HTTPException(409, "This External Examiner Selection has already moved on; please reload.")
    s.status = current_status
    return stages


def _record_action(stage: ExternalExaminerApprovalStage, user: User, status: str, now: datetime, remark: Optional[str] = None) -> None:
    stage.status = status
    stage.approver = user
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id
    stage.acted_at = now
    if remark is not None:
        stage.remark = remark


@router.post("/{selection_id}/approval/approve")
async def approve_stage(
    selection_id: UUID, body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    s = await _get_selection(selection_id, db)
    stage = await _require_my_stage(s, user, db)
    cycle = _active_cycle(s)
    stages = await _lock_and_reload(s, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()

    if stage.stage_type in _SIGNATORY_STAGES:
        await _consume_otp(stage, user, body.otp or "", request, db)
    _record_action(stage, user, "approved", now)

    next_status = {"major_advisor": "hod_pending", "hod": "incharge_pending", "incharge_academic_cell": "dpgs_pending", "dpgs": "vc_pending"}.get(stage.stage_type)
    if next_status:
        s.status = next_status
    elif stage.stage_type == "vc":
        # VC "approving" their own stage without ever calling vc-selection is not a
        # valid path — the count/foreign-id checks only exist on that endpoint.
        raise HTTPException(400, "Use the VC selection endpoint to complete this stage.")

    await db.commit()
    return {"message": f"{stage.role_label} approved.", "status": s.status}


@router.post("/{selection_id}/approval/revert")
async def revert_stage(
    selection_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    """Revert to the Major Advisor — UNCONDITIONALLY, from HOD, Incharge, DPGS, or
    VC alike (confirmed rule; no per-stage alternate destination). A remark is
    REQUIRED; the cycle's other pending stages are cancelled; the Major Advisor's
    stage itself is left untouched (already `approved`) so its history is visible,
    while the SELECTION status returns to `major_advisor_pending` so a resubmit
    creates the next cycle from scratch."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    s = await _get_selection(selection_id, db)
    stage = await _require_my_stage(s, user, db)
    if stage.stage_type == "major_advisor":
        raise HTTPException(400, "The Major Advisor stage has no earlier stage to revert to.")
    cycle = _active_cycle(s)
    stages = await _lock_and_reload(s, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()
    _record_action(stage, user, "reverted", now, remark)
    for other in stages:
        if other.id != stage.id and other.status == "pending":
            other.status = "cancelled"
    cycle.status = "reverted"
    cycle.reverted_at = now
    cycle.revert_remark = remark
    s.status = "reverted"
    await db.commit()
    return {"message": "Reverted to the Major Advisor for correction.", "status": s.status}


# ── VC selection (idempotent, count-checked, confidentiality-aware) ──────────

def _build_first_time_examiner_email(examiner_name: str, student_name: str, department_name: str, degree_name: str, college_name: str, ams_login_url: str, examiner_email: str, temporary_password: str) -> tuple[str, str]:
    """Dedicated External Examiner template — NOT the Orientation template. Never
    mentions an internal workflow stage (Major Advisor/HOD/Incharge/DPGS)."""
    subject = "AVFU AMS — External Examiner Assignment for Thesis Evaluation"
    body = (
        f"Dear {examiner_name},\n\n"
        f"You have been selected as an External Examiner for the thesis evaluation of {student_name}, "
        f"a {degree_name} student of the Department of {department_name}, {college_name}, "
        "Assam Veterinary and Fishery University.\n\n"
        "An account has been created for you on the AVFU Academic Management System (AMS) to support this assignment.\n\n"
        f"AMS Login URL: {ams_login_url}\n"
        f"Username: {examiner_email}\n"
        f"Temporary Password: {temporary_password}\n\n"
        "For security, you will be required to change this password when you first log in.\n\n"
        "Further instructions regarding the evaluation will be shared through the system in due course.\n\n"
        "Regards,\nAssam Veterinary and Fishery University"
    )
    return subject, body


def _build_existing_examiner_email(examiner_name: str, student_name: str, department_name: str, degree_name: str, college_name: str, ams_login_url: str) -> tuple[str, str]:
    """Existing-account notification. Deliberately accepts NO password/credential
    parameter at all — the function signature itself makes it impossible to
    accidentally include one (Section 25 of the confirmed rules)."""
    subject = "AVFU AMS — New External Examiner Assignment"
    body = (
        f"Dear {examiner_name},\n\n"
        f"You have been selected as an External Examiner for the thesis evaluation of {student_name}, "
        f"a {degree_name} student of the Department of {department_name}, {college_name}, "
        "Assam Veterinary and Fishery University.\n\n"
        "Please log in to the Assam Veterinary and Fishery University Academic Management System (AMS) "
        "using your existing credentials to view this assignment.\n\n"
        f"AMS Login URL: {ams_login_url}\n\n"
        "Further instructions regarding the evaluation will be shared through the system in due course.\n\n"
        "Regards,\nAssam Veterinary and Fishery University"
    )
    return subject, body


async def _resolve_or_create_examiner_account(proposal: ExternalExaminerProposal, db: AsyncSession) -> tuple[ExternalExaminer, User, Optional[str]]:
    """Idempotent, race-safe resolve-or-create of the reusable examiner identity AND
    its AMS account. Returns (examiner, user, temp_password) — `temp_password` is
    the plaintext password ONLY when a new account was just created (never stored,
    never logged, held in memory only long enough to be handed to `enqueue_email`
    below); it is `None` when an existing account was reused, so it structurally
    cannot be resent. Locks the `ExternalExaminer` row so two near-simultaneous
    VC-selection requests for the same examiner can never create two accounts."""
    normalized = proposal.email_snapshot
    existing = (await db.execute(
        select(ExternalExaminer).options(selectinload(ExternalExaminer.user)).where(ExternalExaminer.email == normalized).with_for_update()
    )).scalar_one_or_none()
    if existing and existing.user_id:
        return existing, existing.user, None

    examiner = existing
    if examiner is None:
        examiner = ExternalExaminer(email=normalized)
        db.add(examiner)
        await db.flush()

    temp_password = generate_temp_password()
    examiner_user = User(
        email=normalized, hashed_password=hash_password(temp_password),
        first_name=proposal.name_snapshot, role=UserRole.EXTERNAL_EXAMINER,
        designation=proposal.designation_snapshot, mobile=proposal.phone_snapshot,
        is_active=True, is_verified=True, must_change_password=True,
    )
    db.add(examiner_user)
    await db.flush()
    examiner.user_id = examiner_user.id
    return examiner, examiner_user, temp_password


@router.post("/{selection_id}/vc-selection")
async def vc_select_examiners(
    selection_id: UUID, body: VcSelectionIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.VICE_CHANCELLOR)),
):
    """The Vice Chancellor's final selection — count-checked from the selection's
    snapshotted degree level (never the client), every `proposal_id` verified to
    belong to THIS selection's current cycle (a foreign or duplicate id is
    rejected outright), and fully idempotent: a retried identical request returns
    the already-recorded result without creating anything a second time."""
    s = await _get_selection(selection_id, db)
    latest = _latest_cycle(s)
    if latest and latest.vc_selection_completed_at is not None:
        # Idempotent replay, checked BEFORE `_require_my_stage`: once completed,
        # the VC's own stage is no longer "pending" (see the fix below), so the
        # stage lookup would otherwise refuse a retried identical request with a
        # 403 instead of replaying the result — defeating the idempotency this
        # endpoint is supposed to guarantee. The completion marker is checked
        # first, independent of stage/cycle status, so a replay always succeeds.
        return await _selection_dict(s, user, db)
    stage = await _require_my_stage(s, user, db)
    if stage.stage_type != "vc":
        raise HTTPException(403, "This selection is not awaiting Vice Chancellor selection.")
    cycle = _active_cycle(s)

    await db.execute(select(ExternalExaminerApprovalCycle.id).where(ExternalExaminerApprovalCycle.id == cycle.id).with_for_update())
    await db.refresh(cycle, attribute_names=["vc_selection_completed_at"])
    if cycle.vc_selection_completed_at is not None:
        # Idempotent replay: the selection was already completed (this exact request,
        # or an earlier one) — return the current state rather than erroring or
        # redoing any work.
        return await _selection_dict(await _get_selection(selection_id, db), user, db)

    required = REQUIRED_SELECTION_COUNT[s.degree_level]
    ids = list(dict.fromkeys(body.proposal_ids))  # de-duplicate while preserving order, then re-check below
    if len(ids) != len(body.proposal_ids):
        raise HTTPException(400, "The same examiner cannot be selected twice.")
    if len(ids) != required:
        raise HTTPException(400, f"{s.degree_level} requires selecting exactly {required} examiner(s); {len(ids)} were given.")

    by_id = {p.id: p for p in cycle.proposals}
    chosen = []
    for pid in ids:
        proposal = by_id.get(pid)
        if not proposal:
            raise HTTPException(400, "One or more selected examiners do not belong to this selection's current proposal list.")
        chosen.append(proposal)

    now = _now()
    ams_url = settings.AMS_FRONTEND_URL.rstrip("/") + "/login"
    student_name = s.student.full_name
    department_name = s.student.department.name if s.student.department else "—"
    college_name = s.student.college.name if s.student.college else "—"
    degree_name = s.student.program.name if s.student.program else s.degree_level

    for proposal in chosen:
        result = ExternalExaminerSelectionResult(cycle_id=cycle.id, proposal_id=proposal.id)
        db.add(result)
        await db.flush()
        examiner, examiner_user, temp_password = await _resolve_or_create_examiner_account(proposal, db)
        db.add(ExternalExaminerAssignment(examiner_id=examiner.id, student_id=s.student_id, selection_result_id=result.id, status="active"))

        examiner_name = examiner_user.full_name if examiner_user.full_name else proposal.name_snapshot
        if temp_password is not None:
            subject, mail_body = _build_first_time_examiner_email(
                examiner_name, student_name, department_name, degree_name, college_name, ams_url, examiner.email, temp_password,
            )
        else:
            subject, mail_body = _build_existing_examiner_email(examiner_name, student_name, department_name, degree_name, college_name, ams_url)
        enqueue_email(db, examiner.email, subject, mail_body)

    cycle.vc_selection_completed_at = now
    cycle.completed_at = now
    cycle.status = "approved"
    # The VC's own approval stage was never being marked "approved" here (every
    # other stage type is recorded via `_record_action` in `approve_stage`) —
    # left permanently "pending", which kept `cycle.status` stuck at "active"
    # forever after a full approval (nothing else ever advanced it), which in
    # turn kept `_assert_no_external_examiner_under_approval` (research.py)
    # blocking Major Advisor/committee changes even on a FULLY APPROVED
    # selection, not just one still genuinely under approval.
    _record_action(stage, user, "approved", now)
    s.status = "approved"
    try:
        await db.commit()
    except IntegrityError:
        # Race backstop: a concurrent identical/near-identical request committed first.
        await db.rollback()
        return await _selection_dict(await _get_selection(selection_id, db), user, db)
    return await _selection_dict(await _get_selection(selection_id, db), user, db)
