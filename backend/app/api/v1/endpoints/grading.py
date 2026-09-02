"""Grading, Tabulation, GPA (Modules 5.5, 6, 7, 8)."""
import random, string
from typing import Optional, List
from uuid import UUID
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program
from app.models.grading import GradeSheet, GradeEntry, ApprovalStage, DigitalSignature, compute_grade
from app.models.course import CourseOffering, OfferingFaculty
from app.models.enrollment import StudentEnrollment
from app.models.research import AdvisoryCommittee, CommitteeMember

router = APIRouter(prefix="/grading", tags=["Grading"])

_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)
# Roles that see/approve every gradesheet regardless of department/offering —
# REGISTRAR and EXAMINER are university-wide approval stages in APPROVAL_PIPELINE
# (not department-scoped), consistent with how they're treated as unrestricted
# elsewhere in this codebase (e.g. enrollment.py's "mine" scoping).
_GRADING_UNRESTRICTED_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR, UserRole.EXAMINER)


async def _authorize_offering_grading(offering_id: UUID, user: User, db: AsyncSession) -> None:
    """BUSINESS_LOGIC.md Section P — department-isolation audit finding. Every
    gradesheet-touching endpoint (view, create, edit, submit, and each approval
    stage) must be tied back to the offering's department/assignment, not just
    a bare role-name match. Mirrors the established pattern in courses.py's
    _authorize_department_manage / enrollment.py's _authorize_offering_management.
    """
    if user.role in _GRADING_UNRESTRICTED_ROLES:
        return
    offering = await db.get(CourseOffering, offering_id)
    if not offering:
        raise HTTPException(404, "Offering not found.")
    if user.role == UserRole.HOD:
        if user.department_id and offering.department_id and user.department_id == offering.department_id:
            return
        raise HTTPException(403, "You can only access gradesheets within your own department.")
    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        assigned = await db.execute(
            select(OfferingFaculty.id).where(
                OfferingFaculty.offering_id == offering_id, OfferingFaculty.faculty_id == user.id,
            ).limit(1)
        )
        if assigned.scalar_one_or_none():
            return
        raise HTTPException(403, "You can only access gradesheets for courses you are assigned to teach.")
    raise HTTPException(403, "Insufficient permissions.")


async def _authorize_student_academic_view(student_id: UUID, user: User, db: AsyncSession) -> None:
    """Who may view a student's academic-progress data (GPA, etc.).
    Mirrors the role-priority style of research.py's _authorize_committee_view /
    enrollment.py's _authorize_offering_management: admins unrestricted, HOD via
    the student's Program.department_id, student self-only, and faculty/research
    supervisor only via an established relationship (shared course assignment or
    advisory committee membership) — never a bare role check."""
    if user.role in _ADMIN_ROLES:
        return
    if user.role == UserRole.STUDENT:
        if student_id == user.id:
            return
        raise HTTPException(403, "You can only view your own academic progress.")
    if user.role == UserRole.HOD:
        student = await db.get(User, student_id)
        if student and student.program_id and user.department_id:
            program = await db.get(Program, student.program_id)
            if program and program.department_id == user.department_id:
                return
        raise HTTPException(403, "You can only view students within your own department.")
    if user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        course_link = await db.execute(
            select(OfferingFaculty.id)
            .join(StudentEnrollment, StudentEnrollment.offering_id == OfferingFaculty.offering_id)
            .where(OfferingFaculty.faculty_id == user.id, StudentEnrollment.student_id == student_id)
            .limit(1)
        )
        if course_link.scalar_one_or_none():
            return
        committee_link = await db.execute(
            select(CommitteeMember.id)
            .join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
            .where(CommitteeMember.faculty_id == user.id, AdvisoryCommittee.student_id == student_id)
            .limit(1)
        )
        if committee_link.scalar_one_or_none():
            return
        raise HTTPException(403, "You can only view academic progress for students you teach or advise.")
    raise HTTPException(403, "Insufficient permissions.")


APPROVAL_PIPELINE = [
    (1, "faculty"),
    (2, "hod"),
    (3, "registrar"),
    (4, "examiner"),
    (5, "academic_admin"),
]


class GradeEntryIn(BaseModel):
    student_id: UUID
    enrollment_id: Optional[UUID] = None
    internal_marks: Optional[float] = None
    external_marks: Optional[float] = None
    is_absent: bool = False
    remarks: Optional[str] = None

