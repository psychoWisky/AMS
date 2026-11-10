"""seed ams_roles rows for the 4 scoped Incharge Academic Cell roles; remove stale row

Revision ID: 0049_seed_incharge_roles
Revises: 0048_widen_acted_role_columns
Create Date: 2026-11-10

Same convention and idempotency as `0017_seed_role_master_data`/
`0039_seed_role_master_data2`/`0041_seed_coe_role_data`: `code` is the
`UserRole` member NAME, `name` is the frontend label, `is_system=True`,
guarded by `WHERE NOT EXISTS` on `code` so re-running never duplicates.

Additionally removes the now-stale `INCHARGE_ACADEMIC_CELL` row (the single
role `0047_incharge_scope_split` replaced) — mirrors how `0015_remove_
legacy_roles` removed the `ams_roles` rows for roles it retired. This table
is purely cosmetic master data for the Super Admin "Administration -> Roles"
screen (see `app.models.user.Role`'s own docstring) — never the
authorization mechanism — so removing this one row changes no permission
anywhere; it only stops a now-nonexistent role from appearing in that
screen's list.
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = '0049_seed_incharge_roles'
down_revision: Union[str, None] = '0048_widen_acted_role_columns'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SEED_ROLES = [
    ('INCHARGE_ACADEMIC_CELL_PG_VETERINARY', 'Incharge Academic Cell (PG - Veterinary)'),
    ('INCHARGE_ACADEMIC_CELL_UG_VETERINARY', 'Incharge Academic Cell (UG - Veterinary)'),
    ('INCHARGE_ACADEMIC_CELL_PG_FISHERY', 'Incharge Academic Cell (PG - Fishery)'),
    ('INCHARGE_ACADEMIC_CELL_UG_FISHERY', 'Incharge Academic Cell (UG - Fishery)'),
]
_STALE_ROLE_CODE = 'INCHARGE_ACADEMIC_CELL'


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
    conn.execute(sa.text("DELETE FROM ams_roles WHERE code = :code"), {"code": _STALE_ROLE_CODE})


def downgrade() -> None:
    conn = op.get_bind()
    for code, _name in _SEED_ROLES:
        conn.execute(sa.text("DELETE FROM ams_roles WHERE code = :code"), {"code": code})
    conn.execute(
        sa.text(
            "INSERT INTO ams_roles (id, code, name, is_system, is_active, created_at, updated_at) "
            "SELECT :id, :code, :name, TRUE, TRUE, now(), now() "
            "WHERE NOT EXISTS (SELECT 1 FROM ams_roles WHERE code = :existing_code)"
        ),
        {"id": str(uuid.uuid4()), "code": _STALE_ROLE_CODE, "name": "Incharge Academic Cell", "existing_code": _STALE_ROLE_CODE},
    )
