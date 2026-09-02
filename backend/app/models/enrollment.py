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
        teacher_pending -> major_advisor_pending -> hod_pending -> hod_approved
        reverted (terminal-until-corrected; see revert_remark)

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
    enrolled_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    processed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    processed_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    remarks: Mapped[str | None]    = mapped_column(Text)

    __table_args__ = (UniqueConstraint("student_id", "offering_id", name="uq_enrollment"),)

    student: Mapped["User"]        = relationship("User", foreign_keys=[student_id])
    processor: Mapped["User | None"] = relationship("User", foreign_keys=[processed_by])
    offering: Mapped["CourseOffering"] = relationship("CourseOffering", back_populates="enrollments")
    registration: Mapped["CourseRegistration | None"] = relationship("CourseRegistration", back_populates="items")


from app.models.user import User  # noqa: E402
from app.models.course import CourseOffering  # noqa: E402
