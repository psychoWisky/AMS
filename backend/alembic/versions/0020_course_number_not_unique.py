"""remove composite (department_id, course_number) uniqueness constraint

Revision ID: 0020_course_number_not_unique
Revises: 0019_course_dept_scoped_number
Create Date: 2026-09-19

Business-rule correction, confirmed from real AVFU data: the SAME
department can legitimately have multiple courses sharing the same
course code (e.g. Department SAME, code RES101 for both "Research
(Semester II)" and "Research (Semester IV)"). Migration
`0019_course_dept_scoped_number` (already applied to production) added
`uq_course_department_number` — UNIQUE (department_id, course_number) —
which incorrectly forbids this legitimate case.

`0019` is NOT edited (it is already live in production); this is a NEW,
purely additive-in-spirit migration that only DROPS that composite
constraint. `Course.id` remains the sole technical identity for a course
(see `app.models.course.Course`'s updated docstring) — `course_number` is
no longer enforced as unique at the database level in any scope, full
stop. Genuine duplicate-course detection (same department + same code +
same normalized title vs. merely same code) is now an APPLICATION-level
concern (see `app.api.v1.endpoints.courses`'s bulk-upload/create/update
duplicate-detection logic), deliberately NOT a database constraint —
title is human-entered, needs normalization (whitespace/case) the
database cannot express in a portable/robust unique index, and a rigid
DB constraint would make future business-rule refinements harder.

Purely a constraint removal — no column added/removed/retyped, no row
modified, no row deleted, no Course.id changed, no foreign key touched.
Verified read-only, before writing this migration, that no existing
production-mirroring local data would be affected: dropping a UNIQUE
constraint can only ever ALLOW previously-forbidden combinations, never
invalidate existing rows, so this migration cannot fail against any
existing data no matter its content.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0020_course_number_not_unique'
down_revision: Union[str, None] = '0019_course_dept_scoped_number'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_course_department_number", "ams_courses", type_="unique")


def downgrade() -> None:
    # Re-adds the composite constraint this migration removed — matches
    # 0019's own upgrade() exactly. NOTE: if any (department_id,
    # course_number) duplicates were legitimately created while this
    # migration was in effect (the whole point of this change), this
    # downgrade will fail with a Postgres uniqueness-violation error rather
    # than silently deleting/merging data — by design: a schema downgrade
    # must never be the mechanism that silently destroys real business data
    # created under the new, correct rule. Resolve/remove such rows
    # deliberately first if a downgrade is ever genuinely required.
    op.create_unique_constraint(
        "uq_course_department_number", "ams_courses", ["department_id", "course_number"],
    )
