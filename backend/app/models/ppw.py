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
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, UniqueConstraint
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
    # Smallest lifecycle for Phase 1, per explicit instruction: draft -> submitted.
    status: Mapped[str] = mapped_column(String(20), default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    courses: Mapped[list["PpwCourse"]] = relationship("PpwCourse", back_populates="ppw", cascade="all, delete-orphan")


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


from app.models.user import User  # noqa: E402
from app.models.course import Course  # noqa: E402
