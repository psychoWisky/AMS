"""Student Migration (BUSINESS_LOGIC.md section AE).

A single, lightweight table — modelled directly on `WithdrawalRequest`
(`app/models/enrollment.py`), NOT on the heavy multi-stage OTP-signed
cycle/stage/signature engine used by PPW/Synopsis/External Examiner
Selection/Thesis. This is deliberate: the confirmed workflow has exactly one
approval level (Student -> Registrar, approve/reject), so a generic approval
engine would be over-engineering.

`status` lifecycle: `draft -> submitted -> approved` or `submitted ->
rejected`. A REJECTED application is a permanent historical record — it is
never edited back into `draft`, and rejecting never deletes or overwrites it.
A student may create a brand-new `MigrationApplication` row after a rejection
(or after an approval); at most one row per student may be `draft`/
`submitted` at a time (`uq_migration_one_active_per_student`, mirroring
`uq_withdrawal_request_one_active`'s exact "at most one ACTIVE, unlimited
historical rows" shape).

Snapshot fields (`student_name_snapshot`, `student_roll_snapshot`,
`degree_snapshot`, `college_snapshot`) are copied from the student's `User`
record once, at creation — server-derived, never client-supplied, and never
resynced — so a later profile change never silently alters a historical
application (the same reasoning as `Thesis.title_snapshot`). `registration_no`
is NOT a snapshot of any existing `User` field — AMS has no canonical
Registration Number (confirmed by investigation); it is simply
student-entered application data, stored only here.
"""
import uuid
from datetime import date, datetime, timezone
from sqlalchemy import String, Text, Integer, Date, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

MIGRATION_STATUSES = ("draft", "submitted", "approved", "rejected")
# A student may have any number of historical (approved/rejected) applications, but at most
# one ACTIVE (draft/submitted) one at a time — enforced below by a partial unique index.
_ACTIVE_STATUSES = ("draft", "submitted")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MigrationApplication(Base):
    __tablename__ = "ams_migration_applications"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)

    # Server-derived snapshots (Section 5/14) — copied once from the student's User record at
    # creation; never accepted from the client, never resynced afterward.
    student_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    student_roll_snapshot: Mapped[str | None] = mapped_column(String(50))
    degree_snapshot: Mapped[str | None] = mapped_column(String(200))
    college_snapshot: Mapped[str | None] = mapped_column(String(200))

    # Student-entered application data (Section 3/12) — Registration No. is NOT a User field;
    # it exists only here, exactly as confirmed (no canonical Registration Number in AMS today).
    registration_no: Mapped[str | None] = mapped_column(String(100))
    last_exam_name_and_roll: Mapped[str | None] = mapped_column(Text)
    passed_from_institution: Mapped[str | None] = mapped_column(Text)
    fee_payment_date: Mapped[date | None] = mapped_column(Date)
    migration_reason: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)

    # Payment Receipt (one PDF; Section 9/10) — versioned metadata only, mirroring
    # SynopsisFile's convention; the file itself lives under UPLOAD_DIR/migration/<id>/.
    receipt_original_filename: Mapped[str | None] = mapped_column(String(255))
    receipt_stored_filename: Mapped[str | None] = mapped_column(String(100))
    receipt_content_type: Mapped[str | None] = mapped_column(String(100))
    receipt_size_bytes: Mapped[int | None] = mapped_column(Integer)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    receipt_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Decision (Option A — mirrors WithdrawalRequest exactly; Section 8/17).
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_remark: Mapped[str | None] = mapped_column(Text)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        # At most one draft/submitted application per student — NOT a one-ever constraint
        # (Section 6): once an application is approved/rejected it falls outside this
        # predicate and the student may create a new one.
        Index(
            "uq_migration_one_active_per_student", "student_id", unique=True,
            postgresql_where=text("status IN ('draft', 'submitted')"),
        ),
    )

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    decider: Mapped["User | None"] = relationship("User", foreign_keys=[decided_by])


from app.models.user import User  # noqa: E402,F401
