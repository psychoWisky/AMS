import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, CheckConstraint
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
    but not migrated/deleted either, per instruction not to destroy existing data.
    `co_major_advisor` remains architecturally valid to store (it is a plain string
    column with no DB-level enum), but no endpoint currently creates one — see
    `research.py`'s `_MEMBER_ROLES` and the open question in its module docstring.

    Advisory Committee department-eligibility task (this revision) — "Member of
    Others" is now confirmed to mean a genuine EXTERNAL person outside AVFU, who
    has no AMS account and must never be given one. `faculty_id` is therefore
    nullable, and exactly one of (internal faculty) or (external name/designation/
    institute) must be set — enforced by the `ck_committee_member_internal_xor_external`
    CHECK constraint below, not just application code, so a direct/future write path
    can never create an ambiguous row. This is a genuinely NEW concept, distinct from
    RBAC: an external member is never a `User`, never a `UserRoleAssignment`, and is
    stored here purely for reference/documentation — see `app.core.faculty_scope` for
    how INTERNAL faculty department-eligibility is resolved (never from this table)."""
    __tablename__ = "ams_committee_members"
    id: Mapped[uuid.UUID]             = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    committee_id: Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("ams_advisory_committees.id", ondelete="CASCADE"))
    # Nullable: NULL for an external ("member_of_others") member — see the class
    # docstring and the CHECK constraint below, which is the actual source of truth
    # for "exactly one of internal/external" rather than this column's nullability alone.
    faculty_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    role: Mapped[str]                 = mapped_column(String(30), default="member_major")
    accepted: Mapped[bool | None]     = mapped_column(Boolean)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mandatory remark on decline (BUSINESS_LOGIC.md Rule 1/C.8.4) — required by the
    # endpoint layer whenever accepted=False, not enforced at the column level so a
    # legacy/never-responded row (accepted is NULL) is not forced to carry one.
    remark: Mapped[str | None]        = mapped_column(Text)
    invited_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # External ("member_of_others") committee member fields — all three are set
    # together or not at all (see the CHECK constraint). An external member never
    # accepts/declines (BUSINESS_LOGIC.md's "involved offline only") — the endpoint
    # layer sets `accepted=True`/`accepted_at` immediately when the row is created
    # (there is no invitation-response flow for them), so they never block the
    # "every member has accepted" auto-advance gate the internal members go through.
    external_name: Mapped[str | None]        = mapped_column(String(200))
    external_designation: Mapped[str | None] = mapped_column(String(200))
    external_institute: Mapped[str | None]   = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("committee_id", "faculty_id", name="uq_committee_member"),
        # Exactly one of (internal faculty) or (external identity) — never both,
        # never neither. Postgres CHECK constraints treat NULL comparisons as
        # UNKNOWN (not FALSE), so this is written as two mutually-exclusive
        # "IS NULL"/"IS NOT NULL" branches rather than relying on boolean algebra
        # over nullable columns.
        CheckConstraint(
            "(faculty_id IS NOT NULL AND external_name IS NULL AND external_designation IS NULL AND external_institute IS NULL) "
            "OR "
            "(faculty_id IS NULL AND external_name IS NOT NULL AND external_designation IS NOT NULL AND external_institute IS NOT NULL)",
            name="ck_committee_member_internal_xor_external",
        ),
    )

    committee: Mapped["AdvisoryCommittee"] = relationship("AdvisoryCommittee", back_populates="members")
    faculty: Mapped["User | None"]         = relationship("User", foreign_keys=[faculty_id])


from app.models.user import User  # noqa: E402
