"""scope course_number uniqueness to (department_id, course_number)

Revision ID: 0019_course_dept_scoped_number
Revises: 0018_email_outbox
Create Date: 2026-09-18

Root cause fixed: `ams_courses.course_number` was declared `unique=True`,
enforced by a single-column unique constraint/index
(`ams_courses_course_number_key`). This made course numbers globally
unique across the whole institution, so two different departments could
never both use the same course code (e.g. AGRO's "CS101" blocked VETM from
ever using "CS101") — an incorrect business rule; course codes must only
be unique WITHIN a department.

This migration:
  1. Drops the old global unique constraint `ams_courses_course_number_key`.
  2. Adds a new composite unique constraint `uq_course_department_number`
     on (department_id, course_number), matching `app.models.course.Course`'s
     updated `__table_args__`.

Purely a constraint swap — no column added/removed/retyped, no row
modified or deleted. Verified read-only, before writing this migration,
that the current data has zero (department_id, course_number) collisions
(59 existing courses, all with a non-null department_id, no duplicates
under either the old or the new rule) — so this constraint can be added
immediately without any data cleanup.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0019_course_dept_scoped_number'
down_revision: Union[str, None] = '0018_email_outbox'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ams_courses_course_number_key", "ams_courses", type_="unique")
    op.create_unique_constraint(
        "uq_course_department_number", "ams_courses", ["department_id", "course_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_course_department_number", "ams_courses", type_="unique")
    op.create_unique_constraint(
        "ams_courses_course_number_key", "ams_courses", ["course_number"],
    )
