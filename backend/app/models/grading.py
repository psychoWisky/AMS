import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Integer, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# 10-point grading scale
GRADE_SCALE = [
    ("O",  10, 90, 100),
    ("A+",  9, 80,  89),
    ("A",   8, 70,  79),
    ("B+",  7, 60,  69),
    ("B",   6, 55,  59),
    ("C",   5, 50,  54),
    ("P",   4, 45,  49),
    ("F",   0,  0,  44),
]

def compute_grade(marks: float) -> tuple[str, float]:
    for letter, points, low, high in GRADE_SCALE:
        if low <= marks <= high:
            return letter, float(points)
    return "F", 0.0


class GradeSheet(Base):
    __tablename__ = "ams_grade_sheets"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offering_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_course_offerings.id"))
    sheet_type: Mapped[str]        = mapped_column(String(30), default="final")  # internal / external / final
    status: Mapped[str]            = mapped_column(String(20), default="draft")  # draft / submitted / under_review / approved / published
    is_locked: Mapped[bool]        = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    offering: Mapped["CourseOffering"]    = relationship("CourseOffering", back_populates="grade_sheets")
    creator: Mapped["User | None"]        = relationship("User", foreign_keys=[created_by])
    entries: Mapped[list["GradeEntry"]]   = relationship("GradeEntry", back_populates="sheet", cascade="all, delete-orphan")
    approvals: Mapped[list["ApprovalStage"]] = relationship("ApprovalStage", back_populates="sheet", cascade="all, delete-orphan", order_by="ApprovalStage.stage")


class GradeEntry(Base):
    __tablename__ = "ams_grade_entries"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sheet_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"))
    student_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_student_enrollments.id"))
    internal_marks: Mapped[float | None] = mapped_column(Float)
    external_marks: Mapped[float | None] = mapped_column(Float)
    total_marks: Mapped[float | None]    = mapped_column(Float)
    grade_letter: Mapped[str | None]     = mapped_column(String(5))
    grade_points: Mapped[float | None]   = mapped_column(Float)
    is_absent: Mapped[bool]              = mapped_column(Boolean, default=False)
    remarks: Mapped[str | None]          = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    updated_at: Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("sheet_id", "student_id", name="uq_grade_entry"),)

    sheet: Mapped["GradeSheet"]     = relationship("GradeSheet", back_populates="entries")
    student: Mapped["User"]         = relationship("User", foreign_keys=[student_id])


class ApprovalStage(Base):
    __tablename__ = "ams_approval_stages"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sheet_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"))
    stage: Mapped[int]             = mapped_column(Integer, nullable=False)  # 1–2 (see grading.py's APPROVAL_PIPELINE)
    role_required: Mapped[str]     = mapped_column(String(50))  # faculty / hod — free-text, not a FK to UserRole
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    status: Mapped[str]            = mapped_column(String(20), default="pending")  # pending / approved / rejected / skipped
    remarks: Mapped[str | None]    = mapped_column(Text)
    pin_verified: Mapped[bool]     = mapped_column(Boolean, default=False)
    otp_verified: Mapped[bool]     = mapped_column(Boolean, default=False)
    ip_address: Mapped[str | None] = mapped_column(String(50))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    sheet: Mapped["GradeSheet"]    = relationship("GradeSheet", back_populates="approvals")
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])


class DigitalSignature(Base):
    __tablename__ = "ams_digital_signatures"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_approval_stages.id"))
    user_id: Mapped[uuid.UUID]     = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    pin_hash: Mapped[str | None]   = mapped_column(String(64))
    otp_code: Mapped[str | None]   = mapped_column(String(10))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_used: Mapped[bool]         = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])


from app.models.user import User  # noqa: E402
from app.models.course import CourseOffering  # noqa: E402
from app.models.enrollment import StudentEnrollment  # noqa: E402
