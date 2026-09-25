"""Student & Teacher Enrollment Management (Modules 5.3, 5.4).

Course Registration workflow (BUSINESS_LOGIC.md D.5, C.8; STUDENT_SIDE_IMPLEMENTATION_PLAN.md
Section 34 CR-3+CR-4, Registration Card task this revision):

    Student selects/adds courses (1+ per semester, may add more over time)
    -> per selected course: any ONE assigned Course Teacher approves/reverts
    -> once ALL currently-active selected courses have cleared: registration
       reaches `card_pending` — student may still add/withdraw here
    -> Student explicitly submits the Registration Card (locks editing)
    -> Major Advisor approves/reverts
    -> HOD approves/reverts
    -> [I/C Academic Cell -> DPGS: NOT implemented this phase, see below]

SCOPE NOTE (mirrors Advisory Committee P0's identical, already-shipped
reasoning): I/C Academic Cell and DPGS stages are NOT implemented — no
documented demo-role mitigation exists for either. A registration's workflow
terminates at `hod_approved` in this revision, an explicit, honest P0 stopping
point, not a silent omission.

REVERT ASSUMPTION (BUSINESS_LOGIC.md Open Question 44 — the exact multi-course
revert-scope rule is NOT confirmed by AVFU; verification-audit Finding 1 fix):
a Course-Teacher revert and a Major-Advisor revert now behave identically in
shape — both send the WHOLE registration back to `teacher_pending` and reset
EVERY selected course's `StudentEnrollment` row back to `pending`, so the
registration is always actionable again after any revert (no dead end) and no
course can be left showing "Approved" while the registration shows
"Reverted". This mirrors Advisory Committee P0's "revert sends the whole
committee back to an actionable stage" precedent. This is a documented
implementation default, not a confirmed AVFU business rule — see
`_apply_enrollment_decision`'s docstring for the exact mechanics.

REGISTRATION CARD TASK (this revision — supersedes the old one-shot-only
design and the "Course Registration Card generation is explicitly deferred"
note that used to be here):
- `CourseRegistration.stage` gains one new value, `card_pending` (see the
  model's own docstring) — the registration remains STUDENT-EDITABLE in both
  `teacher_pending` and `card_pending` (`_EDITABLE_STAGES` below); it becomes
  locked the moment the student explicitly calls
  `POST /registrations/{id}/submit`, which is the ONLY way to reach
  `major_advisor_pending` now (Course-Teacher approval alone no longer
  auto-advances a registration into the Major-Advisor stage — it only reaches
  the editable `card_pending` "ready to submit" state).
- The existing `(student_id, semester_id)` uniqueness on `CourseRegistration`
  is UNCHANGED and is never dropped — `register_courses` now creates a new
  registration only the FIRST time; subsequent calls for the same semester
  detect and reuse the existing row (see its docstring below).
- A 20-active-credit-per-semester cap (`pending` + `approved`, never
  `withdrawn`) is enforced server-side on every addition
  (`_semester_active_credits`).
- Course-Teacher-approved courses may now be withdrawn via a new, dedicated
  `WithdrawalRequest` record (student requests with a mandatory reason ->
  Course Teacher approves/rejects) rather than being silently marked
  withdrawn by the student's own action — see `request_withdrawal`/
  `decide_withdrawal_request`. A still-`pending` course may instead be
  withdrawn directly by the student (no teacher approval needed) via the
  existing `DELETE /{enrollment_id}`, now permitted for registration-linked
  rows specifically while `status == "pending"` AND the registration is still
  editable.
- A Registration Card PDF is rendered on-demand (never stored) via
  `GET /registrations/{id}/document`, reusing the AMS-owned Playwright/
  Chromium pipeline built for PPW (`app/utils/pdf.py`) — no new PDF
  infrastructure was introduced.
"""
import logging
import re
import urllib.parse
from typing import Optional, List, Dict
from uuid import UUID
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, field_validator

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles, require_advisory_committee_established, find_role_holder_in_department
from app.models.user import User, UserRole, Program, Department
from app.models.enrollment import StudentEnrollment, CourseRegistration, WithdrawalRequest
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.academic import AcademicCalendar, Semester
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.core.major_advisor import resolve_accepted_major_advisor
# Programme<->Department many-to-many redesign — single shared student-scope
# resolvers (app/core/student_scope.py), re-exported under this file's
# existing private names so every call site below is unchanged.
from app.core.student_scope import (
    resolve_student_scope as _resolve_student_scope,
    resolve_student_department_id as _student_department_id,
)
# Major/Minor/Supporting discipline validation (this task) — shared with PPW
# (ppw.py); see app/core/classification.py's docstring for the full rules.
# PPW_CLASSIFICATIONS is re-exported from there (originally defined on
# app.models.ppw) so Course Registration's classification field uses the
# exact same controlled vocabulary, never a second/duplicated one.
from app.core.classification import (
    ClassificationError, LPM_DEPARTMENT_CODE, PPW_CLASSIFICATIONS, get_lpm_department_id, validate_major, validate_minor, validate_supporting,
)

router = APIRouter(prefix="/enrollment", tags=["Enrollment"])
logger = logging.getLogger(__name__)

# BUSINESS_LOGIC.md C.8 status vocabulary.
_REGISTRATION_STAGE_LABELS = {
    "teacher_pending": "Course Teacher Approval Pending",
    "card_pending": "Ready for Registration Card Submission",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "hod_approved": "HOD Approved",
    # Incharge Academic Cell / DPGS task (this revision) — HOD approval no
    # longer terminates the chain (Section 12); it continues through the two
    # global roles above HOD.
    "incharge_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending",
    "dpgs_approved": "DPGS Approved (Final)",
    "reverted": "Reverted",
}
_ENROLLMENT_STATUS_LABELS = {
    "pending": "Course Teacher Approval Pending",
    "approved": "Course Teacher Approved",
    "reverted": "Reverted",
    "withdrawn": "Withdrawn",
    "rejected": "Rejected",  # legacy value only — never written by new code (Rule 35)
}
_WITHDRAWAL_STATUS_LABELS = {
    "pending": "Withdrawal Request Pending",
    "approved": "Withdrawal Approved",
    "rejected": "Withdrawal Rejected",
}

# The only two stages in which a student may still add/remove courses or
# request a withdrawal (Section 6/2 of this task's confirmed requirement).
# Every later stage (major_advisor_pending/hod_pending/hod_approved) is
# permanently locked from the student's side for this cycle. `reverted` is
# NOT included here — in this codebase a revert always writes the registration
# directly back to `teacher_pending` (never a literal "reverted" stage value,
# see `_apply_enrollment_decision`'s docstring), so `reverted` never actually
# needs to appear in this set for editability purposes.
_EDITABLE_STAGES = ("teacher_pending", "card_pending")

_MAX_SEMESTER_CREDITS = 20




async def _get_major_advisor_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    """Resolve a student's Major Advisor from the established Advisory Committee
    (research.py's proven pattern — reused, not duplicated)."""
    result = await db.execute(
        select(CommitteeMember.faculty_id)
        .join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .where(AdvisoryCommittee.student_id == student_id, CommitteeMember.role == "major_advisor")
    )
    return result.scalar_one_or_none()


async def _authorize_offering_management(
    offering_id: UUID, user: User, db: AsyncSession, *, student_id: Optional[UUID] = None,
) -> CourseOffering:
    """Faculty/HOD/admin authorization for managing enrollments of a given offering.
    SUPER_ADMIN: unrestricted.
    HOD: department-wide (current_user.department_id == offering.department_id), no OfferingFaculty needed.
    FACULTY: an OfferingFaculty row exists for (offering_id, user.id) — OR, when acting on a
    SPECIFIC student's own enrollment (`student_id` passed by the caller), that student's
    Research Course enrollment has this faculty member as its `instructor_id` (the per-student
    Major Advisor assignment — see app/core/major_advisor.py). This fallback deliberately never
    grants offering-wide access: a Research Course instructor for Student X gains no authority
    over Student Y's enrollment merely because both are in the same offering — the caller must
    supply the exact student being acted on."""
    offering = await db.get(CourseOffering, offering_id)
    if not offering:
        raise HTTPException(404, "Offering not found.")
    if user.active_role == UserRole.SUPER_ADMIN:
        return offering
    if user.active_role == UserRole.HOD:
        if user.active_department_id and offering.department_id and user.active_department_id == offering.department_id:
            return offering
        raise HTTPException(403, "You can only manage offerings within your own department.")
    if user.active_role == UserRole.FACULTY:
        result = await db.execute(select(OfferingFaculty).where(
            OfferingFaculty.offering_id == offering_id, OfferingFaculty.faculty_id == user.id,
        ))
        if result.scalar_one_or_none():
            return offering
        if student_id is not None:
            research_link = await db.execute(select(StudentEnrollment.id).where(
                StudentEnrollment.offering_id == offering_id,
                StudentEnrollment.student_id == student_id,
                StudentEnrollment.instructor_id == user.id,
            ))
            if research_link.scalar_one_or_none():
                return offering
        raise HTTPException(403, "You are not assigned to this offering.")
    raise HTTPException(403, "Insufficient permissions.")


