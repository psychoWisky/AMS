"""ppw phase 1

Revision ID: 0006_ppw_phase1
Revises: 0005_role_master_data
Create Date: 2026-09-04

Adds the PPW (Post-Graduate Programme of Work) Phase 1 foundation:
ams_ppw (header/draft-submitted document) and ams_ppw_courses (selected
course-plan line items, referencing ams_courses — no course data duplicated).

Deliberately NOT included in this migration (Phase 1 scope, see task):
no approval-stage table, no signature table, no revision/versioning table —
`Ppw.status` is a plain String(20) precisely so later phases can add values
without a column-type migration. No existing table/column is modified.

Does not modify 0001-0005.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0006_ppw_phase1'
down_revision: Union[str, None] = '0005_role_master_data'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ams_ppw',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('student_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('field_of_investigation', sa.Text(), nullable=True),
        sa.Column('minor_field', sa.Text(), nullable=True),
        sa.Column('supporting_field', sa.Text(), nullable=True),
        sa.Column('research_title', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='draft'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['ams_users.id'], name='fk_ppw_student_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', name='uq_ppw_student_id'),
    )
    op.alter_column('ams_ppw', 'status', server_default=None)

    op.create_table(
        'ams_ppw_courses',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('ppw_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('course_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('classification', sa.String(length=20), nullable=False),
        sa.Column('sl_no', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ppw_id'], ['ams_ppw.id'], name='fk_ppw_courses_ppw_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['course_id'], ['ams_courses.id'], name='fk_ppw_courses_course_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ppw_id', 'course_id', name='uq_ppw_course'),
    )


def downgrade() -> None:
    # Destructive on a populated database — not executed as part of this task.
    op.drop_table('ams_ppw_courses')
    op.drop_table('ams_ppw')
