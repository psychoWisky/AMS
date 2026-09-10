"""Course & Course Offering management (Modules 5, 5.1, 5.2).

HOD authorization (BUSINESS_LOGIC.md L.2/L.4, Rule 21/23, STUDENT_SIDE_IMPLEMENTATION_PLAN.md
Section 33 CR-1/CR-2): HOD may create/update/delete courses and create/manage
offerings, scoped to their own department — widened from the prior admin-only
gate. Admin roles remain unrestricted.

Course Type / Credit Type (Section 33.4): `Course.category` and
`Course.credit_type` are NEW fields, deliberately separate from the pre-existing
`Course.course_type` (theory/practical/both, unchanged) — see the model docstring.

ASSUMPTIONS (not confirmed AVFU business rules — narrowest safe interpretation,
per instruction not to silently invent open-question answers):
- "Research"/"Compulsory" display columns (HOD Course Management table) are
  DERIVED from `category` (`category == "research"` / `category == "compulsory"`),
  not independent stored flags — BUSINESS_LOGIC.md Open Questions 29/30 remain open.
- "Course College" is displayed from `Department.stream`, mirroring the same
  assumption already made for Advisory Committee's `college_name` field
  (research.py) — BUSINESS_LOGIC.md Open Question 28 remains open.
- Offering "Remove" (HOD Offer Course table) reuses the existing
  `PATCH .../status?status=closed` transition — BUSINESS_LOGIC.md Open Question 35
  remains open; no new hard-delete endpoint was introduced.
"""
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, model_validator

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program, Department
from app.models.course import Course, CourseOffering, OfferingFaculty, CourseAvailability
# Programme<->Department many-to-many redesign — single shared student-scope
# resolver (app/core/student_scope.py), re-exported under this file's
# existing private name so every call site below is unchanged.
from app.core.student_scope import resolve_student_scope as _resolve_student_scope

router = APIRouter(prefix="/courses", tags=["Courses"])

CREDIT_FORMATS = ["2+0","0+2","1+1","0+1","3+0","2+1","1+2","3+1","4+0","0+4","2+2","4+1"]

# Confirmed HOD Course Management values (BUSINESS_LOGIC.md L.2).
CATEGORY_VALUES = ("optional", "core", "compulsory", "research", "seminar", "deficiency", "bridge", "prerequisite", "mandatory_mba")
CREDIT_TYPE_VALUES = ("credit", "non_credit")
_MIN_OFFERING_FACULTY, _MAX_OFFERING_FACULTY = 1, 3


class CourseIn(BaseModel):
    course_number: str
    title: str
    department_id: Optional[UUID] = None
    credit_theory: int = 0
    credit_practical: int = 0
    program_level: str = "UG"
    category: Optional[str] = None
    credit_type: Optional[str] = None
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
        if self.category is not None and self.category not in CATEGORY_VALUES:
            raise ValueError(f"category must be one of: {', '.join(CATEGORY_VALUES)}")
        if self.credit_type is not None and self.credit_type not in CREDIT_TYPE_VALUES:
            raise ValueError(f"credit_type must be one of: {', '.join(CREDIT_TYPE_VALUES)}")
        return self
    course_type: str = "theory"


class CourseOut(BaseModel):
    id: UUID; course_number: str; title: str
    department_id: Optional[UUID]; department_name: Optional[str] = None; college_name: Optional[str] = None
    credit_theory: int; credit_practical: int
    course_type: str; category: Optional[str]; credit_type: Optional[str]
    program_level: str; status: str
    credit_structure: str
    is_research: bool = False; is_compulsory: bool = False
    model_config = {"from_attributes": True}

    @classmethod
    def from_orm(cls, c: Course):
        return cls(
            id=c.id, course_number=c.course_number, title=c.title,
            department_id=c.department_id,
            department_name=c.department.name if c.department else None,
            college_name=c.department.stream if c.department else None,
            credit_theory=c.credit_theory, credit_practical=c.credit_practical,
            course_type=c.course_type, category=c.category, credit_type=c.credit_type,
            program_level=c.program_level, status=c.status,
            credit_structure=c.credit_structure,
            is_research=c.category == "research", is_compulsory=c.category == "compulsory",
        )


