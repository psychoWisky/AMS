"""Student & Teacher Enrollment Management (Modules 5.3, 5.4).

Course Registration workflow (BUSINESS_LOGIC.md D.5, C.8; STUDENT_SIDE_IMPLEMENTATION_PLAN.md
Section 34 CR-3+CR-4):

    Student submits a registration (1+ selected courses for one semester)
    -> per selected course: any ONE assigned Course Teacher approves/reverts
    -> once ALL selected courses have cleared: Major Advisor approves/reverts
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

Course Registration Card generation is explicitly deferred (BUSINESS_LOGIC.md
D.5 Rule 7) — not built this phase.
"""
from typing import Optional, List
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles, require_advisory_committee_established
from app.models.user import User, UserRole
from app.models.enrollment import StudentEnrollment, CourseRegistration
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.research import AdvisoryCommittee, CommitteeMember
# Programme<->Department many-to-many redesign — single shared student-scope
# resolvers (app/core/student_scope.py), re-exported under this file's
# existing private names so every call site below is unchanged.
from app.core.student_scope import (
    resolve_student_scope as _resolve_student_scope,
    resolve_student_department_id as _student_department_id,
)

router = APIRouter(prefix="/enrollment", tags=["Enrollment"])

# BUSINESS_LOGIC.md C.8 status vocabulary.
_REGISTRATION_STAGE_LABELS = {
    "teacher_pending": "Course Teacher Approval Pending",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "hod_approved": "HOD Approved",
    "reverted": "Reverted",
}
_ENROLLMENT_STATUS_LABELS = {
    "pending": "Course Teacher Approval Pending",
    "approved": "Course Teacher Approved",
    "reverted": "Reverted",
    "withdrawn": "Withdrawn",
    "rejected": "Rejected",  # legacy value only — never written by new code (Rule 35)
}




async def _get_major_advisor_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    """Resolve a student's Major Advisor from the established Advisory Committee
    (research.py's proven pattern — reused, not duplicated)."""
    result = await db.execute(
        select(CommitteeMember.faculty_id)
        .join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .where(AdvisoryCommittee.student_id == student_id, CommitteeMember.role == "major_advisor")
    )
    return result.scalar_one_or_none()


async def _authorize_offering_management(offering_id: UUID, user: User, db: AsyncSession) -> CourseOffering:
    """Faculty/HOD/admin authorization for managing enrollments of a given offering.
    SUPER_ADMIN / ACADEMIC_ADMIN / REGISTRAR: unrestricted.
    HOD: department-wide (current_user.department_id == offering.department_id), no OfferingFaculty needed.
    FACULTY: only if an OfferingFaculty row exists for (offering_id, user.id)."""
    offering = await db.get(CourseOffering, offering_id)
    if not offering:
        raise HTTPException(404, "Offering not found.")
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR):
        return offering
    if user.role == UserRole.HOD:
        if user.department_id and offering.department_id and user.department_id == offering.department_id:
            return offering
        raise HTTPException(403, "You can only manage offerings within your own department.")
    if user.role == UserRole.FACULTY:
        result = await db.execute(select(OfferingFaculty).where(
            OfferingFaculty.offering_id == offering_id, OfferingFaculty.faculty_id == user.id,
        ))
        if result.scalar_one_or_none():
            return offering
        raise HTTPException(403, "You are not assigned to this offering.")
    raise HTTPException(403, "Insufficient permissions.")


async def _authorize_major_advisor(registration: CourseRegistration, user: User, db: AsyncSession) -> None:
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN):
        return
    ma_id = await _get_major_advisor_id(registration.student_id, db)
    if ma_id and ma_id == user.id:
        return
    raise HTTPException(403, "Only this student's Major Advisor (or an administrator) may act on this registration.")


async def _authorize_hod_registration(registration: CourseRegistration, user: User, db: AsyncSession) -> None:
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN):
        return
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(registration.student_id, db)
        if dept_id and user.department_id and dept_id == user.department_id:
            return
        raise HTTPException(403, "You can only approve registrations for students in your own department.")
    raise HTTPException(403, "Only the student's HOD (or an administrator) may act on this registration.")


