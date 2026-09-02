"""faculty profile fields + college master data

Revision ID: 0003_faculty_profile_fields
Revises: 0002_schema_sync
Create Date: 2026-09-01

Adds schema required for HOD Faculty Management and Super Admin Master Data
(BUSINESS_LOGIC.md Section N):

- ams_users.title, ams_users.middle_name — both nullable, no backfill needed
  (existing rows simply have neither set; User.full_name already tolerates
  either being absent).
- ams_colleges — new master-data table (id, name, code, is_active,
  created_at), mirroring ams_departments/ams_programs's existing shape. No
  FK to any other table is introduced (Open Question 49 — no confirmed
  College/Department relationship).

Does not modify 0001_baseline or 0002_schema_sync.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0003_faculty_profile_fields'
down_revision: Union[str, None] = '0002_schema_sync'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ams_users', sa.Column('title', sa.String(length=10), nullable=True))
    op.add_column('ams_users', sa.Column('middle_name', sa.String(length=100), nullable=True))

    op.create_table(
        'ams_colleges',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('code', sa.String(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_colleges_code'),
    )
    op.alter_column('ams_colleges', 'is_active', server_default=None)


def downgrade() -> None:
    # Destructive on a populated database — dropping ams_colleges discards any
    # rows created since this migration; not executed as part of this task.
    op.drop_table('ams_colleges')
    op.drop_column('ams_users', 'middle_name')
    op.drop_column('ams_users', 'title')
