"""Manual Major/Minor/Supporting Discipline selection: nullable department FKs on Ppw and CourseRegistration

Revision ID: 0046_manual_discipline_selection
Revises: 0045_research_assignment_type
Create Date: 2026-11-03

AVFU-confirmed requirement: Major, Minor and Supporting Discipline are no
longer derived from the student's selected courses — the student explicitly
searches and selects each one from the authoritative `Department` records.
The value must be stored and displayed as selected, never re-inferred.

Three additive, nullable columns on EACH of `ams_ppw` and
`ams_course_registrations` (FK -> `ams_departments.id`, no cascade — a
Department is never hard-deleted in this system, only deactivated):
`major_department_id`, `minor_department_id`, `supporting_department_id`.

Neither table had any ID-based storage for these before this revision —
`Ppw.minor_field`/`Ppw.supporting_field` (pre-existing, UNCHANGED, NOT
dropped by this migration) stored a derived DISPLAY STRING, not a selected
record's id, and `CourseRegistration` had no discipline storage at all,
these values being computed fresh on every read from the student's course
classifications. Purely additive — no existing column, table, or row is
touched, and nothing is backfilled: every existing record simply has NULL
in these three new columns, exactly as the application code treats "not yet
selected" going forward.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0046_manual_discipline_selection'
down_revision: Union[str, None] = '0045_research_assignment_type'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)

_COLUMNS = ("major_department_id", "minor_department_id", "supporting_department_id")
_TABLES = ("ams_ppw", "ams_course_registrations")


def upgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.add_column(table, sa.Column(column, UUID, sa.ForeignKey('ams_departments.id'), nullable=True))
            op.create_index(f'ix_{table}_{column}', table, [column])


def downgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.drop_index(f'ix_{table}_{column}', table_name=table)
            op.drop_column(table, column)
