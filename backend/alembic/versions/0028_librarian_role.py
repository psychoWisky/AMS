"""add LIBRARIAN role

Revision ID: 0028_librarian_role
Revises: 0027_external_examiners
Create Date: 2026-09-23

Initial Thesis Management task. Same PostgreSQL enum-rebuild mechanism as
`0016_incharge_dpgs_roles`/`0026_vc_examiner_roles` (Postgres has no safe
`ALTER TYPE ... ADD VALUE` inside this repo's transactional-DDL migration
style), adding exactly one new value: `LIBRARIAN`.

Unlike VICE_CHANCELLOR/DPGS/INCHARGE_ACADEMIC_CELL, LIBRARIAN is explicitly
NOT single-holder (multiple users may hold it, like FACULTY) — so the
existing `uq_user_role_assignment_single_holder` partial index is dropped and
recreated with its predicate UNCHANGED (still just the three existing global
single-holder roles); it still has to be dropped/recreated because its
predicate references the enum type being replaced, and Postgres cannot ALTER
COLUMN TYPE underneath a dependent index expression.

Only two columns use this enum today (unchanged since 0026): `ams_users.role`
and `ams_user_role_assignments.role`. This migration does not create, seed,
or assign the role to anyone, and does not touch any other table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0028_librarian_role'
down_revision: Union[str, None] = '0027_external_examiners'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ('SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'HOD', 'FACULTY', 'STUDENT', 'EXTERNAL_EXAMINER')
_NEW_VALUES = ('SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'HOD', 'FACULTY', 'STUDENT', 'EXTERNAL_EXAMINER', 'LIBRARIAN')

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
)

_SINGLE_HOLDER_INDEX = 'uq_user_role_assignment_single_holder'
_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR'"


def upgrade() -> None:
    conn = op.get_bind()

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

    op.execute(
        f"CREATE UNIQUE INDEX {_SINGLE_HOLDER_INDEX} ON ams_user_role_assignments (role) "
        f"WHERE role IN ({_SINGLE_HOLDER_ROLES})"
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
        f"WHERE role IN ({_SINGLE_HOLDER_ROLES})"
    )