async def _authorize_major_advisor(registration: CourseRegistration, user: User, db: AsyncSession) -> None:
    if user.active_role == UserRole.SUPER_ADMIN:
        return
    ma_id = await _get_major_advisor_id(registration.student_id, db)
    if ma_id and ma_id == user.id:
        return
    raise HTTPException(403, "Only this student's Major Advisor (or an administrator) may act on this registration.")


async def _authorize_hod_registration(registration: CourseRegistration, user: User, db: AsyncSession) -> None:
    if user.active_role == UserRole.SUPER_ADMIN:
        return
    if user.active_role == UserRole.HOD:
        dept_id = await _student_department_id(registration.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
        raise HTTPException(403, "You can only approve registrations for students in your own department.")
    raise HTTPException(403, "Only the student's HOD (or an administrator) may act on this registration.")


async def _authorize_registration_view(registration: CourseRegistration, user: User, db: AsyncSession) -> None:
    if user.active_role in (UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
        return
    if user.active_role == UserRole.STUDENT:
        if registration.student_id == user.id:
            return
        raise HTTPException(403, "You can only view your own registration.")
    if user.active_role == UserRole.HOD:
        dept_id = await _student_department_id(registration.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
        raise HTTPException(403, "You can only view registrations within your own department.")
    ma_id = await _get_major_advisor_id(registration.student_id, db)
    if ma_id and ma_id == user.id:
        return
    if user.active_role == UserRole.FACULTY:
        result = await db.execute(
            select(OfferingFaculty.id)
            .join(StudentEnrollment, StudentEnrollment.offering_id == OfferingFaculty.offering_id)
            .where(OfferingFaculty.faculty_id == user.id, StudentEnrollment.registration_id == registration.id)
            .limit(1)
        )
        if result.scalar_one_or_none():
            return
    raise HTTPException(403, "You are not authorized to view this registration.")


async def _revert_registration_to_student(r: CourseRegistration, remark: str, db: AsyncSession) -> None:
    """Incharge Academic Cell / DPGS task (Section 15/16) — a revert from
    EITHER global stage returns the registration all the way to the very
    first stage (never "one level back", unlike the pre-existing Major-
    Advisor/HOD reverts), resetting every selected course exactly like the
    existing Major Advisor revert already does, so a resubmission always
    replays the identical original approval chain (Course Teacher -> Major
    Advisor -> HOD -> Incharge -> DPGS) — never a shortcut back to DPGS.
    Clears `dpgs_approved_by`/`dpgs_approved_at` so a prior cycle's
    signature can never be mistaken for the new cycle's (Section 17)."""
    r.stage = "teacher_pending"
    r.revert_remark = remark
    r.reverted_at = datetime.now(timezone.utc)
    r.dpgs_approved_by = None
    r.dpgs_approved_at = None
    items = (await db.execute(select(StudentEnrollment).where(StudentEnrollment.registration_id == r.id))).scalars().all()
    for item in items:
        item.status = "pending"; item.processed_by = None; item.processed_at = None; item.remarks = None


def _ensure_registration_editable(registration: CourseRegistration) -> None:
    """The single, shared lock check (Section 2/6's explicit requirement —
    backend-enforced, never a frontend-only hidden button) used by every
    student-facing mutation: add courses, withdraw a pending course, request
    withdrawal of an approved course. Once a registration leaves
    `_EDITABLE_STAGES` (i.e. the Registration Card has been submitted), this
    always raises — there is no code path, direct-ID or otherwise, that
    bypasses this single check, since every mutating endpoint below calls it
    against the registration loaded fresh from the database (never a
    client-supplied stage/flag)."""
    if registration.stage not in _EDITABLE_STAGES:
        raise HTTPException(400, "This registration has already been submitted and cannot be modified.")


async def _semester_active_credits(student_id: UUID, semester_id: UUID, db: AsyncSession) -> int:
    """Live sum of `Course.total_credits` for this student's `pending` +
    `approved` StudentEnrollment rows in this semester (Section 7/8's
    confirmed 20-credit rule) — `withdrawn` (and the legacy `rejected` value)
    never count. Deliberately scoped by the offering's OWN `semester_id`
    (not by `registration_id`) so it also correctly counts any legacy,
    non-registration-linked enrollment a student might still have for this
    semester (the older single-offering `POST /enrollment` path writes the
    same `ams_student_enrollments` table) — this is a defense-in-depth choice,
    not an assumption that legacy enrollment is still an active flow. Always
    computed fresh from the database on every call — no cached/stored total
    column exists or is introduced."""
    result = await db.execute(
        select(StudentEnrollment)
        .join(CourseOffering, StudentEnrollment.offering_id == CourseOffering.id)
        .options(selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course))
        .where(
            StudentEnrollment.student_id == student_id,
            CourseOffering.semester_id == semester_id,
            StudentEnrollment.status.in_(("pending", "approved")),
        )
    )
    return sum(
        e.offering.course.total_credits
        for e in result.scalars().all()
        if e.offering and e.offering.course
    )


async def _recompute_card_readiness(registration_id: UUID, db: AsyncSession) -> None:
    """Recompute `CourseRegistration.stage` between `teacher_pending` and
    `card_pending` based on the CURRENT, live status of every non-withdrawn
    active enrollment for this student's SEMESTER — never a value trusted
    from anywhere else, and never scoped to `registration_id`-linked rows
    alone (bug fix, this revision: a student whose only remaining active
    courses are legacy rows with `registration_id IS NULL` — e.g. after
    withdrawing every registration-linked pending course — would otherwise
    have those legacy-approved courses silently ignored here, incorrectly
    leaving the registration stuck at `teacher_pending` even though nothing
    is actually pending anymore; this uses the same semester-scoped
    population `_registration_dict`'s Selected Courses list and
    `_semester_active_credits`'s 20-credit enforcement already use, so all
    three can never disagree again). Called after every event that can
    change whether "all currently-active selected courses are Course-Teacher
    approved" is true: a teacher's approve/revert decision, a student's
    pending-course withdrawal, a student adding a new (pending) course, and
    an approved withdrawal request being granted. A no-op (by design, not by
    accident) if the registration is already locked (`major_advisor_pending`
    and beyond) — this function must never be able to un-lock or re-lock a
    submitted registration; only `submit_registration_card` moves a
    registration out of `_EDITABLE_STAGES`, and nothing moves it back in
    this phase."""
    registration = await db.get(CourseRegistration, registration_id)
    if not registration or registration.stage not in _EDITABLE_STAGES:
        return
    all_items = await _semester_scoped_enrollments(registration.student_id, registration.semester_id, db)
    active = [e for e in all_items if e.status != "withdrawn"]
    registration.stage = "card_pending" if active and all(e.status == "approved" for e in active) else "teacher_pending"


class EnrollRequest(BaseModel):
    offering_id: UUID

class RegisterCoursesRequest(BaseModel):
    calendar_id: UUID
    semester_id: UUID
    offering_ids: List[UUID]
    # Major/Minor/Supporting discipline task (this revision) — OPTIONAL,
    # keyed by offering_id (as a string; UUID dict keys aren't valid JSON, so
    # the client sends string keys and pydantic parses them back to UUID via
    # the field's own type). Omitted or missing entries mean "no
    # classification" — completely backward compatible with any existing
    # caller that never sends this at all (Course Registration's classic use
    # case — plain course selection with no discipline concept — is
    # untouched). Every value present is independently re-validated
    # server-side in `register_courses` against the actual offering's
    # department and the student's own/already-selected departments — never
    # trusted at face value.
    classifications: Optional[Dict[UUID, str]] = None

    @field_validator("classifications")
    @classmethod
    def _valid_classifications(cls, v: Optional[Dict[UUID, str]]) -> Optional[Dict[UUID, str]]:
        if v is None:
            return v
        for value in v.values():
            if value not in PPW_CLASSIFICATIONS:
                raise ValueError(f"classification must be one of: {', '.join(PPW_CLASSIFICATIONS)}")
        return v

class BulkApproveRequest(BaseModel):
    enrollment_ids: List[UUID]
    # Legacy (non-registration) rows: any status accepted, unchanged.
    # Registration-linked rows: restricted to approved/reverted by
    # _apply_enrollment_decision — see Finding 2 fix.
    status: str
    remarks: Optional[str] = None

class StageDecisionIn(BaseModel):
    approved: bool
    remark: Optional[str] = None

class WithdrawalRequestIn(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("A reason is required to request withdrawal.")
        return v.strip()

class WithdrawalDecisionIn(BaseModel):
    approved: bool
    remark: Optional[str] = None


def _enroll_dict(e: StudentEnrollment, registration: Optional[CourseRegistration] = None) -> dict:
    # Withdrawal-request task: surface the MOST RECENT request (relationship
    # is already ordered newest-first) so the frontend can show "Withdrawal
    # Request: Pending/Rejected" without a second round-trip. A resolved
    # (approved/rejected) older request never hides a legitimate later one —
    # only the single most recent request is ever shown, matching "do not
    # allow repeated withdrawal requests while one is pending" (a new request
    # is blocked while one is pending, so there is only ever one "current"
    # request worth surfacing at a time).
    latest_wr = e.withdrawal_requests[0] if e.withdrawal_requests else None
    # Combined status label (My Courses / Course Registration task): once a
    # course's own Course-Teacher stage is "approved" AND its registration has
    # moved past the student-editable stages, the more specific
    # registration-level stage is the more informative thing to show for that
    # course row — never a value invented beyond what the registration's own
    # authoritative `stage` already says.
    combined_label = _ENROLLMENT_STATUS_LABELS.get(e.status, e.status)
    if e.status == "approved" and registration is not None and registration.stage not in _EDITABLE_STAGES:
        combined_label = _REGISTRATION_STAGE_LABELS.get(registration.stage, registration.stage)
    return {
        "id": str(e.id),
        "student_id": str(e.student_id),
        "student_name": e.student.full_name if e.student else None,
        "student_roll": e.student.student_roll if e.student else None,
        "student_email": e.student.email if e.student else None,
        "offering_id": str(e.offering_id),
        "course_number": e.offering.course.course_number if e.offering and e.offering.course else None,
        "course_title": e.offering.course.title if e.offering and e.offering.course else None,
        "credit_structure": e.offering.course.credit_structure if e.offering and e.offering.course else None,
        "credits": e.offering.course.total_credits if e.offering and e.offering.course else 0,
        "category": e.offering.course.category if e.offering and e.offering.course else None,
        "credit_type": e.offering.course.credit_type if e.offering and e.offering.course else None,
        # On-screen Registration Card Preview task (this revision) — mirrors
        # the PDF's own "Course Instructor" column; every existing caller of
        # `_enroll_dict` already eager-loads `offering.faculty_assignments`
        # (see `_semester_scoped_enrollments` and `offering_enrollments`'s
        # query below), so this never introduces a lazy-load.
        "instructors": [fa.faculty.full_name for fa in e.offering.faculty_assignments if fa.faculty] if e.offering else [],
        # Research Course task — the per-STUDENT instructor (this student's
        # accepted Major Advisor, snapshotted at registration time), distinct
        # from the offering-wide `instructors` list above, which stays empty
        # for every Research Course offering. Null for every non-Research
        # Course enrollment. Every caller below now eager-loads
        # `StudentEnrollment.instructor`.
        "research_instructor_id": str(e.instructor_id) if e.instructor_id else None,
        "research_instructor_name": e.instructor.full_name if e.instructor else None,
        # Major/Minor/Supporting discipline task (this revision) —
        # `classification` is the new per-selection field (see
        # StudentEnrollment.classification's docstring); `department_id`
        # mirrors PPW's own per-course `department_id`/`department_name`
        # pair, sourced from the OFFERING's department (Course Registration
        # is offering-based — see enrollment.py's module docstring — unlike
        # PPW, which has no offering concept and uses `Course.department_id`
        # directly).
        "classification": e.classification,
        "department_id": str(e.offering.department_id) if e.offering and e.offering.department_id else None,
        "department_name": e.offering.department.name if e.offering and e.offering.department else None,
        "registration_id": str(e.registration_id) if e.registration_id else None,
        "status": e.status,
        "status_label": combined_label,
        "enrolled_at": e.enrolled_at.isoformat(),
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
        "remarks": e.remarks,
        "withdrawal_request": ({
            "id": str(latest_wr.id),
            "status": latest_wr.status,
            "status_label": _WITHDRAWAL_STATUS_LABELS.get(latest_wr.status, latest_wr.status),
            "reason": latest_wr.reason,
            "decision_remark": latest_wr.decision_remark,
            "requested_at": latest_wr.requested_at.isoformat(),
            "decided_at": latest_wr.decided_at.isoformat() if latest_wr.decided_at else None,
        } if latest_wr else None),
    }


async def _semester_scoped_enrollments(student_id: UUID, semester_id: UUID, db: AsyncSession) -> list["StudentEnrollment"]:
    """Course Registration display-bug fix (this revision) — the authoritative
    source for "this student's selected courses for this semester," scoped by
    the offering's own `semester_id`, exactly mirroring `_semester_active_credits`'s
    existing scope (never a different/duplicated calculation). Deliberately
    NOT scoped by `registration_id`: a legacy `StudentEnrollment` row created
    before the `CourseRegistration` model existed (or via the older
    single-offering `POST /enrollment`) has `registration_id IS NULL` but is
    still a genuinely active, currently-approved/pending course for that
    semester — it must appear in Selected Courses and count toward the
    displayed credit total exactly like a registration-linked row does,
    since the backend's own 20-credit enforcement already counts it (see
    `_semester_active_credits`). Legacy rows are NEVER backfilled with a
    `registration_id` here or anywhere else — this is a read-only query, no
    data is written."""
    result = await db.execute(
        select(StudentEnrollment)
        .join(CourseOffering, StudentEnrollment.offering_id == CourseOffering.id)
        .where(
            StudentEnrollment.student_id == student_id,
            CourseOffering.semester_id == semester_id,
        )
        .options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty),
            # Major/Minor/Supporting discipline task (this revision) — needed
            # both for classification-department validation and for
            # rendering the discipline names (Registration Card/preview).
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.department),
            selectinload(StudentEnrollment.student),
            selectinload(StudentEnrollment.instructor),
            selectinload(StudentEnrollment.withdrawal_requests),
        )
        .order_by(StudentEnrollment.enrolled_at)
    )
    # Dedup by StudentEnrollment.id (Section 7's explicit requirement) — the
    # primary key itself, never course number/title (two distinct offerings
    # can legitimately share similar course metadata). In practice this join
    # cannot return the same row twice, but keying by `.id` makes that
    # guarantee explicit and robust rather than incidental.
    by_id: dict[UUID, StudentEnrollment] = {}
    for e in result.scalars().all():
        by_id[e.id] = e
    return list(by_id.values())


