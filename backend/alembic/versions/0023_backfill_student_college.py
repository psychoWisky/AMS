"""backfill User.college_id for legacy students from their Orientation candidate

Revision ID: 0023_backfill_student_college
Revises: 0022_college_program
Create Date: 2026-09-20

`ams_users.college_id` is the canonical student college (section T.5). Orientation
account issuance now copies `ams_orientation_candidates.college_id` onto the new
student, but accounts issued before that change carry no college of their own —
the Students API had to fall back to the Orientation candidate's college for
them. This DATA-ONLY migration populates the canonical column for those accounts
so the fallback can be removed.

What it does (one UPDATE, inside Alembic's transaction; no schema change):

  UPDATE ams_users u SET college_id = c.college_id
  FROM ams_orientation_candidates c
  WHERE c.student_user_id = u.id          -- the existing link candidate -> account
    AND u.college_id IS NULL              -- never overwrites a college already set
    AND c.college_id IS NOT NULL          -- never writes NULL, never guesses
    AND <u is a student account>          -- same definition as student_user_clause()

"Student account" = holds a STUDENT role assignment, or has no assignment rows at
all and its legacy role is STUDENT (an Orientation-created account before its
first login). Non-student users are never touched. `student_user_id` is UNIQUE on
the candidate table, so each account matches at most one candidate and the result
is deterministic. A student with no linked candidate, or whose candidate has no
college, is left as-is (NULL) — nothing is invented.

Verified read-only on the development database before this was written: exactly
5 student accounts have a NULL college, each has exactly one linked candidate, all
5 candidates have a non-NULL college, and no student's own college differs from
its candidate's.

Downgrade is intentionally a NO-OP: once the canonical value is populated, later
edits (Student edit / User edit) are legitimate data, and resetting the column to
NULL would destroy them (and would be indistinguishable from rows this migration
never touched).
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0023_backfill_student_college'
down_revision: Union[str, None] = '0022_college_program'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BACKFILL_SQL = """
UPDATE ams_users AS u
SET college_id = c.college_id
FROM ams_orientation_candidates AS c
WHERE c.student_user_id = u.id
  AND u.college_id IS NULL
  AND c.college_id IS NOT NULL
  AND (
        u.id IN (SELECT ra.user_id FROM ams_user_role_assignments ra WHERE ra.role = 'STUDENT')
        OR (
            u.id NOT IN (SELECT ra.user_id FROM ams_user_role_assignments ra)
            AND u.role = 'STUDENT'
        )
  )
"""


def upgrade() -> None:
    op.execute(_BACKFILL_SQL)


def downgrade() -> None:
    # Deliberate no-op — see the module docstring.
    pass
