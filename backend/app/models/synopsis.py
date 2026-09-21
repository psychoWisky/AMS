"""Synopsis of Thesis/Dissertation Problem (BUSINESS_LOGIC.md section AB).

Modelled on PPW's approval-cycle design (`app/models/ppw.py`) — one cycle per
submission, one stage per person, OTP signature rows — with three differences
required by the Synopsis business rules:

* **First vs Revise.** `Synopsis.synopsis_type` is "first" today; "revise" is
  reserved for a future Revise Synopsis (which will point at the approved First
  Synopsis through `parent_synopsis_id`). Exactly one "first" Synopsis per
  student is enforced by a PARTIAL UNIQUE INDEX in the database, so it holds
  regardless of what the API does; "revise" rows are outside that constraint.
* **Uploaded PDF versions.** Every upload is an immutable `SynopsisFile` row; an
  approval cycle points at the exact file it was submitted with, so an approval
  always refers to one specific document.
* **Frozen final document.** When the DPGS approves, the approved cycle stores a
  JSON snapshot of everything the document shows (student data, committee,
  approvers) and the path/hash of the rendered final PDF. The approved document
  is served from that frozen copy, never regenerated from mutable live data.

Nothing here duplicates student or committee master data: the student's name,
roll number, programme, disciplines and college are read live while a Synopsis
is in progress (and captured once, in the snapshot, at final approval).
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, CheckConstraint, JSON, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

SYNOPSIS_TYPES = ("first", "revise")

# draft -> major_advisor_pending -> committee_pending -> hod_pending ->
# incharge_pending -> dpgs_pending -> approved ; "reverted" is reachable from any
# pending state and returns the Synopsis to the student (BUSINESS_LOGIC.md Rule 37).
SYNOPSIS_STATUSES = (
    "draft", "major_advisor_pending", "committee_pending", "hod_pending",
    "incharge_pending", "dpgs_pending", "approved", "reverted",
)
SYNOPSIS_CYCLE_STATUSES = ("active", "approved", "reverted")
SYNOPSIS_STAGE_TYPES = ("major_advisor", "committee_member", "hod", "incharge_academic_cell", "dpgs")
# "cancelled" = a stage that was still pending when its cycle was reverted by someone else.
SYNOPSIS_STAGE_STATUSES = ("pending", "approved", "reverted", "cancelled")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Synopsis(Base):
    __tablename__ = "ams_synopses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), index=True)
    synopsis_type: Mapped[str] = mapped_column(String(20), nullable=False, default="first")
    # Reserved for the future Revise Synopsis (points at the approved First Synopsis). Unused now.
    parent_synopsis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_synopses.id"))
    # "Title of the Research Problem" (item 1 of the AAU reference form). Nullable while a
    # draft is incomplete; required at submit.
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("synopsis_type IN ('first', 'revise')", name="ck_synopsis_type"),
        # One FIRST Synopsis per student, enforced by the database. Revise rows are exempt.
        Index("uq_synopsis_first_per_student", "student_id", unique=True, postgresql_where=text("synopsis_type = 'first'")),
    )

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    files: Mapped[list["SynopsisFile"]] = relationship(
        "SynopsisFile", back_populates="synopsis", cascade="all, delete-orphan", order_by="SynopsisFile.version_number",
    )
    cycles: Mapped[list["SynopsisApprovalCycle"]] = relationship(
        "SynopsisApprovalCycle", back_populates="synopsis", cascade="all, delete-orphan", order_by="SynopsisApprovalCycle.cycle_number",
    )


class SynopsisFile(Base):
    """One uploaded PDF version. The stored file lives under
    `UPLOAD_DIR/synopsis/<synopsis_id>/<stored_filename>` (a server-generated
    name); the client never sees a filesystem path, only the authorized
    download endpoint. Rows referenced by an approval cycle are never deleted."""
    __tablename__ = "ams_synopsis_files"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    synopsis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_synopses.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(100), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("synopsis_id", "version_number", name="uq_synopsis_file_version"),)

    synopsis: Mapped["Synopsis"] = relationship("Synopsis", back_populates="files")


class SynopsisApprovalCycle(Base):
    """One submission/approval attempt. A new cycle is created on every
    (re)submission; a reverted cycle is kept forever (history) and never counts
    toward a later one. `file_id`/`title_snapshot` pin what was submitted."""
    __tablename__ = "ams_synopsis_approval_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    synopsis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_synopses.id", ondelete="CASCADE"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_synopsis_files.id"), nullable=False)
    title_snapshot: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)
    # Frozen final document — set only on the approved cycle, in the same transaction as the DPGS approval.
    snapshot: Mapped[dict | None] = mapped_column(JSON)
    frozen_pdf_filename: Mapped[str | None] = mapped_column(String(100))
    frozen_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    frozen_pdf_size: Mapped[int | None] = mapped_column(Integer)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("synopsis_id", "cycle_number", name="uq_synopsis_cycle_number"),
        # At most one in-progress cycle per Synopsis.
        Index("uq_synopsis_one_active_cycle", "synopsis_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    synopsis: Mapped["Synopsis"] = relationship("Synopsis", back_populates="cycles")
    file: Mapped["SynopsisFile"] = relationship("SynopsisFile", foreign_keys=[file_id])
    stages: Mapped[list["SynopsisApprovalStage"]] = relationship(
        "SynopsisApprovalStage", back_populates="cycle", cascade="all, delete-orphan", order_by="SynopsisApprovalStage.sequence",
    )


class SynopsisApprovalStage(Base):
    """One person's stage within a cycle. Major Advisor / committee stages are bound
    to the exact `CommitteeMember` row (`committee_member_id`, SET NULL if that
    membership is later removed, so history survives) and remember who was
    assigned (`assignee_id`) and which Advisory label they held (`role_label`).
    HOD / Incharge / DPGS stages carry no person: whoever holds the role (and, for
    HOD, the student's department) in their ACTIVE session may act, checked live.
    `approver_id`/`acted_role`/`acted_department_id`/`acted_at`/`remark` record the
    actual action for the audit trail."""
    __tablename__ = "ams_synopsis_approval_stages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_synopsis_approval_cycles.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_type: Mapped[str] = mapped_column(String(30), nullable=False)
    role_label: Mapped[str] = mapped_column(String(60), nullable=False)
    committee_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_committee_members.id", ondelete="SET NULL"))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    acted_role: Mapped[str | None] = mapped_column(String(30))
    acted_department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("cycle_id", "sequence", name="uq_synopsis_stage_sequence"),)

    cycle: Mapped["SynopsisApprovalCycle"] = relationship("SynopsisApprovalCycle", back_populates="stages")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    assignee: Mapped["User | None"] = relationship("User", foreign_keys=[assignee_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])
    signatures: Mapped[list["SynopsisSignature"]] = relationship("SynopsisSignature", back_populates="stage", cascade="all, delete-orphan")


class SynopsisSignature(Base):
    """OTP record for one signing attempt on one stage (same shape and semantics as
    `PpwSignature`: single-use, expiring, scoped to this stage and this user)."""
    __tablename__ = "ams_synopsis_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_synopsis_approval_stages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    otp_code: Mapped[str | None] = mapped_column(String(10))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_used: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    stage: Mapped["SynopsisApprovalStage"] = relationship("SynopsisApprovalStage", back_populates="signatures")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])


from app.models.user import User, Department  # noqa: E402,F401
from app.models.research import CommitteeMember  # noqa: E402,F401
