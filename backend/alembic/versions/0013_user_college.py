"""user.college_id (bulk user/faculty upload task)

Revision ID: 0013_user_college
Revises: 0012_enrollment_classification
Create Date: 2026-09-17

Bulk Faculty/User Excel Upload task. One additive, nullable column only:

`ams_users.college_id` (nullable UUID, FK -> `ams_colleges.id`) — College is
part of a user's own profile (confirmed requirement), deliberately
INDEPENDENT of `ams_users.department_id`: no College<->Department
relationship exists or is introduced by this migration (none was confirmed
by AVFU — see `ams_colleges`' own pre-existing docstring). Both are resolved
separately from their own master-data tables at bulk-upload time.

Nullable and never backfilled: every existing user predates this column and
has no College recorded. Does NOT touch `ams_departments`, `ams_colleges`,
or any other column/table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0013_user_college'
down_revision: Union[str, None] = '0012_enrollment_classification'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ams_users',
        sa.Column('college_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_users_college_id', 'ams_users', 'ams_colleges', ['college_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_users_college_id', 'ams_users', type_='foreignkey')
    op.drop_column('ams_users', 'college_id')