class BulkGradeIn(BaseModel):
    entries: List[GradeEntryIn]

class SignatureRequest(BaseModel):
    pin: str
    otp: str


def _compute_totals(entry: GradeEntry, offering: CourseOffering) -> None:
    if entry.is_absent:
        entry.grade_letter = "F"; entry.grade_points = 0.0; entry.total_marks = 0.0
        return
    course = offering.course
    if course.credit_practical == 0:
        total = entry.external_marks or 0
    elif course.credit_theory == 0:
        total = entry.internal_marks or 0
    else:
        total = (entry.internal_marks or 0) * 0.4 + (entry.external_marks or 0) * 0.6
    entry.total_marks = round(total, 2)
    entry.grade_letter, entry.grade_points = compute_grade(entry.total_marks)


def _send_notification_email(to: str, subject: str, body: str):
    # Delegates to the shared helper (Section 28.4, STUDENT_SIDE_IMPLEMENTATION_PLAN.md)
    # now reused by Orientation credential emails — kept as a thin wrapper here so this
    # file's existing call sites don't need to change.
    from app.core.email import send_email
    send_email(to, subject, body)


# ── Grade Sheets ──────────────────────────────────────────────────────────────

@router.post("/sheets", status_code=201)
async def create_sheet(
    offering_id: UUID, sheet_type: str = "final",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    await _authorize_offering_grading(offering_id, user, db)
    existing = await db.execute(
        select(GradeSheet).where(GradeSheet.offering_id == offering_id, GradeSheet.sheet_type == sheet_type)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Grade sheet already exists for this offering and type.")

    sheet = GradeSheet(offering_id=offering_id, sheet_type=sheet_type, created_by=user.id)
    db.add(sheet); await db.flush()

    # Auto-populate entries from approved enrollments
    result = await db.execute(
        select(StudentEnrollment).where(
            StudentEnrollment.offering_id == offering_id,
            StudentEnrollment.status == "approved",
        )
    )
    for e in result.scalars().all():
        db.add(GradeEntry(sheet_id=sheet.id, student_id=e.student_id, enrollment_id=e.id))

    # Create approval pipeline stages
    for stage, role in APPROVAL_PIPELINE:
        db.add(ApprovalStage(sheet_id=sheet.id, stage=stage, role_required=role))

    await db.commit(); await db.refresh(sheet)
    return {"id": str(sheet.id), "message": "Grade sheet created with enrolled students."}


@router.get("/sheets/{sheet_id}")
async def get_sheet(sheet_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(GradeSheet).options(
            selectinload(GradeSheet.entries).selectinload(GradeEntry.student),
            selectinload(GradeSheet.offering).selectinload(CourseOffering.course),
            selectinload(GradeSheet.approvals).selectinload(ApprovalStage.approver),
        ).where(GradeSheet.id == sheet_id)
    )
    sheet = result.scalar_one_or_none()
    if not sheet: raise HTTPException(404, "Sheet not found.")
    await _authorize_offering_grading(sheet.offering_id, user, db)
    return {
        "id": str(sheet.id),
        "offering_id": str(sheet.offering_id),
        "course_title": sheet.offering.course.title if sheet.offering and sheet.offering.course else None,
        "sheet_type": sheet.sheet_type, "status": sheet.status, "is_locked": sheet.is_locked,
        "entries": [{
            "id": str(e.id), "student_id": str(e.student_id),
            "student_name": e.student.full_name if e.student else None,
            "student_roll": e.student.student_roll if e.student else None,
            "internal_marks": e.internal_marks, "external_marks": e.external_marks,
            "total_marks": e.total_marks, "grade_letter": e.grade_letter,
            "grade_points": e.grade_points, "is_absent": e.is_absent, "remarks": e.remarks,
        } for e in sheet.entries],
        "approvals": [{
            "stage": a.stage, "role_required": a.role_required,
            "status": a.status, "remarks": a.remarks,
            "approver_name": a.approver.full_name if a.approver else None,
            "signed_at": a.signed_at.isoformat() if a.signed_at else None,
        } for a in sheet.approvals],
    }


@router.get("/offering/{offering_id}/sheets")
async def sheets_for_offering(
    offering_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    await _authorize_offering_grading(offering_id, user, db)
    result = await db.execute(select(GradeSheet).where(GradeSheet.offering_id == offering_id))
    return [{"id": str(s.id), "sheet_type": s.sheet_type, "status": s.status, "is_locked": s.is_locked} for s in result.scalars().all()]


@router.put("/sheets/{sheet_id}/entries")
async def save_grades(
    sheet_id: UUID, body: BulkGradeIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    result = await db.execute(
        select(GradeSheet).options(selectinload(GradeSheet.offering).selectinload(CourseOffering.course)).where(GradeSheet.id == sheet_id)
    )
    sheet = result.scalar_one_or_none()
    if not sheet: raise HTTPException(404, "Sheet not found.")
    await _authorize_offering_grading(sheet.offering_id, user, db)
    if sheet.is_locked: raise HTTPException(400, "Grade sheet is locked.")
    if sheet.status not in ("draft", "submitted"):
        raise HTTPException(400, "Grades can only be edited in draft/submitted state.")

    for item in body.entries:
        entry_result = await db.execute(
            select(GradeEntry).where(GradeEntry.sheet_id == sheet_id, GradeEntry.student_id == item.student_id)
        )
        entry = entry_result.scalar_one_or_none()
        if not entry:
            entry = GradeEntry(sheet_id=sheet_id, student_id=item.student_id)
            db.add(entry)
        entry.internal_marks = item.internal_marks
        entry.external_marks = item.external_marks
        entry.is_absent = item.is_absent
        entry.remarks = item.remarks
        entry.updated_by = user.id
        if sheet.offering:
            _compute_totals(entry, sheet.offering)

    await db.commit()
    return {"message": "Grades saved."}


@router.patch("/sheets/{sheet_id}/submit")
async def submit_sheet(
    sheet_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    sheet = await db.get(GradeSheet, sheet_id)
    if not sheet: raise HTTPException(404, "Sheet not found.")
    await _authorize_offering_grading(sheet.offering_id, user, db)
    if sheet.status != "draft": raise HTTPException(400, "Sheet is not in draft.")
    sheet.status = "submitted"; sheet.submitted_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Sheet submitted for approval."}


# ── Approval workflow ─────────────────────────────────────────────────────────

@router.get("/sheets/{sheet_id}/approval/otp")
async def request_approval_otp(
    sheet_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sheet = await db.get(GradeSheet, sheet_id)
    if not sheet: raise HTTPException(404, "Sheet not found.")
    await _authorize_offering_grading(sheet.offering_id, user, db)
    # Find the current pending stage matching user role
    result = await db.execute(
        select(ApprovalStage).where(
            ApprovalStage.sheet_id == sheet_id,
            ApprovalStage.status == "pending",
            ApprovalStage.role_required == user.role.value,
        ).order_by(ApprovalStage.stage).limit(1)
    )
    stage = result.scalar_one_or_none()
    if not stage: raise HTTPException(400, "No pending approval stage for your role.")

    # Generate OTP
    otp_code = "".join(random.choices(string.digits, k=6))
    sig = DigitalSignature(
        approval_stage_id=stage.id, user_id=user.id,
        otp_code=otp_code,
        otp_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(sig); await db.commit()
    _send_notification_email(user.email, "AMS Approval OTP",
                             f"Your OTP for grade sheet approval is: {otp_code}\n\nValid for 10 minutes.")
    return {"message": "OTP sent to your email.", "dev_otp": otp_code}


@router.post("/sheets/{sheet_id}/approve")
async def approve_sheet_stage(
    sheet_id: UUID, body: SignatureRequest, request: Request,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    sheet_lookup = await db.get(GradeSheet, sheet_id)
    if not sheet_lookup: raise HTTPException(404, "Sheet not found.")
    await _authorize_offering_grading(sheet_lookup.offering_id, user, db)

    result = await db.execute(
        select(ApprovalStage).where(
            ApprovalStage.sheet_id == sheet_id,
            ApprovalStage.status == "pending",
            ApprovalStage.role_required == user.role.value,
        ).order_by(ApprovalStage.stage).limit(1)
    )
    stage = result.scalar_one_or_none()
    if not stage: raise HTTPException(400, "No pending stage for your role.")

    # Verify OTP
    now = datetime.now(timezone.utc)
    sig_result = await db.execute(
        select(DigitalSignature).where(
            DigitalSignature.approval_stage_id == stage.id,
            DigitalSignature.user_id == user.id,
            DigitalSignature.otp_code == body.otp,
            DigitalSignature.otp_used == False,
            DigitalSignature.otp_expires_at > now,
        ).order_by(DigitalSignature.created_at.desc()).limit(1)
    )
    sig = sig_result.scalar_one_or_none()
    if not sig: raise HTTPException(400, "Invalid or expired OTP.")

    sig.otp_used = True
    sig.verified_at = now
    sig.ip_address = request.client.host if request.client else None

    stage.status = "approved"
    stage.approver_id = user.id
    stage.otp_verified = True
    stage.signed_at = now
    stage.ip_address = request.client.host if request.client else None

    # Check if all stages approved → mark sheet approved/published
    sheet = await db.get(GradeSheet, sheet_id)
    stages_result = await db.execute(select(ApprovalStage).where(ApprovalStage.sheet_id == sheet_id))
    all_stages = stages_result.scalars().all()
    if all(s.status == "approved" for s in all_stages):
        sheet.status = "approved"
        sheet.is_locked = True
    else:
        sheet.status = "under_review"

    await db.commit()
    return {"message": f"Stage {stage.stage} approved."}


@router.post("/sheets/{sheet_id}/reject")
async def reject_sheet_stage(
    sheet_id: UUID, remarks: str, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sheet = await db.get(GradeSheet, sheet_id)
    if not sheet: raise HTTPException(404, "Sheet not found.")
    await _authorize_offering_grading(sheet.offering_id, user, db)

    result = await db.execute(
        select(ApprovalStage).where(
            ApprovalStage.sheet_id == sheet_id,
            ApprovalStage.status == "pending",
            ApprovalStage.role_required == user.role.value,
        ).order_by(ApprovalStage.stage).limit(1)
    )
    stage = result.scalar_one_or_none()
    if not stage: raise HTTPException(400, "No pending stage for your role.")
    if not remarks.strip(): raise HTTPException(400, "Rejection remarks required.")

    stage.status = "rejected"; stage.remarks = remarks; stage.approver_id = user.id
    sheet.status = "draft"; sheet.is_locked = False

    # Reset all stages back to pending
    stages_result = await db.execute(select(ApprovalStage).where(ApprovalStage.sheet_id == sheet_id))
    for s in stages_result.scalars().all():
        s.status = "pending"

    await db.commit()
    return {"message": "Sheet returned to draft for revision."}


@router.patch("/sheets/{sheet_id}/publish")
async def publish_sheet(
    sheet_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)),
):
    sheet = await db.get(GradeSheet, sheet_id)
    if not sheet: raise HTTPException(404, "Sheet not found.")
    if sheet.status != "approved": raise HTTPException(400, "Sheet must be fully approved first.")
    sheet.status = "published"; sheet.published_at = datetime.now(timezone.utc)
    await db.commit()
    return {"message": "Results published."}


# ── GPA / CGPA ────────────────────────────────────────────────────────────────

@router.get("/student/{student_id}/gpa")
async def student_gpa(student_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await _authorize_student_academic_view(student_id, user, db)
    result = await db.execute(
        select(GradeEntry).options(
            selectinload(GradeEntry.sheet).selectinload(GradeSheet.offering).selectinload(CourseOffering.course)
        ).where(GradeEntry.student_id == student_id)
    )
    entries = result.scalars().all()
    published = [e for e in entries if e.sheet and e.sheet.status == "published"]

    sem_groups: dict = {}
    for e in published:
        if not e.sheet or not e.sheet.offering or not e.sheet.offering.course: continue
        sem_id = str(e.sheet.offering.semester_id)
        sem_groups.setdefault(sem_id, []).append(e)

    sem_gpas = []
    total_credits = total_weighted = 0.0
    for sem_id, sem_entries in sem_groups.items():
        credits_sum = weighted_sum = 0.0
        for e in sem_entries:
            c = e.sheet.offering.course.total_credits
            gp = e.grade_points or 0
            credits_sum += c; weighted_sum += c * gp
        sgpa = round(weighted_sum / credits_sum, 2) if credits_sum else 0.0
        sem_gpas.append({"semester_id": sem_id, "sgpa": sgpa, "credits": credits_sum})
        total_credits += credits_sum; total_weighted += weighted_sum

    cgpa = round(total_weighted / total_credits, 2) if total_credits else 0.0
    return {"student_id": str(student_id), "cgpa": cgpa, "semesters": sem_gpas}
