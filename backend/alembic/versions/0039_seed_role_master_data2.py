"""seed ams_roles master-data rows for VICE_CHANCELLOR / REGISTRAR / EXTERNAL_EXAMINER / LIBRARIAN

Revision ID: 0039_seed_role_master_data2
Revises: 0038_comprehensive_exam
Create Date: 2026-09-26

Role audit: `ams_roles` (the cosmetic, non-authoritative master-data table
behind the Super Admin "Administration -> Roles" screen — see
`app.models.user.Role`'s docstring: "deliberately NOT the authorization
mechanism") was only ever seeded up to `0017_seed_role_master_data`
(SUPER_ADMIN, HOD, FACULTY, STUDENT, INCHARGE_ACADEMIC_CELL, DPGS). Four
`UserRole` enum members added by later migrations were never given a
matching `ams_roles` row, each purely by omission in its own migration
(confirmed by inspection — none of `0026_vc_examiner_roles`,
`0028_librarian_role`, `0030_registrar_role` touches `ams_roles` at all):

    VICE_CHANCELLOR    (0026_vc_examiner_roles)
    EXTERNAL_EXAMINER  (0026_vc_examiner_roles)
    LIBRARIAN          (0028_librarian_role)
    REGISTRAR          (0030_registrar_role)

These four roles are fully functional everywhere else (role assignment,
active-role switching, `require_roles()` authorization, every workflow) —
they are simply missing from this one cosmetic listing table, exactly the
same class of gap `0017` fixed for INCHARGE_ACADEMIC_CELL/DPGS.

This migration inserts exactly the four missing rows, `is_system=True`,
mirroring `0017_seed_role_master_data`/`0005_role_master_data` exactly (same
columns, same `is_system=True`/`is_active=True` convention for real system
roles). `code` matches the `UserRole` enum member NAME exactly (confirmed
convention: `_role_dict()` in departments.py looks up `UserRole[r.code]`),
`name` matches the existing frontend label in `frontend/lib/utils.ts::ROLES`
exactly. It does NOT:
  * create or modify any `ams_users` row
  * create or modify any `ams_user_role_assignments` row
  * touch the `ams_user_role` Postgres enum (already correct since 0030)
  * touch any of the 6 existing `ams_roles` rows in any way
  * create an AMS user account for EXTERNAL_EXAMINER — that role continues to
    be granted only automatically on VC selection (external_examiner.py),
    never manually assigned and never seeded with a user here

Idempotent: each insert is guarded by a `WHERE NOT EXISTS` check on `code`,
so re-running this migration against a database where some or all of these
rows already exist (e.g. a Super Admin manually added one via
`POST /admin/roles` before this migration ran, or this migration ran
before) does not create duplicates. Safe against a fresh database (runs
after `0038`'s tables/enum already exist) and against a database that
already has some subset of these four rows.

(Revision id deliberately shortened to `0039_seed_role_master_data2` —
`alembic_version.version_num` is `VARCHAR(32)`; the originally-intended
longer id was tried first and rejected by the database with
`StringDataRightTruncationError`, caught and rolled back cleanly with zero
rows written before retrying with this shorter id — same issue and same fix
`0017_seed_role_master_data`'s docstring already documented.)
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = '0039_seed_role_master_data2'
down_revision: Union[str, None] = '0038_comprehensive_exam'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# code -> display name — code matches the UserRole enum member NAME exactly,
# name matches frontend/lib/utils.ts::ROLES exactly (same convention as 0017).
_SEED_ROLES = [
    ('VICE_CHANCELLOR', 'Vice Chancellor'),
    ('REGISTRAR', 'Registrar'),
    ('EXTERNAL_EXAMINER', 'External Examiner'),
    ('LIBRARIAN', 'Librarian'),
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