async def _authorize_registration_view(registration: CourseRegistration, user: User, db: AsyncSession) -> None:
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR):
        return
    if user.role == UserRole.STUDENT:
        if registration.student_id == user.id:
            return
        raise HTTPException(403, "You can only view your own registration.")
    if user.role == UserRole.HOD:
        dept_id = await _student_department_id(registration.student_id, db)
        if dept_id and user.department_id and dept_id == user.department_id:
            return
        raise HTTPException(403, "You can only view registrations within your own department.")
    ma_id = await _get_major_advisor_id(registration.student_id, db)
    if ma_id and ma_id == user.id:
        return
    if user.role == UserRole.FACULTY:
        result = await db.execute(
            select(OfferingFaculty.id)
            .join(StudentEnrollment, StudentEnrollment.offering_id == OfferingFaculty.offering_id)
            .where(OfferingFaculty.faculty_id == user.id, StudentEnrollment.registration_id == registration.id)
            .limit(1)
        )
        if result.scalar_one_or_none():
            return
    raise HTTPException(403, "You are not authorized to view this registration.")


class EnrollRequest(BaseModel):
    offering_id: UUID

class RegisterCoursesRequest(BaseModel):
    calendar_id: UUID
    semester_id: UUID
    offering_ids: List[UUID]

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


def _enroll_dict(e: StudentEnrollment) -> dict:
    return {
        "id": str(e.id),
        "student_id": str(e.student_id),
        "student_name": e.student.full_name if e.student else None,
        "student_roll": e.student.student_roll if e.student else None,
        "student_email": e.student.email if e.student else None,
        "offering_id": str(e.offering_id),
        "course_number": e.offering.course.course_number if e.offering and e.offering.course else None,
        "course_title": e.offering.course.title if e.offering and e.offering.course else None,
        "registration_id": str(e.registration_id) if e.registration_id else None,
        "status": e.status,
        "status_label": _ENROLLMENT_STATUS_LABELS.get(e.status, e.status),
        "enrolled_at": e.enrolled_at.isoformat(),
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
        "remarks": e.remarks,
    }


def _registration_dict(r: CourseRegistration) -> dict:
    return {
        "id": str(r.id),
        "student_id": str(r.student_id),
        "student_name": r.student.full_name if r.student else None,
        "student_roll": r.student.student_roll if r.student else None,
        "program_name": r.student.program.name if r.student and r.student.program else None,
        "semester_id": str(r.semester_id),
        "calendar_id": str(r.calendar_id),
        "stage": r.stage,
        "status_label": _REGISTRATION_STAGE_LABELS.get(r.stage, r.stage),
        "revert_remark": r.revert_remark,
        "reverted_at": r.reverted_at.isoformat() if r.reverted_at else None,
        "submitted_at": r.submitted_at.isoformat(),
        "items": [_enroll_dict(e) for e in r.items],
    }


