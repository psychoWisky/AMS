"""Course & Course Offering management (Modules 5, 5.1, 5.2)."""
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, model_validator

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program, Department
from app.models.course import Course, CourseOffering, OfferingFaculty

router = APIRouter(prefix="/courses", tags=["Courses"])

CREDIT_FORMATS = ["2+0","0+2","1+1","0+1","3+0","2+1","1+2","3+1","4+0","0+4","2+2","4+1"]


class CourseIn(BaseModel):
    course_number: str
    title: str
    department_id: Optional[UUID] = None
    credit_theory: int = 0
    credit_practical: int = 0
    program_level: str = "UG"
    description: Optional[str] = None
    status: str = "active"

    @model_validator(mode="after")
    def set_course_type(self):
        if self.credit_theory > 0 and self.credit_practical > 0:
            self.course_type = "both"
        elif self.credit_practical > 0:
            self.course_type = "practical"
        else:
            self.course_type = "theory"
        return self
    course_type: str = "theory"


class CourseOut(BaseModel):
    id: UUID; course_number: str; title: str
    department_id: Optional[UUID]; credit_theory: int; credit_practical: int
    course_type: str; program_level: str; status: str
    credit_structure: str
    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, c: Course):
        return cls(
            id=c.id, course_number=c.course_number, title=c.title,
            department_id=c.department_id, credit_theory=c.credit_theory,
            credit_practical=c.credit_practical, course_type=c.course_type,
            program_level=c.program_level, status=c.status,
            credit_structure=c.credit_structure,
        )


class OfferingIn(BaseModel):
    calendar_id: UUID
    semester_id: UUID
    course_id: UUID
    department_id: UUID
    max_enrollment: int = 60
    section: Optional[str] = None
    practical_group: Optional[str] = None
    faculty_ids: List[UUID] = []

class OfferingOut(BaseModel):
    id: UUID; calendar_id: UUID; semester_id: UUID; course_id: UUID
    max_enrollment: int; section: Optional[str]; practical_group: Optional[str]
    status: str
    course_number: Optional[str] = None
    course_title: Optional[str] = None
    department_id: Optional[UUID] = None
    department_name: Optional[str] = None
    stream: Optional[str] = None
    faculty_names: List[str] = []
    enrolled_count: int = 0
    model_config = {"from_attributes": True}


# ── Credit structure listing ──────────────────────────────────────────────────

@router.get("/credit-formats")
async def credit_formats():
    return {"formats": CREDIT_FORMATS}


# ── Courses ───────────────────────────────────────────────────────────────────

@router.get("", response_model=List[CourseOut])
async def list_courses(
    status: Optional[str] = None, level: Optional[str] = None,
    department_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
):
    q = select(Course)
    if status: q = q.where(Course.status == status)
    if level:  q = q.where(Course.program_level == level)
    if department_id: q = q.where(Course.department_id == department_id)
    result = await db.execute(q.order_by(Course.course_number))
    return [CourseOut.from_orm(c) for c in result.scalars().all()]


