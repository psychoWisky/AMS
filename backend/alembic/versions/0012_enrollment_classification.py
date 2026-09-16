"""course registration classification (major/minor/supporting/research/seminar/compulsory)

Revision ID: 0012_enrollment_classification
Revises: 0011_reg_card_withdrawal
Create Date: 2026-09-16

Major/Minor/Supporting discipline task. One additive, nullable column only:

`ams_student_enrollments.classification` (nullable String(20)) — the same
six-value controlled vocabulary PPW's pre-existing `PpwCourse.classification`
already uses (major/minor/supporting/research/seminar/compulsory), applied
here to Course Registration's own per-course selection row. Placed on
`StudentEnrollment` (the per-course row), not `CourseRegistration` (the
per-semester parent), mirroring `PpwCourse.classification` exactly — a
per-course concept, not a per-registration one.

Nullable and never backfilled: every existing enrollment (legacy or
registration-linked) predates this classification and simply has no value
here. Does NOT touch `ams_courses.category` (a different, pre-existing,
unrelated taxonomy) or any other column/table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012_enrollment_classification'
down_revision: Union[str, None] = '0011_reg_card_withdrawal'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ams_student_enrollments',
        sa.Column('classification', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('ams_student_enrollments', 'classification')
