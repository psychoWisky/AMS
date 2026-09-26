"""seed ams_roles master-data row for CONTROLLER_OF_EXAMINATION

Revision ID: 0041_seed_coe_role_data
Revises: 0040_coe_role
Create Date: 2026-09-27

Same convention and idempotency as `0017_seed_role_master_data` /
`0039_seed_role_master_data2`: `code` is the `UserRole` member NAME, `name` is
the frontend label, `is_system=True`, guarded by `WHERE NOT EXISTS` on `code`
so re-running (or a Super Admin having added it by hand) never duplicates.
Touches no user, assignment, or enum data.
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa


revision: str = '0041_seed_coe_role_data'
down_revision: Union[str, None] = '0040_coe_role'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SEED_ROLES = [
    ('CONTROLLER_OF_EXAMINATION', 'Controller of Examination'),
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
