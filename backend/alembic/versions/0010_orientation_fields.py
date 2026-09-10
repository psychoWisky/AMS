"""orientation candidate fields (name split, avfu email, college)

Revision ID: 0010_orientation_fields
Revises: 0009_program_department_m2m
Create Date: 2026-09-11

Confirmed business requirement: Orientation's "Add Candidate" must capture
separate First/Middle/Last name, a required Mobile, a distinct AVFU Email
(externally issued by IT for each shortlisted student — this becomes the
created student's AMS login/account email) separate from the existing
Personal Email (kept as the candidate's own contact address), and a required
College (using the existing `ams_colleges` master-data table — no new College
concept introduced).

Additive/non-destructive only:
- `ams_orientation_candidates.name` (the pre-existing single-field name) is
  RELAXED from NOT NULL to nullable — never dropped, never rewritten. The two
  existing candidate rows keep their `name` value exactly as-is; new
  candidates going forward populate the new first_name/middle_name/last_name
  columns instead (see ppw.py's PpwCourse.classification convention of never
  duplicating data that already lives elsewhere — same reasoning applied
  here: a row uses EITHER `name` (legacy) OR the three structured columns
  (new), never both, decided by the application layer, not enforced by a DB
  constraint since that would require a CHECK complex enough to risk breaking
  the two existing rows).
- New nullable columns: first_name, middle_name, last_name, avfu_email,
  college_id (FK to ams_colleges.id). All NULL for the two pre-existing
  candidates — never guessed/backfilled.
- `avfu_email` gets a UNIQUE constraint (nullable-safe — multiple NULLs are
  permitted under a Postgres UNIQUE constraint) so two candidates can never
  share the same AVFU email once both have one on file.

Does NOT touch: ams_programs, ams_departments, ams_program_departments,
ams_users, ams_admission_applications, any PPW table, or any prior migration
(0001-0009). No existing candidate row is deleted or has its primary key
changed. The existing uniqueness rule on
(personal_email, academic_year, program_id) is untouched.

Downgrade note: intentionally does NOT restore `name`'s original NOT NULL
constraint — any candidate created after this migration may legitimately have
`name IS NULL` (using the new structured fields instead), and re-imposing
NOT NULL on downgrade would fail unpredictably depending on what data exists
at downgrade time. Leaving it nullable keeps downgrade unconditionally safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0010_orientation_fields'
down_revision: Union[str, None] = '0009_program_department_m2m'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('ams_orientation_candidates', 'name', existing_type=sa.String(length=200), nullable=True)

    op.add_column('ams_orientation_candidates', sa.Column('first_name', sa.String(length=100), nullable=True))
    op.add_column('ams_orientation_candidates', sa.Column('middle_name', sa.String(length=100), nullable=True))
    op.add_column('ams_orientation_candidates', sa.Column('last_name', sa.String(length=100), nullable=True))

    op.add_column('ams_orientation_candidates', sa.Column('avfu_email', sa.String(length=255), nullable=True))
    op.create_unique_constraint('uq_orientation_candidate_avfu_email', 'ams_orientation_candidates', ['avfu_email'])

    op.add_column('ams_orientation_candidates', sa.Column('college_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_orientation_candidates_college_id', 'ams_orientation_candidates',
        'ams_colleges', ['college_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_orientation_candidates_college_id', 'ams_orientation_candidates', type_='foreignkey')
    op.drop_column('ams_orientation_candidates', 'college_id')

    op.drop_constraint('uq_orientation_candidate_avfu_email', 'ams_orientation_candidates', type_='unique')
    op.drop_column('ams_orientation_candidates', 'avfu_email')

    op.drop_column('ams_orientation_candidates', 'last_name')
    op.drop_column('ams_orientation_candidates', 'middle_name')
    op.drop_column('ams_orientation_candidates', 'first_name')

    # `name` deliberately left nullable — see module docstring.
