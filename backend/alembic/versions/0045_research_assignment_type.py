"""research course offering assignment strategy

Revision ID: 0045_research_assignment_type
Revises: 0044_department_college
Create Date: 2026-10-04

Research Course Assignment Strategy task. Adds one new, nullable column to
`ams_course_offerings`: `research_assignment_type` (`major_advisor` /
`external_examiner`), an OFFERING-LEVEL property — never on `ams_courses` —
since the confirmed requirement is that the SAME Research-category course may
be offered in one context as "major_advisor" and in another as
"external_examiner".

A CHECK constraint restricts the column to exactly those two values or NULL.
It deliberately does NOT try to also enforce "only non-NULL for a
Research-category course" at the database level — that would require a
cross-table lookup into `ams_courses.category`, which a single-table CHECK
constraint cannot express; that half of the rule is enforced in the
application layer (`courses.py`'s `create_offering`/`update_offering`),
consistent with this repository's established convention of enforcing
cross-table business rules in code rather than DB triggers (e.g. course
duplicate-detection, Programme/Department pair validation).

Backfill (confirmed safe default, Section 15 of this task): any EXISTING
Research-category offering (`ams_courses.category = 'research'`) whose
`research_assignment_type` is NULL is set to `'major_advisor'` — the
existing, only-ever-implemented behavior before this task — never
`'external_examiner'`, which would be inventing data. Verified against the
real dev database before writing this migration: zero existing offerings
have a Research-category course, so this backfill is a documented no-op
today, not an untested assumption. Every non-Research-category offering is
left untouched (NULL, "not applicable").

Purely additive and idempotent: safe to re-run against an already-upgraded
or a fresh database.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0045_research_assignment_type'
down_revision: Union[str, None] = '0044_department_college'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NAME = "ck_course_offering_research_assignment_type"
_CHECK_SQL = "research_assignment_type IS NULL OR research_assignment_type IN ('major_advisor', 'external_examiner')"


def upgrade() -> None:
    conn = op.get_bind()
    existing_columns = {c["name"] for c in sa.inspect(conn).get_columns("ams_course_offerings")}
    if "research_assignment_type" not in existing_columns:
        op.add_column("ams_course_offerings", sa.Column("research_assignment_type", sa.String(20), nullable=True))

    existing_checks = {c["name"] for c in sa.inspect(conn).get_check_constraints("ams_course_offerings")}
    if _CHECK_NAME not in existing_checks:
        op.create_check_constraint(_CHECK_NAME, "ams_course_offerings", _CHECK_SQL)

    conn.execute(sa.text("""
        UPDATE ams_course_offerings
        SET research_assignment_type = 'major_advisor'
        WHERE research_assignment_type IS NULL
          AND course_id IN (SELECT id FROM ams_courses WHERE category = 'research')
    """))


def downgrade() -> None:
    conn = op.get_bind()
    existing_checks = {c["name"] for c in sa.inspect(conn).get_check_constraints("ams_course_offerings")}
    if _CHECK_NAME in existing_checks:
        op.drop_constraint(_CHECK_NAME, "ams_course_offerings", type_="check")

    existing_columns = {c["name"] for c in sa.inspect(conn).get_columns("ams_course_offerings")}
    if "research_assignment_type" in existing_columns:
        op.drop_column("ams_course_offerings", "research_assignment_type")