async def _registration_dict(r: CourseRegistration, db: AsyncSession) -> dict:
    # Course Registration display-bug fix (this revision): Selected Courses
    # and the credit total are now BOTH derived from the same semester-scoped
    # population `_semester_active_credits` already uses for enforcement —
    # not from `r.items` (registration-linked rows only). `r.student_id`/
    # `r.semester_id` come from the already-authorized `CourseRegistration`
    # row itself (never a client-supplied id), so this cannot be widened into
    # another student's data by any request parameter.
    all_items = await _semester_scoped_enrollments(r.student_id, r.semester_id, db)
    active = sorted((e for e in all_items if e.status != "withdrawn"), key=lambda e: e.enrolled_at)
    withdrawn = sorted((e for e in all_items if e.status == "withdrawn"), key=lambda e: e.enrolled_at)
    selected_credits = sum(
        e.offering.course.total_credits for e in active if e.offering and e.offering.course
    )
    # On-screen Registration Card Preview task (this revision) — a handful of
    # extra, purely-additive display fields so the frontend can render a
    # PPW-Preview-style on-page mock-up of the printed card without depending
    # on the Chromium/PDF pipeline at all. Computed independently here (not
    # refactored to share code with `_build_registration_card_context`, the
    # PDF's own context builder) so the already-working PDF path is never
    # touched by this addition — a couple of small extra read-only queries,
    # no different from what the PDF path already does for the same data.
    department = await db.get(Department, r.student.department_id) if r.student and r.student.department_id else None
    semester = await db.get(Semester, r.semester_id)
    calendar = await db.get(AcademicCalendar, r.calendar_id)
    ma_id = await _get_major_advisor_id(r.student_id, db)
    major_advisor = await db.get(User, ma_id) if ma_id else None
    hod_dept_id = await _student_department_id(r.student_id, db)
    hod = None
    if hod_dept_id:
        # Multi-role/multi-department task (this revision) — a HOD's
        # assignment department can now differ from their legacy
        # `User.department_id`, so "who is the HOD of this department" is
        # resolved via `UserRoleAssignment`, never the scalar column.
        hod_id = await find_role_holder_in_department(UserRole.HOD, hod_dept_id, db)
        hod = await db.get(User, hod_id) if hod_id else None

    # Major/Minor/Supporting discipline task (this revision) — same
    # derivation approach as PPW's `_ppw_dict` (see its docstring): Major =
    # the student's own department (already resolved above as `department`);
    # Minor = the department of every "minor"-classified active item
    # (validated single by `register_courses`, so any one of them is
    # representative); Supporting = see below — order-independent bug fix
    # (verification testing found that deriving from insertion order, i.e.
    # "whichever was added second," gives the wrong answer whenever the
    # non-LPM course happens to be added FIRST, and can become ambiguous
    # after a remove+re-add cycle; see app/core/classification.py's
    # docstring for the full investigation).
    minor_items = [e for e in active if e.classification == "minor"]
    supporting_items = [e for e in active if e.classification == "supporting"]
    minor_discipline_name = (
        minor_items[0].offering.department.name if minor_items and minor_items[0].offering and minor_items[0].offering.department else None
    )
    supporting_discipline_name = None
    if len(supporting_items) >= 2:
        non_lpm = [
            e for e in supporting_items
            if e.offering and e.offering.department and e.offering.department.code != LPM_DEPARTMENT_CODE
        ]
        chosen = non_lpm[0] if non_lpm else supporting_items[0]
        supporting_discipline_name = chosen.offering.department.name if chosen.offering and chosen.offering.department else None

    return {
        "id": str(r.id),
        "student_id": str(r.student_id),
        "student_name": r.student.full_name if r.student else None,
        "student_roll": r.student.student_roll if r.student else None,
        "student_mobile": r.student.mobile if r.student else None,
        "program_name": r.student.program.name if r.student and r.student.program else None,
        "program_level": r.student.program.level if r.student and r.student.program else None,
        "department_name": department.name if department else None,
        "semester_name": semester.name if semester else None,
        "academic_year": calendar.academic_year if calendar else None,
        "major_advisor_name": major_advisor.full_name if major_advisor else None,
        "hod_name": hod.full_name if hod else None,
        # Major/Minor/Supporting discipline task (this revision).
        "major_discipline_name": department.name if department else None,
        "minor_discipline_name": minor_discipline_name,
        "supporting_discipline_name": supporting_discipline_name,
        "semester_id": str(r.semester_id),
        "calendar_id": str(r.calendar_id),
        "stage": r.stage,
        "status_label": _REGISTRATION_STAGE_LABELS.get(r.stage, r.stage),
        "is_editable": r.stage in _EDITABLE_STAGES,
        "is_card_ready": r.stage == "card_pending",
        "revert_remark": r.revert_remark,
        "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
        "submitted_at": r.submitted_at.isoformat(),
        "card_submitted_at": r.card_submitted_at.isoformat() if r.card_submitted_at else None,
        # Authoritative selected-credit total (Section 8) — same population as
        # `items` below, so the two can never disagree. Still not cached
        # anywhere: recomputed on every read from live StudentEnrollment rows.
        "selected_credits": selected_credits,
        "max_credits": _MAX_SEMESTER_CREDITS,
        "items": [_enroll_dict(e, r) for e in active],
        "withdrawn_items": [_enroll_dict(e, r) for e in withdrawn],
    }


