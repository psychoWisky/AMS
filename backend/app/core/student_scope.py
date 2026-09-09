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
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, Program, Department


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


async def resolve_student_department_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    """Equivalent to `resolve_student_scope`, but for callers that only have a
    student's id (typically an authorization helper checking some OTHER
    user's department against this student's) and don't need `level`. Reads
    `User.department_id` directly — no Program join at all anymore."""
    student = await db.get(User, student_id)
    if not student:
        return None
    return student.department_id
