"""Super Admin Students management (BUSINESS_LOGIC.md section AA).

There is no separate Student table: a student is a `User` (see
`app.core.dependencies.student_user_clause`). This router is the ONLY place a
Super Admin lists and edits students — they are deliberately excluded from the
generic user directory (`GET /auth/users`). Every endpoint here is SUPER_ADMIN-
only; no other role gets the global student list.

What a student's fields really are (nothing is duplicated or invented):
  * identity / profile — columns on `User`: names, email, `student_roll`,
    mobile, date of birth, gender, blood group, father's name, ABC ID, address.
  * academic placement — `User.program_id` (Programme), `User.department_id`
    (the student's own Department, authoritative for students) and the College.
    College is independent of Department (section T.5). `User.college_id` is the
    student's college and the only source used here: Orientation copies the
    candidate's college onto the account when it is created (migration
    0023_backfill_student_college did the same for accounts issued earlier), and
    the Orientation candidate's own college is never consulted afterwards.
  * Academic Year / Semester are NOT stored on the student. They are derived
    from the student's Course Registrations and Enrollments, so the filters
    and the "latest" columns follow those relationships and cannot be edited
    here (they change by registering/enrolling, never by editing a profile).
"""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints.departments import validate_program_department_pair
from app.core import profile_fields as pf
from app.core.dependencies import require_roles, student_user_clause
from app.db.base import get_db
from app.models.academic import AcademicCalendar, Semester
from app.models.course import CourseOffering
from app.models.enrollment import CourseRegistration, StudentEnrollment
from app.models.orientation import OrientationCandidate
from app.models.user import College, CollegeProgram, Department, Program, User, UserRole
from app.core.profile_fields import blank_to_none, is_blank

router = APIRouter(prefix="/students", tags=["Students (Super Admin)"])

_MANAGE_ROLES = (UserRole.SUPER_ADMIN,)


class StudentUpdate(BaseModel):
    """PATCH body — only fields actually sent are applied. Optional profile
    fields may be cleared with null/blank; the fields a student cannot exist
    without (name, email, roll number, programme, department, status) may be
    replaced but never cleared (422)."""
    email: Optional[EmailStr] = None
    first_name: Optional[str] = Field(None, max_length=100)
    middle_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    student_roll: Optional[str] = Field(None, max_length=50)
    mobile: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    blood_group: Optional[str] = None
    father_name: Optional[str] = Field(None, max_length=200)
    abc_id: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = None
    admission_year: Optional[int] = Field(None, ge=1950, le=2100)
    program_id: Optional[UUID] = None
    department_id: Optional[UUID] = None
    college_id: Optional[UUID] = None
    is_active: Optional[bool] = None

    @field_validator("email", "program_id", "department_id", "is_active", mode="before")
    @classmethod
    def _required_not_null(cls, v):
        if v is None:
            raise ValueError("This field cannot be cleared.")
        return v

    @field_validator("first_name", "last_name", "student_roll", mode="before")
    @classmethod
    def _required_text(cls, v):
        if v is None or not isinstance(v, str) or is_blank(v):
            raise ValueError("This field is required and cannot be blank.")
        return v.strip()

    @field_validator("middle_name", "father_name", "abc_id", "address", mode="before")
    @classmethod
    def _optional_text(cls, v):
        return blank_to_none(v)

    @field_validator("gender")
    @classmethod
    def _gender(cls, v):
        return pf.canonical_choice(v, pf.GENDERS, "Gender")

    @field_validator("blood_group")
    @classmethod
    def _blood_group(cls, v):
        return pf.canonical_choice(v, pf.BLOOD_GROUPS, "Blood group")

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v):
        return pf.check_mobile(v)

    @field_validator("date_of_birth")
    @classmethod
    def _dob(cls, v):
        return pf.check_date_of_birth(v)


async def _college_names(db: AsyncSession) -> dict:
    return {cid: name for cid, name in (await db.execute(select(College.id, College.name))).all()}