_REGISTRATION_LOAD_OPTIONS = (
    selectinload(CourseRegistration.student).selectinload(User.program),
    # Major/Minor/Supporting discipline task (this revision) — Major
    # Discipline is always the student's own department.
    selectinload(CourseRegistration.student).selectinload(User.department),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.offering).selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.offering).selectinload(CourseOffering.department),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.student),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.instructor),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.withdrawal_requests),
)


# ── Student: enroll self (legacy, single-offering) ──────────────────────────────

@router.post("", status_code=201)
async def enroll(
    body: EnrollRequest, db: AsyncSession = Depends(get_db),
    # require_advisory_committee_established composes require_complete_profile,
    # which itself implies require_roles(STUDENT) — see core/dependencies.py.
    # Backend-authoritative Course Registration gate (Section 32 Phase F-4):
    # Student Intake -> Profile Completion -> Advisory Committee -> Course
    # Registration. Other roles are entirely unaffected.
    user: User = Depends(require_advisory_committee_established),
):
    offering = await db.get(CourseOffering, body.offering_id)
    if not offering or offering.status != "published":
        raise HTTPException(400, "Course offering not available for enrollment.")

    # Server-side eligibility authorization — never trust the frontend's course list.
    scope = await _resolve_student_scope(user, db)
    if not scope:
        raise HTTPException(403, "Your academic program is not configured; contact administration.")
    # Course-visibility change: department is no longer part of eligibility —
    # a student may enroll in any department's offering, subject to every
    # OTHER existing rule (program level, published status, capacity,
    # duplicate prevention — all unchanged below).
    course = await db.get(Course, offering.course_id)
    if not course or course.program_level != scope["level"]:
        raise HTTPException(403, "This course offering is not available to your program.")

    # Check capacity
    count_result = await db.execute(
        select(func.count()).select_from(StudentEnrollment).where(
            StudentEnrollment.offering_id == body.offering_id,
            StudentEnrollment.status == "approved",
        )
    )
    if count_result.scalar() >= offering.max_enrollment:
        raise HTTPException(400, "Offering is full.")

    # Prevent duplicate
    existing = await db.execute(
        select(StudentEnrollment).where(
            StudentEnrollment.student_id == user.id,
            StudentEnrollment.offering_id == body.offering_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Already enrolled or request pending.")

    # Research Course task: this offering's OWN instructor is never touched
    # here (Rule #4) — for a Research Course, the AUTHENTICATED student's
    # (never a client-supplied id) accepted Major Advisor is resolved and
    # snapshotted onto THIS enrollment row only. Resolution raises a clear
    # 400 if zero or more than one accepted Major Advisor exists — never
    # silently proceeds with none, never arbitrarily picks one. A client
    # cannot influence this by supplying an instructor_id: `EnrollRequest`
    # has no such field.
    instructor_id = None
    if course.category == "research":
        instructor_id = await resolve_accepted_major_advisor(user.id, db)

    e = StudentEnrollment(student_id=user.id, offering_id=body.offering_id, instructor_id=instructor_id)
    db.add(e); await db.commit()
    return {"message": "Enrollment request submitted.", "id": str(e.id)}


# ── Student: submit / add to a Course Registration (1+ courses, one semester) ──

@router.post("/register", status_code=201)
async def register_courses(
    body: RegisterCoursesRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_advisory_committee_established),
):
    """Registration Card task (this revision) — this endpoint now serves
    BOTH the first submission for a semester AND every subsequent "add more
    courses" call, without ever creating a second `CourseRegistration` row
    for the same `(student_id, semester_id)` (that uniqueness is UNCHANGED
    and is never dropped): if a registration already exists, this appends
    new `StudentEnrollment` rows to it (after re-validating it is still
    editable) instead of attempting a second insert.

    Calendar/semester consistency fix (this revision): `calendar_id` is no
    longer trusted from the client at all — it is derived from the loaded
    `Semester` row's own `calendar_id`, so a `CourseRegistration` can never
    be created with a `calendar_id` inconsistent with its `semester_id`.
    """
    if not body.offering_ids:
        raise HTTPException(400, "Select at least one course.")
    if len(set(body.offering_ids)) != len(body.offering_ids):
        raise HTTPException(400, "Duplicate course selected.")

    scope = await _resolve_student_scope(user, db)
    if not scope:
        raise HTTPException(403, "Your academic program is not configured; contact administration.")

    semester = await db.get(Semester, body.semester_id)
    if not semester:
        raise HTTPException(404, "Semester not found.")
    derived_calendar_id = semester.calendar_id  # authoritative — never body.calendar_id

    # Lock check FIRST (Section 2's explicit "malicious student must not be
    # able to bypass the lock through repeated POST requests" requirement) —
    # checked before any per-offering validation, so a locked registration
    # always gets the unambiguous "already submitted" message regardless of
    # what else might also be wrong with the request body (e.g. a duplicate
    # or invalid offering id would otherwise mask the real reason).
    existing_result = await db.execute(
        select(CourseRegistration).where(
            CourseRegistration.student_id == user.id, CourseRegistration.semester_id == body.semester_id,
        )
    )
    registration = existing_result.scalar_one_or_none()
    if registration:
        # Row-lock this registration for the remainder of the transaction —
        # same concurrency-safety pattern already used by PPW's approve/revert
        # endpoints — so two near-simultaneous "add more courses" calls for
        # the same registration cannot both read a stale credit total and
        # both pass the 20-credit check (Section 8's explicit requirement).
        await db.execute(select(CourseRegistration.id).where(CourseRegistration.id == registration.id).with_for_update())
        registration = await db.get(CourseRegistration, registration.id)
        _ensure_registration_editable(registration)

    # Per-course eligibility, capacity, and duplicate-within-this-request
    # checks — validated before any row is written, mirroring the legacy
    # single-course `enroll()` checks exactly.
    offerings: dict[UUID, CourseOffering] = {}
    courses: dict[UUID, Course] = {}
    requested_credits = 0
    for oid in body.offering_ids:
        offering = await db.get(CourseOffering, oid)
        if not offering or offering.status != "published" or offering.semester_id != body.semester_id:
            raise HTTPException(400, "One or more selected offerings are not available for this semester.")
        # Course-visibility change: department is no longer part of
        # eligibility here either — mirrors the single-offering `enroll()`.
        course = await db.get(Course, offering.course_id)
        if not course or course.program_level != scope["level"]:
            raise HTTPException(403, "This course is not available to your programme level.")
        count_result = await db.execute(
            select(func.count()).select_from(StudentEnrollment).where(
                StudentEnrollment.offering_id == oid, StudentEnrollment.status == "approved",
            )
        )
        if count_result.scalar() >= offering.max_enrollment:
            raise HTTPException(400, f"{course.course_number} is full.")
        dupe = await db.execute(select(StudentEnrollment.id).where(
            StudentEnrollment.student_id == user.id, StudentEnrollment.offering_id == oid,
        ))
        if dupe.scalar_one_or_none():
            raise HTTPException(409, f"You are already registered for {course.course_number}.")
        offerings[oid] = offering
        courses[oid] = course
        requested_credits += course.total_credits

    # Research Course task: resolved ONCE for the whole request (same student,
    # same instant, same transaction) — never per-offering — and applied only
    # to the offerings in THIS batch whose course is a Research Course
    # (`category == "research"`). Raises a clear 400 before any row is
    # written if the student has zero or more than one accepted Major
    # Advisor (see app/core/major_advisor.py); never silently proceeds with
    # a null/arbitrary instructor. This offering's own CourseOffering row is
    # never mutated — the resolved instructor is only ever written onto this
    # student's own StudentEnrollment row(s) below.
    research_offering_ids = {oid for oid, c in courses.items() if c.category == "research"}
    research_instructor_id: Optional[UUID] = None
    if research_offering_ids:
        research_instructor_id = await resolve_accepted_major_advisor(user.id, db)

    # Major/Minor/Supporting discipline task (this revision) — validated
    # against PRE-EXISTING selections for this semester (via the same
    # semester-scoped population used everywhere else in this file) AND,
    # within this single request, against each other in order — so a
    # request classifying two offerings "minor" from different departments
    # in the SAME call is rejected exactly like doing it in two separate
    # calls would be. Runs entirely before any row is written (alongside
    # every other per-offering check above), so an invalid classification
    # never partially commits some of the batch.
    if body.classifications:
        existing_items = await _semester_scoped_enrollments(user.id, body.semester_id, db)
        existing_active = [e for e in existing_items if e.status != "withdrawn"]
        student_department_id = user.department_id
        existing_minor_dept_id = next(
            (e.offering.department_id for e in existing_active if e.classification == "minor" and e.offering), None,
        )
        existing_supporting_dept_ids = [
            e.offering.department_id for e in sorted(
                (x for x in existing_active if x.classification == "supporting" and x.offering), key=lambda x: x.enrolled_at,
            )
        ]
        lpm_department_id: Optional[UUID] = None
        try:
            for oid in body.offering_ids:
                classification = body.classifications.get(oid)
                if not classification:
                    continue
                offering = offerings[oid]
                if classification == "major":
                    validate_major(offering.department_id, student_department_id)
                elif classification == "minor":
                    validate_minor(offering.department_id, student_department_id, existing_minor_dept_id)
                    existing_minor_dept_id = offering.department_id
                elif classification == "supporting":
                    if lpm_department_id is None:
                        lpm_department_id = await get_lpm_department_id(db)
                    validate_supporting(
                        offering.department_id, lpm_department_id, student_department_id,
                        existing_minor_dept_id, existing_supporting_dept_ids,
                    )
                    existing_supporting_dept_ids = existing_supporting_dept_ids + [offering.department_id]
                # research/seminar/compulsory: no department restriction — unchanged.
        except ClassificationError as exc:
            raise HTTPException(400, str(exc))

    if not registration:
        registration = CourseRegistration(
            student_id=user.id, semester_id=body.semester_id, calendar_id=derived_calendar_id,
            stage="teacher_pending",
        )
        db.add(registration)
        try:
            await db.flush()
        except IntegrityError:
            # A concurrent request created it first between our SELECT and
            # this INSERT — extremely unlikely in this demo AMS, but fail
            # closed with a clear, retryable message rather than a 500.
            await db.rollback()
            raise HTTPException(409, "You already have a course registration for this semester. Please retry.")

    # 20-credit cap (Section 7/8) — always computed live, never trusted from
    # the client, and evaluated against the FULL prospective total (existing
    # active credits + everything requested in this call).
    current_credits = await _semester_active_credits(user.id, body.semester_id, db)
    if current_credits + requested_credits > _MAX_SEMESTER_CREDITS:
        raise HTTPException(
            400,
            f"Adding these course(s) would exceed the maximum of {_MAX_SEMESTER_CREDITS} credits for this "
            f"semester ({current_credits} already selected + {requested_credits} requested).",
        )

    for oid in body.offering_ids:
        classification = body.classifications.get(oid) if body.classifications else None
        db.add(StudentEnrollment(
            student_id=user.id, offering_id=oid, registration_id=registration.id, classification=classification,
            instructor_id=research_instructor_id if oid in research_offering_ids else None,
        ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "One or more selected courses are already part of an existing registration.")

    # A freshly-added course is always `pending` — if the registration had
    # reached `card_pending` (all previously-selected courses already
    # cleared), it must fall back to `teacher_pending` now that a new,
    # not-yet-approved course exists.
    await _recompute_card_readiness(registration.id, db)
    await db.commit()

    await db.refresh(registration)
    return {"id": str(registration.id), "message": "Registration submitted.", "stage": registration.stage}


@router.get("/my")
async def my_enrollments(
    calendar_id: Optional[UUID] = None, semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """My Courses task (this revision) — the confirmed K.7 column set (SL No
    is a frontend-assigned row index, not stored) plus enough extra fields
    (course_type/credit_type/semester_name/academic_year/teachers) for the
    "View Details" action and the Academic-Year/Semester filters, without a
    second endpoint — `calendar_id`/`semester_id` are optional narrowing
    filters, self-scoped (always `user.id`, never a client-supplied student).
    `status_label` reflects the REGISTRATION-level stage once a course's own
    Course-Teacher stage is done and the registration has moved into the
    locked chain (Major Advisor/HOD), exactly like `_enroll_dict`'s combined
    label — never a value that doesn't correspond to real backend state."""
    q = (
        select(StudentEnrollment)
        .join(CourseOffering, StudentEnrollment.offering_id == CourseOffering.id)
        .options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.semester),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.calendar),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.department),
            selectinload(StudentEnrollment.registration),
            selectinload(StudentEnrollment.instructor),
            selectinload(StudentEnrollment.withdrawal_requests),
        )
        .where(StudentEnrollment.student_id == user.id)
    )
    if calendar_id: q = q.where(CourseOffering.calendar_id == calendar_id)
    if semester_id: q = q.where(CourseOffering.semester_id == semester_id)
    result = await db.execute(q.order_by(StudentEnrollment.enrolled_at.desc()))

    items = []
    for e in result.scalars().all():
        o = e.offering
        c = o.course if o else None
        registration = e.registration
        combined_label = _ENROLLMENT_STATUS_LABELS.get(e.status, e.status)
        if e.status == "approved" and registration is not None and registration.stage not in _EDITABLE_STAGES:
            combined_label = _REGISTRATION_STAGE_LABELS.get(registration.stage, registration.stage)
        latest_wr = e.withdrawal_requests[0] if e.withdrawal_requests else None
        items.append({
            "id": str(e.id),
            "offering_id": str(e.offering_id),
            "registration_id": str(e.registration_id) if e.registration_id else None,
            "registration_stage": registration.stage if registration else None,
            "course_number": c.course_number if c else None,
            "course_title": c.title if c else None,
            "credit_structure": c.credit_structure if c else None,
            "credits": c.total_credits if c else 0,
            "category": c.category if c else None,
            "credit_type": c.credit_type if c else None,
            # Major/Minor/Supporting discipline task (this revision).
            "classification": e.classification,
            "department_id": str(o.department_id) if o and o.department_id else None,
            "department_name": o.department.name if o and o.department else None,
            "section": o.section if o else None,
            "semester_name": o.semester.name if o and o.semester else None,
            "academic_year": o.calendar.academic_year if o and o.calendar else None,
            "calendar_id": str(o.calendar_id) if o and o.calendar_id else None,
            "semester_id": str(o.semester_id) if o and o.semester_id else None,
            "teachers": [fa.faculty.full_name for fa in o.faculty_assignments if fa.faculty] if o else [],
            "status": e.status,
            "status_label": combined_label,
            "enrolled_at": e.enrolled_at.isoformat(),
            "remarks": e.remarks,
            "withdrawal_request": ({
                "status": latest_wr.status, "status_label": _WITHDRAWAL_STATUS_LABELS.get(latest_wr.status, latest_wr.status),
            } if latest_wr else None),
        })
    return items


