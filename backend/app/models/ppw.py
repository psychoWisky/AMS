"""PPW — Post-Graduate Programme of Work (Phase 1 foundation only).

BUSINESS_LOGIC.md D.3/E.5 confirm the eventual approval chain (Student ->
Major Advisor -> Advisory Committee (all) -> HOD -> Incharge Academic Cell ->
DPGS) but NONE of it is implemented here — this file is deliberately limited
to the Phase 1 scope: a student-owned draft/submitted document with a
six-classification course plan. `status` is a plain string (not a Postgres
enum) precisely so later phases can add more values without a migration that
touches the column type, mirroring AdvisoryCommittee.status's own convention.

Advisory Committee membership, HOD, and DPGS are NOT modeled here at all —
Phase 1 reads them live from the existing `AdvisoryCommittee`/`CommitteeMember`
tables at request time (see ppw.py endpoint's `_committee_rows` helper) rather
than duplicating committee data, per instruction not to create duplicate
student/programme/committee master data merely for PPW.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# Controlled classification values (BUSINESS_LOGIC.md-adjacent PPW spec, this
# task) — a Python-side constant tuple, not a Postgres enum, consistent with
# this project's existing convention for Course.category/credit_type (plain
# validated strings, API-level Literal/tuple — see courses.py's
# CATEGORY_VALUES/CREDIT_TYPE_VALUES). Confirmed NOT reusable from
# Course.category, whose values (optional/core/compulsory/research/seminar/
# deficiency/bridge/prerequisite/mandatory_mba) do not include major/minor/
# supporting at all — see this task's investigation report for the full
# reasoning; PPW's classification is therefore its own controlled vocabulary,
# stored on the link table below, independent of Course.category.
PPW_CLASSIFICATIONS = ("major", "minor", "supporting", "research", "seminar", "compulsory")

PPW_CLASSIFICATION_LABELS = {
    "major": "Major Courses",
    "minor": "Minor Courses",
    "supporting": "Supporting Courses",
    "research": "Research",
    "seminar": "Seminars",
    "compulsory": "Compulsory Credit Courses",
}

# Target credits per classification (this task's confirmed PPW spec) — used
# for informational display only in Phase 1; see ppw.py endpoint docstring
# for why this is NOT a hard submission gate.
PPW_REQUIRED_CREDITS = {
    "major": 21,
    "minor": 8,
    "supporting": 6,
    "research": 29,
    "seminar": 2,
    "compulsory": 5,
}


class Ppw(Base):
    __tablename__ = "ams_ppw"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # One PPW per student for Phase 1 — no revision/versioning model exists yet
    # (explicitly deferred, Section 15 of this task). Mirrors
    # AdvisoryCommittee.student_id's own unique=True pattern.
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), unique=True)
    # The 4 confirmed required fields (this task, Section 1). Text, not a
    # bounded String, specifically so no input is ever silently truncated.
    # Nullable at the DB level — a draft may be saved incrementally with these
    # still empty; non-emptiness is enforced only at submit time (see
    # ppw.py's submit_ppw), consistent with "draft" meaning genuinely
    # incomplete-allowed until the student is ready to lock it.
    field_of_investigation: Mapped[str | None] = mapped_column(Text)
    minor_field: Mapped[str | None] = mapped_column(Text)
    supporting_field: Mapped[str | None] = mapped_column(Text)
    research_title: Mapped[str | None] = mapped_column(Text)
    # Phase 2 (this task) extends the Phase 1 draft -> submitted lifecycle into:
    # draft -> major_advisor_pending -> committee_pending -> hod_pending ->
    # hod_approved, with a "reverted" state reachable from any of the three
    # pending states (see ppw.py's PPW_STATUSES / submit_ppw / approval
    # endpoints). "submitted" (the old Phase 1 terminal value) is superseded by
    # major_advisor_pending — normalized rather than kept as a redundant extra
    # state, per this task's explicit instruction. Still a plain string column,
    # not a Postgres enum, consistent with the Phase 1 convention (so later
    # phases — Incharge Academic Cell/DPGS — can add values without a migration
    # touching the column type).
    status: Mapped[str] = mapped_column(String(30), default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    courses: Mapped[list["PpwCourse"]] = relationship("PpwCourse", back_populates="ppw", cascade="all, delete-orphan")
    approval_cycles: Mapped[list["PpwApprovalCycle"]] = relationship(
        "PpwApprovalCycle", back_populates="ppw", cascade="all, delete-orphan", order_by="PpwApprovalCycle.cycle_number",
    )


class PpwCourse(Base):
    """PPW course-plan line item. Deliberately stores ONLY the PPW reference,
    course reference, classification, and ordering — course_number/title/
    department are always read live via the `course` relationship, never
    duplicated here, per instruction not to store them redundantly."""
    __tablename__ = "ams_ppw_courses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ppw_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_ppw.id", ondelete="CASCADE"))
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_courses.id"))
    classification: Mapped[str] = mapped_column(String(20), nullable=False)
    sl_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("ppw_id", "course_id", name="uq_ppw_course"),)

    ppw: Mapped["Ppw"] = relationship("Ppw", back_populates="courses")
    course: Mapped["Course"] = relationship("Course")


# ── Phase 2: Advisory Committee approval workflow ───────────────────────────
# PPW-specific approval tables (this task's confirmed instruction not to reuse
# the Gradesheet ApprovalStage/DigitalSignature tables, which are hard-FK'd to
# ams_grade_sheets). Committee membership itself is never duplicated here —
# PpwApprovalStage.committee_member_id points at the existing
# ams_committee_members row, which remains the sole source of truth for who a
# member/major-advisor actually is; only the approval-history-specific fields
# (status/signed_at/remark) live here.

# Cycle-level lifecycle. "active" is the single in-progress cycle for a PPW
# (enforced by a partial unique index on ppw_id WHERE status='active' — see
# migration); "approved"/"reverted" are terminal, kept permanently for audit
# (never deleted, per explicit instruction).
PPW_CYCLE_STATUSES = ("active", "approved", "reverted")

# Stage-level types, in the confirmed fixed order: Major Advisor first, then
# all applicable committee members (unordered among themselves), then HOD.
# Co-Major Advisor and Incharge Academic Cell/DPGS are deliberately absent —
# not implemented in this phase.
PPW_STAGE_TYPES = ("major_advisor", "committee_member", "hod")
PPW_STAGE_STATUSES = ("pending", "approved", "reverted")

# CommitteeMember.role values eligible to become a PPW committee-member stage
# (this task's confirmed list) — explicitly excludes "major_advisor" (its own,
# earlier stage_type) and "co_major_advisor" (not implemented this phase).
PPW_COMMITTEE_STAGE_ROLES = ("member_major", "member_minor", "supporting", "member_of_others")


class PpwApprovalCycle(Base):
    """One submission/approval attempt for a PPW. A new cycle is created on
    every (re)submission; prior cycles are never deleted on revert, only
    superseded, so the full approval history remains queryable (explicit
    auditability instruction)."""
    __tablename__ = "ams_ppw_approval_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ppw_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_ppw.id", ondelete="CASCADE"))
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / approved / reverted
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("ppw_id", "cycle_number", name="uq_ppw_cycle_number"),)

    ppw: Mapped["Ppw"] = relationship("Ppw", back_populates="approval_cycles")
    stages: Mapped[list["PpwApprovalStage"]] = relationship(
        "PpwApprovalStage", back_populates="cycle", cascade="all, delete-orphan", order_by="PpwApprovalStage.sequence",
    )


class PpwApprovalStage(Base):
    """One specific person's stage within a cycle. Major Advisor/committee-member
    stages are identified by the EXACT `committee_member_id` row (never a bare
    role match — this task's central authorization requirement); the HOD stage
    has no CommitteeMember (HODs are not committee members) and instead stores
    `approver_id` as the department-resolved-at-seed-time HOD, which is always
    RE-verified live against the student's current department at act-time
    (approver_id alone is never trusted as authorization)."""
    __tablename__ = "ams_ppw_approval_stages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_ppw_approval_cycles.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_type: Mapped[str] = mapped_column(String(30), nullable=False)  # major_advisor / committee_member / hod
    committee_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_committee_members.id"))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / approved / reverted
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("cycle_id", "committee_member_id", name="uq_ppw_stage_committee_member"),)

    cycle: Mapped["PpwApprovalCycle"] = relationship("PpwApprovalCycle", back_populates="stages")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])
    signatures: Mapped[list["PpwSignature"]] = relationship("PpwSignature", back_populates="stage", cascade="all, delete-orphan")


class PpwSignature(Base):
    """PPW-specific OTP/signature record — deliberately SEPARATE from
    `ams_digital_signatures` (Gradesheet-specific via its FK to
    `ams_approval_stages`), per explicit instruction. One row per OTP
    generation attempt; `otp_used`/`otp_expires_at` enforce single-use and
    expiry exactly like the Gradesheet convention, scoped to this stage and
    this user only."""
    __tablename__ = "ams_ppw_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_ppw_approval_stages.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    otp_code: Mapped[str | None] = mapped_column(String(10))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_used: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    stage: Mapped["PpwApprovalStage"] = relationship("PpwApprovalStage", back_populates="signatures")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])


from app.models.user import User  # noqa: E402
from app.models.course import Course  # noqa: E402
from app.models.research import CommitteeMember  # noqa: E402
