"""Widen acted_role columns from VARCHAR(30) to VARCHAR(50)

Revision ID: 0048_widen_acted_role_columns
Revises: 0047_incharge_scope_split
Create Date: 2026-11-10

Discovered while updating the test suite for `0047_incharge_scope_split`:
`stage.acted_role = user.active_role.value` (external_examiner.py,
progress_report.py, synopsis.py, thesis.py) persists the caller's FULL
`UserRole` enum value as plain text — and 4 of these `*_approval_stages`
tables had `acted_role VARCHAR(30)`, too short for the new scoped Incharge
Academic Cell role names (`incharge_academic_cell_pg_veterinary` = 37 chars,
the longest). `grading.py`'s equivalent column was already `VARCHAR(50)`
(apparently sized with headroom already) and needed no change — this
migration brings the other 4 tables up to that same width, a simple,
purely-additive, lossless `ALTER COLUMN TYPE` widening (never a narrowing,
never touches existing data).

Affected tables/columns:
    ams_external_examiner_approval_stages.acted_role
    ams_progress_report_approval_stages.acted_role
    ams_synopsis_approval_stages.acted_role
    ams_thesis_approval_stages.acted_role
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0048_widen_acted_role_columns'
down_revision: Union[str, None] = '0047_incharge_scope_split'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    'ams_external_examiner_approval_stages',
    'ams_progress_report_approval_stages',
    'ams_synopsis_approval_stages',
    'ams_thesis_approval_stages',
)


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, 'acted_role', type_=sa.String(50), existing_type=sa.String(30))


def downgrade() -> None:
    # Narrowing back to VARCHAR(30) would truncate/fail on any row written
    # with one of the new, longer scoped role names since this migration's
    # upgrade — refuse rather than risk silent data loss or a failed ALTER.
    conn = op.get_bind()
    for table in _TABLES:
        overlong = conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE length(acted_role) > 30")).scalar_one()
        if overlong:
            raise RuntimeError(
                f"Refusing to narrow {table}.acted_role back to VARCHAR(30): "
                f"{overlong} row(s) already have a longer value. Remove/trim "
                f"those rows manually first if downgrading is truly required."
            )
    for table in _TABLES:
        op.alter_column(table, 'acted_role', type_=sa.String(30), existing_type=sa.String(50))
