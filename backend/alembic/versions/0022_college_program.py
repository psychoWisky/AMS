"""college programme many-to-many

Revision ID: 0022_college_program
Revises: 0021_multi_dept_role_assign
Create Date: 2026-09-20

College <-> Programme association, explicitly managed by Super Admin. Purely
additive: creates ONE new table, `ams_college_programs`, modeled directly on
`ams_program_departments` (migration 0009) — own `id` PK, UNIQUE(college_id,
program_id), a supporting index on the reverse-lookup column (program_id;
college_id is already the leading column of the unique constraint), and FKs
with ON DELETE CASCADE that can only ever remove this join row.

Nothing is backfilled and nothing existing is touched: no College, Programme,
Department, User or `ams_program_departments` row is read, changed or deleted.
Which programmes belong to which colleges is left for Super Admin to define.
Reversible: downgrade drops only the new table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0022_college_program'
down_revision: Union[str, None] = '0021_multi_dept_role_assign'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ams_college_programs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('college_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('program_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['college_id'], ['ams_colleges.id'], name='fk_college_programs_college_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['program_id'], ['ams_programs.id'], name='fk_college_programs_program_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('college_id', 'program_id', name='uq_college_program'),
    )
    op.create_index('ix_college_programs_program_id', 'ams_college_programs', ['program_id'])


def downgrade() -> None:
    op.drop_index('ix_college_programs_program_id', table_name='ams_college_programs')
    op.drop_table('ams_college_programs')