class OfferingIn(BaseModel):
    calendar_id: UUID
    semester_id: UUID
    course_id: UUID
    department_id: UUID
    max_enrollment: int = 60
    section: Optional[str] = None
    practical_group: Optional[str] = None
    faculty_ids: List[UUID]
    leader_id: UUID

    @model_validator(mode="after")
    def validate_faculty(self):
        if not (_MIN_OFFERING_FACULTY <= len(self.faculty_ids) <= _MAX_OFFERING_FACULTY):
            raise ValueError(f"An offering must have between {_MIN_OFFERING_FACULTY} and {_MAX_OFFERING_FACULTY} assigned faculty.")
        if len(set(self.faculty_ids)) != len(self.faculty_ids):
            raise ValueError("Duplicate faculty selected.")
        if self.leader_id not in self.faculty_ids:
            raise ValueError("The Leader must be one of the selected faculty members.")
        return self

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


# ── Authorization ────────────────────────────────────────────────────────────
# HOD is department-scoped (own department only); admins unrestricted. Mirrors
# the established pattern in enrollment.py's _authorize_offering_management /
# research.py's _authorize_propose_major_advisor, per this project's convention
# of per-file, hand-written authorization helpers.

_MANAGE_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)


def _authorize_department_manage(department_id: Optional[UUID], user: User) -> None:
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN):
        return
    if user.role == UserRole.HOD:
        if department_id and user.department_id and department_id == user.department_id:
            return
        raise HTTPException(403, "You can only manage courses/offerings within your own department.")
    raise HTTPException(403, "Insufficient permissions.")


def _course_visibility_condition(department_id: UUID):
    """Shared course-visibility predicate (course-availability task) — a
    course is visible to `department_id` if that department OWNS it
    (`Course.department_id`) OR has an explicit `CourseAvailability` grant.
    Defined exactly once and reused by list_courses/get_course's STUDENT
    branches and PPW's available-courses endpoint (ppw.py), per instruction
    not to duplicate this logic. CourseOffering/offering visibility is
    unrelated and intentionally untouched."""
    return or_(
        Course.department_id == department_id,
        Course.id.in_(select(CourseAvailability.course_id).where(CourseAvailability.department_id == department_id)),
    )


# ── Credit structure listing ──────────────────────────────────────────────────

@router.get("/credit-formats")
async def credit_formats():
    return {"formats": CREDIT_FORMATS}


@router.get("/category-values")
async def category_values():
    return {"category": CATEGORY_VALUES, "credit_type": CREDIT_TYPE_VALUES}


# ── Course Availability — fixed-path routes (course-availability task) ─────────
# Standing, semester-independent cross-department accessibility — separate from
# Course.department_id (ownership, never changed here) and CourseOffering
# (semester-specific, untouched by this task). "Option A": the RECEIVING
# department's HOD manages the grant; the owning department's course record
# and course-management authorization (_authorize_department_manage) are
# never touched by any endpoint here or below. Registered BEFORE the dynamic
# GET /{course_id} route (same convention as /credit-formats, /category-values
# above) so "/search"/"available-to-me" are never swallowed by {course_id}.

class AvailabilityIn(BaseModel):
    # Optional: if omitted, defaults to the caller's own department (HOD).
    # If supplied, it MUST equal the caller's own department for HOD — never
    # client-controlled beyond that, per instruction "do not allow the request
    # body to arbitrarily select any department." Admins may target any dept.
    department_id: Optional[UUID] = None


def _authorize_availability_manage(department_id: UUID, user: User) -> None:
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN):
        return
    if user.role == UserRole.HOD:
        if user.department_id and department_id == user.department_id:
            return
        raise HTTPException(403, "You can only manage course availability for your own department.")
    raise HTTPException(403, "Insufficient permissions.")


