"""add CONTROLLER_OF_EXAMINATION role + single-holder constraint

Revision ID: 0040_coe_role
Revises: 0039_seed_role_master_data2
Create Date: 2026-09-27

Gradesheet/Result task. Same two independent changes, following the exact
pattern of `0030_registrar_role`:

1. **PostgreSQL enum rebuild** (`ams_user_role`) with one new value added:
   `CONTROLLER_OF_EXAMINATION`. Only two columns use this enum today:
   `ams_users.role` and `ams_user_role_assignments.role`.

2. **Exactly-one-active-holder enforcement** — the existing partial unique index
   `uq_user_role_assignment_single_holder` is dropped and recreated with the
   same name and a wider predicate, so there is still exactly ONE
   single-holder mechanism, now covering INCHARGE_ACADEMIC_CELL, DPGS,
   VICE_CHANCELLOR, REGISTRAR and CONTROLLER_OF_EXAMINATION.

This migration does not create, seed, or assign the role to anyone, and does
not touch any other table. (The `ams_roles` master-data row is seeded by the
separate `0041_seed_coe_role_data`, mirroring 0017/0039.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0040_coe_role'
down_revision: Union[str, None] = '0039_seed_role_master_data2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ('SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'REGISTRAR', 'HOD', 'FACULTY', 'STUDENT', 'EXTERNAL_EXAMINER', 'LIBRARIAN')
_NEW_VALUES = _OLD_VALUES + ('CONTROLLER_OF_EXAMINATION',)

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
)

_SINGLE_HOLDER_INDEX = 'uq_user_role_assignment_single_holder'
_OLD_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR', 'REGISTRAR'"
_NEW_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR', 'REGISTRAR', 'CONTROLLER_OF_EXAMINATION'"


def _rebuild_enum(conn, values: tuple, single_holder_roles: str) -> None:
    # The single-holder index is a partial index whose predicate compares `role`
    # against enum literals — Postgres cannot ALTER COLUMN TYPE underneath it,
    # so it is dropped first and recreated against the new type afterwards.
    op.execute(f"DROP INDEX {_SINGLE_HOLDER_INDEX}")
    op.execute("CREATE TYPE ams_user_role_new AS ENUM (" + ", ".join(f"'{v}'" for v in values) + ")")
    for table, column in _COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE ams_user_role_new "
            f"USING {column}::text::ams_user_role_new"
        )
    op.execute("DROP TYPE ams_user_role")
    op.execute("ALTER TYPE ams_user_role_new RENAME TO ams_user_role")

    final_values = conn.execute(sa.text(
        "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'ams_user_role' ORDER BY e.enumsortorder"
    )).scalars().all()
    if set(final_values) != set(values) or len(final_values) != len(values):
        raise RuntimeError(f"Post-migration verification failed: ams_user_role now contains {final_values}, expected {values}.")

    op.execute(
        f"CREATE UNIQUE INDEX {_SINGLE_HOLDER_INDEX} ON ams_user_role_assignments (role) "
        f"WHERE role IN ({single_holder_roles})"
    )


def upgrade() -> None:
    _rebuild_enum(op.get_bind(), _NEW_VALUES, _NEW_SINGLE_HOLDER_ROLES)


def downgrade() -> None:
    conn = op.get_bind()
    leftover = conn.execute(sa.text(
        "SELECT (SELECT count(*) FROM ams_users WHERE role::text = 'CONTROLLER_OF_EXAMINATION') "
        "+ (SELECT count(*) FROM ams_user_role_assignments WHERE role::text = 'CONTROLLER_OF_EXAMINATION')"
    )).scalar_one()
    if leftover:
        raise RuntimeError(
            "Refusing to remove CONTROLLER_OF_EXAMINATION: rows still use it. Remove those "
            "assignments/users manually before downgrading."
        )
    _rebuild_enum(conn, _OLD_VALUES, _OLD_SINGLE_HOLDER_ROLES)
