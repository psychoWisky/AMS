"""Progress Report field correction: completion fields are Student's, not Major Advisor's;
add the actual Major Advisor fields (Advisory Remark / Overall Progress / Student Conduct)

Revision ID: 0033_pr_field_correction
Revises: 0032_progress_report
Create Date: 2026-09-26

AVFU clarified that `advisor_completion_expected`/`advisor_reason` on `ams_progress_reports`
were misattributed in the initial implementation — they are STUDENT-entered fields, not Major
Advisor fields. There are zero rows in `ams_progress_reports` in every environment this has
been deployed to so far (the module was implemented and tested, never used in production), so
a plain rename is safe and preferred over add-new-column-and-drop-old (no data migration
needed, no risk of silently losing a value).

* `advisor_completion_expected` -> `expected_completion` (same Boolean type/semantics — Yes/No,
  now correctly understood as the student's own answer).
* `advisor_reason` -> `completion_delay_reason` (same Text type/semantics).
* NEW: `advisory_remark`, `overall_progress`, `student_conduct` (all nullable Text) — the
  actual Major Advisor fields, entered only by the current Major Advisor at their stage.

No other table is touched. Downgrade reverses both renames and drops the three new columns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0033_pr_field_correction'
down_revision: Union[str, None] = '0032_progress_report'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('ams_progress_reports', 'advisor_completion_expected', new_column_name='expected_completion')
    op.alter_column('ams_progress_reports', 'advisor_reason', new_column_name='completion_delay_reason')
    op.add_column('ams_progress_reports', sa.Column('advisory_remark', sa.Text, nullable=True))
    op.add_column('ams_progress_reports', sa.Column('overall_progress', sa.Text, nullable=True))
    op.add_column('ams_progress_reports', sa.Column('student_conduct', sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column('ams_progress_reports', 'student_conduct')
    op.drop_column('ams_progress_reports', 'overall_progress')
    op.drop_column('ams_progress_reports', 'advisory_remark')
    op.alter_column('ams_progress_reports', 'completion_delay_reason', new_column_name='advisor_reason')
    op.alter_column('ams_progress_reports', 'expected_completion', new_column_name='advisor_completion_expected')