_REGISTRATION_LOAD_OPTIONS = (
    selectinload(CourseRegistration.student).selectinload(User.program),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
    selectinload(CourseRegistration.items).selectinload(StudentEnrollment.student),
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

    e = StudentEnrollment(student_id=user.id, offering_id=body.offering_id)
    db.add(e); await db.commit()
    return {"message": "Enrollment request submitted.", "id": str(e.id)}


# ── Student: submit a Course Registration (1+ courses, one semester) ────────────

@router.post("/register", status_code=201)
async def register_courses(
    body: RegisterCoursesRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_advisory_committee_established),
):
    if not body.offering_ids:
        raise HTTPException(400, "Select at least one course.")
    if len(set(body.offering_ids)) != len(body.offering_ids):
        raise HTTPException(400, "Duplicate course selected.")

    scope = await _resolve_student_scope(user, db)
    if not scope:
        raise HTTPException(403, "Your academic program is not configured; contact administration.")

    # Per-course eligibility, capacity — validated before any row is written,
    # mirroring the legacy single-course `enroll()` checks exactly.
    offerings: dict[UUID, CourseOffering] = {}
    for oid in body.offering_ids:
        offering = await db.get(CourseOffering, oid)
        if not offering or offering.status != "published" or offering.semester_id != body.semester_id:
            raise HTTPException(400, "One or more selected offerings are not available for this semester.")
        # Course-visibility change: department is no longer part of
        # eligibility here either — mirrors the single-offering `enroll()`.
        course = await db.get(Course, offering.course_id)
        if not course or course.program_level != scope["level"]:
            raise HTTPException(403, "One or more selected offerings are not available to your program.")
        count_result = await db.execute(
            select(func.count()).select_from(StudentEnrollment).where(
                StudentEnrollment.offering_id == oid, StudentEnrollment.status == "approved",
            )
        )
        if count_result.scalar() >= offering.max_enrollment:
            raise HTTPException(400, f"{course.course_number} is full.")
        offerings[oid] = offering

    registration = CourseRegistration(
        student_id=user.id, semester_id=body.semester_id, calendar_id=body.calendar_id,
        stage="teacher_pending",
    )
    db.add(registration)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You already have a course registration for this semester.")

    for oid in body.offering_ids:
        db.add(StudentEnrollment(student_id=user.id, offering_id=oid, registration_id=registration.id))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "One or more selected courses are already part of an existing registration.")

    await db.refresh(registration)
    return {"id": str(registration.id), "message": "Registration submitted."}


@router.get("/my")
async def my_enrollments(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(StudentEnrollment).options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
        ).where(StudentEnrollment.student_id == user.id).order_by(StudentEnrollment.enrolled_at.desc())
    )
    items = []
    for e in result.scalars().all():
        items.append({
            "id": str(e.id),
            "offering_id": str(e.offering_id),
            "registration_id": str(e.registration_id) if e.registration_id else None,
            "course_number": e.offering.course.course_number if e.offering and e.offering.course else None,
            "course_title": e.offering.course.title if e.offering and e.offering.course else None,
            "credit_structure": e.offering.course.credit_structure if e.offering and e.offering.course else None,
            "section": e.offering.section if e.offering else None,
            "status": e.status,
            "status_label": _ENROLLMENT_STATUS_LABELS.get(e.status, e.status),
            "enrolled_at": e.enrolled_at.isoformat(),
            "remarks": e.remarks,
        })
    return items


