import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class AdvisoryCommittee(Base):
    """Advisory Committee formation (BUSINESS_LOGIC.md D.1/M.6, STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 32
    Phase F-1). `status` now holds the internal workflow STAGE code (not the free-form
    draft/active/locked/dissolved vocabulary used before this revision):

        major_advisor_pending -> member_selection -> members_pending -> hod_pending
        -> hod_approved (P0 terminal — see plan doc for the deferred Incharge/DPGS stages)
        reverted (terminal-until-corrected; see revert_remark)

    The confirmed final stages (I/C Academic Cell, DPGS) are NOT implemented — no
    documented demo-role mitigation exists for either (unlike Orientation's Incharge
    Academic Cell substitution), so no endpoint transitions a committee past
    `hod_approved` in this revision. This is a deliberate, documented limitation,
    not an oversight — see the plan doc's Phase F-5.
    """
    __tablename__ = "ams_advisory_committees"
    id: Mapped[uuid.UUID]           = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), unique=True)
    research_title: Mapped[str | None] = mapped_column(String(500))
    research_area: Mapped[str | None]  = mapped_column(String(200))
    status: Mapped[str]             = mapped_column(String(30), default="major_advisor_pending")
    is_locked: Mapped[bool]         = mapped_column(Boolean, default=False)
    formed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mandatory-remark revert tracking (BUSINESS_LOGIC.md C.8.4/Rule 1) — preserved,
    # not overwritten to null, so the recipient can always see why a stage reverted.
    revert_remark: Mapped[str | None] = mapped_column(Text)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    student: Mapped["User"]         = relationship("User", foreign_keys=[student_id])
    members: Mapped[list["CommitteeMember"]] = relationship("CommitteeMember", back_populates="committee", cascade="all, delete-orphan")


class CommitteeMember(Base):
    """`role` values (BUSINESS_LOGIC.md M.5, Rule 29 — 5 confirmed PG/PhD Research
    Committee member types): major_advisor, member_major, member_minor, supporting,
    member_of_others. Legacy rows created before this revision may still hold the
    earlier ad hoc values (`co_major_advisor`, `member`) — never written by new code,
    but not migrated/deleted either, per instruction not to destroy existing data."""
    __tablename__ = "ams_committee_members"
    id: Mapped[uuid.UUID]             = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    committee_id: Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("ams_advisory_committees.id", ondelete="CASCADE"))
    faculty_id: Mapped[uuid.UUID]     = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    role: Mapped[str]                 = mapped_column(String(30), default="member_major")
    accepted: Mapped[bool | None]     = mapped_column(Boolean)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mandatory remark on decline (BUSINESS_LOGIC.md Rule 1/C.8.4) — required by the
    # endpoint layer whenever accepted=False, not enforced at the column level so a
    # legacy/never-responded row (accepted is NULL) is not forced to carry one.
    remark: Mapped[str | None]        = mapped_column(Text)
    invited_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("committee_id", "faculty_id", name="uq_committee_member"),)

    committee: Mapped["AdvisoryCommittee"] = relationship("AdvisoryCommittee", back_populates="members")
    faculty: Mapped["User"]                = relationship("User", foreign_keys=[faculty_id])


from app.models.user import User  # noqa: E402
