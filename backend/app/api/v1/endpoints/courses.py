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
import hashlib
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, field_validator, model_validator
import openpyxl

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.core.config import settings
from app.core.security import create_bulk_upload_confirmation_token, decode_token
from app.core.bulk_upload import parse_bulk_upload_file
from app.models.user import User, UserRole, UserRoleAssignment, Program, Department
from app.models.course import Course, CourseOffering, OfferingFaculty, CourseAvailability
from app.models.enrollment import StudentEnrollment
# Programme<->Department many-to-many redesign — single shared student-scope
# resolver (app/core/student_scope.py), re-exported under this file's
# existing private name so every call site below is unchanged.
from app.core.student_scope import resolve_student_scope as _resolve_student_scope

router = APIRouter(prefix="/courses", tags=["Courses"])

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

    @field_validator("program_level")
    @classmethod
    def _canonical_program_level(cls, v: str) -> str:
        # Programme Level is part of the course duplicate key
        # (department + code + normalized title + level), so it must always be
        # one canonical value — otherwise "pg"/" PG " would slip past the
        # duplicate check. Same case-insensitive resolution the bulk upload uses.
        resolved = _resolve_program_level(v)
        if resolved is None:
            raise ValueError(f"Programme must be one of: {', '.join(_PROGRAM_LEVEL_VALUES)}.")
        return resolved

    @model_validator(mode="after")
    def set_course_type(self):
        # Course-credit task (confirmed requirement): Super Admin/HOD may enter
        # any Theory/Practical credit values — the previous fixed whitelist
        # (2+0, 0+2, 1+1, ...) is removed (see this file's git history / the
        # investigation report). The only remaining numeric validation is
        # "not negative" — total_credits/credit_structure are still derived
        # unchanged (Course.total_credits property), never stored separately,
        # so there is no separate value that could drift out of sync with
        # credit_theory + credit_practical.
        if self.credit_theory < 0 or self.credit_practical < 0:
            raise ValueError("Credit values cannot be negative.")
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
    # Research Course task: `faculty_ids` may now be empty and `leader_id` may
    # now be omitted — but ONLY for a Research Course offering. This schema
    # has no DB access and cannot itself know the course's category, so the
    # authoritative "non-Research Course still requires 1-3 faculty and a
    # Leader" rule is enforced server-side in `create_offering` (after loading
    # the real `Course.category` from the database), never here and never
    # bypassable by a client claiming any particular category.
    faculty_ids: List[UUID] = []
    leader_id: Optional[UUID] = None

    @model_validator(mode="after")
    def validate_faculty(self):
        if not self.faculty_ids:
            if self.leader_id is not None:
                raise ValueError("leader_id must not be set when no faculty are selected.")
            return self
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

_MANAGE_ROLES = (UserRole.SUPER_ADMIN, UserRole.HOD)


def _authorize_department_manage(department_id: Optional[UUID], user: User) -> None:
    if user.active_role == UserRole.SUPER_ADMIN:
        return
    if user.active_role == UserRole.HOD:
        if department_id and user.active_department_id and department_id == user.active_department_id:
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
# GET /{course_id} route (same convention as /category-values above) so
# "/search"/"available-to-me" are never swallowed by {course_id}.

class AvailabilityIn(BaseModel):
    # Optional: if omitted, defaults to the caller's own department (HOD).
    # If supplied, it MUST equal the caller's own department for HOD — never
    # client-controlled beyond that, per instruction "do not allow the request
    # body to arbitrarily select any department." Admins may target any dept.
    department_id: Optional[UUID] = None


def _authorize_availability_manage(department_id: UUID, user: User) -> None:
    if user.active_role == UserRole.SUPER_ADMIN:
        return
    if user.active_role == UserRole.HOD:
        if user.active_department_id and department_id == user.active_department_id:
            return
        raise HTTPException(403, "You can only manage course availability for your own department.")
    raise HTTPException(403, "Insufficient permissions.")


