"""Student & Teacher Enrollment Management (Modules 5.3, 5.4)."""
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
from app.models.enrollment import StudentEnrollment
from app.models.course import Course, CourseOffering, OfferingFaculty

router = APIRouter(prefix="/enrollment", tags=["Enrollment"])


async def _resolve_student_scope(user: User, db: AsyncSession) -> Optional[dict]:
    """Resolve a student's (program_level, department_id) from User -> Program -> Department.
    Returns None if the student's academic program is not fully configured (fail closed)."""
    if not user.program_id:
        return None
    program = await db.get(Program, user.program_id)
    if not program or not program.department_id:
        return None
    department = await db.get(Department, program.department_id)
    if not department or not department.stream:
        return None
    return {"level": program.level, "department_id": program.department_id}


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


class EnrollRequest(BaseModel):
    offering_id: UUID

class BulkApproveRequest(BaseModel):
    enrollment_ids: List[UUID]
    status: str  # approved / rejected
    remarks: Optional[str] = None


def _enroll_dict(e: StudentEnrollment) -> dict:
    return {
        "id": str(e.id),
        "student_id": str(e.student_id),
        "student_name": e.student.full_name if e.student else None,
        "student_roll": e.student.student_roll if e.student else None,
        "student_email": e.student.email if e.student else None,
        "offering_id": str(e.offering_id),
        "status": e.status,
        "enrolled_at": e.enrolled_at.isoformat(),
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
        "remarks": e.remarks,
    }


# ── Student: enroll self ──────────────────────────────────────────────────────

@router.post("", status_code=201)
async def enroll(
    body: EnrollRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    offering = await db.get(CourseOffering, body.offering_id)
    if not offering or offering.status != "published":
        raise HTTPException(400, "Course offering not available for enrollment.")

    # Server-side eligibility authorization — never trust the frontend's course list.
    scope = await _resolve_student_scope(user, db)
    if not scope:
        raise HTTPException(403, "Your academic program is not configured; contact administration.")
    course = await db.get(Course, offering.course_id)
    if not course or course.program_level != scope["level"] or offering.department_id != scope["department_id"]:
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
            "course_number": e.offering.course.course_number if e.offering and e.offering.course else None,
            "course_title": e.offering.course.title if e.offering and e.offering.course else None,
            "credit_structure": e.offering.course.credit_structure if e.offering and e.offering.course else None,
            "section": e.offering.section if e.offering else None,
            "status": e.status,
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
    if e.status != "pending":
        raise HTTPException(400, "Can only withdraw pending enrollments.")
    e.status = "withdrawn"; await db.commit()


# ── Faculty/Admin: manage enrollments ─────────────────────────────────────────

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
    q = select(StudentEnrollment).options(selectinload(StudentEnrollment.student)).where(
        StudentEnrollment.offering_id == offering_id
    )
    if status: q = q.where(StudentEnrollment.status == status)
    result = await db.execute(q.order_by(StudentEnrollment.enrolled_at))
    return [_enroll_dict(e) for e in result.scalars().all()]


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
    if e.status not in ("pending",):
        raise HTTPException(400, "Only pending enrollments can be processed.")
    e.status = status
    e.processed_by = user.id
    e.processed_at = datetime.now(timezone.utc)
    e.remarks = remarks
    await db.commit()
    return {"message": f"Enrollment {status}."}


@router.post("/offering/{offering_id}/bulk-approve")
async def bulk_approve(
    offering_id: UUID, body: BulkApproveRequest, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD, UserRole.FACULTY, UserRole.REGISTRAR,
    )),
):
    await _authorize_offering_management(offering_id, user, db)
    updated = 0
    for eid in body.enrollment_ids:
        e = await db.get(StudentEnrollment, eid)
        if e and e.offering_id == offering_id and e.status == "pending":
            e.status = body.status
            e.processed_by = user.id
            e.processed_at = datetime.now(timezone.utc)
            e.remarks = body.remarks
            updated += 1
    await db.commit()
    return {"message": f"{updated} enrollments {body.status}."}
