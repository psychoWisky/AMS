"""Canonical student academic-scope resolution (Programme<->Department
many-to-many redesign).

Before this change, a student's Department was inferred as
`User.program_id -> Program.department_id`, independently re-implemented as
`_resolve_student_scope`/`_student_department_id` in courses.py, enrollment.py
(two separate copies), research.py, and inline in credit_details.py. That
inference is no longer valid: a Programme can now have many Departments (see
`app.models.user.ProgramDepartment`), so a student's Department can no longer
be derived from their Programme alone.

Going forward, both fields are read DIRECTLY and independently from `User`:
- `User.program_id`   — the student's Programme (unchanged meaning)
- `User.department_id` — the student's Department/specialization (now the
  authoritative field for students too, exactly as it already was for
  Faculty/HOD)

This module is the SINGLE place that resolution happens now. Every endpoint
file that used to duplicate it imports from here instead of re-implementing
it, so there is exactly one implementation left, not five.
"""
from typing import Optional
from uuid import UUID
from sqlalchemy import and_, or_, false
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, Program, Department, UserRole


async def resolve_student_scope(user: User, db: AsyncSession) -> Optional[dict]:
    """Resolve a student's (program_level, department_id) directly from their
    own User row. Returns None (fail-closed) if either the student's Programme
    or Department is not set, or either referenced row no longer exists —
    matches the fail-closed behavior every predecessor of this helper already
    had for a missing Programme; a missing Department is now checked the same
    way, since Department is no longer something that can be silently
    recovered via a Programme join."""
    if not user.program_id or not user.department_id:
        return None
    program = await db.get(Program, user.program_id)
    if not program:
        return None
    department = await db.get(Department, user.department_id)
    if not department:
        return None
    return {"level": program.level, "department_id": user.department_id}


async def resolve_student_program(student_id: UUID, db: AsyncSession) -> Optional[Program]:
    """Incharge Academic Cell scope-split task (this revision) — fetches a
    student's `Program` row directly from their id, for the many
    authorization call sites that only have a `student_id` (no eager-loaded
    `User.program` relationship available) and would otherwise risk an
    unsupported lazy load under the async session. Returns None if the
    student or their Programme can't be found — callers pass this straight
    into `resolve_programme_scope`/`incharge_can_access_program`, which
    already fail closed on None."""
    student = await db.get(User, student_id)
    if not student or not student.program_id:
        return None
    return await db.get(Program, student.program_id)


async def resolve_student_department_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    """Equivalent to `resolve_student_scope`, but for callers that only have a
    student's id (typically an authorization helper checking some OTHER
    user's department against this student's) and don't need `level`. Reads
    `User.department_id` directly — no Program join at all anymore."""
    student = await db.get(User, student_id)
    if not student:
        return None
    return student.department_id


def course_level_matches(course_program_level: Optional[str], scope_level: Optional[str]) -> bool:
    """Programme-level eligibility check (PPW course-selection task) — exact
    string match, mirroring the identical `course.program_level ==
    scope["level"]` comparison already used at enrollment time in
    courses.py's `list_all_offerings` and enrollment.py's `enroll`/
    `register_courses`. Canonical values are "UG"/"PG"/"PhD" (see
    `Program.level`/`Course.program_level` column comments) — confirmed
    distinct, never treated as interchangeable (a PG student may not select a
    PhD course or vice versa). Extracted here as a small shared helper
    (rather than a fourth inline duplicate) specifically for PPW's two course-
    selection call sites (`GET /ppw/available-courses`, `POST
    /ppw/{id}/courses`); courses.py/enrollment.py's existing, already-correct
    inline checks are left exactly as they are."""
    return bool(course_program_level) and course_program_level == scope_level


# ════════════════════════════════════════════════════════════════════════════
# Incharge Academic Cell scope-split task (this revision, AVFU-confirmed) —
# resolution/authorization helpers for the 4 scoped Incharge Academic Cell
# roles. Centralized HERE (not duplicated per endpoint file) specifically so
# every one of the ~25 call sites identified in the investigation applies the
# exact same rule — see UserRole's own docstring for the full business
# framing and the "union of held scopes, not just the active role" design
# note.
# ════════════════════════════════════════════════════════════════════════════

#: AVFU-confirmed: PG and PhD are administratively the SAME group for this
#: feature ("PG includes both Masters and PhD"); UG is the other group. This
#: mapping exists ONLY here — `Program.level`/`Course.program_level` remain
#: the real, unmodified, 3-distinct-value column everywhere else in AMS
#: (course eligibility, External Examiner proposal counts, etc.). Returns
#: None (fail-closed) for any value outside the 3 known levels — never
#: silently defaults to a group.
def programme_group(level: Optional[str]) -> Optional[str]:
    if level == "UG":
        return "UG"
    if level in ("PG", "PhD"):
        return "PG"
    return None


