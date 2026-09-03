"""course availability

Revision ID: 0007_course_availability
Revises: 0006_ppw_phase1
Create Date: 2026-09-05

Adds ams_course_availability (course-availability/cross-department-access
task) — a standing, semester-independent link between a course and a
department other than its owner, used by GET /courses, GET /courses/{id},
and GET /ppw/available-courses to widen student visibility beyond strict
ownership.

Does NOT modify ams_courses.department_id (owning department, unchanged
meaning), ams_course_offerings (a separate, semester-specific concept,
untouched by this task), or any prior migration (0001-0006).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0007_course_availability'
down_revision: Union[str, None] = '0006_ppw_phase1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ams_course_availability',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('course_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('department_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['ams_courses.id'], name='fk_course_availability_course_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['department_id'], ['ams_departments.id'], name='fk_course_availability_department_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('course_id', 'department_id', name='uq_course_availability'),
    )
    # Supports the department_id-only lookups used by student/PPW course
    # listing (WHERE department_id = :dept), separate from the composite
    # unique index above which is keyed course_id-first.
    op.create_index('ix_course_availability_department_id', 'ams_course_availability', ['department_id'])


def downgrade() -> None:
    op.drop_index('ix_course_availability_department_id', table_name='ams_course_availability')
    op.drop_table('ams_course_availability')
