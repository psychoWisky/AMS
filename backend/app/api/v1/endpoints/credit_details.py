"""Student Credit Details (BUSINESS_LOGIC.md Section K.6).

Read-only aggregation of a student's already-existing academic data
(enrollment, course, credit, department/program) into a single view.

Renamed from the original "academic_progress"/"/academic-progress" naming
(Module 1 Phase A) once BUSINESS_LOGIC.md confirmed this endpoint's actual
content — a per-course credit ledger + credit summary — corresponds to the
"Student Credit Details" module specifically, not a generic "academic
progress" concept. See STUDENT_SIDE_IMPLEMENTATION_PLAN.md for the rename
record. The frontend page consuming this endpoint remains at the broader
`/academic-progress` route, since that page also aggregates GPA/committee
data from other endpoints and is not itself a 1:1 match for this rename.

Deliberately does NOT model: Progress Report status/workflow, Sign/approval,
Thesis Evaluation, required-credit totals, or Remaining Credit — none of these
have a confirmed source anywhere in the existing schema (see investigation).
"""
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.base import get_db
from app.core.dependencies import get_current_user
from app.models.user import User, UserRole, Program, Department
from app.models.course import CourseOffering, OfferingFaculty
from app.models.enrollment import StudentEnrollment
from app.models.research import AdvisoryCommittee, CommitteeMember
# Programme<->Department many-to-many redesign — single shared implementation
# in app/core/student_scope.py.
from app.core.student_scope import resolve_student_department_id

router = APIRouter(prefix="/credit-details", tags=["Student Credit Details"])

_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)


# ── Authorization ────────────────────────────────────────────────────────────
# Duplicated (not shared) from grading.py's _authorize_student_academic_view,
# consistent with this project's existing convention of each endpoint file
# carrying its own local authorization helper (see courses.py/enrollment.py's
# duplicated _resolve_student_scope, research.py's own committee helpers).

async def _authorize_student_credit_view(student_id: UUID, user: User, db: AsyncSession) -> None:
    if user.role in _ADMIN_ROLES:
        return
    if user.role == UserRole.STUDENT:
        if student_id == user.id:
            return
        raise HTTPException(403, "You can only view your own credit details.")
    if user.role == UserRole.HOD:
        dept_id = await resolve_student_department_id(student_id, db)
        if dept_id and user.department_id and dept_id == user.department_id:
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
        raise HTTPException(403, "You can only view credit details for students you teach or advise.")
    raise HTTPException(403, "Insufficient permissions.")


@router.get("/student/{student_id}")
async def get_student_credit_details(
    student_id: UUID,
    calendar_id: Optional[UUID] = None,
    semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    student = await db.get(User, student_id)
    if not student:
        raise HTTPException(404, "Student not found.")
    await _authorize_student_credit_view(student_id, user, db)

    program = await db.get(Program, student.program_id) if student.program_id else None
    department = await db.get(Department, student.department_id) if student.department_id else None

    # Courses: derived from the student's own enrollments — no "research course"
    # flag exists anywhere in the schema, so this lists all enrolled courses,
    # not a filtered "research courses" subset (see investigation report).
    q = (
        select(StudentEnrollment)
        .options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.semester),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.calendar),
        )
        .where(StudentEnrollment.student_id == student_id)
    )
    if calendar_id or semester_id:
        q = q.join(CourseOffering, StudentEnrollment.offering_id == CourseOffering.id)
        if calendar_id: q = q.where(CourseOffering.calendar_id == calendar_id)
        if semester_id: q = q.where(CourseOffering.semester_id == semester_id)

    result = await db.execute(q.order_by(StudentEnrollment.enrolled_at))
    enrollments: List[StudentEnrollment] = result.scalars().all()

    courses = []
    total_credit_taken = 0
    for e in enrollments:
        o = e.offering
        c = o.course if o else None
        if not o or not c:
            continue
        if e.status == "approved":
            total_credit_taken += c.total_credits
        courses.append({
            "offering_id": str(o.id),
            "course_number": c.course_number,
            "course_title": c.title,
            "course_credit": f"{c.total_credits}({c.credit_structure})",
            "credit_theory": c.credit_theory,
            "credit_practical": c.credit_practical,
            "semester_name": o.semester.name if o.semester else None,
            "academic_year": o.calendar.academic_year if o.calendar else None,
            "enrollment_status": e.status,
        })

    return {
        "student": {
            "id": str(student.id),
            "name": student.full_name,
            "roll_no": student.student_roll,
            "department_name": department.name if department else None,
            "program_name": program.name if program else None,
            "program_level": program.level if program else None,
        },
        "courses": courses,
        "credit_summary": {
            "total_credit_taken": total_credit_taken,
        },
    }