def _student_dict(u: User, latest: Optional[tuple] = None, college_names: Optional[dict] = None) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "title": u.title,
        "first_name": u.first_name,
        "middle_name": u.middle_name,
        "last_name": u.last_name,
        "student_roll": u.student_roll,
        "mobile": u.mobile,
        "date_of_birth": u.date_of_birth.isoformat() if u.date_of_birth else None,
        "gender": u.gender,
        "blood_group": u.blood_group,
        "father_name": u.father_name,
        "abc_id": u.abc_id,
        "address": u.address,
        "admission_year": u.admission_year,
        "program_id": str(u.program_id) if u.program_id else None,
        "program_name": u.program.name if u.program else None,
        "program_code": u.program.code if u.program else None,
        "department_id": str(u.department_id) if u.department_id else None,
        "department_name": u.department.name if u.department else None,
        "college_id": str(u.college_id) if u.college_id else None,
        "college_name": (college_names or {}).get(u.college_id) if u.college_id else None,
        "is_active": u.is_active,
        "must_change_password": u.must_change_password,
        # Derived from Course Registrations / Enrollments, never stored on the student.
        "latest_academic_year": latest[0] if latest else None,
        "latest_semester": latest[1] if latest else None,
    }


_LOAD = (selectinload(User.program), selectinload(User.department))


async def _validate_college_programme(db: AsyncSession, college_id: Optional[UUID], program_id: Optional[UUID]) -> None:
    """A student's Programme must be one the student's College offers: the
    pair must exist in `ams_college_programs` (the mapping Super Admin
    configures). Skipped when either side is empty. Enforced here, in the
    backend — the Edit dialog's dependent dropdowns are only a convenience."""
    if not college_id or not program_id:
        return
    mapped = (await db.execute(
        select(CollegeProgram.id).where(CollegeProgram.college_id == college_id, CollegeProgram.program_id == program_id).limit(1)
    )).scalar_one_or_none()
    if mapped is None:
        college, program = await db.get(College, college_id), await db.get(Program, program_id)
        raise HTTPException(
            400,
            f"Programme '{program.name}' is not offered by College '{college.name}'. "
            "Map the programme to the college under Administration first, or choose a different combination.",
        )


async def _latest_academic_records(db: AsyncSession, student_ids: list[UUID]) -> dict[UUID, tuple]:
    """student_id -> (academic year, semester name) of the most recent semester
    (by semester start date) in which the student has a Course Registration or
    an Enrollment. Two batched queries for the whole page — no per-student queries."""
    if not student_ids:
        return {}
    reg = (
        select(CourseRegistration.student_id, AcademicCalendar.academic_year, Semester.name, Semester.start_date)
        .select_from(CourseRegistration)
        .join(Semester, Semester.id == CourseRegistration.semester_id)
        .join(AcademicCalendar, AcademicCalendar.id == CourseRegistration.calendar_id)
        .where(CourseRegistration.student_id.in_(student_ids))
    )
    enr = (
        select(StudentEnrollment.student_id, AcademicCalendar.academic_year, Semester.name, Semester.start_date)
        .select_from(StudentEnrollment)
        .join(CourseOffering, CourseOffering.id == StudentEnrollment.offering_id)
        .join(Semester, Semester.id == CourseOffering.semester_id)
        .join(AcademicCalendar, AcademicCalendar.id == CourseOffering.calendar_id)
        .where(StudentEnrollment.student_id.in_(student_ids))
    )
    best: dict[UUID, tuple] = {}
    for query in (reg, enr):
        for sid, year, semester, start in (await db.execute(query)).all():
            if sid not in best or start > best[sid][2]:
                best[sid] = (year, semester, start)
    return {sid: (v[0], v[1]) for sid, v in best.items()}


