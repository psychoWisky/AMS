"""Durable PostgreSQL-backed email outbox (Bulk Upload SMTP timeout fix).

Root cause being fixed: the bulk-upload endpoints (and `create_faculty`)
used to send credential emails synchronously, inside the request, via a
blocking `smtplib` call per user — with a single Uvicorn worker, a batch of
22 sequential SMTP sends could run past Nginx's ~60s default
`proxy_read_timeout`, producing a 504 to the browser even though the users
had already been created and committed successfully.

This table is the durable hand-off point: callers (`auth.py`'s
`create_faculty`, `_create_bulk_users`) insert one PENDING row per email
using their OWN AsyncSession, in the SAME transaction as the user/role-
assignment rows they are also writing — so a row here exists if and only if
the corresponding user was actually committed (transactional outbox
pattern; see `app.core.email.enqueue_email`). The separate, standalone
`app.core.email_worker` process polls this table and performs the actual
SMTP send entirely out-of-request, so SMTP latency/hangs can never block an
API response again.

`body` intentionally stores the exact plaintext message already used by the
pre-existing synchronous emails (which includes the user's initial
password, exactly as today's `send_email(...)` call already did in memory)
— this is not new sensitive-data exposure, it is the necessary trade-off of
making delivery durable/retryable instead of a single in-memory attempt.
No password HASH, no unrelated user PII, and no SMTP credentials are ever
stored on this table.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import String, Text, DateTime, Integer, Enum as SAEnum, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class EmailOutboxStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class EmailOutbox(Base):
    """One row per queued outbound email.

    `next_attempt_at` is deliberately overloaded for two purposes, so no
    extra "lease" column is needed:
      * while PENDING, it is the earliest time this row is eligible to be
        claimed (used for retry backoff);
      * while PROCESSING, it is the lease expiry — if a worker claims a row
        and then crashes/restarts before finishing, `email_worker.py`'s
        stale-job recovery pass reclaims it once this time has passed.
    """
    __tablename__ = "ams_email_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[EmailOutboxStatus] = mapped_column(
        SAEnum(EmailOutboxStatus, name="ams_email_outbox_status"),
        nullable=False, default=EmailOutboxStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        # Serves both the worker's normal claim query (status='pending' AND
        # next_attempt_at<=now()) and its stale-PROCESSING recovery query
        # (status='processing' AND next_attempt_at<=now()) without a scan.
        Index("ix_ams_email_outbox_claim", "status", "next_attempt_at"),
    )