@router.post("", status_code=201)
async def create_course(
    body: CourseIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    existing = await db.execute(select(Course).where(Course.course_number == body.course_number))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Course number already exists.")
    c = Course(**body.model_dump(), created_by=user.id)
    db.add(c); await db.commit(); await db.refresh(c)
    return CourseOut.from_orm(c)


@router.get("/{course_id}", response_model=CourseOut)
async def get_course(course_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    return CourseOut.from_orm(c)


@router.put("/{course_id}")
async def update_course(
    course_id: UUID, body: CourseIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    await db.commit(); return {"message": "Updated."}


@router.patch("/{course_id}/status")
async def update_course_status(
    course_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    c.status = status; await db.commit()
    return {"message": f"Status set to {status}."}


# ── Offerings ─────────────────────────────────────────────────────────────────

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


def _offering_dict(o: CourseOffering, enrolled: int) -> dict:
    return {
        "id": str(o.id), "calendar_id": str(o.calendar_id),
        "semester_id": str(o.semester_id), "course_id": str(o.course_id),
        "course_number": o.course.course_number if o.course else None,
        "course_title": o.course.title if o.course else None,
        "credit_structure": o.course.credit_structure if o.course else None,
        "max_enrollment": o.max_enrollment, "section": o.section,
        "practical_group": o.practical_group, "status": o.status,
        "department_id": str(o.department_id) if o.department_id else None,
        "department_name": o.department.name if o.department else None,
        "stream": o.department.stream if o.department else None,
        "faculty_names": [fa.faculty.full_name for fa in o.faculty_assignments if fa.faculty],
        "enrolled_count": enrolled,
    }


@router.get("/offerings/all")
async def list_all_offerings(
    semester_id: Optional[UUID] = None, calendar_id: Optional[UUID] = None,
    department_id: Optional[UUID] = None, level: Optional[str] = None,
    mine: bool = False,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    q = select(CourseOffering).options(
        selectinload(CourseOffering.course),
        selectinload(CourseOffering.department),
        selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty),
        selectinload(CourseOffering.enrollments),
    )
    if semester_id: q = q.where(CourseOffering.semester_id == semester_id)
    if calendar_id: q = q.where(CourseOffering.calendar_id == calendar_id)

    if user.role == UserRole.STUDENT:
        # Eligibility is derived server-side; client-supplied department_id/level are ignored.
        scope = await _resolve_student_scope(user, db)
        if not scope:
            return []
        q = q.where(
            CourseOffering.status == "published",
            CourseOffering.department_id == scope["department_id"],
        )
    elif mine:
        # "My courses" — scoping mirrors _authorize_offering_management's role priority
        # (enrollment.py) applied as a list filter instead of a single-offering guard.
        # The current user is always derived server-side; a client can never widen this
        # to another faculty member's assignments.
        if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR):
            pass  # unrestricted, same as _authorize_offering_management
        elif user.role == UserRole.HOD:
            if not user.department_id:
                return []
            q = q.where(CourseOffering.department_id == user.department_id)
        elif user.role == UserRole.FACULTY:
            q = q.where(CourseOffering.id.in_(
                select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id)
            ))
        else:
            return []  # fail closed for roles with no defined "mine" scope
    else:
        if department_id: q = q.where(CourseOffering.department_id == department_id)
        if level: q = q.join(Course, Course.id == CourseOffering.course_id).where(Course.program_level == level)

    result = await db.execute(q)
    offerings = result.scalars().all()

    if user.role == UserRole.STUDENT and offerings:
        offerings = [o for o in offerings if o.course and o.course.program_level == scope["level"]]

    items = []
    for o in offerings:
        enrolled = sum(1 for e in o.enrollments if e.status == "approved")
        items.append(_offering_dict(o, enrolled))
    return items


@router.post("/offerings", status_code=201)
async def create_offering(
    body: OfferingIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    offering = CourseOffering(
        calendar_id=body.calendar_id, semester_id=body.semester_id,
        course_id=body.course_id, department_id=body.department_id,
        max_enrollment=body.max_enrollment,
        section=body.section, practical_group=body.practical_group,
        created_by=user.id,
    )
    db.add(offering); await db.flush()
    for fid in body.faculty_ids:
        db.add(OfferingFaculty(offering_id=offering.id, faculty_id=fid))
    await db.commit(); await db.refresh(offering)
    return {"id": str(offering.id), "message": "Offering created."}


@router.get("/offerings/{offering_id}")
async def get_offering(offering_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(
        select(CourseOffering).options(
            selectinload(CourseOffering.course),
            selectinload(CourseOffering.department),
            selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty).selectinload(User.department),
            selectinload(CourseOffering.enrollments),
        ).where(CourseOffering.id == offering_id)
    )
    o = result.scalar_one_or_none()
    if not o: raise HTTPException(404, "Offering not found.")
    enrolled = sum(1 for e in o.enrollments if e.status == "approved")
    return {
        "id": str(o.id), "calendar_id": str(o.calendar_id),
        "semester_id": str(o.semester_id), "course_id": str(o.course_id),
        "course_number": o.course.course_number if o.course else None,
        "course_title": o.course.title if o.course else None,
        "credit_structure": o.course.credit_structure if o.course else None,
        "max_enrollment": o.max_enrollment, "section": o.section,
        "practical_group": o.practical_group, "status": o.status,
        "department_id": str(o.department_id) if o.department_id else None,
        "department_name": o.department.name if o.department else None,
        "stream": o.department.stream if o.department else None,
        "faculty": [{
            "id": str(fa.faculty_id), "name": fa.faculty.full_name,
            "designation": fa.faculty.designation,
            "department_name": fa.faculty.department.name if fa.faculty.department else None,
            "role": fa.role, "is_leader": fa.role == "primary",
        } for fa in o.faculty_assignments],
        "enrolled_count": enrolled,
    }


@router.patch("/offerings/{offering_id}/status")
async def update_offering_status(
    offering_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    o = await db.get(CourseOffering, offering_id)
    if not o: raise HTTPException(404, "Offering not found.")
    o.status = status; await db.commit()
    return {"message": f"Offering status set to {status}."}


@router.post("/offerings/{offering_id}/faculty")
async def assign_faculty(
    offering_id: UUID, faculty_id: UUID, role: str = "primary",
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    o = await db.get(CourseOffering, offering_id)
    if not o: raise HTTPException(404, "Offering not found.")
    db.add(OfferingFaculty(offering_id=offering_id, faculty_id=faculty_id, role=role))
    await db.commit(); return {"message": "Faculty assigned."}


@router.delete("/offerings/{offering_id}/faculty/{faculty_id}", status_code=204)
async def remove_faculty(
    offering_id: UUID, faculty_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    result = await db.execute(select(OfferingFaculty).where(
        OfferingFaculty.offering_id == offering_id,
        OfferingFaculty.faculty_id == faculty_id,
    ))
    fa = result.scalar_one_or_none()
    if fa: await db.delete(fa)
    await db.commit()
