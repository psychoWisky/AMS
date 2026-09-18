"""create ams_email_outbox table (Bulk Upload SMTP timeout fix)

Revision ID: 0018_email_outbox
Revises: 0017_seed_role_master_data
Create Date: 2026-09-18

Adds the durable, PostgreSQL-backed email outbox that replaces the previous
synchronous-SMTP-in-request-path pattern used by the Super Admin/HOD bulk
user-upload endpoints and `create_faculty`. Callers now insert a PENDING row
here in the SAME transaction as the user/role-assignment rows they create;
the separate `app.core.email_worker` process (a standalone OS process, never
imported by the FastAPI app) polls this table and performs the actual SMTP
send outside the request path, so SMTP latency/hangs can never again block
an API response.

Purely additive: a brand-new table plus a brand-new Postgres enum type, no
existing table/column touched. `downgrade()` drops both cleanly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0018_email_outbox'
down_revision: Union[str, None] = '0017_seed_role_master_data'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches this repo's existing enum-storage convention (see
# `ams_user_role`'s own labels 'SUPER_ADMIN'/'HOD'/etc.): SQLAlchemy's
# `Enum(PyEnumClass, name=...)` stores the Python Enum member's NAME, not
# its `.value` — so these Postgres labels must be the uppercase member
# names of `app.models.email_outbox.EmailOutboxStatus`, not its lowercase
# string values.
_STATUS_ENUM_NAME = "ams_email_outbox_status"
_STATUSES = ("PENDING", "PROCESSING", "SENT", "FAILED")


def upgrade() -> None:
    status_enum = postgresql.ENUM(*_STATUSES, name=_STATUS_ENUM_NAME)
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ams_email_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", postgresql.ENUM(*_STATUSES, name=_STATUS_ENUM_NAME, create_type=False), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # Serves both the worker's normal claim query (status='PENDING' AND
    # next_attempt_at<=now()) and its stale-PROCESSING recovery query
    # (status='PROCESSING' AND next_attempt_at<=now()) without a full scan.
    op.create_index(
        "ix_ams_email_outbox_claim", "ams_email_outbox", ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ams_email_outbox_claim", table_name="ams_email_outbox")
    op.drop_table("ams_email_outbox")
    postgresql.ENUM(*_STATUSES, name=_STATUS_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
