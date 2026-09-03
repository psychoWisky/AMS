"""role master data

Revision ID: 0005_role_master_data
Revises: 0004_designation_master_data
Create Date: 2026-09-04

Adds ams_roles (BUSINESS_LOGIC.md Section N.5 / role-management task) — Super
Admin master data for the Administration UI's Roles tab, replacing the
previous read-only view computed purely from the Python UserRole enum.

This is MASTER DATA ONLY. UserRole, User.role, the ams_user_role PostgreSQL
enum, require_roles(), and every existing authorization check are entirely
untouched by this migration — ams_roles has no foreign key relationship to
ams_users and none is introduced here. Seeds exactly the 8 existing UserRole
values as system roles (is_system=True, is_active=True); their `code` values
match the enum member names so the API can compute a live user_count by
joining on User.role's string value, not because of any structural link.

Does not modify 0001_baseline, 0002_schema_sync, 0003_faculty_profile_fields,
or 0004_designation_master_data.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0005_role_master_data'
down_revision: Union[str, None] = '0004_designation_master_data'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


roles_table = sa.table(
    'ams_roles',
    sa.column('id', postgresql.UUID(as_uuid=True)),
    sa.column('code', sa.String),
    sa.column('name', sa.String),
    sa.column('is_system', sa.Boolean),
    sa.column('is_active', sa.Boolean),
    sa.column('created_at', sa.DateTime(timezone=True)),
    sa.column('updated_at', sa.DateTime(timezone=True)),
)

# code -> display name, matching the current UserRole enum exactly (8 values).
_SEED_ROLES = [
    ('SUPER_ADMIN', 'Super Admin'),
    ('ACADEMIC_ADMIN', 'Academic Admin'),
    ('HOD', 'HOD'),
    ('FACULTY', 'Faculty'),
    ('STUDENT', 'Student'),
    ('REGISTRAR', 'Registrar'),
    ('EXAMINER', 'Examiner'),
    ('RESEARCH_SUPERVISOR', 'Research Supervisor'),
]


def upgrade() -> None:
    op.create_table(
        'ams_roles',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('code', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_roles_code'),
    )
    op.alter_column('ams_roles', 'is_system', server_default=None)
    op.alter_column('ams_roles', 'is_active', server_default=None)

    now = datetime.now(timezone.utc)
    op.bulk_insert(roles_table, [
        {'id': uuid.uuid4(), 'code': code, 'name': name, 'is_system': True, 'is_active': True, 'created_at': now, 'updated_at': now}
        for code, name in _SEED_ROLES
    ])


def downgrade() -> None:
    # Removes only what this migration created — the table itself (and with it,
    # any custom rows added since, which is unavoidable for a table-level
    # downgrade). Does not touch ams_users, ams_user_role, or any other table.
    op.drop_table('ams_roles')