@router.get("/search")
async def search_courses(
    q: Optional[str] = None, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    """Deliberately NOT department-scoped — HOD/Admin only. This is the narrow,
    explicitly-authorized cross-department discovery surface a receiving HOD
    uses to find an existing other-department course to grant availability
    for (Section 6's "Add Existing Course" search). It does not replace or
    weaken GET /courses's strict department scoping for STUDENT/HOD's own
    catalogue view — that endpoint and its authorization are untouched."""
    query = select(Course).options(selectinload(Course.department)).where(Course.status == "active")
    if q:
        like = f"%{q}%"
        query = query.where(or_(Course.course_number.ilike(like), Course.title.ilike(like)))
    result = await db.execute(query.order_by(Course.course_number).limit(50))
    return [CourseOut.from_orm(c) for c in result.scalars().all()]


@router.get("/available-to-me")
async def list_courses_available_to_my_department(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    """Courses NOT owned by the caller's department but explicitly made
    available to it — the "Courses available to your department" list
    (Section 5). For SUPER_ADMIN/ACADEMIC_ADMIN (no home department), this is
    intentionally empty rather than an error — there is no meaningful "my
    department" for those roles; use GET /courses/{id}/availability instead."""
    if not user.department_id:
        return []
    result = await db.execute(
        select(CourseAvailability)
        .options(selectinload(CourseAvailability.course).selectinload(Course.department))
        .where(CourseAvailability.department_id == user.department_id)
        .join(Course, Course.id == CourseAvailability.course_id)
        .order_by(Course.course_number)
    )
    return [{
        "availability_id": str(a.id),
        "course_id": str(a.course_id),
        "course_number": a.course.course_number if a.course else None,
        "course_title": a.course.title if a.course else None,
        "credit_structure": a.course.credit_structure if a.course else None,
        "owning_department_id": str(a.course.department_id) if a.course and a.course.department_id else None,
        "owning_department_name": a.course.department.name if a.course and a.course.department else None,
    } for a in result.scalars().all()]


# ── Courses ───────────────────────────────────────────────────────────────────

@router.get("", response_model=List[CourseOut])
async def list_courses(
    status: Optional[str] = None, level: Optional[str] = None,
    department_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    q = select(Course).options(selectinload(Course.department))
    if status: q = q.where(Course.status == status)
    if level:  q = q.where(Course.program_level == level)
    # BUSINESS_LOGIC.md Section P (department-isolation audit) — STUDENT and HOD
    # are ALWAYS scoped to their own department's courses; any client-supplied
    # department_id is ignored for them, mirroring the scoping pattern already
    # used in list_all_offerings/GET /auth/users. Previously HOD had NO scoping
    # at all here (only STUDENT did), so any HOD saw the full cross-department
    # catalog by default — closed this gap.
    if user.role == UserRole.STUDENT:
        scope = await _resolve_student_scope(user, db)
        if not scope:
            return []
        # Course-availability task — widened from strict ownership equality to
        # ownership-OR-explicit-availability (see _course_visibility_condition).
        q = q.where(_course_visibility_condition(scope["department_id"]))
    elif user.role == UserRole.HOD:
        if not user.department_id:
            return []
        q = q.where(Course.department_id == user.department_id)
    elif department_id:
        q = q.where(Course.department_id == department_id)
    result = await db.execute(q.order_by(Course.course_number))
    return [CourseOut.from_orm(c) for c in result.scalars().all()]


@router.post("", status_code=201)
async def create_course(
    body: CourseIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    if user.role == UserRole.HOD and not body.department_id:
        raise HTTPException(400, "Department is required.")
    _authorize_department_manage(body.department_id, user)
    existing = await db.execute(select(Course).where(Course.course_number == body.course_number))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Course number already exists.")
    c = Course(**body.model_dump(), created_by=user.id)
    db.add(c); await db.commit(); await db.refresh(c, attribute_names=["department"])
    return CourseOut.from_orm(c)


@router.get("/{course_id}", response_model=CourseOut)
async def get_course(course_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(select(Course).options(selectinload(Course.department)).where(Course.id == course_id))
    c = result.scalar_one_or_none()
    if not c: raise HTTPException(404, "Course not found.")
    if user.role == UserRole.STUDENT:
        scope = await _resolve_student_scope(user, db)
        visible = False
        if scope:
            check = await db.execute(select(Course.id).where(Course.id == c.id, _course_visibility_condition(scope["department_id"])))
            visible = check.scalar_one_or_none() is not None
        if not visible:
            raise HTTPException(404, "Course not found.")
    elif user.role == UserRole.HOD:
        if not (user.department_id and c.department_id and user.department_id == c.department_id):
            raise HTTPException(403, "You can only view courses within your own department.")
    elif user.role == UserRole.FACULTY:
        # BUSINESS_LOGIC.md Section Q — Faculty has no generic course-catalogue
        # need; only courses they are actually assigned to teach (via some
        # CourseOffering) are visible. Mirrors get_offering's FACULTY check.
        assigned = await db.execute(
            select(CourseOffering.id).where(
                CourseOffering.course_id == c.id,
                CourseOffering.id.in_(select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id)),
            ).limit(1)
        )
        if not assigned.scalar_one_or_none():
            raise HTTPException(403, "You can only view courses you are assigned to teach.")
    return CourseOut.from_orm(c)


@router.put("/{course_id}")
async def update_course(
    course_id: UUID, body: CourseIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    _authorize_department_manage(c.department_id, user)
    # Verification finding B fix: authorization above only checked the course's
    # EXISTING department — without this, an HOD authorized for their own
    # department could still move a course to a different department via the
    # update payload, since department_id is otherwise applied unconditionally
    # below. HOD may not change department_id at all; admins are unrestricted.
    if user.role == UserRole.HOD and body.department_id is not None and body.department_id != c.department_id:
        raise HTTPException(403, "You cannot move a course to a different department.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    await db.commit(); return {"message": "Updated."}


@router.delete("/{course_id}", status_code=204)
async def delete_course(
    course_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    _authorize_department_manage(c.department_id, user)
    offering_exists = await db.execute(select(CourseOffering.id).where(CourseOffering.course_id == course_id).limit(1))
    if offering_exists.scalar_one_or_none():
        raise HTTPException(400, "This course has semester offerings and cannot be deleted. Remove its offerings first.")
    await db.delete(c)
    await db.commit()


@router.patch("/{course_id}/status")
async def update_course_status(
    course_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    _authorize_department_manage(c.department_id, user)
    c.status = status; await db.commit()
    return {"message": f"Status set to {status}."}


@router.get("/{course_id}/availability")
async def list_course_availability(
    course_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")
    result = await db.execute(
        select(CourseAvailability).options(selectinload(CourseAvailability.department)).where(CourseAvailability.course_id == course_id)
    )
    return [{
        "id": str(a.id), "department_id": str(a.department_id),
        "department_name": a.department.name if a.department else None,
        "created_at": a.created_at.isoformat(),
    } for a in result.scalars().all()]


@router.post("/{course_id}/availability", status_code=201)
async def add_course_availability(
    course_id: UUID, body: AvailabilityIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")

    target_department_id = body.department_id or user.department_id
    if not target_department_id:
        raise HTTPException(400, "department_id is required.")
    _authorize_availability_manage(target_department_id, user)

    if c.department_id == target_department_id:
        raise HTTPException(400, "This course is already owned by that department and is inherently available to it.")

    existing = await db.execute(
        select(CourseAvailability).where(CourseAvailability.course_id == course_id, CourseAvailability.department_id == target_department_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "This course is already available to that department.")

    a = CourseAvailability(course_id=course_id, department_id=target_department_id)
    db.add(a)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This course is already available to that department.")
    return {"id": str(a.id), "message": "Course made available to the department."}


@router.delete("/{course_id}/availability/{department_id}", status_code=204)
async def remove_course_availability(
    course_id: UUID, department_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.HOD)),
):
    _authorize_availability_manage(department_id, user)
    result = await db.execute(
        select(CourseAvailability).where(CourseAvailability.course_id == course_id, CourseAvailability.department_id == department_id)
    )
    a = result.scalar_one_or_none()
    if not a:
        return
    await db.delete(a)
    await db.commit()


# ── Offerings ─────────────────────────────────────────────────────────────────

def _offering_dict(o: CourseOffering, enrolled: int) -> dict:
    return {
        "id": str(o.id), "calendar_id": str(o.calendar_id),
        "semester_id": str(o.semester_id), "course_id": str(o.course_id),
        "course_number": o.course.course_number if o.course else None,
        "course_title": o.course.title if o.course else None,
        "credit_structure": o.course.credit_structure if o.course else None,
        "category": o.course.category if o.course else None,
        "credit_type": o.course.credit_type if o.course else None,
        "is_research": bool(o.course and o.course.category == "research"),
        "semester_name": o.semester.name if o.semester else None,
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
        selectinload(CourseOffering.semester),
        selectinload(CourseOffering.department),
        selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty),
        selectinload(CourseOffering.enrollments),
    )
    if semester_id: q = q.where(CourseOffering.semester_id == semester_id)
    if calendar_id: q = q.where(CourseOffering.calendar_id == calendar_id)

    if user.role == UserRole.STUDENT:
        # Course-visibility change: a student's own department no longer
        # restricts the catalogue — they may view/enroll in published
        # offerings from ANY department now. `level` is still derived
        # server-side (never client-supplied — see the program_level filter
        # below), but `department_id` is now honored as an OPTIONAL narrowing
        # filter exactly like every other role already gets: omitted -> all
        # departments; supplied -> only that department's offerings. This is
        # authorization-neutral — it only changes what is LISTED, never what
        # a student is allowed to enroll in (see enrollment.py's `enroll`/
        # `register_courses`, which independently re-check eligibility
        # server-side and never trust this list).
        scope = await _resolve_student_scope(user, db)
        if not scope:
            return []
        q = q.where(CourseOffering.status == "published")
        if department_id:
            q = q.where(CourseOffering.department_id == department_id)
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
        if user.role == UserRole.HOD and user.department_id:
            # HOD's Offer Course / Course Management views are implicitly scoped
            # to their own department (BUSINESS_LOGIC.md L.4) — no explicit
            # department filter is part of the confirmed filter set.
            q = q.where(CourseOffering.department_id == user.department_id)
        elif user.role == UserRole.FACULTY:
            # BUSINESS_LOGIC.md Section Q — Faculty has no legitimate "browse
            # all offerings" use; Teacher Courses (mine=true) is the only
            # intended Faculty course-access surface, so the default/non-mine
            # branch is restricted identically to the mine=true FACULTY branch
            # above, regardless of the `mine` flag's value.
            q = q.where(CourseOffering.id.in_(
                select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id)
            ))
        elif department_id:
            q = q.where(CourseOffering.department_id == department_id)
        if level: q = q.join(Course, Course.id == CourseOffering.course_id).where(Course.program_level == level)

    result = await db.execute(q)
    offerings = result.scalars().all()

    if user.role == UserRole.STUDENT and offerings:
        offerings = [o for o in offerings if o.course and o.course.program_level == scope["level"]]
    elif level and not (user.role == UserRole.STUDENT):
        offerings = [o for o in offerings if o.course and o.course.program_level == level]

    items = []
    for o in offerings:
        enrolled = sum(1 for e in o.enrollments if e.status == "approved")
        items.append(_offering_dict(o, enrolled))
    return items


async def _assert_faculty_in_department(faculty_ids: list[UUID], department_id: UUID, db: AsyncSession) -> None:
    """BUSINESS_LOGIC.md Section N (HOD Offer Course faculty selector) — backend-
    authoritative check that every selected faculty member actually belongs to
    the offering's department. Never trust the frontend's candidate list alone
    (Open Question 48, now closed by this check)."""
    if not faculty_ids:
        return
    result = await db.execute(select(User.id).where(User.id.in_(faculty_ids), User.department_id == department_id))
    valid_ids = {row[0] for row in result.all()}
    if valid_ids != set(faculty_ids):
        raise HTTPException(403, "One or more selected faculty members do not belong to this department.")


@router.post("/offerings", status_code=201)
async def create_offering(
    body: OfferingIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    _authorize_department_manage(body.department_id, user)
    await _assert_faculty_in_department(body.faculty_ids, body.department_id, db)

    # Verification finding D fix: the offering-uniqueness constraint and the
    # faculty-assignment inserts are now guarded by SEPARATE try/except blocks,
    # so a faculty-related integrity failure (e.g. a stale/invalid faculty_id)
    # can never be misreported as "offering already exists" — each failure mode
    # gets its own, accurate error.
    offering = CourseOffering(
        calendar_id=body.calendar_id, semester_id=body.semester_id,
        course_id=body.course_id, department_id=body.department_id,
        max_enrollment=body.max_enrollment,
        section=body.section, practical_group=body.practical_group,
        created_by=user.id,
    )
    db.add(offering)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "An offering for this course/semester/department/section combination already exists.")

    for fid in body.faculty_ids:
        db.add(OfferingFaculty(
            offering_id=offering.id, faculty_id=fid,
            role="primary" if fid == body.leader_id else "secondary",
        ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "One or more selected faculty members could not be assigned. Please verify the faculty selection and try again.")

    await db.refresh(offering)
    return {"id": str(offering.id), "message": "Offering created."}


@router.get("/offerings/{offering_id}")
async def get_offering(offering_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(
        select(CourseOffering).options(
            selectinload(CourseOffering.course),
            selectinload(CourseOffering.semester),
            selectinload(CourseOffering.department),
            selectinload(CourseOffering.faculty_assignments).selectinload(OfferingFaculty.faculty).selectinload(User.department),
            selectinload(CourseOffering.enrollments),
        ).where(CourseOffering.id == offering_id)
    )
    o = result.scalar_one_or_none()
    if not o: raise HTTPException(404, "Offering not found.")
    # BUSINESS_LOGIC.md Section P (department-isolation audit) — a student
    # guessing/typing a foreign-department offering ID gets the same 404 as a
    # nonexistent one, never a 403 that would confirm the ID is valid.
    if user.role == UserRole.STUDENT:
        scope = await _resolve_student_scope(user, db)
        if not scope or o.department_id != scope["department_id"]:
            raise HTTPException(404, "Offering not found.")
    # HOD: own department only. FACULTY/RESEARCH_SUPERVISOR: only offerings they
    # are actually assigned to (mirrors _authorize_offering_grading in grading.py
    # and _authorize_offering_management in enrollment.py — same principle, this
    # endpoint previously had no such check at all beyond the student branch above).
    elif user.role == UserRole.HOD:
        if not (user.department_id and o.department_id and user.department_id == o.department_id):
            raise HTTPException(403, "You can only view offerings within your own department.")
    elif user.role in (UserRole.FACULTY, UserRole.RESEARCH_SUPERVISOR):
        assigned = await db.execute(
            select(OfferingFaculty.id).where(
                OfferingFaculty.offering_id == offering_id, OfferingFaculty.faculty_id == user.id,
            ).limit(1)
        )
        if not assigned.scalar_one_or_none():
            raise HTTPException(403, "You can only view offerings you are assigned to teach.")
    enrolled = sum(1 for e in o.enrollments if e.status == "approved")
    return {
        "id": str(o.id), "calendar_id": str(o.calendar_id),
        "semester_id": str(o.semester_id), "semester_name": o.semester.name if o.semester else None,
        "course_id": str(o.course_id),
        "course_number": o.course.course_number if o.course else None,
        "course_title": o.course.title if o.course else None,
        "credit_structure": o.course.credit_structure if o.course else None,
        "category": o.course.category if o.course else None,
        "credit_type": o.course.credit_type if o.course else None,
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
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    o = await db.get(CourseOffering, offering_id)
    if not o: raise HTTPException(404, "Offering not found.")
    _authorize_department_manage(o.department_id, user)
    o.status = status; await db.commit()
    return {"message": f"Offering status set to {status}."}


@router.post("/offerings/{offering_id}/faculty")
async def assign_faculty(
    offering_id: UUID, faculty_id: UUID, role: str = "secondary",
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    o = await db.get(CourseOffering, offering_id)
    if not o: raise HTTPException(404, "Offering not found.")
    _authorize_department_manage(o.department_id, user)
    await _assert_faculty_in_department([faculty_id], o.department_id, db)

    count_result = await db.execute(select(OfferingFaculty).where(OfferingFaculty.offering_id == offering_id))
    current = count_result.scalars().all()
    if len(current) >= _MAX_OFFERING_FACULTY:
        raise HTTPException(400, f"An offering can have at most {_MAX_OFFERING_FACULTY} faculty.")
    if role == "primary" and any(fa.role == "primary" for fa in current):
        raise HTTPException(400, "This offering already has a Leader. Remove the existing Leader assignment before assigning a new one.")

    db.add(OfferingFaculty(offering_id=offering_id, faculty_id=faculty_id, role=role))
    await db.commit(); return {"message": "Faculty assigned."}


@router.delete("/offerings/{offering_id}/faculty/{faculty_id}", status_code=204)
async def remove_faculty(
    offering_id: UUID, faculty_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    o = await db.get(CourseOffering, offering_id)
    if not o: raise HTTPException(404, "Offering not found.")
    _authorize_department_manage(o.department_id, user)

    result = await db.execute(select(OfferingFaculty).where(
        OfferingFaculty.offering_id == offering_id,
        OfferingFaculty.faculty_id == faculty_id,
    ))
    fa = result.scalar_one_or_none()
    if not fa:
        return
    count_result = await db.execute(select(OfferingFaculty).where(OfferingFaculty.offering_id == offering_id))
    current = count_result.scalars().all()
    if len(current) <= _MIN_OFFERING_FACULTY:
        raise HTTPException(400, f"An offering must retain at least {_MIN_OFFERING_FACULTY} faculty member.")
    if fa.role == "primary" and len(current) > 1:
        raise HTTPException(400, "Assign a different Leader before removing the current one.")
    await db.delete(fa)
    await db.commit()
