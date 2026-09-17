"""add INCHARGE_ACADEMIC_CELL / DPGS roles + single-holder constraint + DPGS signatory fields

Revision ID: 0016_incharge_dpgs_roles
Revises: 0015_remove_legacy_roles
Create Date: 2026-09-18

Confirmed business decision: two new GLOBAL, departmentless roles above HOD
(SUPER_ADMIN > DPGS > INCHARGE_ACADEMIC_CELL > HOD > FACULTY > STUDENT).
This migration does NOT create, seed, or assign either role to any user —
it only adds the schema/enum support; a Super Admin assigns the actual
office holder(s) later via the existing role-assignment endpoints.

Three independent changes:

1. **PostgreSQL enum rebuild** (`ams_user_role`) — same type-recreate-and-
   cast pattern as `0015_remove_legacy_roles` (Postgres has no
   `ALTER TYPE ... ADD VALUE` usable safely inside a single transaction
   alongside other DDL in this repo's migration style, and the prior
   migration already established this exact rebuild approach), this time
   ADDING two values instead of removing any. Casing matches the existing
   convention (SQLAlchemy's plain `Enum` stores the Python member NAME,
   not `.value` — confirmed directly against the live schema before writing
   this migration): `INCHARGE_ACADEMIC_CELL`, `DPGS`.

2. **Exactly-one-active-holder enforcement** — a partial unique index on
   `ams_user_role_assignments(role)` restricted to these two role values.
   This enforces "at most one assignment row for this role, period" (not
   scoped by `ams_users.is_active`, since `UserRoleAssignment` has no
   activity column of its own and Postgres partial-index predicates cannot
   reference another table) — replacing an existing holder requires a Super
   Admin to explicitly remove the old assignment first (the existing
   `DELETE /auth/users/{id}/roles/{role}` endpoint), exactly like every
   other role removal already works. The SAME user may hold BOTH roles
   simultaneously — this index only restricts how many DIFFERENT users may
   hold EACH role, never how many roles one user may hold.

3. **DPGS signatory persistence on Course Registration** — `dpgs_approved_by`
   (FK -> ams_users.id) and `dpgs_approved_at`, both nullable. DPGS is the
   Registration Card's actual final signatory (unlike HOD, which remains an
   unpersisted, live department-lookup display value, unchanged by this
   migration) — the document must reflect the real authenticated DPGS
   approval event, never a "whoever currently holds DPGS" lookup at PDF-
   generation time. PPW/Advisory Committee need NO schema change: PPW's
   `PpwApprovalStage.stage_type`/`Ppw.status` are already plain, unbounded
   strings (by original design, specifically so future stages like this
   could be added without a migration); Advisory Committee has no
   downloadable document at all, so no signature field is needed there.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0016_incharge_dpgs_roles'
down_revision: Union[str, None] = '0015_remove_legacy_roles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = ('SUPER_ADMIN', 'HOD', 'FACULTY', 'STUDENT')
_NEW_VALUES = ('SUPER_ADMIN', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'HOD', 'FACULTY', 'STUDENT')

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
    ('ams_refresh_tokens', 'active_role'),
)

_SINGLE_HOLDER_INDEX = 'uq_user_role_assignment_single_holder'
_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS'"


def upgrade() -> None:
    conn = op.get_bind()

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

    # Exactly-one-active-holder constraint (Section 3.1/8).
    op.execute(
        f"CREATE UNIQUE INDEX {_SINGLE_HOLDER_INDEX} ON ams_user_role_assignments (role) "
        f"WHERE role IN ({_SINGLE_HOLDER_ROLES})"
    )

    # DPGS signatory persistence on Course Registration.
    op.add_column('ams_course_registrations', sa.Column('dpgs_approved_by', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('ams_course_registrations', sa.Column('dpgs_approved_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        'fk_course_registrations_dpgs_approved_by', 'ams_course_registrations', 'ams_users',
        ['dpgs_approved_by'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_course_registrations_dpgs_approved_by', 'ams_course_registrations', type_='foreignkey')
    op.drop_column('ams_course_registrations', 'dpgs_approved_at')
    op.drop_column('ams_course_registrations', 'dpgs_approved_by')

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
