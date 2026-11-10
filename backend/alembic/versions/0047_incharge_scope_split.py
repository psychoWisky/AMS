"""Split INCHARGE_ACADEMIC_CELL into 4 scoped roles + add Program.faculty

Revision ID: 0047_incharge_scope_split
Revises: 0046_manual_discipline_selection
Create Date: 2026-11-10

AVFU-confirmed business requirement: Incharge Academic Cell responsibilities
are split into 4 distinct (programme-group x faculty) scopes:

    INCHARGE_ACADEMIC_CELL_PG_VETERINARY, INCHARGE_ACADEMIC_CELL_UG_VETERINARY,
    INCHARGE_ACADEMIC_CELL_PG_FISHERY,    INCHARGE_ACADEMIC_CELL_UG_FISHERY

replacing the single, institution-wide INCHARGE_ACADEMIC_CELL role entirely —
see `app.models.user.UserRole`'s docstring for the full business framing, and
the investigation report (production-clone analysis, this task's predecessor)
for how this was decided.

Three independent changes, in dependency order:

1. **Remove existing INCHARGE_ACADEMIC_CELL assignment rows FIRST** — the
   production investigation found exactly one existing holder
   (rita.nath@avfu.ac.in). AVFU's EXPLICIT decision: do NOT auto-map her (or
   anyone else holding this role) to any of the 4 new scopes — a Super Admin
   must assign her new scope(s) manually, later, through the application.
   This migration therefore DELETES every `ams_user_role_assignments` row
   with `role = 'INCHARGE_ACADEMIC_CELL'` (whatever their count — zero in
   this local dev database, confirmed before writing this migration; exactly
   one in the production clone investigated) before the enum rebuild below,
   which would otherwise fail to cast a now-nonexistent enum value. A
   pre-flight check aborts the migration (not silently drops anything) if
   any `ams_users.role` (the separate legacy/display column) is still
   `'INCHARGE_ACADEMIC_CELL'` — confirmed via direct inspection that this
   never happens in practice (every real holder's legacy `role` is their
   primary role, e.g. FACULTY; INCHARGE_ACADEMIC_CELL is only ever held as
   an additional `UserRoleAssignment`), so this is a safety net, not an
   expected code path. This step preserves every OTHER role assignment for
   every user untouched, including every other role Dr. Nath holds.

2. **PostgreSQL enum rebuild** (`ams_user_role`), same 3-step
   create-cast-rename pattern as `0016`/`0030`/`0040` — `INCHARGE_ACADEMIC_
   CELL` is replaced by the 4 new values in the same relative position. Only
   two columns use this enum: `ams_users.role`, `ams_user_role_assignments.
   role`. The pre-existing single-institution-wide-holder partial index
   (`uq_user_role_assignment_single_holder`) is dropped and recreated with
   the 4 new names in place of the old single one — AVFU-confirmed "exactly
   one holder per scope" — using the exact same mechanism, no new schema
   concept.

3. **`Program.faculty`** — a new nullable column (`"Veterinary"` /
   `"Fishery"`), backfilled for the 4 currently-active programmes exactly as
   confirmed by AVFU from the production-clone investigation:
   MVSc->Veterinary, Ph.D(V)->Veterinary, MFSc->Fishery, PhD(F)->Fishery.
   Matched by `code` (safe/no-op if a given database doesn't have these
   exact programme codes — never destructive). A `CHECK` constraint
   restricts it to the two known values (or NULL), consistent with this
   project's existing convention of a plain, checked string column over a
   second Postgres enum type for a small, display-oriented classification
   (mirrors `Department.stream`'s plain-string pattern, with validation
   added since this field drives real authorization logic, unlike
   `stream`). `Program.level` is NOT touched by this migration, anywhere,
   for any row — AVFU was explicit that `Ph.D(V).level = 'PhD'` in
   production (and whatever a given dev database happens to have) must be
   preserved exactly as-is; this migration reads `level` nowhere and writes
   it nowhere.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0047_incharge_scope_split'
down_revision: Union[str, None] = '0046_manual_discipline_selection'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_VALUES = (
    'SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', 'INCHARGE_ACADEMIC_CELL', 'REGISTRAR', 'HOD', 'FACULTY',
    'STUDENT', 'EXTERNAL_EXAMINER', 'LIBRARIAN', 'CONTROLLER_OF_EXAMINATION',
)
_NEW_INCHARGE_VALUES = (
    'INCHARGE_ACADEMIC_CELL_PG_VETERINARY', 'INCHARGE_ACADEMIC_CELL_UG_VETERINARY',
    'INCHARGE_ACADEMIC_CELL_PG_FISHERY', 'INCHARGE_ACADEMIC_CELL_UG_FISHERY',
)
_NEW_VALUES = (
    'SUPER_ADMIN', 'VICE_CHANCELLOR', 'DPGS', *_NEW_INCHARGE_VALUES, 'REGISTRAR', 'HOD', 'FACULTY',
    'STUDENT', 'EXTERNAL_EXAMINER', 'LIBRARIAN', 'CONTROLLER_OF_EXAMINATION',
)

_COLUMNS = (
    ('ams_users', 'role'),
    ('ams_user_role_assignments', 'role'),
)

_SINGLE_HOLDER_INDEX = 'uq_user_role_assignment_single_holder'
_OLD_SINGLE_HOLDER_ROLES = "'INCHARGE_ACADEMIC_CELL', 'DPGS', 'VICE_CHANCELLOR', 'REGISTRAR', 'CONTROLLER_OF_EXAMINATION'"
_NEW_SINGLE_HOLDER_ROLES = (
    "'INCHARGE_ACADEMIC_CELL_PG_VETERINARY', 'INCHARGE_ACADEMIC_CELL_UG_VETERINARY', "
    "'INCHARGE_ACADEMIC_CELL_PG_FISHERY', 'INCHARGE_ACADEMIC_CELL_UG_FISHERY', "
    "'DPGS', 'VICE_CHANCELLOR', 'REGISTRAR', 'CONTROLLER_OF_EXAMINATION'"
)

_FACULTY_BACKFILL = {
    'MVSc': 'Veterinary', 'Ph.D(V)': 'Veterinary',
    'MFSc': 'Fishery', 'PhD(F)': 'Fishery',
}


def _rebuild_enum(conn, values: tuple, single_holder_roles: str) -> None:
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
    conn = op.get_bind()

    # Step 1 — AVFU's explicit decision: no existing holder is auto-mapped to
    # any new scope. Removed BEFORE the enum rebuild (a value about to stop
    # existing cannot be cast forward).
    stray_legacy_role = conn.execute(sa.text(
        "SELECT count(*) FROM ams_users WHERE role::text = 'INCHARGE_ACADEMIC_CELL'"
    )).scalar_one()
    if stray_legacy_role:
        raise RuntimeError(
            "Refusing to proceed: one or more ams_users rows have the legacy "
            "role column set to INCHARGE_ACADEMIC_CELL (expected: none — this "
            "role is only ever held as an additional UserRoleAssignment). "
            "Investigate and resolve manually before re-running this migration."
        )
    removed = conn.execute(sa.text(
        "DELETE FROM ams_user_role_assignments WHERE role::text = 'INCHARGE_ACADEMIC_CELL' RETURNING id"
    )).fetchall()
    print(f"0047: removed {len(removed)} existing INCHARGE_ACADEMIC_CELL assignment row(s) "
          f"(no new scope assigned — Super Admin must assign explicitly).")

    # Step 2 — enum rebuild + single-holder index.
    _rebuild_enum(conn, _NEW_VALUES, _NEW_SINGLE_HOLDER_ROLES)

    # Step 3 — Program.faculty.
    op.add_column('ams_programs', sa.Column('faculty', sa.String(20), nullable=True))
    op.create_check_constraint(
        'ck_program_faculty', 'ams_programs',
        "faculty IS NULL OR faculty IN ('Veterinary', 'Fishery')",
    )
    for code, faculty in _FACULTY_BACKFILL.items():
        op.execute(sa.text("UPDATE ams_programs SET faculty = :faculty WHERE code = :code").bindparams(faculty=faculty, code=code))


def downgrade() -> None:
    conn = op.get_bind()
    leftover = conn.execute(sa.text(
        "SELECT count(*) FROM ams_user_role_assignments WHERE role::text IN ("
        + ", ".join(f"'{v}'" for v in _NEW_INCHARGE_VALUES) + ")"
    )).scalar_one()
    if leftover:
        raise RuntimeError(
            "Refusing to downgrade: one or more of the 4 scoped Incharge Academic "
            "Cell roles are still assigned. Remove those assignments manually "
            "first — downgrading cannot recreate the single pre-split role or "
            "guess which (if any) scope should become it."
        )
    op.drop_constraint('ck_program_faculty', 'ams_programs', type_='check')
    op.drop_column('ams_programs', 'faculty')
    _rebuild_enum(conn, _OLD_VALUES, _OLD_SINGLE_HOLDER_ROLES)