@router.get("/search")
async def search_courses(
    q: Optional[str] = None, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
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
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    """Courses NOT owned by the caller's department but explicitly made
    available to it — the "Courses available to your department" list
    (Section 5). For SUPER_ADMIN (no home department), this is
    intentionally empty rather than an error — there is no meaningful "my
    department" for those roles; use GET /courses/{id}/availability instead."""
    if not user.active_department_id:
        return []
    result = await db.execute(
        select(CourseAvailability)
        .options(selectinload(CourseAvailability.course).selectinload(Course.department))
        .where(CourseAvailability.department_id == user.active_department_id)
        .join(Course, Course.id == CourseAvailability.course_id)
        .order_by(Course.course_number)
    )
    return [{
        "availability_id": str(a.id),
        "course_id": str(a.course_id),
        "course_number": a.course.course_number if a.course else None,
        "course_title": a.course.title if a.course else None,
        "program_level": a.course.program_level if a.course else None,
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
    if user.active_role == UserRole.STUDENT:
        scope = await _resolve_student_scope(user, db)
        if not scope:
            return []
        # Course-availability task — widened from strict ownership equality to
        # ownership-OR-explicit-availability (see _course_visibility_condition).
        q = q.where(_course_visibility_condition(scope["department_id"]))
    elif user.active_role == UserRole.HOD:
        if not user.active_department_id:
            return []
        q = q.where(Course.department_id == user.active_department_id)
    elif user.active_role == UserRole.FACULTY:
        # BUSINESS_LOGIC.md Section Q.1 — Faculty has no generic course-
        # catalogue need ("Teacher Courses" is their course surface); this
        # list was the one catalogue endpoint left open. Same 403 as get_course.
        raise HTTPException(403, "Faculty do not have access to the course catalogue. Use Teacher Courses.")
    elif department_id:
        q = q.where(Course.department_id == department_id)
    result = await db.execute(q.order_by(Course.course_number))
    return [CourseOut.from_orm(c) for c in result.scalars().all()]


@router.post("", status_code=201)
async def create_course(
    body: CourseIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    if user.active_role == UserRole.HOD and not body.department_id:
        raise HTTPException(400, "Department is required.")
    # _authorize_department_manage already guarantees that for an HOD,
    # body.department_id equals their OWN authenticated department (or the
    # request is rejected with 403 before this point) — so body.department_id
    # is always the actual, authorized department by the time the course-code
    # department-scoping fix's uniqueness check below runs, never a
    # client-supplied value used un-checked.
    _authorize_department_manage(body.department_id, user)
    # Course-code duplicate-detection fix (this revision) — `course_number`
    # is NOT unique, even within a department (confirmed AVFU business case:
    # the same department can legitimately run "RES101" for both "Research
    # (Semester II)" and "Research (Semester IV)"). Only an EXACT duplicate —
    # same department, same code, same normalized title, AND same Programme
    # Level (`Course.program_level`, e.g. UG/PG/PhD — NOT the ams_programs
    # master table) — is rejected here; a same-code-different-title course,
    # or the same code+title at a different Programme Level, is allowed. This
    # endpoint intentionally does NOT implement the bulk-upload's warning/
    # confirmation workflow: it is a single synchronous create action with
    # no existing preview step to hang a warning off, and the task scoping
    # this change explicitly permits leaving single-course creation's UX
    # alone as long as the underlying rule is correct (no DB constraint
    # blocks the legitimate same-code case) — see docs/BUSINESS_LOGIC.md.
    norm_incoming_title = _normalize_title(body.title)
    existing = await db.execute(
        select(Course.id, Course.title).where(
            Course.department_id == body.department_id, Course.course_number == body.course_number,
            Course.program_level == body.program_level,
        )
    )
    for existing_id, existing_title in existing.all():
        if _normalize_title(existing_title) == norm_incoming_title:
            raise HTTPException(
                409,
                f"A course with this code, the same title and the same Programme ({body.program_level}) already exists in this department (course ID {existing_id}).",
            )
    c = Course(**body.model_dump(), created_by=user.id)
    db.add(c)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Could not create course — the selected department may no longer exist.")
    await db.refresh(c, attribute_names=["department"])
    return CourseOut.from_orm(c)


@router.get("/{course_id}", response_model=CourseOut)
async def get_course(course_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    result = await db.execute(select(Course).options(selectinload(Course.department)).where(Course.id == course_id))
    c = result.scalar_one_or_none()
    if not c: raise HTTPException(404, "Course not found.")
    if user.active_role == UserRole.STUDENT:
        scope = await _resolve_student_scope(user, db)
        visible = False
        if scope:
            check = await db.execute(select(Course.id).where(Course.id == c.id, _course_visibility_condition(scope["department_id"])))
            visible = check.scalar_one_or_none() is not None
        if not visible:
            raise HTTPException(404, "Course not found.")
    elif user.active_role == UserRole.HOD:
        if not (user.active_department_id and c.department_id and user.active_department_id == c.department_id):
            raise HTTPException(403, "You can only view courses within your own department.")
    elif user.active_role == UserRole.FACULTY:
        # BUSINESS_LOGIC.md Section Q — Faculty has no generic course-catalogue
        # need; only courses they are actually assigned to teach (via some
        # CourseOffering) are visible. Mirrors get_offering's FACULTY check.
        assigned = await db.execute(
            select(CourseOffering.id).where(
                CourseOffering.course_id == c.id,
                CourseOffering.id.in_(
                    select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id)
                    # Research Course task — same per-student instructor
                    # fallback as list_all_offerings/get_offering above.
                    .union(select(StudentEnrollment.offering_id).where(StudentEnrollment.instructor_id == user.id))
                ),
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
    if user.active_role == UserRole.HOD and body.department_id is not None and body.department_id != c.department_id:
        raise HTTPException(403, "You cannot move a course to a different department.")

    # Course-code duplicate-detection fix (this revision) — same rule as
    # create_course: only an EXACT duplicate (same department, same code,
    # same normalized title, same Programme Level) is rejected; a
    # same-code-different-title course, or the same code+title at a
    # different Programme Level, is allowed. The PUT body is a full
    # replacement (CourseIn), so `body.program_level` is the course's final
    # level. `final_department_id` is the department this
    # course will actually have AFTER this update: an explicit
    # body.department_id (already authorization-checked above — HOD can
    # only ever supply their own, or omit it) if provided, else the
    # course's existing, unchanged department. Excludes the course's own
    # row so a no-op update (same code, same title, same department) never
    # false-positives against itself.
    final_department_id = body.department_id if body.department_id is not None else c.department_id
    norm_incoming_title = _normalize_title(body.title)
    dup_check = await db.execute(
        select(Course.id, Course.title).where(
            Course.department_id == final_department_id,
            Course.course_number == body.course_number,
            Course.program_level == body.program_level,
            Course.id != course_id,
        )
    )
    for other_id, other_title in dup_check.all():
        if _normalize_title(other_title) == norm_incoming_title:
            raise HTTPException(
                409,
                f"A course with this code, the same title and the same Programme ({body.program_level}) already exists in this department (course ID {other_id}).",
            )

    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Could not update course — the selected department may no longer exist.")
    return {"message": "Updated."}


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


# ── Bulk Course Upload (this revision) ──────────────────────────────────────
#
# Reuses the shared structural parser (app/core/bulk_upload.py) already used
# by the Faculty/User bulk upload — only file-format/header parsing lives
# there; all Course-specific business validation lives here, mirroring
# auth.py's own bulk-upload split exactly.
#
# Security (Section 13's explicit requirement): the SAME authorization
# helper used by the individual create_course endpoint governs bulk upload —
# HOD's own department is validated-and-rejected-on-mismatch (never silently
# forced), exactly like `_authorize_department_manage`/create_course already
# do. This is deliberately the "validate and reject" convention already
# established by create_course, not the "force and ignore" convention used
# by create_faculty's bulk upload — chosen because it is what THIS
# endpoint's own individual counterpart already does, per the instruction to
# match existing Course-creation behavior exactly rather than importing a
# different endpoint's convention.
_COURSE_BULK_COLUMNS = [
    "Course Number", "Course Title", "Programme", "Course Type", "Credit Type",
    "Theory Credit", "Practical Credit", "Status", "Department",
]
# Programme/Course Type/Credit Type/Theory Credit/Practical Credit/Status may
# be left blank — a blank cell falls back to the same default the individual
# CourseIn schema already uses (program_level="UG", category=None,
# credit_type=None, credit_theory=0, credit_practical=0, status="active").
# Course Number/Course Title/Department must always be supplied — Department
# is mandatory for bulk upload (unlike Super Admin's optional department on
# the individual form) since every bulk-created course must resolve to a
# real department to be associated with here.
_COURSE_BULK_REQUIRED_COLUMNS = ["Course Number", "Course Title", "Department"]
_MAX_COURSE_BULK_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024

_PROGRAM_LEVEL_VALUES = ("UG", "PG", "PhD")
_COURSE_STATUS_VALUES = ("active", "inactive")


def _normalize_course_number(raw: str) -> str:
    """Case-insensitive Excel input -> canonical uppercase storage (Section 5's
    explicit requirement). Only case is changed — internal spaces/punctuation
    (e.g. "LPM 601*") are preserved exactly, never stripped beyond the
    surrounding-whitespace trim."""
    return raw.strip().upper()


def _normalize_title(raw: str) -> str:
    """Course-code duplicate-detection fix (this revision) — comparison key
    for deciding whether two courses sharing the same (department,
    course_number) are the SAME course (title differs only by whitespace/
    case) or genuinely DIFFERENT courses (e.g. "Research (Semester II)" vs
    "Research (Semester IV)"). Deliberately narrow: only whitespace and case
    are folded — no punctuation stripping, no word removal, no semester
    parsing. "Research (Semester II)" and "Research (Semester IV)" must
    keep comparing as different strings; only "Research", " research ", and
    "RESEARCH" should collapse to the same key."""
    return raw.strip().casefold()


def _resolve_program_level(raw: str) -> Optional[str]:
    return {"ug": "UG", "pg": "PG", "phd": "PhD"}.get(raw.strip().lower())


def _normalize_label_key(raw: str) -> str:
    """Case/whitespace/separator-tolerant comparison key — lets a human label
    like "Mandatory MBA" and the canonical value "mandatory_mba" resolve to
    the same key without hardcoding a separate synonym list per value."""
    return " ".join(raw.strip().lower().replace("-", " ").replace("_", " ").split())


# Built directly from the EXISTING CATEGORY_VALUES/CREDIT_TYPE_VALUES
# constants (Section 8/9's explicit "reuse existing constants, do not create
# a second independent definition") — never duplicated or hand-typed.
_CATEGORY_LABEL_MAP = {_normalize_label_key(v.replace("_", " ")): v for v in CATEGORY_VALUES}
_CREDIT_TYPE_LABEL_MAP = {_normalize_label_key(v.replace("_", " ")): v for v in CREDIT_TYPE_VALUES}


def _resolve_category(raw: str) -> Optional[str]:
    return _CATEGORY_LABEL_MAP.get(_normalize_label_key(raw))


def _resolve_credit_type(raw: str) -> Optional[str]:
    return _CREDIT_TYPE_LABEL_MAP.get(_normalize_label_key(raw))


def _resolve_course_status(raw: str) -> Optional[str]:
    # Bulk upload deliberately supports only the two values the individual
    # Add Course UI actually offers (Section 11) — "archived" is a
    # documented-but-never-exposed model value and is NOT accepted here,
    # per the explicit instruction not to introduce it without confirmation.
    key = raw.strip().lower()
    return key if key in _COURSE_STATUS_VALUES else None


async def _validate_course_bulk_rows(
    rows: list[dict], db: AsyncSession, *, force_department_id: Optional[UUID],
) -> tuple[list[dict], list[dict], list[dict]]:
    """All-or-nothing validation — no database write happens here. Returns
    (findings, warnings, valid_row_data); any non-empty `findings` means the
    caller must create nothing at all, mirroring auth.py's bulk-upload
    philosophy exactly. Ordinary field-validation findings run first, as
    before; duplicate-COURSE detection runs BEFORE any insert is attempted,
    both against the live database and within the uploaded file itself.

    Course-code duplicate-detection fix (this revision, superseding the
    previous (department_id, course_number) uniqueness rule from migration
    0019) — `course_number` is NOT unique, even within one department: real
    AVFU data legitimately reuses a code within the same department for
    genuinely different courses (e.g. "RES101" for "Research (Semester II)"
    and "Research (Semester IV)"). Two rows/records sharing (department_id,
    course_number) are therefore compared on TITLE too, via `_normalize_title`
    (whitespace/case-insensitive, nothing else folded):
      * same department + same code + SAME normalized title + SAME Programme
        Level (`Course.program_level`: UG/PG/PhD — not the ams_programs
        master table) -> a genuine duplicate -> added to `findings` (hard
        reject, no override possible, exactly like every other
        field-validation finding).
      * same department + same code + same normalized title but a DIFFERENT
        Programme Level -> a distinct, allowed course (no finding, no warning).
      * same department + same code + DIFFERENT normalized title -> a
        `warnings` entry (informational; does not by itself block creation —
        the caller decides whether to require confirmation).
      * different department, any title -> no relationship at all; never
        compared.
    """
    department_rows = (await db.execute(select(Department.id, Department.code, Department.name))).all()
    department_by_code = {code.strip().lower(): did for did, code, name in department_rows}
    department_names = {did: name for did, code, name in department_rows}
    hod_department_label = department_names.get(force_department_id) if force_department_id else None

    # (department_id, UPPERCASE course_number) -> [(existing course id, existing title, existing
    # Programme Level), ...] — a list, not a single value, because MULTIPLE
    # existing courses can legitimately share the same pair now (that is the
    # entire point of this fix); each one is checked against the incoming
    # title AND Programme Level separately: only all four of department,
    # code, normalized title and level matching is a duplicate.
    existing_by_pair: dict[tuple[Optional[UUID], str], list[tuple[UUID, str, str]]] = {}
    for existing_id, dept_id, number, existing_title, existing_level in (
        await db.execute(select(Course.id, Course.department_id, Course.course_number, Course.title, Course.program_level))
    ).all():
        existing_by_pair.setdefault((dept_id, number.upper()), []).append((existing_id, existing_title, existing_level))

    findings: list[dict] = []
    warnings: list[dict] = []
    # Same key shape as existing_by_pair — (row_no, raw_title, normalized_title,
    # Programme Level) per already-seen row, so a later row is checked against
    # EVERY earlier row sharing its pair, not just the most recent one.
    seen_by_pair: dict[tuple[Optional[UUID], str], list[tuple[int, str, str, str]]] = {}
    row_payload: dict[int, dict] = {}

    for entry in rows:
        row_no = entry["row"]
        v = entry["values"]
        row_findings: list[dict] = []

        def add(column: str, value: str, error: str) -> None:
            row_findings.append({"row": row_no, "column": column, "value": value, "error": error})

        for col in _COURSE_BULK_REQUIRED_COLUMNS:
            if not v.get(col):
                add(col, v.get(col, ""), f"{col} is required.")

        raw_number = v.get("Course Number", "")
        course_number: Optional[str] = _normalize_course_number(raw_number) if raw_number else None

        title = v.get("Course Title", "").strip()

        program_level = "UG"
        level_valid = True
        raw_level = v.get("Programme", "")
        if raw_level:
            resolved_level = _resolve_program_level(raw_level)
            if resolved_level is None:
                level_valid = False
                add("Programme", raw_level, f"Invalid Programme. Allowed values: {', '.join(_PROGRAM_LEVEL_VALUES)}.")
            else:
                program_level = resolved_level

        category: Optional[str] = None
        raw_category = v.get("Course Type", "")
        if raw_category:
            resolved_category = _resolve_category(raw_category)
            if resolved_category is None:
                add("Course Type", raw_category, f"Invalid Course Type. Allowed values: {', '.join(CATEGORY_VALUES)}.")
            else:
                category = resolved_category

        credit_type: Optional[str] = None
        raw_credit_type = v.get("Credit Type", "")
        if raw_credit_type:
            resolved_credit_type = _resolve_credit_type(raw_credit_type)
            if resolved_credit_type is None:
                add("Credit Type", raw_credit_type, f"Invalid Credit Type. Allowed values: {', '.join(CREDIT_TYPE_VALUES)}.")
            else:
                credit_type = resolved_credit_type

        credit_theory = 0
        raw_theory = v.get("Theory Credit", "")
        if raw_theory:
            try:
                credit_theory = int(float(raw_theory))
                if credit_theory < 0:
                    add("Theory Credit", raw_theory, "Credit must be >= 0.")
            except ValueError:
                add("Theory Credit", raw_theory, "Theory Credit must be a whole number.")

        credit_practical = 0
        raw_practical = v.get("Practical Credit", "")
        if raw_practical:
            try:
                credit_practical = int(float(raw_practical))
                if credit_practical < 0:
                    add("Practical Credit", raw_practical, "Credit must be >= 0.")
            except ValueError:
                add("Practical Credit", raw_practical, "Practical Credit must be a whole number.")

        status = "active"
        raw_status = v.get("Status", "")
        if raw_status:
            resolved_status = _resolve_course_status(raw_status)
            if resolved_status is None:
                add("Status", raw_status, "Invalid Status. Allowed values: Active, Inactive.")
            else:
                status = resolved_status

        department_id_value: Optional[UUID] = None
        raw_dept = v.get("Department", "")
        if raw_dept:
            resolved_dept = department_by_code.get(raw_dept.strip().lower())
            if not resolved_dept:
                add("Department", raw_dept, f"Department '{raw_dept}' was not found.")
            elif force_department_id is not None and resolved_dept != force_department_id:
                suffix = f" (expected: {hod_department_label})" if hod_department_label else ""
                add("Department", raw_dept, f"Department does not match the authenticated HOD's department{suffix}.")
            else:
                department_id_value = resolved_dept

        # Only checked once both a department AND a title are actually
        # resolved for this row — an unresolved department/missing title
        # already carries its own finding above and excludes the row from
        # row_payload regardless, so there is no meaningful comparison yet.
        # (An invalid Programme value likewise already carries its own finding.)
        if course_number and title and department_id_value and level_valid:
            pair = (department_id_value, course_number)
            norm_title = _normalize_title(title)
            dept_label = department_names.get(department_id_value) or (raw_dept.strip() if raw_dept else "this department")

            for existing_id, existing_title, existing_level in existing_by_pair.get(pair, []):
                if _normalize_title(existing_title) == norm_title:
                    if existing_level == program_level:
                        add(
                            "Course Number", raw_number,
                            f"Duplicate course: {dept_label} already has a course with this code, the same title "
                            f"and the same Programme ({program_level}) (existing course ID {existing_id}, title \"{existing_title}\").",
                        )
                    # else: same code + title at a DIFFERENT Programme Level is a
                    # distinct, allowed course — neither a duplicate nor a warning.
                else:
                    warnings.append({
                        "row": row_no,
                        "department_id": str(department_id_value), "department_name": dept_label,
                        "course_number": course_number,
                        "incoming_title": title,
                        "existing_course_id": str(existing_id), "existing_title": existing_title,
                        "existing_row": None,
                        "message": (
                            f"Course code '{course_number}' already exists in {dept_label} with a different title "
                            f"(\"{existing_title}\"). Please verify these are intentionally separate courses."
                        ),
                    })

            for other_row_no, other_raw_title, other_norm_title, other_level in seen_by_pair.get(pair, []):
                if other_norm_title == norm_title:
                    if other_level == program_level:
                        add(
                            "Course Number", raw_number,
                            f"Duplicate course number, title and Programme ({program_level}) within the uploaded file (also row {other_row_no}).",
                        )
                        findings.append({
                            "row": other_row_no, "column": "Course Number", "value": raw_number,
                            "error": f"Duplicate course number, title and Programme ({program_level}) within the uploaded file (also row {row_no}).",
                        })
                    # else: same code + title at a different Programme Level — allowed.
                else:
                    warnings.append({
                        "row": row_no,
                        "department_id": str(department_id_value), "department_name": dept_label,
                        "course_number": course_number,
                        "incoming_title": title,
                        "existing_course_id": None, "existing_title": other_raw_title,
                        "existing_row": other_row_no,
                        "message": (
                            f"Course code '{course_number}' also appears in row {other_row_no} of this file with a "
                            f"different title (\"{other_raw_title}\"). Please verify these are intentionally separate courses."
                        ),
                    })

            seen_by_pair.setdefault(pair, []).append((row_no, title, norm_title, program_level))

        if row_findings:
            findings.extend(row_findings)
        elif course_number and title and department_id_value:
            course_type = "both" if (credit_theory > 0 and credit_practical > 0) else ("practical" if credit_practical > 0 else "theory")
            row_payload[row_no] = {
                "course_number": course_number, "title": title, "program_level": program_level,
                "category": category, "credit_type": credit_type,
                "credit_theory": credit_theory, "credit_practical": credit_practical,
                "course_type": course_type, "status": status, "department_id": department_id_value,
            }

    # A row that looked valid in isolation may have been retroactively
    # invalidated once a LATER duplicate course number was found above — drop
    # it from the create list (mirrors auth.py's identical handling).
    invalidated_rows = {f["row"] for f in findings}
    for r in list(row_payload):
        if r in invalidated_rows:
            del row_payload[r]
    # A row hard-rejected as an exact duplicate is already blocking the
    # whole file outright — its own "different title" warning (if any, from
    # a THIRD, unrelated row sharing the same code) would only be
    # confusing/redundant noise in that response, so it's dropped here too.
    warnings = [w for w in warnings if w["row"] not in invalidated_rows]

    findings.sort(key=lambda f: (f["row"], f["column"]))
    valid = [row_payload[r] for r in sorted(row_payload)]
    return findings, warnings, valid


async def _create_bulk_courses(valid_rows: list[dict], db: AsyncSession, user: User) -> list[Course]:
    """Phase 2 — every row already passed validation; create them all in one
    transaction (Section 16's explicit all-or-nothing requirement — no row is
    ever committed while validation is still in progress)."""
    created: list[Course] = []
    for r in valid_rows:
        c = Course(
            course_number=r["course_number"], title=r["title"], program_level=r["program_level"],
            category=r["category"], credit_type=r["credit_type"],
            credit_theory=r["credit_theory"], credit_practical=r["credit_practical"],
            course_type=r["course_type"], status=r["status"], department_id=r["department_id"],
            created_by=user.id,
        )
        db.add(c)
        created.append(c)
    try:
        await db.commit()
    except IntegrityError:
        # course_number is no longer unique in any scope (migration 0020) —
        # duplicate-course rejection is handled entirely by
        # _validate_course_bulk_rows above, before this function is ever
        # called. An IntegrityError here means something else went wrong
        # (e.g. a department was deleted between validation and this
        # commit) — a generic, honest message, not a stale "duplicate
        # Course Number" claim.
        await db.rollback()
        raise HTTPException(
            409,
            "One or more rows conflict with existing data (e.g. a department that no longer exists). "
            "No courses were created.",
        )
    for c in created:
        await db.refresh(c, attribute_names=["department"])
    return created


def _build_course_bulk_template_workbook() -> io.BytesIO:
    """Mirrors auth.py's `_build_user_bulk_template_workbook` exactly — one
    clearly-marked, unimportable example row ("EXAMPLE" is never a real
    Department code, so even if left in by mistake it fails validation
    loudly rather than being silently imported as a real course)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AMS Courses"
    ws.append(_COURSE_BULK_COLUMNS)
    ws.append([
        "EXAMPLE — DELETE THIS ROW", "Do Not Import", "UG", "Core", "Credit",
        "3", "0", "Active", "<exact existing Department code>",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@router.post("/bulk-upload", status_code=201)
async def bulk_upload_courses(
    file: UploadFile = File(...),
    # Course duplicate-warning confirmation fix (this revision) — these two
    # optional fields turn this SAME endpoint into a two-step preview/
    # confirm flow without inventing a separate URL or a persisted "pending
    # upload" table (Section 8/9's explicit "reuse existing architecture,
    # don't over-engineer" guidance): the first call (no token) behaves as
    # a preview when duplicate-code warnings are found; the caller then
    # re-submits the SAME file with `confirm_warnings=true` and the
    # `confirmation_token` this endpoint returned, to actually create the
    # courses. See the confirmation-verification block below for why a
    # bare `confirm_warnings=true` boolean is never trusted by itself.
    confirm_warnings: bool = Form(False),
    confirmation_token: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    """SUPER_ADMIN/HOD only (Section 15 — same `_MANAGE_ROLES` gate as the
    individual create_course endpoint; Faculty/Student are rejected before
    this function body ever runs). HOD's own department is validated against
    every row (never silently forced) — a single mismatched row rejects the
    ENTIRE file, exactly like create_course's own `_authorize_department_manage`.

    Course duplicate-warning confirmation fix — response shapes:
      * ordinary field-validation errors OR hard duplicates -> HTTP 400,
        unchanged `{success, imported_count, errors}` shape (hard duplicates
        are just another `errors` finding — see `_validate_course_bulk_rows`).
      * same-code-different-title warnings, not yet confirmed -> HTTP 409,
        `{success: false, requires_confirmation: true, warnings, confirmation_token}`.
        NOTHING is created at this point.
      * no findings, and (no warnings OR warnings + valid confirmation) ->
        HTTP 201, unchanged `{success, imported_count, filename}` shape.
    """
    if user.active_role == UserRole.HOD and not user.active_department_id:
        raise HTTPException(400, "Your account has no department assigned; contact an administrator.")

    content = await file.read()
    if len(content) > _MAX_COURSE_BULK_BYTES:
        raise HTTPException(413, f"File exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")
    file_hash = hashlib.sha256(content).hexdigest()

    # Confirmation-tampering fix — a bare `confirm_warnings: true` is worth
    # nothing on its own (any client could send that trivially). It is only
    # honored if `confirmation_token` is a signature-valid, non-expired
    # token whose `sub` is THIS authenticated user and whose `file_hash`
    # matches the file actually attached to THIS request byte-for-byte —
    # proving this exact user was actually shown this exact file's warnings
    # before confirming. Any mismatch (wrong user, expired, tampered,
    # missing, or a DIFFERENT file re-submitted) is treated as "not
    # confirmed", never surfaced as a distinct error — it just falls
    # through to the same preview/warning response a first-time upload
    # would get, with a fresh token.
    confirmed = False
    if confirm_warnings and confirmation_token:
        payload = decode_token(confirmation_token, token_type="course_bulk_upload_confirm")
        if payload and payload.get("sub") == str(user.id) and payload.get("file_hash") == file_hash:
            confirmed = True

    # Parsing + validation (including duplicate detection) always runs
    # fresh against the CURRENT database state — this is also what makes
    # the confirm step's revalidation a true final check, not a reuse of
    # whatever was true at preview time (Section 11's explicit race-
    # condition requirement): if another user created a conflicting course
    # between an earlier preview and this call, it is caught right here.
    rows = parse_bulk_upload_file(file.filename or "", content, _COURSE_BULK_COLUMNS)
    force_department_id = user.active_department_id if user.active_role == UserRole.HOD else None
    findings, warnings, valid = await _validate_course_bulk_rows(rows, db, force_department_id=force_department_id)

    # Hard duplicates / ordinary validation errors block unconditionally —
    # no confirmation can ever override this (Rule 2 / Rule 4).
    if findings:
        return JSONResponse(status_code=400, content={"success": False, "imported_count": 0, "errors": findings})

    if warnings and not confirmed:
        new_token = create_bulk_upload_confirmation_token(str(user.id), file_hash, len(warnings))
        return JSONResponse(status_code=409, content={
            "success": False, "imported_count": 0,
            "requires_confirmation": True, "warnings": warnings,
            "confirmation_token": new_token,
        })

    created = await _create_bulk_courses(valid, db, user)
    return {"success": True, "imported_count": len(created), "filename": file.filename}


@router.get("/bulk-upload/template")
async def download_course_bulk_upload_template(_: User = Depends(require_roles(*_MANAGE_ROLES))):
    buf = _build_course_bulk_template_workbook()
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ams_courses_bulk_upload_template.xlsx"},
    )


@router.get("/{course_id}/availability")
async def list_course_availability(
    course_id: UUID, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
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
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404, "Course not found.")

    target_department_id = body.department_id or user.active_department_id
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
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD)),
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
        # Programme Level (Course.program_level: UG/PG/PhD) — lets selectors tell
        # apart two courses that share department + code + title but differ by level.
        "program_level": o.course.program_level if o.course else None,
        "credit_structure": o.course.credit_structure if o.course else None,
        # Registration Card / 20-credit task — a numeric credit value alongside
        # the existing display-only credit_structure string, so the frontend
        # can compute a live running total without re-parsing "2+1" strings.
        # Backend enforcement of the 20-credit cap does not depend on this
        # field at all (it always recomputes from Course.total_credits itself).
        "credits": o.course.total_credits if o.course else 0,
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

    if user.active_role == UserRole.STUDENT:
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
        if user.active_role == UserRole.SUPER_ADMIN:
            pass  # unrestricted, same as _authorize_offering_management
        elif user.active_role == UserRole.HOD:
            if not user.active_department_id:
                return []
            q = q.where(CourseOffering.department_id == user.active_department_id)
        elif user.active_role == UserRole.FACULTY:
            # Research Course task — a faculty member also sees an offering
            # here if they are the per-student Research Course instructor
            # (StudentEnrollment.instructor_id) for at least one student in
            # it, even with no OfferingFaculty row at all (Research Course
            # offerings never have one — see courses.py's create_offering).
            # This only affects LIST visibility ("does this offering show up
            # in my Teacher Courses"); it grants no offering-wide authority —
            # roster/grading actions are separately scoped per student.
            q = q.where(CourseOffering.id.in_(
                select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id).union(
                    select(StudentEnrollment.offering_id).where(StudentEnrollment.instructor_id == user.id)
                )
            ))
        else:
            return []  # fail closed for roles with no defined "mine" scope
    else:
        if user.active_role == UserRole.HOD and user.active_department_id:
            # HOD's Offer Course / Course Management views are implicitly scoped
            # to their own department (BUSINESS_LOGIC.md L.4) — no explicit
            # department filter is part of the confirmed filter set.
            q = q.where(CourseOffering.department_id == user.active_department_id)
        elif user.active_role == UserRole.FACULTY:
            # BUSINESS_LOGIC.md Section Q — Faculty has no legitimate "browse
            # all offerings" use; Teacher Courses (mine=true) is the only
            # intended Faculty course-access surface, so the default/non-mine
            # branch is restricted identically to the mine=true FACULTY branch
            # above, regardless of the `mine` flag's value.
            q = q.where(CourseOffering.id.in_(
                select(OfferingFaculty.offering_id).where(OfferingFaculty.faculty_id == user.id).union(
                    select(StudentEnrollment.offering_id).where(StudentEnrollment.instructor_id == user.id)
                )
            ))
        elif department_id:
            q = q.where(CourseOffering.department_id == department_id)
        if level: q = q.join(Course, Course.id == CourseOffering.course_id).where(Course.program_level == level)

    result = await db.execute(q)
    offerings = result.scalars().all()

    if user.active_role == UserRole.STUDENT and offerings:
        offerings = [o for o in offerings if o.course and o.course.program_level == scope["level"]]
    elif level and not (user.active_role == UserRole.STUDENT):
        offerings = [o for o in offerings if o.course and o.course.program_level == level]

    items = []
    for o in offerings:
        enrolled = sum(1 for e in o.enrollments if e.status == "approved")
        items.append(_offering_dict(o, enrolled))
    return items


async def _assert_faculty_in_department(faculty_ids: list[UUID], department_id: Optional[UUID], db: AsyncSession) -> None:
    """BUSINESS_LOGIC.md Section N (HOD Offer Course faculty selector) — backend-
    authoritative check that every selected faculty member is eligible to teach
    in the offering's department. Never trust the frontend's candidate list alone.

    Eligibility is a `UserRoleAssignment(role=FACULTY, department_id=<offering
    department>)` on an ACTIVE user — never the legacy `User.role`/
    `User.department_id`, which cannot describe a person who is Faculty in
    several departments. Holding HOD (or any other role) in that department
    grants nothing here: a HOD must also hold an explicit FACULTY assignment
    to be assignable, and a Faculty assignment in a different department does
    not qualify. Applies to every caller, Super Admin included."""
    if not faculty_ids:
        return
    if department_id is None:
        raise HTTPException(403, "One or more selected faculty members do not hold a Faculty assignment in this department.")
    result = await db.execute(
        select(User.id).where(
            User.id.in_(faculty_ids), User.is_active == True,
            User.id.in_(
                select(UserRoleAssignment.user_id).where(
                    UserRoleAssignment.role == UserRole.FACULTY,
                    UserRoleAssignment.department_id == department_id,
                )
            ),
        )
    )
    valid_ids = {row[0] for row in result.all()}
    if valid_ids != set(faculty_ids):
        raise HTTPException(403, "One or more selected faculty members do not hold a Faculty assignment in this department.")


@router.post("/offerings", status_code=201)
async def create_offering(
    body: OfferingIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_MANAGE_ROLES)),
):
    _authorize_department_manage(body.department_id, user)

    # Research Course task — the authoritative faculty-requirement rule, keyed
    # off the REAL `Course.category` loaded from the database (never a
    # client-supplied category/flag). A Research Course offering must NOT
    # have a pre-assigned OfferingFaculty instructor at all (Rule #5: never
    # create two competing instructor authorities — the per-student Major
    # Advisor assignment, resolved at registration time, is the sole
    # authority for a Research Course). Every other course category keeps
    # the pre-existing "1-3 faculty, exactly one Leader" requirement exactly
    # as it was, enforced here rather than in the (now-relaxed) Pydantic
    # schema so a client cannot bypass it by omitting faculty_ids.
    course = await db.get(Course, body.course_id)
    if not course:
        raise HTTPException(404, "Course not found.")
    if course.category == "research":
        if body.faculty_ids:
            raise HTTPException(
                400,
                "Research Course offerings cannot have a pre-assigned instructor. The instructor "
                "is determined individually for each student from their Major Advisor at "
                "registration time.",
            )
    else:
        if not (_MIN_OFFERING_FACULTY <= len(body.faculty_ids) <= _MAX_OFFERING_FACULTY):
            raise HTTPException(400, f"An offering must have between {_MIN_OFFERING_FACULTY} and {_MAX_OFFERING_FACULTY} assigned faculty.")
        if body.leader_id is None:
            raise HTTPException(400, "A Leader must be selected.")

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
    if user.active_role == UserRole.STUDENT:
        scope = await _resolve_student_scope(user, db)
        if not scope or o.department_id != scope["department_id"]:
            raise HTTPException(404, "Offering not found.")
    # HOD: own department only. FACULTY: only offerings they are actually
    # assigned to (mirrors _authorize_offering_grading in grading.py and
    # _authorize_offering_management in enrollment.py — same principle, this
    # endpoint previously had no such check at all beyond the student branch above).
    elif user.active_role == UserRole.HOD:
        if not (user.active_department_id and o.department_id and user.active_department_id == o.department_id):
            raise HTTPException(403, "You can only view offerings within your own department.")
    elif user.active_role == UserRole.FACULTY:
        assigned = await db.execute(
            select(OfferingFaculty.id).where(
                OfferingFaculty.offering_id == offering_id, OfferingFaculty.faculty_id == user.id,
            ).limit(1)
        )
        if not assigned.scalar_one_or_none():
            # Research Course task — same per-student instructor fallback as
            # list_all_offerings above.
            research_link = await db.execute(
                select(StudentEnrollment.id).where(
                    StudentEnrollment.offering_id == offering_id, StudentEnrollment.instructor_id == user.id,
                ).limit(1)
            )
            if not research_link.scalar_one_or_none():
                raise HTTPException(403, "You can only view offerings you are assigned to teach.")
    enrolled = sum(1 for e in o.enrollments if e.status == "approved")
    return {
        "id": str(o.id), "calendar_id": str(o.calendar_id),
        "semester_id": str(o.semester_id), "semester_name": o.semester.name if o.semester else None,
        "course_id": str(o.course_id),
        "course_number": o.course.course_number if o.course else None,
        "course_title": o.course.title if o.course else None,
        "program_level": o.course.program_level if o.course else None,
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
    course = await db.get(Course, o.course_id)
    if course and course.category == "research":
        raise HTTPException(
            400,
            "Research Course offerings do not use a pre-assigned instructor; the instructor is "
            "determined per student from their Major Advisor at registration time.",
        )
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
