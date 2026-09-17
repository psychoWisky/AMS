"""remove legacy dummy roles from ams_user_role enum (role-cleanup task)

Revision ID: 0015_remove_legacy_roles
Revises: 0014_multi_role
Create Date: 2026-09-17

Business decision: `ACADEMIC_ADMIN`, `REGISTRAR`, `EXAMINER`, and
`RESEARCH_SUPERVISOR` were early-development dummy/testing roles, never real
AVFU roles. The current, intentional `UserRole` enum
(`app/models/user.py`) now contains only:

    SUPER_ADMIN, HOD, FACULTY, STUDENT

IMPORTANT casing note (verified directly against the live schema before
writing this migration, not assumed): SQLAlchemy's `SAEnum(UserRole, ...)`
stores each Python enum member's NAME, not its `.value` — so the actual
PostgreSQL enum labels are the uppercase forms (`SUPER_ADMIN`, `ACADEMIC_ADMIN`,
...), not the lowercase `.value` strings (`"super_admin"`, ...) that appear
in application code/JSON. This migration operates on the real, uppercase
labels throughout.

PostgreSQL does not support `ALTER TYPE ... DROP VALUE`, so this migration
rebuilds the `ams_user_role` enum type from scratch:

  1. Safety check (again, inside this migration, not just trusted from an
     earlier manual investigation): abort loudly if any row in any of the
     three columns using this enum (`ams_users.role`,
     `ams_user_role_assignments.role`, `ams_refresh_tokens.active_role`)
     currently holds one of the four removed values. This migration performs
     NO value remapping (no REGISTRAR->HOD, no ACADEMIC_ADMIN->SUPER_ADMIN,
     etc.) — if such rows existed, the correct action is a human decision,
     not a guess baked into a migration.
  2. Create a new enum type (`ams_user_role_new`) with only the four values.
  3. Cast all three columns over to it (`USING col::text::ams_user_role_new`).
  4. Drop the old 8-value enum type.
  5. Rename the new type to `ams_user_role`.

Verified immediately before writing this migration (read-only queries against
the real enum labels):
    ams_users.role                       legacy rows = 0
    ams_user_role_assignments.role       legacy rows = 0
    ams_refresh_tokens.active_role       legacy rows = 0
These are the only three columns in the schema using `ams_user_role`
(confirmed via information_schema.columns WHERE udt_name = 'ams_user_role').

`ams_roles` (unrelated master-data label table) is untouched by this
migration — it already contains only the 4 required rows (SUPER_ADMIN/HOD/
FACULTY/STUDENT), independently of this enum.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0015_remove_legacy_roles'
down_revision: Union[str, None] = '0014_multi_role'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = (
    'SUPER_ADMIN', 'ACADEMIC_ADMIN', 'HOD', 'FACULTY', 'STUDENT',
    'REGISTRAR', 'EXAMINER', 'RESEARCH_SUPERVISOR',
)
_NEW_VALUES = ('SUPER_ADMIN', 'HOD', 'FACULTY', 'STUDENT')
_LEGACY_VALUES = ('ACADEMIC_ADMIN', 'REGISTRAR', 'EXAMINER', 'RESEARCH_SUPERVISOR')

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
    ('ams_refresh_tokens', 'active_role'),
)


def _assert_no_legacy_rows(conn) -> None:
    for table, column in _COLUMNS:
        legacy_list = ", ".join(f"'{v}'" for v in _LEGACY_VALUES)
        count = conn.execute(sa.text(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({legacy_list})"
        )).scalar_one()
        if count:
            raise RuntimeError(
                f"Refusing to remove legacy UserRole values: {table}.{column} still has "
                f"{count} row(s) using a removed role. This migration performs no automatic "
                f"remapping — resolve these rows manually before re-running the migration."
            )


def upgrade() -> None:
    conn = op.get_bind()
    _assert_no_legacy_rows(conn)

    op.execute("CREATE TYPE ams_user_role_new AS ENUM ('SUPER_ADMIN', 'HOD', 'FACULTY', 'STUDENT')")

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
    if set(final_values) != set(_NEW_VALUES) or len(final_values) != 4:
        raise RuntimeError(f"Post-migration verification failed: ams_user_role now contains {final_values}, expected {_NEW_VALUES}.")


def downgrade() -> None:
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
