"""student Academic Year: ams_users.academic_year_id -> ams_academic_calendars

Revision ID: 0024_user_academic_year
Revises: 0023_backfill_student_college
Create Date: 2026-09-20

Confirmed requirement: a student's Academic Year is an explicit, editable
reference to the existing Super Admin-managed Academic Calendar
(`ams_academic_calendars`), not a number and not derived from registrations.

Schema-only, additive:

* `ams_users.academic_year_id` — nullable UUID.
* FK `fk_users_academic_year_id` -> `ams_academic_calendars.id`, with NO
  `ON DELETE` action (default NO ACTION), matching how course offerings and
  course registrations reference a calendar: deleting a calendar that students
  are assigned to is refused (the delete endpoint checks first and returns a
  400), and a calendar delete can never remove or silently unassign a student.
* Index `ix_ams_users_academic_year_id` — the Students list filters on it.

NOTHING IS POPULATED. Every existing student keeps `academic_year_id = NULL`
(unassigned). There is no exact, unambiguous stored relationship to copy from:
`admission_year` (an integer, e.g. 2026), the Orientation candidate's free-text
`academic_year` label (e.g. "2026", "2025-26 (Phase-II)") and calendar rows
(e.g. "2026-27") are different concepts, and no mapping between them has been
confirmed — so none is inferred. `ams_users.admission_year`, every calendar
row, and all registration/enrollment data are untouched.

Downgrade drops the index, FK and column (it is a schema change, unlike the
data-only backfill 0023): it discards any Academic Year assignments made after
the upgrade, and nothing else.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0024_user_academic_year'
down_revision: Union[str, None] = '0023_backfill_student_college'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ams_users',
        sa.Column('academic_year_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_users_academic_year_id', 'ams_users', 'ams_academic_calendars', ['academic_year_id'], ['id'],
    )
    op.create_index('ix_ams_users_academic_year_id', 'ams_users', ['academic_year_id'])


def downgrade() -> None:
    op.drop_index('ix_ams_users_academic_year_id', table_name='ams_users')
    op.drop_constraint('fk_users_academic_year_id', 'ams_users', type_='foreignkey')
    op.drop_column('ams_users', 'academic_year_id')