@router.delete("/{enrollment_id}", status_code=204)
async def withdraw(
    enrollment_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    e = await db.get(StudentEnrollment, enrollment_id)
    if not e or e.student_id != user.id:
        raise HTTPException(404, "Enrollment not found.")
    if e.registration_id:
        # Finding 3 (verification audit): individual withdrawal from an active
        # Course Registration is NOT a confirmed AVFU business rule — allowing
        # it would let one course sit permanently at "withdrawn" (neither
        # pending nor approved), which the all-courses-cleared check could
        # never satisfy, silently stranding the registration. Rather than
        # invent unconfirmed semantics for partial withdrawal, this path is
        # disabled for registration-linked items. Open question, not decided
        # here: whether/how a student should be able to drop a single course
        # from an already-submitted registration.
        raise HTTPException(400, "This course is part of a Course Registration and cannot be withdrawn individually.")
    if e.status != "pending":
        raise HTTPException(400, "Can only withdraw pending enrollments.")
    e.status = "withdrawn"; await db.commit()


# ── Registration-level views ─────────────────────────────────────────────────

@router.get("/registrations")
async def list_registrations(
    stage: Optional[str] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    q = select(CourseRegistration).options(*_REGISTRATION_LOAD_OPTIONS)
    if user.role == UserRole.STUDENT:
        q = q.where(CourseRegistration.student_id == user.id)
    elif user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR):
        pass  # unrestricted
    elif user.role == UserRole.HOD:
        if not user.department_id:
            return []
        # Programme<->Department many-to-many redesign — the student's OWN
        # department_id is authoritative now, never inferred via their
        # Programme's department (a Programme can have many Departments).
        q = (
            q.join(User, CourseRegistration.student_id == User.id)
             .where(User.department_id == user.department_id)
        )
    elif user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
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

    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
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

    return [_registration_dict(r) for r in registrations]


@router.get("/registrations/{registration_id}")
async def get_registration(registration_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(CourseRegistration).options(*_REGISTRATION_LOAD_OPTIONS).where(CourseRegistration.id == registration_id)
    )
    r = result.scalar_one_or_none()
    if not r: raise HTTPException(404, "Registration not found.")
    await _authorize_registration_view(r, user, db)
    return _registration_dict(r)


# ── Faculty/Admin: manage per-course enrollments (Course Teacher stage) ─────────

@router.get("/offering/{offering_id}")
async def offering_enrollments(
    offering_id: UUID, status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD,
        UserRole.FACULTY, UserRole.REGISTRAR,
    )),
):
    await _authorize_offering_management(offering_id, user, db)
    q = select(StudentEnrollment).options(
        selectinload(StudentEnrollment.student), selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
    ).where(StudentEnrollment.offering_id == offering_id)
    if status: q = q.where(StudentEnrollment.status == status)
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
        siblings = (await db.execute(
            select(StudentEnrollment).where(StudentEnrollment.registration_id == registration.id)
        )).scalars().all()
        if siblings and all(s.status == "approved" for s in siblings):
            registration.stage = "major_advisor_pending"
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
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD, UserRole.FACULTY, UserRole.REGISTRAR,
    )),
):
    e = await db.get(StudentEnrollment, enrollment_id)
    if not e: raise HTTPException(404, "Enrollment not found.")
    await _authorize_offering_management(e.offering_id, user, db)
    try:
        await _apply_enrollment_decision(e, status, remarks, user, db)
    except _EnrollmentDecisionError as exc:
        raise HTTPException(400, str(exc))
    return {"message": f"Enrollment {status}.", "status_label": _ENROLLMENT_STATUS_LABELS.get(status, status)}


@router.post("/offering/{offering_id}/bulk-approve")
async def bulk_approve(
    offering_id: UUID, body: BulkApproveRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD, UserRole.FACULTY, UserRole.REGISTRAR,
    )),
):
    """Finding 2 fix: now routes every row through `_apply_enrollment_decision`
    — the same shared logic `PATCH /{enrollment_id}` uses — so a
    registration-linked row can no longer bypass CR-3's approved/reverted-only
    restriction, mandatory-revert-remark rule, or the registration-stage
    cascade. Legacy (non-registration) rows are unaffected. Rows that fail
    per-item validation (e.g. a registration-linked row given an unsupported
    status) are skipped, not batch-aborting — consistent with this endpoint's
    original best-effort semantics."""
    await _authorize_offering_management(offering_id, user, db)
    updated = 0
    skipped = 0
    for eid in body.enrollment_ids:
        e = await db.get(StudentEnrollment, eid)
        if not e or e.offering_id != offering_id:
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
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR,
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
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    r = await db.get(CourseRegistration, registration_id)
    if not r: raise HTTPException(404, "Registration not found.")
    await _authorize_hod_registration(r, user, db)
    if r.stage != "hod_pending":
        raise HTTPException(400, "This registration is not awaiting HOD approval.")

    if body.approved:
        r.stage = "hod_approved"
        r.revert_remark = None; r.reverted_at = None
        await db.commit()
        return {"message": "Approved by HOD.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}

    if not body.remark:
        raise HTTPException(400, "A remark is required when reverting.")
    r.stage = "major_advisor_pending"  # immediately previous level (Rule 2)
    r.revert_remark = body.remark; r.reverted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Reverted to Major Advisor stage.", "stage": r.stage, "status_label": _REGISTRATION_STAGE_LABELS[r.stage]}