@router.delete("/{enrollment_id}", status_code=204)
async def withdraw(
    enrollment_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Withdrawal task (this revision) — a still-PENDING, registration-linked
    course may now be withdrawn directly by the student (no Course Teacher
    involvement needed, since it was never approved), but ONLY while the
    registration is still editable (`_EDITABLE_STAGES`). This is Case A from
    the confirmed requirement; an APPROVED course must instead go through
    `request_withdrawal` below (Case B) — this endpoint explicitly refuses an
    approved, registration-linked item rather than silently withdrawing it,
    so a student can never bypass the Course-Teacher-approval requirement for
    an already-cleared course merely by calling the older endpoint. Legacy
    (non-registration) rows keep their original, unrestricted behavior."""
    e = await db.get(StudentEnrollment, enrollment_id)
    if not e or e.student_id != user.id:
        raise HTTPException(404, "Enrollment not found.")
    if e.registration_id:
        registration = await db.get(CourseRegistration, e.registration_id)
        if not registration:
            raise HTTPException(404, "Enrollment not found.")
        _ensure_registration_editable(registration)
        if e.status == "approved":
            raise HTTPException(400, "This course has already been approved by its Course Teacher — submit a withdrawal request instead.")
        if e.status != "pending":
            raise HTTPException(400, "This course cannot be withdrawn in its current state.")
        e.status = "withdrawn"
        await db.commit()
        await _recompute_card_readiness(registration.id, db)
        await db.commit()
        return
    if e.status != "pending":
        raise HTTPException(400, "Can only withdraw pending enrollments.")
    e.status = "withdrawn"; await db.commit()


# ── Approved-course withdrawal requests (Case B) ─────────────────────────────

@router.post("/{enrollment_id}/withdrawal-request", status_code=201)
async def request_withdrawal(
    enrollment_id: UUID, body: WithdrawalRequestIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    e = await db.get(StudentEnrollment, enrollment_id)
    if not e or e.student_id != user.id:
        raise HTTPException(404, "Enrollment not found.")
    if e.status != "approved":
        raise HTTPException(400, "A withdrawal request can only be made for a course already approved by its Course Teacher.")
    if e.registration_id:
        registration = await db.get(CourseRegistration, e.registration_id)
        if not registration:
            raise HTTPException(404, "Enrollment not found.")
        _ensure_registration_editable(registration)
    existing = await db.execute(select(WithdrawalRequest).where(
        WithdrawalRequest.enrollment_id == e.id, WithdrawalRequest.status == "pending",
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A withdrawal request is already pending for this course.")
    wr = WithdrawalRequest(enrollment_id=e.id, reason=body.reason, requested_by=user.id)
    db.add(wr)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A withdrawal request is already pending for this course.")
    return {"id": str(wr.id), "message": "Withdrawal request submitted."}


@router.patch("/withdrawal-requests/{request_id}")
async def decide_withdrawal_request(
    request_id: UUID, body: WithdrawalDecisionIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    """Approve/reject an approved-course withdrawal request. Reuses
    `_authorize_offering_management` UNCHANGED (Section 11's explicit
    instruction not to create a second, incompatible authorization
    mechanism) — a Course Teacher may only decide a request for an offering
    they are actually assigned to (or their own department, for HOD; or
    unrestricted, for admins) — never merely by knowing the request's ID."""
    wr = await db.get(WithdrawalRequest, request_id)
    if not wr:
        raise HTTPException(404, "Withdrawal request not found.")
    e = await db.get(StudentEnrollment, wr.enrollment_id)
    if not e:
        raise HTTPException(404, "Enrollment not found.")
    await _authorize_offering_management(e.offering_id, user, db)
    if wr.status != "pending":
        raise HTTPException(400, "This withdrawal request has already been decided.")

    wr.status = "approved" if body.approved else "rejected"
    wr.decided_by = user.id
    wr.decided_at = datetime.now(timezone.utc)
    wr.decision_remark = body.remark
    if body.approved:
        # Only on approval does the enrollment itself change — a rejection
        # leaves `StudentEnrollment.status` completely untouched (Section 12's
        # explicit "do not silently alter the enrollment on rejection").
        e.status = "withdrawn"
    await db.commit()

    if body.approved and e.registration_id:
        await _recompute_card_readiness(e.registration_id, db)
        await db.commit()

    return {"message": f"Withdrawal request {wr.status}.", "status": wr.status}


# ── Registration-level views ─────────────────────────────────────────────────

@router.get("/registrations")
async def list_registrations(
    stage: Optional[str] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    q = select(CourseRegistration).options(*_REGISTRATION_LOAD_OPTIONS)
    if user.active_role == UserRole.STUDENT:
        q = q.where(CourseRegistration.student_id == user.id)
    elif user.active_role in (UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
        pass  # unrestricted — global roles, Section 12/13
    elif user.active_role == UserRole.HOD:
        if not user.active_department_id:
            return []
        # Programme<->Department many-to-many redesign — the student's OWN
        # department_id is authoritative now, never inferred via their
        # Programme's department (a Programme can have many Departments).
        q = (
            q.join(User, CourseRegistration.student_id == User.id)
             .where(User.department_id == user.active_department_id)
        )
    elif user.active_role == UserRole.FACULTY:
        # A faculty member's registration queue = registrations where they are
        # either the student's Major Advisor, or an assigned teacher on at
        # least one selected course. Filtered in Python below (post-query) —
        # both conditions require joining data not expressible as a single
        # simple WHERE clause without duplicating the two lookups anyway.
        pass
    else:
        return []

    if stage: q = q.where(CourseRegistration.stage == stage)
    result = await db.execute(q.order_by(CourseRegistration.submitted_at.desc()))
    registrations = result.scalars().all()

    if user.active_role == UserRole.FACULTY:
        ma_student_ids = set((await db.execute(
            select(AdvisoryCommittee.student_id).join(
                CommitteeMember, CommitteeMember.committee_id == AdvisoryCommittee.id
            ).where(CommitteeMember.faculty_id == user.id, CommitteeMember.role == "major_advisor")
        )).scalars().all())
        teacher_offering_ids = set((await db.execute(
            select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id)
        )).scalars().all())
        registrations = [
            r for r in registrations
            if r.student_id in ma_student_ids or any(i.offering_id in teacher_offering_ids for i in r.items)
        ]

    return [await _registration_dict(r, db) for r in registrations]


@router.get("/registrations/{registration_id}")
async def get_registration(registration_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(CourseRegistration).options(*_REGISTRATION_LOAD_OPTIONS).where(CourseRegistration.id == registration_id)
    )
    r = result.scalar_one_or_none()
    if not r: raise HTTPException(404, "Registration not found.")
    await _authorize_registration_view(r, user, db)
    return await _registration_dict(r, db)


# ── Registration Card: student submission (the lock point) ──────────────────

@router.post("/registrations/{registration_id}/submit")
async def submit_registration_card(
    registration_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """The ONE and ONLY way a registration moves from `card_pending` into the
    locked Major-Advisor chain (Section 2/3/21's explicit requirement).
    Independently re-validates every precondition against live DB state —
    never trusts the registration's own cached `stage` alone, so a desync
    (however unlikely) can never let an incomplete registration through."""
    r = await db.get(CourseRegistration, registration_id)
    if not r:
        raise HTTPException(404, "Registration not found.")
    if r.student_id != user.id:
        raise HTTPException(404, "Registration not found.")
    if r.stage not in _EDITABLE_STAGES:
        raise HTTPException(400, "This registration has already been submitted and cannot be modified.")

    items = (await db.execute(
        select(StudentEnrollment).where(
            StudentEnrollment.registration_id == r.id, StudentEnrollment.status != "withdrawn",
        )
    )).scalars().all()
    if not items:
        raise HTTPException(400, "Add at least one course before submitting the Registration Card.")
    if not all(i.status == "approved" for i in items):
        raise HTTPException(400, "All selected courses must be approved by their Course Teacher before submitting the Registration Card.")

    r.stage = "major_advisor_pending"
    r.card_submitted_at = datetime.now(timezone.utc)
    r.revert_remark = None; r.reverted_at = None
    await db.commit()
    return {"message": "Registration Card submitted.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}


# ── Registration Card: PDF document (on-demand, never stored) ───────────────

_CATEGORY_LABELS = {
    "optional": "Optional Course", "core": "Core Course", "compulsory": "Compulsory Course (CC)",
    "research": "Research Course", "seminar": "Seminar Course", "deficiency": "Deficiency",
    "bridge": "Bridge", "prerequisite": "Prerequisite", "mandatory_mba": "Mandatory Course (MBA)",
    "uncategorized": "Other Course(s)",
}

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
_reg_card_jinja_env = None  # lazily constructed, cached at module level — mirrors ppw.py's own pattern, kept file-local rather than importing ppw.py's private cache


def _render_registration_card_html(context: dict) -> str:
    global _reg_card_jinja_env
    if _reg_card_jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _reg_card_jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=select_autoescape(["html"]),
        )
    template = _reg_card_jinja_env.get_template("registration_card.html")
    return template.render(**context)


async def _build_registration_card_context(r: CourseRegistration, db: AsyncSession) -> dict:
    """Builds the full render context for the Registration Card PDF, using
    ONLY real AMS data relationships (Section 17's explicit "do not invent
    values" instruction) — any field with no confirmed source in the current
    schema (e.g. "Year" of study) is simply rendered as "—", never fabricated.
    Course grouping uses the EXISTING `Course.category` vocabulary (already
    used across AMS) rather than PPW's six-way classification, per explicit
    instruction not to force PPW's taxonomy onto this table's grouping — left
    completely unchanged by the discipline task below.

    Major/Minor/Supporting discipline task (this revision): `context["registration"]`
    IS `_registration_dict(r, db)`'s own return value, which already carries
    `major_discipline_name`/`minor_discipline_name`/`supporting_discipline_name`
    (derived from the student's own department and the classified selections'
    departments — never client-supplied text) — the template reads them
    straight off `registration.*`, no separate computation needed here."""
    student = await db.get(User, r.student_id)
    program = await db.get(Program, student.program_id) if student and student.program_id else None
    department = await db.get(Department, student.department_id) if student and student.department_id else None
    semester = await db.get(Semester, r.semester_id)
    calendar = await db.get(AcademicCalendar, r.calendar_id)
    ma_id = await _get_major_advisor_id(r.student_id, db)
    major_advisor = await db.get(User, ma_id) if ma_id else None

    # Deliberately scoped to `r.items` (registration-linked rows only), NOT
    # the semester-scoped `_semester_scoped_enrollments` used by
    # `_registration_dict`'s `items`/`selected_credits` fields — the official
    # Registration Card PDF represents specifically what THIS registration
    # batch contains; whether legacy, pre-Course-Registration enrollments
    # should also appear on the printed card is a separate question this
    # task did not ask to resolve, so the PDF's course table is left exactly
    # as it was (unchanged scope) even though the on-screen Selected Courses
    # list above it now shows the wider, corrected population.
    active_items = [e for e in r.items if e.status != "withdrawn"]
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for e in active_items:
        o = e.offering
        c = o.course if o else None
        cat = (c.category if c and c.category else "uncategorized")
        if cat not in order:
            order.append(cat)
            groups[cat] = []
        groups[cat].append({
            "course_number": c.course_number if c else None,
            "course_title": c.title if c else None,
            "credit_structure": c.credit_structure if c else None,
            "credits": c.total_credits if c else 0,
            "nature": "Non-Credit" if c and c.credit_type == "non_credit" else "Credit",
            "instructors": ", ".join(fa.faculty.full_name for fa in o.faculty_assignments if fa.faculty) if o else "",
        })

    classifications = []
    total_credits = 0
    for cat in order:
        rows = groups[cat]
        for idx, row in enumerate(rows, start=1):
            row["sl_no"] = idx
            total_credits += row["credits"]
        classifications.append({"label": _CATEGORY_LABELS.get(cat, cat.replace("_", " ").title()), "courses": rows})

    # HOD signature status is derived the same way _authorize_hod_registration
    # resolves the student's HOD — live, never a stored/cached value. HOD is
    # not a persisted signatory (unchanged by this task — see model docstring).
    hod_dept_id = await _student_department_id(r.student_id, db)
    hod = None
    if hod_dept_id:
        # Multi-role/multi-department task (this revision) — resolved via
        # UserRoleAssignment, not User.role/department_id (see the other
        # call site's identical comment above in this file).
        hod_id = await find_role_holder_in_department(UserRole.HOD, hod_dept_id, db)
        hod = await db.get(User, hod_id) if hod_id else None

    # DPGS IS the final, persisted signatory (Section 17) — the ACTUAL
    # authenticated user who approved, never a live "whoever holds DPGS
    # right now" lookup (deliberately unlike the HOD lookup above).
    dpgs_approver = await db.get(User, r.dpgs_approved_by) if r.dpgs_approved_by else None

    from app.utils.pdf import get_logo_data_uri
    return {
        "registration": await _registration_dict(r, db),
        "student": {
            "name": student.full_name if student else None,
            "roll_no": student.student_roll if student else None,
            "mobile": student.mobile if student else None,
        },
        "program_name": program.name if program else None,
        "program_level": program.level if program else None,
        "department_name": department.name if department else None,
        "college_name": department.stream if department else None,  # BUSINESS_LOGIC.md Open Question 28 — same established assumption as PPW/research.py
        "semester_name": semester.name if semester else None,
        "academic_year": calendar.academic_year if calendar else None,
        "major_advisor_name": major_advisor.full_name if major_advisor else None,
        "hod_name": hod.full_name if hod else None,
        # Incharge Academic Cell is workflow-approval-only and deliberately
        # has NO field here at all (Section 13 — never a signatory). DPGS is
        # the final signatory; both fields are None until the actual DPGS
        # approval event has happened.
        "dpgs_name": dpgs_approver.full_name if dpgs_approver else None,
        "dpgs_signed_at": r.dpgs_approved_at.isoformat() if r.dpgs_approved_at else None,
        "classifications": classifications,
        "total_credits": total_credits,
        "logo_data_uri": get_logo_data_uri(),
        "generated_at": datetime.now(timezone.utc),
    }


@router.get("/registrations/{registration_id}/document")
async def get_registration_document(
    registration_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Official Registration Card as a PDF — on-demand rendering, never
    persisted (Section 19's explicit "prefer on-demand generation" — the
    document always reflects live registration state). Reuses
    `_authorize_registration_view` UNCHANGED (Section 20's explicit
    instruction) — the exact same viewer rules `GET /registrations/{id}`
    already enforces, so a student can never download another student's card
    merely by changing `registration_id` in the URL."""
    result = await db.execute(
        select(CourseRegistration).options(*_REGISTRATION_LOAD_OPTIONS).where(CourseRegistration.id == registration_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Registration not found.")
    await _authorize_registration_view(r, user, db)
    if r.stage == "teacher_pending":
        raise HTTPException(400, "The Registration Card is available once every selected course has been approved by its Course Teacher.")

    context = await _build_registration_card_context(r, db)
    html = _render_registration_card_html(context)

    from starlette.concurrency import run_in_threadpool
    from app.utils.pdf import render_ppw_pdf, ChromiumUnavailable, ChromiumRenderFailed
    try:
        pdf_bytes = await run_in_threadpool(render_ppw_pdf, html)
    except ChromiumUnavailable as exc:
        logger.error("Registration Card generation unavailable (registration_id=%s): %s", registration_id, exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("Registration Card render failed (registration_id=%s): %s", registration_id, exc)
        raise HTTPException(422, "Unable to generate Registration Card.")

    roll_or_id = (r.student.student_roll or str(r.student_id)) if r.student else str(r.student_id)
    safe_roll = re.sub(r"[^A-Za-z0-9_-]", "-", roll_or_id)
    filename = f"RegistrationCard-{safe_roll}-{r.stage}.pdf"
    encoded_name = urllib.parse.quote(filename, safe="")

    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


# ── Faculty/Admin: manage per-course enrollments (Course Teacher stage) ─────────

@router.get("/offering/{offering_id}")
async def offering_enrollments(
    offering_id: UUID, status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    # Research Course task: a FACULTY member with no OfferingFaculty row for
    # this offering (true for every Research Course offering) may still be
    # the per-student instructor for SOME students here. Rather than grant
    # them the whole roster (which would expose other students' rows they
    # have no relationship to), the roster query itself is additionally
    # filtered to only their own instructed students in that case — full
    # offering-wide visibility is unchanged for SUPER_ADMIN/HOD/an actually-
    # assigned OfferingFaculty member.
    restrict_to_own_students = False
    try:
        await _authorize_offering_management(offering_id, user, db)
    except HTTPException:
        if user.active_role != UserRole.FACULTY:
            raise
        research_link = await db.execute(select(StudentEnrollment.id).where(
            StudentEnrollment.offering_id == offering_id, StudentEnrollment.instructor_id == user.id,
        ).limit(1))
        if not research_link.scalar_one_or_none():
            raise
        restrict_to_own_students = True
    # MissingGreenlet fix (this revision): `_enroll_dict()` below reads
    # `e.withdrawal_requests` for every row — that relationship was never
    # eager-loaded here, so accessing it triggered an implicit lazy load,
    # which isn't supported under this async session outside of an explicit
    # async-safe context (`sqlalchemy.exc.MissingGreenlet`). This crashed the
    # endpoint with a 500 for ANY offering with at least one enrollment (an
    # offering with zero enrollments never reached `_enroll_dict` at all, so
    # it looked identical to a genuinely empty course — the frontend has no
    # error handling on this query and silently falls back to an empty
    # list). Adding the eager load here fixes it with no change to the
    # returned data shape or any other behavior.
    q = select(StudentEnrollment).options(
        selectinload(StudentEnrollment.student), selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
        selectinload(StudentEnrollment.instructor),
        selectinload(StudentEnrollment.withdrawal_requests),
        # `_enroll_dict` also now reads `offering.faculty_assignments` (the
        # Registration Card Preview task's new `instructors` field) — eager
        # load it here too, same MissingGreenlet-avoidance reasoning as above.
        selectinload(StudentEnrollment.offering).selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty),
        # Major/Minor/Supporting discipline task (this revision) —
        # `_enroll_dict` now also reads `offering.department`; same
        # MissingGreenlet-avoidance reasoning as above.
        selectinload(StudentEnrollment.offering).selectinload(CourseOffering.department),
    ).where(StudentEnrollment.offering_id == offering_id)
    if status: q = q.where(StudentEnrollment.status == status)
    if restrict_to_own_students: q = q.where(StudentEnrollment.instructor_id == user.id)
    result = await db.execute(q.order_by(StudentEnrollment.enrolled_at))
    return [_enroll_dict(e) for e in result.scalars().all()]


class _EnrollmentDecisionError(Exception):
    """Raised by `_apply_enrollment_decision` for a per-item validation
    failure — caught and converted to an HTTP 400 by the single-item endpoint,
    and caught-and-skipped (not batch-aborting) by bulk-approve."""


async def _apply_enrollment_decision(e: StudentEnrollment, status: str, remarks: Optional[str], user: User, db: AsyncSession) -> None:
    """Shared Course-Teacher-stage state-transition logic (verification-audit
    Finding 2 fix) — used by BOTH the single-item `PATCH /{enrollment_id}` and
    `POST /offering/{id}/bulk-approve`, so a registration-linked row can never
    bypass CR-3's rules via one path but not the other. Legacy (registration_id
    is None) rows keep their original, unrestricted status/remarks behavior —
    unchanged, per instruction not to break legacy flows.

    Revert semantics (Finding 1 fix — Open Question 44 remains genuinely
    unresolved by AVFU; this is an explicit, documented implementation default,
    not a confirmed business rule): a Course-Teacher revert now sends the
    WHOLE registration back to `teacher_pending` and resets EVERY selected
    course (not just the reverted one) back to `pending` — mirroring the
    Major-Advisor-revert path's already-established "step back to the
    immediately previous level, reset for re-review" pattern exactly, instead
    of the previous behavior of jumping straight to a terminal `reverted`
    stage with no way back. This keeps every course's individual status and
    the registration's overall stage mutually consistent (no course can be
    left showing "Approved" while the registration shows "Reverted") and
    guarantees the registration is always actionable again after a revert.
    """
    if e.status != "pending":
        raise _EnrollmentDecisionError("Only pending enrollments can be processed.")
    if e.registration_id:
        # Course Registration items: confirmed workflow is Approve/Revert only
        # (BUSINESS_LOGIC.md Rule 35) — no Reject path for this chain.
        if status not in ("approved", "reverted"):
            raise _EnrollmentDecisionError("Status must be 'approved' or 'reverted' for a Course Registration item.")
        if status == "reverted" and not remarks:
            raise _EnrollmentDecisionError("A remark is required when reverting a Course Registration item.")

    e.status = status
    e.processed_by = user.id
    e.processed_at = datetime.now(timezone.utc)
    e.remarks = remarks
    await db.commit()

    if not e.registration_id:
        return
    registration = await db.get(CourseRegistration, e.registration_id)
    if not registration or registration.stage != "teacher_pending":
        return

    if status == "reverted":
        registration.stage = "teacher_pending"  # stays actionable — see docstring
        registration.revert_remark = remarks
        registration.reverted_at = datetime.now(timezone.utc)
        siblings = (await db.execute(
            select(StudentEnrollment).where(StudentEnrollment.registration_id == registration.id)
        )).scalars().all()
        for s in siblings:
            s.status = "pending"; s.processed_by = None; s.processed_at = None; s.remarks = None
        await db.commit()
    elif status == "approved":
        # Registration Card task (this revision): reaching "every currently-
        # active course approved" now lands on `card_pending` — a student-
        # editable "ready to submit" state — never a direct jump to
        # `major_advisor_pending` anymore. Only the student's own explicit
        # `POST /registrations/{id}/submit` call can lock the registration.
        await _recompute_card_readiness(registration.id, db)
        registration = await db.get(CourseRegistration, registration.id)
        if registration.stage == "card_pending":
            # Clear any earlier revert marker now that every course has been
            # corrected and cleared again — prevents a stale "Reverted" remark
            # from lingering after the cycle successfully advances.
            registration.revert_remark = None; registration.reverted_at = None
        await db.commit()


@router.patch("/{enrollment_id}")
async def process_enrollment(
    enrollment_id: UUID,
    status: str,
    remarks: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    e = await db.get(StudentEnrollment, enrollment_id)
    if not e: raise HTTPException(404, "Enrollment not found.")
    await _authorize_offering_management(e.offering_id, user, db, student_id=e.student_id)
    try:
        await _apply_enrollment_decision(e, status, remarks, user, db)
    except _EnrollmentDecisionError as exc:
        raise HTTPException(400, str(exc))
    return {"message": f"Enrollment {status}.", "status_label": _ENROLLMENT_STATUS_LABELS.get(status, status)}


@router.post("/offering/{offering_id}/bulk-approve")
async def bulk_approve(
    offering_id: UUID, body: BulkApproveRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.FACULTY,
    )),
):
    """Finding 2 fix: now routes every row through `_apply_enrollment_decision`
    — the same shared logic `PATCH /{enrollment_id}` uses — so a
    registration-linked row can no longer bypass CR-3's approved/reverted-only
    restriction, mandatory-revert-remark rule, or the registration-stage
    cascade. Legacy (non-registration) rows are unaffected. Rows that fail
    per-item validation (e.g. a registration-linked row given an unsupported
    status) are skipped, not batch-aborting — consistent with this endpoint's
    original best-effort semantics.

    Research Course task: a FACULTY member with no OfferingFaculty row for
    this offering (true for every Research Course offering — see
    courses.py's create_offering) may still call this, but each row is then
    individually re-authorized against `StudentEnrollment.instructor_id`
    (via `_authorize_offering_management`'s `student_id` fallback) — a row
    for a student they do not instruct is skipped, never processed, exactly
    like any other per-item validation failure."""
    offering_wide_ok = True
    try:
        await _authorize_offering_management(offering_id, user, db)
    except HTTPException:
        if user.active_role != UserRole.FACULTY:
            raise
        offering_wide_ok = False
    updated = 0
    skipped = 0
    for eid in body.enrollment_ids:
        e = await db.get(StudentEnrollment, eid)
        if not e or e.offering_id != offering_id:
            continue
        if not offering_wide_ok:
            try:
                await _authorize_offering_management(offering_id, user, db, student_id=e.student_id)
            except HTTPException:
                skipped += 1
                continue
        try:
            await _apply_enrollment_decision(e, body.status, body.remarks, user, db)
            updated += 1
        except _EnrollmentDecisionError:
            skipped += 1
    message = f"{updated} enrollments {body.status}."
    if skipped:
        message += f" {skipped} skipped (not eligible for this status)."
    return {"message": message}


# ── Major Advisor stage ──────────────────────────────────────────────────────

@router.patch("/registrations/{registration_id}/major-advisor-approval")
async def major_advisor_approval(
    registration_id: UUID, body: StageDecisionIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.FACULTY,
    )),
):
    r = await db.get(CourseRegistration, registration_id)
    if not r: raise HTTPException(404, "Registration not found.")
    await _authorize_major_advisor(r, user, db)
    if r.stage != "major_advisor_pending":
        raise HTTPException(400, "This registration is not awaiting Major Advisor approval.")

    if body.approved:
        r.stage = "hod_pending"
        r.revert_remark = None; r.reverted_at = None
        await db.commit()
        return {"message": "Approved by Major Advisor.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    # Revert to the immediately previous level (Rule 2) — the Course Teacher
    # stage — and reset sibling courses so they can be re-reviewed.
    r.stage = "teacher_pending"
    r.revert_remark = body.remark; r.reverted_at = datetime.now(timezone.utc)
    items = (await db.execute(select(StudentEnrollment).where(StudentEnrollment.registration_id == r.id))).scalars().all()
    for item in items:
        item.status = "pending"; item.processed_by = None; item.processed_at = None; item.remarks = None
    await db.commit()
    return {"message": "Reverted to Course Teacher stage.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}


# ── HOD stage ─────────────────────────────────────────────────────────────────

@router.patch("/registrations/{registration_id}/hod-approval")
async def hod_registration_approval(
    registration_id: UUID, body: StageDecisionIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    r = await db.get(CourseRegistration, registration_id)
    if not r: raise HTTPException(404, "Registration not found.")
    await _authorize_hod_registration(r, user, db)
    if r.stage != "hod_pending":
        raise HTTPException(400, "This registration is not awaiting HOD approval.")

    if body.approved:
        # Incharge Academic Cell / DPGS task — HOD approval no longer
        # terminates the chain; it continues to Incharge Academic Cell.
        r.stage = "incharge_pending"
        r.revert_remark = None; r.reverted_at = None
        await db.commit()
        return {"message": "Approved by HOD.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    r.stage = "major_advisor_pending"  # immediately previous level (Rule 2)
    r.revert_remark = body.remark; r.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Reverted to Major Advisor stage.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}


# ── Incharge Academic Cell stage (global — Incharge Academic Cell/DPGS task) ──

@router.patch("/registrations/{registration_id}/incharge-approval")
async def incharge_registration_approval(
    registration_id: UUID, body: StageDecisionIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL)),
):
    """Global role — no department match (Section 9/13): any Incharge
    Academic Cell holder may act on any department's registration. Approval
    is workflow-only, never a signature (Section 13) — no approver identity
    is persisted, mirroring the existing HOD stage's own behavior."""
    r = await db.get(CourseRegistration, registration_id)
    if not r: raise HTTPException(404, "Registration not found.")
    if r.stage != "incharge_pending":
        raise HTTPException(400, "This registration is not awaiting Incharge Academic Cell approval.")

    if body.approved:
        r.stage = "dpgs_pending"
        r.revert_remark = None; r.reverted_at = None
        await db.commit()
        return {"message": "Approved by Incharge Academic Cell.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    # Section 15/16 — Incharge/DPGS revert goes ALL THE WAY back to the
    # student (never one level back, unlike every earlier stage in this
    # workflow), so a resubmission always replays the identical original
    # approval chain from the very first stage.
    await _revert_registration_to_student(r, body.remark, db)
    await db.commit()
    return {"message": "Reverted to the student for correction.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}


# ── DPGS stage (global, final signatory — Incharge Academic Cell/DPGS task) ──

@router.patch("/registrations/{registration_id}/dpgs-approval")
async def dpgs_registration_approval(
    registration_id: UUID, body: StageDecisionIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.DPGS)),
):
    """DPGS is the Registration Card's final signatory (Section 14/17) — the
    approver identity/timestamp are persisted on the registration itself
    (`dpgs_approved_by`/`dpgs_approved_at`), derived ONLY from the
    authenticated user, never a client-supplied id."""
    r = await db.get(CourseRegistration, registration_id)
    if not r: raise HTTPException(404, "Registration not found.")
    if r.stage != "dpgs_pending":
        raise HTTPException(400, "This registration is not awaiting DPGS approval.")

    if body.approved:
        r.stage = "dpgs_approved"
        r.dpgs_approved_by = user.id
        r.dpgs_approved_at = datetime.now(timezone.utc)
        r.revert_remark = None; r.reverted_at = None
        await db.commit()
        return {"message": "Approved and signed by DPGS.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    await _revert_registration_to_student(r, body.remark, db)
    await db.commit()
    return {"message": "Reverted to the student for correction.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}
