"""multi-role assignments + session-scoped active role (role-switching task)

Revision ID: 0014_multi_role
Revises: 0013_user_college
Create Date: 2026-09-17

Two additive changes, fully backward-compatible with existing single-role
users:

1. `ams_user_role_assignments` (new table) — the authoritative record of
   which roles a user has been granted. `(user_id, role)` unique. Backfilled
   with exactly one row per EXISTING user (`role = ams_users.role`), so no
   existing account loses access. Reuses the existing `ams_user_role`
   Postgres enum type (create_type=False — it already exists).

2. `ams_refresh_tokens.active_role` (new nullable column, same enum type) —
   each RefreshToken row is this app's existing "session" unit (one per
   login/refresh, already per-device via `device_info`); storing the active
   role here (rather than on `ams_users`) keeps role-switching scoped to one
   browser/session, never leaking to another concurrent session for the same
   account. Left NULL for pre-existing refresh-token rows; the backend
   deterministically derives an active role on first use after this
   migration (see app.core.dependencies.get_current_user).

`ams_users.role` itself is NOT touched, dropped, or renamed — it remains as
the legacy/display "primary role" column (see User.role's docstring).
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0014_multi_role'
down_revision: Union[str, None] = '0013_user_college'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLE_ENUM = postgresql.ENUM(
    'super_admin', 'academic_admin', 'hod', 'faculty', 'student',
    'registrar', 'examiner', 'research_supervisor',
    name='ams_user_role', create_type=False,
)


def upgrade() -> None:
    op.create_table(
        'ams_user_role_assignments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('ams_users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', _ROLE_ENUM, nullable=False),
        sa.Column('assigned_by', postgresql.UUID(as_uuid=True), sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'role', name='uq_user_role_assignment'),
    )
    op.create_index('ix_user_role_assignments_user_id', 'ams_user_role_assignments', ['user_id'])

    op.add_column('ams_refresh_tokens', sa.Column('active_role', _ROLE_ENUM, nullable=True))

    # Backfill: exactly one assignment row per existing user, matching their
    # current scalar role — no existing account loses access, and none gains
    # a role they didn't already have.
    conn = op.get_bind()
    users = conn.execute(sa.text('SELECT id, role FROM ams_users')).fetchall()
    now = sa.func.now()
    for user_id, role in users:
        conn.execute(
            sa.text(
                'INSERT INTO ams_user_role_assignments (id, user_id, role, assigned_by, assigned_at) '
                'VALUES (:id, :user_id, :role, NULL, now())'
            ),
            {"id": str(uuid.uuid4()), "user_id": str(user_id), "role": role},
        )


def downgrade() -> None:
    op.drop_column('ams_refresh_tokens', 'active_role')
    op.drop_index('ix_user_role_assignments_user_id', table_name='ams_user_role_assignments')
    op.drop_table('ams_user_role_assignments')
