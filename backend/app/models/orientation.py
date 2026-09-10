"""Orientation / Student Intake (STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 28).

A candidate is explicitly NOT a student yet — no login, no role — until
selected. Deliberately a separate, lightweight model from AdmissionApplication
(Section 28.2/28.4: that model is a heavier, production public-admission-form
workflow with a different state machine; forcing orientation intake through it
would mean weakening a dozen unrelated NOT NULL constraints on a model already
in live use for something else). `admission_application_id` is included now,
unused by the demo, purely so a future real entrance-registration flow can
link back to an orientation candidate without a later schema surprise.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Float, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class OrientationCandidate(Base):
    __tablename__ = "ams_orientation_candidates"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # LEGACY — kept exactly as-is for candidates created before the
    # first/middle/last split below existed. Never written by new code; never
    # backfilled for old rows. Relaxed to nullable (migration 0010) so new
    # candidates can leave it unset entirely.
    name: Mapped[str | None] = mapped_column(String(200))
    # First/Middle/Last split (this task's confirmed business requirement) —
    # First and Last are required at the API layer for new/updated
    # candidates; Middle is always optional. Nullable at the DB level only to
    # accommodate the pre-existing candidates that predate this field set
    # (see migration 0010_orientation_candidate_fields) — never guessed for
    # those rows. A row uses EITHER `name` (legacy) OR these three columns
    # (new), decided by _candidate_full_name() in the endpoint layer.
    first_name: Mapped[str | None] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    personal_email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Required at the API layer for new/updated candidates (was optional
    # before this task); nullable at the DB level for the same
    # pre-existing-row reason as above.
    mobile: Mapped[str | None] = mapped_column(String(20))
    # Distinct from personal_email — the AVFU-issued address the IT team
    # creates for each shortlisted student ahead of Orientation. Required at
    # the API layer for new/updated candidates; this is what becomes the
    # created student's ams_users.email (AMS login), NEVER personal_email.
    # Globally unique (nullable-safe — multiple NULLs are allowed) so two
    # candidates can never collide once both have one on file.
    avfu_email: Mapped[str | None] = mapped_column(String(255), unique=True)
    entrance_exam_name: Mapped[str | None] = mapped_column(String(200))
    entrance_exam_marks: Mapped[float | None] = mapped_column(Float)

    academic_year: Mapped[str] = mapped_column(String(20), nullable=False)  # plain year label, e.g. "2026" — see Section 28.4 (not AcademicCalendar-linked yet)
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_programs.id"), nullable=False)
    # Programme<->Department many-to-many redesign — nullable at the DB level
    # (existing candidates predate this column and are never guessed at), but
    # the endpoint layer requires it going forward for new/updated candidates
    # once a Programme is selected, validated against ams_program_departments
    # (see departments.py's validate_program_department_pair). Deliberately
    # NOT part of the uniqueness constraint below — multiple different
    # candidates/students legitimately share the same Programme+Department
    # (e.g. many students in B.Tech+CSE), so uniqueness stays scoped to one
    # person's own repeat application, unchanged.
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    # College (this task's confirmed business requirement) — reuses the
    # EXISTING flat ams_colleges master-data table (app/models/user.py's
    # College model) rather than inventing a new concept; that table already
    # has no confirmed relationship to Department/Program, so this FK is a
    # plain, independent reference, not part of the Programme<->Department
    # M:N redesign. Required at the API layer for new/updated candidates;
    # nullable at the DB level for the same pre-existing-row reason as above.
    college_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_colleges.id"))

    # pending / present / absent
    attendance_status: Mapped[str] = mapped_column(String(20), default="pending")
    # pending / selected / not_selected
    selection_status: Mapped[str] = mapped_column(String(20), default="pending")
    # not_generated / generated / sent / failed
    credential_status: Mapped[str] = mapped_column(String(20), default="not_generated")

    roll_no: Mapped[str | None] = mapped_column(String(50), unique=True)
    student_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), unique=True)
    admission_application_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_admission_applications.id"))

    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("personal_email", "academic_year", "program_id", name="uq_orientation_candidate_intake"),
    )

    program: Mapped["Program"] = relationship("Program", foreign_keys=[program_id])  # type: ignore[name-defined]
    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[department_id])  # type: ignore[name-defined]
    college: Mapped["College | None"] = relationship("College", foreign_keys=[college_id])  # type: ignore[name-defined]
    student: Mapped["User | None"] = relationship("User", foreign_keys=[student_user_id])  # type: ignore[name-defined]
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])  # type: ignore[name-defined]


from app.models.user import User, Program, Department, College  # noqa: E402
