"""multi-role, multi-department role assignments

Revision ID: 0021_multi_dept_role_assign
Revises: 0020_course_number_not_unique
Create Date: 2026-09-19

Confirmed business requirement: a person may hold the SAME staff role
(HOD/FACULTY) in more than one department simultaneously (e.g. HOD @ A and
HOD @ B), and different roles independently (e.g. DPGS + HOD @ A +
FACULTY @ B) — one role assignment never implies another. The prior model
(`UserRoleAssignment` unique on `(user_id, role)`, with department living
only on the single-valued `ams_users.department_id`) cannot represent this:
a user could hold each role at most once, period, and that one row had no
department of its own at all.

Five changes, in dependency order:

1. Add nullable `department_id` (FK -> ams_departments) to
   `ams_user_role_assignments`.
2. Backfill: for each existing HOD/FACULTY assignment row, set
   `department_id` = that user's current `ams_users.department_id` (their
   only department under the pre-existing single-department model, so this
   is a lossless, unambiguous 1:1 copy — verified read-only beforehand: zero
   existing users hold more than one role assignment, zero HOD/FACULTY rows
   have a NULL `ams_users.department_id`). DPGS/INCHARGE_ACADEMIC_CELL/
   SUPER_ADMIN/STUDENT assignment rows are left with `department_id = NULL`
   (global roles stay global; STUDENT explicitly stays out of this
   department-assignment model per the confirmed business rule — a
   student's department continues to come from `ams_users.department_id`
   directly, unchanged).
3. Replace the `(user_id, role)` unique constraint with
   `(user_id, role, department_id)` — allows the same role to repeat for
   different departments. A SEPARATE partial unique index additionally
   enforces `(user_id, role)` WHERE `department_id IS NULL`, since Postgres
   treats every NULL as distinct in a plain unique constraint and would
   otherwise allow a user to hold e.g. two NULL-department DPGS rows. The
   pre-existing single-institution-wide-holder partial index for DPGS/
   INCHARGE_ACADEMIC_CELL (migration 0016, keyed on `role` alone,
   unconditional on department) is untouched — it already does not
   reference department at all, so it is unaffected by this change.
4. Add nullable `active_role_assignment_id` (FK -> ams_user_role_assignments,
   ON DELETE SET NULL) to `ams_refresh_tokens`, replacing the bare
   `active_role` enum column — the active session context must identify
   ONE SPECIFIC persisted (role, department) grant, not an independently
   editable role name that could drift out of sync with a department value.
5. Backfill each refresh token's `active_role_assignment_id` from the
   `UserRoleAssignment` row matching its old `(user_id, active_role)` pair
   (unambiguous: under the pre-migration `(user_id, role)` uniqueness,
   at most one such row exists per user/role), then drop the old
   `active_role` column.

`ams_users.role`/`ams_users.department_id` are NOT touched, dropped, or
renamed — they remain the legacy/display "primary role"/"home department"
columns exactly as before (see their own model docstrings for their
narrowed, post-this-task meaning).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0021_multi_dept_role_assign'
down_revision: Union[str, None] = '0020_course_number_not_unique'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEPARTMENT_SCOPED_ROLES = ("HOD", "FACULTY")
_GLOBAL_INDEX = "uq_user_role_assignment_global"
_UNIQUE_CONSTRAINT = "uq_user_role_assignment"


def upgrade() -> None:
    conn = op.get_bind()

    # 1. ams_user_role_assignments.department_id
    op.add_column("ams_user_role_assignments", sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_user_role_assignments_department_id", "ams_user_role_assignments", "ams_departments",
        ["department_id"], ["id"],
    )

    # 2. Backfill HOD/FACULTY rows from the user's current single department;
    #    everything else stays NULL (global roles + Student, both correct).
    conn.execute(sa.text(
        "UPDATE ams_user_role_assignments ura "
        "SET department_id = u.department_id "
        "FROM ams_users u "
        "WHERE ura.user_id = u.id AND ura.role IN :roles"
    ).bindparams(sa.bindparam("roles", expanding=True)), {"roles": list(_DEPARTMENT_SCOPED_ROLES)})

    # 3. Replace (user_id, role) with (user_id, role, department_id), plus
    #    the NULL-department partial index that closes the gap the plain
    #    constraint leaves for global roles.
    op.drop_constraint(_UNIQUE_CONSTRAINT, "ams_user_role_assignments", type_="unique")
    op.create_unique_constraint(_UNIQUE_CONSTRAINT, "ams_user_role_assignments", ["user_id", "role", "department_id"])
    op.create_index(
        _GLOBAL_INDEX, "ams_user_role_assignments", ["user_id", "role"],
        unique=True, postgresql_where=sa.text("department_id IS NULL"),
    )

    # 4. ams_refresh_tokens.active_role_assignment_id
    op.add_column("ams_refresh_tokens", sa.Column("active_role_assignment_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_refresh_tokens_active_role_assignment_id", "ams_refresh_tokens", "ams_user_role_assignments",
        ["active_role_assignment_id"], ["id"], ondelete="SET NULL",
    )

    # 5. Backfill from the old active_role column, then drop it.
    conn.execute(sa.text(
        "UPDATE ams_refresh_tokens rt "
        "SET active_role_assignment_id = ura.id "
        "FROM ams_user_role_assignments ura "
        "WHERE rt.user_id = ura.user_id AND rt.active_role IS NOT NULL AND ura.role::text = rt.active_role::text"
    ))
    op.drop_column("ams_refresh_tokens", "active_role")


def downgrade() -> None:
    # Reverse step 5/4: restore the bare active_role column, backfilled from
    # whatever assignment each session currently points at.
    op.add_column("ams_refresh_tokens", sa.Column(
        "active_role", postgresql.ENUM(name="ams_user_role", create_type=False), nullable=True,
    ))
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE ams_refresh_tokens rt "
        "SET active_role = ura.role "
        "FROM ams_user_role_assignments ura "
        "WHERE rt.active_role_assignment_id = ura.id"
    ))
    op.drop_constraint("fk_refresh_tokens_active_role_assignment_id", "ams_refresh_tokens", type_="foreignkey")
    op.drop_column("ams_refresh_tokens", "active_role_assignment_id")

    # Reverse step 3. NOTE: if any user genuinely holds the same role in more
    # than one department at this point (the entire point of this feature),
    # restoring the old (user_id, role) constraint will fail with a Postgres
    # uniqueness violation rather than silently deleting/merging rows — by
    # design. This downgrade is only safe before any real multi-department
    # assignment has been created; treat it as an emergency/pre-adoption
    # rollback path, not a routine one.
    op.drop_index(_GLOBAL_INDEX, table_name="ams_user_role_assignments")
    op.drop_constraint(_UNIQUE_CONSTRAINT, "ams_user_role_assignments", type_="unique")
    op.create_unique_constraint(_UNIQUE_CONSTRAINT, "ams_user_role_assignments", ["user_id", "role"])

    # Reverse step 1.
    op.drop_constraint("fk_user_role_assignments_department_id", "ams_user_role_assignments", type_="foreignkey")
    op.drop_column("ams_user_role_assignments", "department_id")
