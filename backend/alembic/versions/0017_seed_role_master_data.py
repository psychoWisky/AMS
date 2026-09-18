"""seed ams_roles master-data rows for INCHARGE_ACADEMIC_CELL / DPGS

Revision ID: 0017_seed_role_master_data
Revises: 0016_incharge_dpgs_roles
Create Date: 2026-09-18

Migration `0016_incharge_dpgs_roles` added `INCHARGE_ACADEMIC_CELL`/`DPGS` to
the `UserRole` Python/PostgreSQL enum (the actual authorization mechanism)
but never touched `ams_roles` — a separate, cosmetic master-data table that
only feeds the Super Admin "Administration -> Roles" screen (see
`app.models.user.Role`'s own docstring: "deliberately NOT the authorization
mechanism"). That table currently has exactly 4 rows (`SUPER_ADMIN`, `HOD`,
`FACULTY`, `STUDENT`) — the two new roles are simply missing rows there,
which is why they don't appear on that screen despite being fully
functional everywhere else (role assignment, active-role switching,
authorization, workflows).

This migration inserts exactly the two missing rows, `is_system=True`,
mirroring `0005_role_master_data`'s original seeding exactly (same columns,
same `is_system=True`/`is_active=True` convention for real system roles).
It does NOT:
  * create or modify any `ams_users` row
  * create or modify any `ams_user_role_assignments` row
  * touch the `ams_user_role` Postgres enum (already correct since 0016)
  * touch the 4 existing `ams_roles` rows in any way

Idempotent: each insert is guarded by a `WHERE NOT EXISTS` check on `code`,
so re-running this migration against a database where these rows already
exist (e.g. if a Super Admin manually added them via `POST /admin/roles`
before this migration ran) does not create duplicates.

(Revision id deliberately shorter than the original filename idea —
`alembic_version.version_num` is `VARCHAR(32)`; a longer id was tried first
and rejected by the database with `StringDataRightTruncationError`, caught
and rolled back cleanly before any row was written — verified read-only
before retrying with this shorter id.)
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = '0017_seed_role_master_data'
down_revision: Union[str, None] = '0016_incharge_dpgs_roles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# code -> display name — code matches the UserRole enum member NAME exactly
# (confirmed convention: `_role_dict()` in departments.py looks up
# `UserRole[r.code]`, i.e. by member name, not `.value`), name matches the
# existing frontend label in `frontend/lib/utils.ts::ROLES` exactly.
_SEED_ROLES = [
    ('INCHARGE_ACADEMIC_CELL', 'Incharge Academic Cell'),
    ('DPGS', 'DPGS'),
]


def upgrade() -> None:
    conn = op.get_bind()
    for code, name in _SEED_ROLES:
        conn.execute(
            sa.text(
                "INSERT INTO ams_roles (id, code, name, is_system, is_active, created_at, updated_at) "
                "SELECT :id, :code, :name, TRUE, TRUE, now(), now() "
                "WHERE NOT EXISTS (SELECT 1 FROM ams_roles WHERE code = :existing_code)"
            ),
            {"id": str(uuid.uuid4()), "code": code, "name": name, "existing_code": code},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for code, _name in _SEED_ROLES:
        conn.execute(sa.text("DELETE FROM ams_roles WHERE code = :code"), {"code": code})