@router.get("")
async def list_students(
    q: Optional[str] = None,
    academic_year_id: Optional[UUID] = None,
    semester_id: Optional[UUID] = None,
    department_id: Optional[UUID] = None,
    program_id: Optional[UUID] = None,
    college_id: Optional[UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    """All students, newest filters combined with AND, paginated. Filters are
    applied in SQL:
      department_id / program_id / college_id — the student's own columns.
      academic_year_id (an AcademicCalendar) / semester_id — students who have a
        Course Registration OR an Enrollment in that year / semester (both
        given: in that semester of that year). `IN (subquery)` is used, so a
        student with many matching records still appears once.
    `q` searches name, email, roll number and ABC ID."""
    conditions = [student_user_clause()]
    if department_id:
        conditions.append(User.department_id == department_id)
    if program_id:
        conditions.append(User.program_id == program_id)
    if college_id:
        conditions.append(User.college_id == college_id)
    if academic_year_id or semester_id:
        reg = select(CourseRegistration.student_id)
        enr = select(StudentEnrollment.student_id).join(CourseOffering, CourseOffering.id == StudentEnrollment.offering_id)
        if academic_year_id:
            reg = reg.where(CourseRegistration.calendar_id == academic_year_id)
            enr = enr.where(CourseOffering.calendar_id == academic_year_id)
        if semester_id:
            reg = reg.where(CourseRegistration.semester_id == semester_id)
            enr = enr.where(CourseOffering.semester_id == semester_id)
        conditions.append(or_(User.id.in_(reg), User.id.in_(enr)))
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(or_(
            func.concat_ws(" ", User.first_name, User.middle_name, User.last_name).ilike(like),
            User.email.ilike(like), User.student_roll.ilike(like), User.abc_id.ilike(like),
        ))

    total = (await db.execute(select(func.count()).select_from(User).where(*conditions))).scalar_one()
    students = (await db.execute(
        select(User).where(*conditions).options(*_LOAD)
        .order_by(User.student_roll.asc().nulls_last(), User.first_name, User.id)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    latest = await _latest_academic_records(db, [u.id for u in students])
    names = await _college_names(db)
    return {
        "items": [_student_dict(u, latest.get(u.id), names) for u in students],
        "total": total, "page": page, "page_size": page_size,
    }


async def _get_student(db: AsyncSession, student_id: UUID) -> User:
    student = (await db.execute(
        select(User).where(User.id == student_id, student_user_clause()).options(*_LOAD)
    )).scalar_one_or_none()
    if not student:
        raise HTTPException(404, "Student not found.")
    return student


async def _render(db: AsyncSession, student_id: UUID) -> dict:
    student = await _get_student(db, student_id)
    latest = await _latest_academic_records(db, [student.id])
    return _student_dict(student, latest.get(student.id), await _college_names(db))


@router.get("/{student_id}")
async def get_student(
    student_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    return await _render(db, student_id)


@router.patch("/{student_id}")
async def update_student(
    student_id: UUID, body: StudentUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    """Edit any student profile field. Foreign keys are verified to exist, the
    resulting Programme/Department pair must satisfy the existing Programme<->
    Department association (the same rule Users already obey), and — new — the
    resulting College + Programme must be a mapped pair (`ams_college_programs`).
    Both rules judge the COMPLETE RESULTING STATE (fields in the request replace
    the stored ones; the rest are read from the student), and every check runs
    before any field is written, so a rejected request changes nothing. They
    are evaluated only when the request touches a relationship field (college,
    programme, department), so an unrelated edit (e.g. a phone number) is never
    blocked by a legacy combination that predates the rule. College is
    independent of Department (section T.5): no College/Department rule
    exists. Changing programme/department affects only what the student can
    select from now on — existing registrations and enrollments are history
    and are not rewritten."""
    student = await _get_student(db, student_id)
    updates = body.model_dump(exclude_unset=True)

    if updates.get("program_id") and not await db.get(Program, updates["program_id"]):
        raise HTTPException(400, "The specified programme does not exist.")
    if updates.get("department_id") and not await db.get(Department, updates["department_id"]):
        raise HTTPException(400, "The specified department does not exist.")
    if updates.get("college_id") and not await db.get(College, updates["college_id"]):
        raise HTTPException(400, "The specified college does not exist.")
    if "program_id" in updates or "department_id" in updates:
        await validate_program_department_pair(
            updates.get("program_id", student.program_id), updates.get("department_id", student.department_id), db,
        )
    if "program_id" in updates or "college_id" in updates:
        # Final college: the request's value (null clears it, so there is nothing
        # to check against), else the stored one.
        final_college_id = updates["college_id"] if "college_id" in updates else student.college_id
        await _validate_college_programme(db, final_college_id, updates.get("program_id", student.program_id))

    if "email" in updates:
        updates["email"] = updates["email"].lower()
        if updates["email"] != student.email:
            taken = await db.execute(select(User.id).where(User.email == updates["email"], User.id != student.id))
            if taken.scalar_one_or_none():
                raise HTTPException(409, "Email already registered.")
    old_roll = student.student_roll
    if "student_roll" in updates and updates["student_roll"] != old_roll:
        taken = await db.execute(select(User.id).where(User.student_roll == updates["student_roll"], User.id != student.id))
        if taken.scalar_one_or_none():
            raise HTTPException(409, "Roll number already in use.")

    for field, value in updates.items():
        setattr(student, field, value)
    # Orientation keeps a unique COPY of the roll number issued at selection;
    # keep it consistent with the canonical User.student_roll.
    if "student_roll" in updates and updates["student_roll"] != old_roll:
        candidate = (await db.execute(
            select(OrientationCandidate).where(OrientationCandidate.student_user_id == student.id)
        )).scalar_one_or_none()
        if candidate:
            candidate.roll_no = updates["student_roll"]
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Could not update student: the email or roll number conflicts with an existing record.")

    return await _render(db, student_id)
