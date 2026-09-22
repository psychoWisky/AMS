"""add VICE_CHANCELLOR / EXTERNAL_EXAMINER roles + VC single-holder constraint

Revision ID: 0026_vc_examiner_roles
Revises: 0025_synopsis
Create Date: 2026-09-23

External Examiner Selection task. Two independent changes, following the exact
pattern of `0016_incharge_dpgs_roles` (which added INCHARGE_ACADEMIC_CELL/DPGS
the same way):

1. **PostgreSQL enum rebuild** (`ams_user_role`) — Postgres has no
   `ALTER TYPE ... ADD VALUE` usable safely inside a transaction alongside
   other DDL in this repo's migration style, so the type is recreated with the
   two new values added: `VICE_CHANCELLOR`, `EXTERNAL_EXAMINER`. Only two
   columns use this enum today (verified against the live schema before
   writing this migration — `0021_multi_dept_role_assign` already dropped
   `ams_refresh_tokens.active_role`, unlike the three-column list `0016` had
   to handle): `ams_users.role` and `ams_user_role_assignments.role`.

2. **Exactly-one-active-holder enforcement for VICE_CHANCELLOR** — extends the
   existing partial unique index on `ams_user_role_assignments(role)`
   (`uq_user_role_assignment_single_holder`, created by `0016` for
   INCHARGE_ACADEMIC_CELL/DPGS) to also cover VICE_CHANCELLOR, by dropping and
   recreating that index with the same name and a wider predicate — not a new,
   second index — so there is exactly one single-holder-enforcement mechanism
   for all three global roles, matching the existing convention exactly. As
   before, this only restricts how many DIFFERENT users may hold this ONE
   role; a user may still hold VICE_CHANCELLOR alongside another role if a
   future business rule ever allows it (nothing here forbids that).

EXTERNAL_EXAMINER is NOT single-holder (it is an ordinary, unlimited-count
account role) and needs no index — it is added to the enum only.

This migration does not create, seed, or assign either role, and does not
touch any other table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0026_vc_examiner_roles'
down_revision: Union[str, None] = '0025_synopsis'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ('SUPER_ADMIN', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'HOD', 'FACULTY', 'STUDENT')
_NEW_VALUES = ('SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'HOD', 'FACULTY', 'STUDENT', 'EXTERNAL_EXAMINER')

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
)

_SINGLE_HOLDER_INDEX = 'uq_user_role_assignment_single_holder'
_OLD_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS'"
_NEW_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR'"


def upgrade() -> None:
    conn = op.get_bind()

    # The existing single-holder index is a partial index WHOSE PREDICATE compares
    # `role` against enum literals — Postgres cannot ALTER COLUMN TYPE underneath a
    # dependent index expression like that, so it must be dropped first and
    # recreated afterwards (against the new type), not merely "widened" in place.
    op.execute(f"DROP INDEX {_SINGLE_HOLDER_INDEX}")

    op.execute(
        "CREATE TYPE ams_user_role_new AS ENUM ("
        + ", ".join(f"'{v}'" for v in _NEW_VALUES) + ")"
    )
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
    if set(final_values) != set(_NEW_VALUES) or len(final_values) != len(_NEW_VALUES):
        raise RuntimeError(f"Post-migration verification failed: ams_user_role now contains {final_values}, expected {_NEW_VALUES}.")

    # Recreate the single-holder index against the NEW type, widened to also cover
    # VICE_CHANCELLOR — same index name, same mechanism, one more role.
    op.execute(
        f"CREATE UNIQUE INDEX {_SINGLE_HOLDER_INDEX} ON ams_user_role_assignments (role) "
        f"WHERE role IN ({_NEW_SINGLE_HOLDER_ROLES})"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX {_SINGLE_HOLDER_INDEX}")

    op.execute(
        "CREATE TYPE ams_user_role_old AS ENUM ("
        + ", ".join(f"'{v}'" for v in _OLD_VALUES) + ")"
    )
    for table, column in _COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE ams_user_role_old "
            f"USING {column}::text::ams_user_role_old"
        )
    op.execute("DROP TYPE ams_user_role")
    op.execute("ALTER TYPE ams_user_role_old RENAME TO ams_user_role")

    op.execute(
        f"CREATE UNIQUE INDEX {_SINGLE_HOLDER_INDEX} ON ams_user_role_assignments (role) "
        f"WHERE role IN ({_OLD_SINGLE_HOLDER_ROLES})"
    )
