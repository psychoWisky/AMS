import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class CourseRegistration(Base):
    """Registration-level parent for Course Registration (BUSINESS_LOGIC.md D.5,
    STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 34.5/CR-3+CR-4 Implementation
    Record). Groups the `StudentEnrollment` rows a student submits together for
    one semester, so the confirmed "all selected courses must clear their
    Course Teacher stage before Major Advisor" rule can be evaluated across the
    group rather than per row.

    `stage` (internal code, C.8 display label computed in the endpoint layer,
    mirroring the proven `AdvisoryCommittee.status`/`status_label` pattern):
        teacher_pending -> card_pending -> major_advisor_pending -> hod_pending -> hod_approved
        reverted (terminal-until-corrected; see revert_remark)

    Registration Card task (this revision) — `card_pending` is a NEW value
    inserted between `teacher_pending` and `major_advisor_pending`: it means
    every currently-active (non-withdrawn) selected course has cleared its
    Course Teacher stage, but the STUDENT has not yet explicitly submitted the
    Registration Card. This is deliberately a new value on the existing plain
    String column (no migration needed for this part — the column already
    supports arbitrary future values, per its original design intent), not a
    new column or a parallel state machine. `teacher_pending` and
    `card_pending` are jointly the only two STUDENT-EDITABLE stages (see
    `_EDITABLE_STAGES` in the endpoint layer) — the student may add/remove
    courses in either; submitting the Registration Card
    (`POST /registrations/{id}/submit`) is the only way to move from
    `card_pending` to `major_advisor_pending`, at which point the registration
    is permanently locked from the student's side for this cycle.

    I/C Academic Cell and DPGS stages are deliberately NOT modeled here — no
    documented demo-role mitigation exists for either (same reasoning as
    Advisory Committee P0's identical omission) — `hod_approved` is the honest
    P0 terminal state for this phase.
    """
    __tablename__ = "ams_course_registrations"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_semesters.id"))
    calendar_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_academic_calendars.id"))
    stage: Mapped[str]             = mapped_column(String(30), default="teacher_pending")
    revert_remark: Mapped[str | None] = mapped_column(Text)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # Registration Card task (this revision) — distinct from `submitted_at`
    # (which marks when this batch/row was first CREATED, i.e. the student's
    # first course selection for the semester). `card_submitted_at` marks the
    # separate, later moment the student explicitly submitted the Registration
    # Card (stage teacher_pending/card_pending -> major_advisor_pending). Null
    # until that happens; never reset afterward (permanent audit marker).
    card_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("student_id", "semester_id", name="uq_course_registration"),)

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    items: Mapped[list["StudentEnrollment"]] = relationship("StudentEnrollment", back_populates="registration")


class StudentEnrollment(Base):
    __tablename__ = "ams_student_enrollments"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    offering_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_course_offerings.id"))
    # Registration-level grouping (nullable: rows created before this revision,
    # or via the legacy single-course POST /enrollment, have no parent).
    registration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_course_registrations.id"))
    # pending → approved / reverted / withdrawn. `rejected` remains a legacy
    # value (pre-existing rows only) — Course Registration's confirmed chain
    # has no reject path (BUSINESS_LOGIC.md Rule 35); new code never writes it.
    status: Mapped[str]            = mapped_column(String(20), default="pending")
    # Major/Minor/Supporting/Research/Seminar/Compulsory classification task
    # (this revision) — deliberately placed on the per-course selection row
    # (StudentEnrollment), not on the registration-level CourseRegistration
    # parent, mirroring PPW's own PpwCourse.classification exactly (a
    # per-course concept, not a per-registration one). Nullable: every
    # existing enrollment (legacy or otherwise) predates this classification
    # and simply has no value here — never backfilled. Deliberately reuses
    # the SAME controlled vocabulary as PpwCourse.classification
    # (major/minor/supporting/research/seminar/compulsory — see
    # app/core/classification.py's re-export of PPW_CLASSIFICATIONS) rather
    # than inventing a second one, and is entirely independent from
    # `Course.category` (a different, pre-existing, unrelated taxonomy that
    # this task does not touch).
    classification: Mapped[str | None] = mapped_column(String(20))
    enrolled_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    processed_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    remarks: Mapped[str | None]    = mapped_column(Text)

    __table_args__ = (UniqueConstraint("student_id", "offering_id", name="uq_enrollment"),)

    student: Mapped["User"]        = relationship("User", foreign_keys=[student_id])
    processor: Mapped["User | None"] = relationship("User", foreign_keys=[processed_by])
    offering: Mapped["CourseOffering"] = relationship("CourseOffering", back_populates="enrollments")
    registration: Mapped["CourseRegistration | None"] = relationship("CourseRegistration", back_populates="items")
    withdrawal_requests: Mapped[list["WithdrawalRequest"]] = relationship(
        "WithdrawalRequest", back_populates="enrollment", cascade="all, delete-orphan",
        order_by="WithdrawalRequest.requested_at.desc()",
    )


class WithdrawalRequest(Base):
    """Approved-course withdrawal request (Registration Card task, this
    revision) — a dedicated, small, auditable table, deliberately SEPARATE
    from `StudentEnrollment.status` itself (per explicit instruction): a
    student's click on "Request Withdrawal" must NOT immediately mark the
    enrollment withdrawn — it only creates this pending request, which the
    Course Teacher (the same authorization as any other action on this
    offering — `_authorize_offering_management`, never a second/different
    mechanism) then approves or rejects. Only on APPROVAL does
    `StudentEnrollment.status` actually become "withdrawn" (see
    `decide_withdrawal_request` in the endpoint layer). Rejecting leaves the
    enrollment's own status completely untouched — this table is the only
    place a rejection is ever recorded.

    At most one PENDING request may exist per enrollment at a time — enforced
    by a partial unique index (`uq_withdrawal_request_one_active`, migration),
    the same pattern already used for PPW's "one active approval cycle per
    PPW" constraint (`uq_ppw_cycle_one_active`) — not reinvented here.
    Historical (approved/rejected) requests are never deleted, so a full
    audit trail of every withdrawal attempt is preserved even if a later
    request for the same enrollment is made (e.g. after a rejection, the
    student may try again with a different reason)."""
    __tablename__ = "ams_withdrawal_requests"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_student_enrollments.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / approved / rejected
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_remark: Mapped[str | None] = mapped_column(Text)

    enrollment: Mapped["StudentEnrollment"] = relationship("StudentEnrollment", back_populates="withdrawal_requests")
    requester: Mapped["User"] = relationship("User", foreign_keys=[requested_by])
    decider: Mapped["User | None"] = relationship("User", foreign_keys=[decided_by])


from app.models.user import User  # noqa: E402
from app.models.course import CourseOffering  # noqa: E402
