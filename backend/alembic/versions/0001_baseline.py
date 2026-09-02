"""baseline: establish alembic history for existing live schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-31

This revision intentionally performs ZERO DDL. Its sole purpose is to give
Alembic a starting point (an `alembic_version` row) that represents the
database's actual pre-existing state as of this migration's creation —
20 application tables, already containing real seeded/application data,
created historically via `seed.py`'s `Base.metadata.create_all()` and never
previously tracked by Alembic.

Do not add operations here. Schema changes belong in the next revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