INCHARGE_SCOPE_ROLES: tuple[UserRole, ...] = (
    UserRole.INCHARGE_ACADEMIC_CELL_PG_VETERINARY,
    UserRole.INCHARGE_ACADEMIC_CELL_UG_VETERINARY,
    UserRole.INCHARGE_ACADEMIC_CELL_PG_FISHERY,
    UserRole.INCHARGE_ACADEMIC_CELL_UG_FISHERY,
)

#: Each scoped role's (programme_group, faculty) pair — the single place this
#: mapping is spelled out.
_INCHARGE_SCOPE_BY_ROLE: dict[UserRole, tuple[str, str]] = {
    UserRole.INCHARGE_ACADEMIC_CELL_PG_VETERINARY: ("PG", "Veterinary"),
    UserRole.INCHARGE_ACADEMIC_CELL_UG_VETERINARY: ("UG", "Veterinary"),
    UserRole.INCHARGE_ACADEMIC_CELL_PG_FISHERY: ("PG", "Fishery"),
    UserRole.INCHARGE_ACADEMIC_CELL_UG_FISHERY: ("UG", "Fishery"),
}


def incharge_scopes_held(user: User) -> set[tuple[str, str]]:
    """The UNION of every (programme_group, faculty) scope this user
    currently holds, read from `user.assigned_roles` (ALL roles granted to
    them — see `get_current_user`), deliberately NOT limited to whichever
    single role is their current session's `active_role`. This is the one,
    explicitly-documented exception in this codebase to "authorization reads
    active_role only" — see UserRole's docstring for why. A user holding
    none of the 4 scoped roles gets an empty set."""
    assigned = getattr(user, "assigned_roles", None) or []
    return {_INCHARGE_SCOPE_BY_ROLE[r] for r in assigned if r in _INCHARGE_SCOPE_BY_ROLE}


def is_incharge_academic_cell(user: User) -> bool:
    """Gate check — is the caller currently ACTING as one of the 4 scoped
    Incharge roles (their session's active_role, exactly like every other
    role check in this codebase)? This is the ENTRY gate; `incharge_scopes_
    held` (above) is the separate, wider UNION used once inside, for
    filtering which records they may see/act on."""
    return user.active_role in INCHARGE_SCOPE_ROLES


def resolve_programme_scope(program: Optional[Program]) -> Optional[tuple[str, str]]:
    """Resolve a (programme_group, faculty) pair from a real `Program` row.
    FAILS CLOSED (returns None) whenever the classification is missing or
    invalid: no `program`, no `program.level`, no `program.faculty`, or a
    `faculty` value outside the two known ones — a caller must treat None as
    "cannot verify this student's scope, deny access", never as "allow by
    default". Deliberately reads ONLY `Program.faculty` (never
    `Department.stream`, never a College relationship, never any
    administrative/support department such as DPGS) — see `Program.faculty`'s
    own docstring for why those alternate sources were rejected."""
    if program is None or not program.level or not program.faculty:
        return None
    if program.faculty not in ("Veterinary", "Fishery"):
        return None
    group = programme_group(program.level)
    if group is None:
        return None
    return (group, program.faculty)


def incharge_can_access_program(scopes: set[tuple[str, str]], program: Optional[Program]) -> bool:
    """Single-record authorization check — does ANY of the caller's held
    scopes (see `incharge_scopes_held`) match this specific Program? Fails
    closed via `resolve_programme_scope` for an unclassified Programme,
    regardless of how many scopes the caller holds."""
    resolved = resolve_programme_scope(program)
    if resolved is None:
        return False
    return resolved in scopes


def incharge_scope_clause(scopes: set[tuple[str, str]]):
    """SQL-level filter for LIST queries — an OR'd condition over `Program`
    (the caller must already have `Program` joined/aliased into the query as
    the literal `Program` entity, e.g. via `User.program_id == Program.id`)
    matching ANY of the caller's held scopes. A single OR'd WHERE clause
    (never a UNION of several separately-run queries) so overlapping scopes
    can never produce duplicate rows. Returns `false()` — never "no filter
    at all" — when `scopes` is empty, so an Incharge holder with zero
    resolvable scopes (e.g. mid-migration, see `0047_incharge_scope_split`)
    sees zero records rather than everything."""
    if not scopes:
        return false()
    conditions = []
    for group, faculty in scopes:
        level_condition = Program.level.in_(("PG", "PhD")) if group == "PG" else Program.level == "UG"
        conditions.append(and_(level_condition, Program.faculty == faculty))
    return or_(*conditions)
