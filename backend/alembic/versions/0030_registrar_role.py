"""add REGISTRAR role + single-holder constraint

Revision ID: 0030_registrar_role
Revises: 0029_initial_thesis
Create Date: 2026-09-24

Student Migration task. Two independent changes, following the exact pattern of
`0026_vc_examiner_roles` (which added VICE_CHANCELLOR the same way):

1. **PostgreSQL enum rebuild** (`ams_user_role`) — Postgres has no
   `ALTER TYPE ... ADD VALUE` usable safely inside a transaction alongside other
   DDL in this repo's migration style, so the type is recreated with one new
   value added: `REGISTRAR`. Only two columns use this enum today (unchanged
   since 0026/0028): `ams_users.role` and `ams_user_role_assignments.role`.

2. **Exactly-one-active-holder enforcement for REGISTRAR** — extends the
   existing partial unique index on `ams_user_role_assignments(role)`
   (`uq_user_role_assignment_single_holder`) to also cover REGISTRAR, by
   dropping and recreating that index with the same name and a wider
   predicate — not a new, second index — so there remains exactly one
   single-holder-enforcement mechanism for all four global single-holder
   roles (INCHARGE_ACADEMIC_CELL, DPGS, VICE_CHANCELLOR, REGISTRAR).

`REGISTRAR` is confirmed to have exactly one holder in the university (unlike
LIBRARIAN, which is deliberately multi-holder) — this migration enforces that
at the database level, not merely in application code.

Historical note: `REGISTRAR` previously existed in this same enum as one of
four early-development dummy/testing roles, fully removed by
`0015_remove_legacy_roles` (verified zero rows used any of them at removal
time). Because this migration recreates the enum type from scratch each time,
reusing the same string label is not a conflict — this is a fresh definition
for a newly confirmed, genuine business role.

This migration does not create, seed, or assign the role to anyone, and does
not touch any other table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0030_registrar_role'
down_revision: Union[str, None] = '0029_initial_thesis'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ('SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'HOD', 'FACULTY', 'STUDENT', 'EXTERNAL_EXAMINER', 'LIBRARIAN')
_NEW_VALUES = ('SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'REGISTRAR', 'HOD', 'FACULTY', 'STUDENT', 'EXTERNAL_EXAMINER', 'LIBRARIAN')

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
)

_SINGLE_HOLDER_INDEX = 'uq_user_role_assignment_single_holder'
_OLD_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR'"
_NEW_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR', 'REGISTRAR'"


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
    # REGISTRAR — same index name, same mechanism, one more role.
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
