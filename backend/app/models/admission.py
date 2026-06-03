"""Admission application model."""
import uuid
from datetime import datetime, timezone, date
from sqlalchemy import (
    String, Text, Integer, Float, Date, DateTime, ForeignKey, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class AdmissionApplication(Base):
    __tablename__ = "ams_admission_applications"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)

    # Program
    program_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ams_programs.id"), nullable=True
    )
    academic_year: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)

    # Personal
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    gender: Mapped[str] = mapped_column(String(20), nullable=False)
    nationality: Mapped[str] = mapped_column(String(50), default="Indian", nullable=False)
    religion: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mother_tongue: Mapped[str | None] = mapped_column(String(50), nullable=True)
    aadhar_number: Mapped[str] = mapped_column(String(12), nullable=False)
    personal_email: Mapped[str] = mapped_column(String(255), nullable=False)
    mobile: Mapped[str] = mapped_column(String(15), nullable=False)
    alt_mobile: Mapped[str | None] = mapped_column(String(15), nullable=True)

    # Current address
    current_address: Mapped[str] = mapped_column(Text, nullable=False)
    current_city: Mapped[str] = mapped_column(String(100), nullable=False)
    current_state: Mapped[str] = mapped_column(String(100), nullable=False)
    current_pincode: Mapped[str] = mapped_column(String(10), nullable=False)

    # Permanent address
    permanent_address: Mapped[str] = mapped_column(Text, nullable=False)
    permanent_city: Mapped[str] = mapped_column(String(100), nullable=False)
    permanent_state: Mapped[str] = mapped_column(String(100), nullable=False)
    permanent_pincode: Mapped[str] = mapped_column(String(10), nullable=False)

    # Guardian
    father_name: Mapped[str] = mapped_column(String(200), nullable=False)
    father_occupation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    father_mobile: Mapped[str | None] = mapped_column(String(15), nullable=True)
    father_income: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mother_name: Mapped[str] = mapped_column(String(200), nullable=False)
    mother_occupation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mother_mobile: Mapped[str | None] = mapped_column(String(15), nullable=True)

    # Academics
    tenth_board: Mapped[str] = mapped_column(String(100), nullable=False)
    tenth_school: Mapped[str] = mapped_column(String(200), nullable=False)
    tenth_year: Mapped[int] = mapped_column(Integer, nullable=False)
    tenth_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    tenth_roll: Mapped[str | None] = mapped_column(String(50), nullable=True)
    twelfth_board: Mapped[str] = mapped_column(String(100), nullable=False)
    twelfth_school: Mapped[str] = mapped_column(String(200), nullable=False)
    twelfth_year: Mapped[int] = mapped_column(Integer, nullable=False)
    twelfth_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    twelfth_roll: Mapped[str | None] = mapped_column(String(50), nullable=True)
    twelfth_stream: Mapped[str] = mapped_column(String(50), nullable=False)
    entrance_exam: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entrance_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Documents (relative file paths under UPLOAD_DIR)
    doc_photo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_signature: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_tenth_marksheet: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_twelfth_marksheet: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_aadhar: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_category_cert: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doc_transfer_cert: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Review
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ams_users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    program: Mapped["Program | None"] = relationship(  # type: ignore[name-defined]
        "Program", foreign_keys=[program_id]
    )
    reviewer: Mapped["User | None"] = relationship(  # type: ignore[name-defined]
        "User", foreign_keys=[reviewed_by]
    )
