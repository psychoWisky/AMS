"""designation master data

Revision ID: 0004_designation_master_data
Revises: 0003_faculty_profile_fields
Create Date: 2026-09-03

Adds ams_designations (BUSINESS_LOGIC.md Section N.5 / designation-management
investigation) — Super Admin master data replacing the previously hardcoded
Professor/Associate Professor/Assistant Professor list used by HOD's Add
Faculty form. ams_users.designation remains an unrelated free-text column
(NOT a foreign key to this table) — existing values (including non-faculty
ones like "University Registrar") are untouched by this migration.

Seeds exactly the 3 initial designations, all active. Does not modify
0001_baseline, 0002_schema_sync, or 0003_faculty_profile_fields.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0004_designation_master_data'
down_revision: Union[str, None] = '0003_faculty_profile_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


designations_table = sa.table(
    'ams_designations',
    sa.column('id', postgresql.UUID(as_uuid=True)),
    sa.column('name', sa.String),
    sa.column('is_active', sa.Boolean),
    sa.column('created_at', sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_table(
        'ams_designations',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_designations_name'),
    )
    op.alter_column('ams_designations', 'is_active', server_default=None)

    now = datetime.now(timezone.utc)
    op.bulk_insert(designations_table, [
        {'id': uuid.uuid4(), 'name': 'Professor', 'is_active': True, 'created_at': now},
        {'id': uuid.uuid4(), 'name': 'Associate Professor', 'is_active': True, 'created_at': now},
        {'id': uuid.uuid4(), 'name': 'Assistant Professor', 'is_active': True, 'created_at': now},
    ])


def downgrade() -> None:
    # Destructive on a populated database if any faculty selection has since
    # relied on rows added after this migration — not executed as part of
    # this task; provided for completeness only.
    op.drop_table('ams_designations')
