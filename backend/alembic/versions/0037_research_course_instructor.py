"""Research Course per-student instructor: nullable instructor_id on StudentEnrollment

Revision ID: 0037_research_course_instructor
Revises: 0036_final_thesis
Create Date: 2026-09-25

Research Course task: a Research Course (`Course.category == "research"`) may now be offered by
the HOD without a pre-assigned `OfferingFaculty` instructor; instead, each student's own accepted
Major Advisor becomes the instructor for THEIR registration only, resolved and snapshotted at the
moment they register.

One additive, nullable column: `ams_student_enrollments.instructor_id` (FK -> ams_users.id).
Deliberately placed on `StudentEnrollment` (the per-student-per-offering row), not on
`CourseOffering` (which must stay a single shared row, never mutated per student) and not on
`CourseRegistration` (the per-semester parent, which has no offering/course FK at all). Nullable
and never backfilled: every existing enrollment (and every non-Research-Course enrollment going
forward) simply has no value here — instructor continues to come from `OfferingFaculty` exactly as
before. Purely additive — no existing table, column, or row is touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0037_research_course_instructor'
down_revision: Union[str, None] = '0036_final_thesis'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        'ams_student_enrollments',
        sa.Column('instructor_id', UUID, sa.ForeignKey('ams_users.id'), nullable=True),
    )
    op.create_index(
        'ix_student_enrollments_instructor_id', 'ams_student_enrollments', ['instructor_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_student_enrollments_instructor_id', table_name='ams_student_enrollments')
    op.drop_column('ams_student_enrollments', 'instructor_id')
