"""department college/outstation many-to-many

Revision ID: 0044_department_college
Revises: 0043_committee_external_member
Create Date: 2026-10-03

College/Outstation + Department-mapping task. Department <-> College
association, explicitly managed by Super Admin — CONFIRMED business rule: one
Department may belong to multiple Colleges/Outstations, and one
College/Outstation may have multiple Departments. Purely additive: creates
ONE new table, `ams_department_colleges`, modeled directly on
`ams_college_programs` (migration 0022) — own `id` PK,
UNIQUE(department_id, college_id), a supporting index on the reverse-lookup
column (college_id; department_id is already the leading column of the
unique constraint), and FKs with ON DELETE CASCADE that can only ever remove
this join row.

Nothing is backfilled and nothing existing is touched: no Department,
College, Programme, User or any other row is read, changed or deleted. This
table is deliberately independent of `ams_program_departments`/
`ams_college_programs` — no Department->Programme->College chain is derived
from or validated against it. Which departments belong to which
colleges/outstations is left for Super Admin to define; existing departments
have zero rows here until explicitly mapped (no mapping is invented).

This table is NOT an authorization mechanism — department-based RBAC
continues to read exclusively from `UserRoleAssignment.department_id`/
`User.department_id`, never from this table.

Reversible: downgrade drops only the new table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0044_department_college'
down_revision: Union[str, None] = '0043_committee_external_member'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    existing_tables = sa.inspect(conn).get_table_names()
    if 'ams_department_colleges' in existing_tables:
        return
    op.create_table(
        'ams_department_colleges',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('department_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('college_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['department_id'], ['ams_departments.id'], name='fk_department_colleges_department_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['college_id'], ['ams_colleges.id'], name='fk_department_colleges_college_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('department_id', 'college_id', name='uq_department_college'),
    )
    op.create_index('ix_department_colleges_college_id', 'ams_department_colleges', ['college_id'])


def downgrade() -> None:
    conn = op.get_bind()
    existing_tables = sa.inspect(conn).get_table_names()
    if 'ams_department_colleges' not in existing_tables:
        return
    op.drop_index('ix_department_colleges_college_id', table_name='ams_department_colleges')
    op.drop_table('ams_department_colleges')
