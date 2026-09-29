"""Advisory Committee external ("Member of Others") committee member support

Revision ID: 0043_committee_external_member
Revises: 0042_gradesheet_workflow
Create Date: 2026-09-30

(Revision id deliberately shortened to `0043_committee_external_member` —
`alembic_version.version_num` is `VARCHAR(32)`; the originally-intended longer id
(`0043_advisory_committee_external_member`) was tried first and rejected by the
database with `StringDataRightTruncationError`, caught and rolled back cleanly with
zero DDL applied before retrying with this shorter id — same issue and fix already
documented in `0017_seed_role_master_data`/`0039_seed_role_master_data2`.)

Advisory Committee department-eligibility task. "Member of Others" is now confirmed
to mean a genuine EXTERNAL person outside AVFU (no AMS account, no UserRoleAssignment,
involved offline only) — see `app/models/research.py`'s `CommitteeMember` docstring.
This migration:

  1. Makes `ams_committee_members.faculty_id` NULLABLE (it was previously a required
     FK to `ams_users.id`) — an external member's row has no faculty_id at all.
  2. Adds three new nullable columns: `external_name`, `external_designation`,
     `external_institute`.
  3. Adds a CHECK constraint, `ck_committee_member_internal_xor_external`, requiring
     exactly one of (faculty_id set, all three external fields NULL) or
     (faculty_id NULL, all three external fields set) — never an ambiguous mix,
     enforced at the database level so no future write path can create one.

Purely additive/widening for existing data: every row that exists today has a
non-NULL `faculty_id` (confirmed by inspection before writing this migration — 0
NULL-faculty_id rows in the real dev database) and all three new columns default
to NULL, so every existing row already satisfies the CHECK constraint's first
branch unchanged. No existing row is read, modified, or reclassified as external —
this migration performs zero UPDATEs, only DDL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0043_committee_external_member'
down_revision: Union[str, None] = '0042_gradesheet_workflow'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NAME = "ck_committee_member_internal_xor_external"
_CHECK_SQL = (
    "(faculty_id IS NOT NULL AND external_name IS NULL AND external_designation IS NULL AND external_institute IS NULL) "
    "OR "
    "(faculty_id IS NULL AND external_name IS NOT NULL AND external_designation IS NOT NULL AND external_institute IS NOT NULL)"
)


def _columns(conn) -> set:
    return {c["name"] for c in sa.inspect(conn).get_columns("ams_committee_members")}


def upgrade() -> None:
    conn = op.get_bind()
    existing = _columns(conn)

    # Widen faculty_id to nullable (idempotent: only touches the column if it is
    # currently NOT NULL, so re-running this migration against an already-upgraded
    # or a fresh create_all() database is a harmless no-op).
    col = next((c for c in sa.inspect(conn).get_columns("ams_committee_members") if c["name"] == "faculty_id"), None)
    if col is not None and not col["nullable"]:
        op.alter_column("ams_committee_members", "faculty_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)

    if "external_name" not in existing:
        op.add_column("ams_committee_members", sa.Column("external_name", sa.String(200), nullable=True))
    if "external_designation" not in existing:
        op.add_column("ams_committee_members", sa.Column("external_designation", sa.String(200), nullable=True))
    if "external_institute" not in existing:
        op.add_column("ams_committee_members", sa.Column("external_institute", sa.String(300), nullable=True))

    existing_checks = {c["name"] for c in sa.inspect(conn).get_check_constraints("ams_committee_members")}
    if _CHECK_NAME not in existing_checks:
        op.create_check_constraint(_CHECK_NAME, "ams_committee_members", _CHECK_SQL)


def downgrade() -> None:
    conn = op.get_bind()
    existing_checks = {c["name"] for c in sa.inspect(conn).get_check_constraints("ams_committee_members")}
    if _CHECK_NAME in existing_checks:
        op.drop_constraint(_CHECK_NAME, "ams_committee_members", type_="check")

    leftover = conn.execute(sa.text("SELECT count(*) FROM ams_committee_members WHERE faculty_id IS NULL")).scalar_one()
    if leftover:
        raise RuntimeError(
            f"Refusing to downgrade: {leftover} external committee member row(s) have no faculty_id and would violate "
            "the restored NOT NULL constraint. Remove or reassign those rows manually before downgrading."
        )

    for col in ("external_institute", "external_designation", "external_name"):
        if col in _columns(conn):
            op.drop_column("ams_committee_members", col)

    op.alter_column("ams_committee_members", "faculty_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
