import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Integer, Float, Numeric, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# 10-point grading scale. Bands are defined by their LOWER bound (`low`); the
# `high` column is retained for reference/seed data only. A mark belongs to the
# first band (highest first) whose `low` it meets — this is what makes
# fractional marks such as 89.5 or 44.5 resolve correctly (the previous
# `low <= marks <= high` test on integer bounds left gaps such as (89, 90)
# that silently fell through to "F"). The grade bands themselves are unchanged;
# whether AVFU's authoritative bands/grade-points differ is an OPEN question
# (see BUSINESS_LOGIC.md's Gradesheet section).
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

def compute_grade(marks: float | Decimal) -> tuple[str, float]:
    for letter, points, low, _high in GRADE_SCALE:
        if marks >= low:
            return letter, float(points)
    return "F", 0.0


# Gradesheet types (explicit, never collapsed). The academic semantics of
# repeat/revised/make-up beyond "a distinct, related sheet" are NOT confirmed
# by AVFU — only the type and the optional link to the related earlier sheet
# are recorded.
GRADESHEET_TYPES = ("new", "repeat", "revised", "make_up")

# Assessment components a Gradesheet may configure (code -> (label, type)).
COMPONENT_CATALOG = {
    "first_test": ("First Test", "theory"),
    "mid_term": ("Mid Term", "theory"),
    "end_term": ("End Term", "theory"),
    "practical": ("Practical", "practical"),
}

# Gradesheet workflow statuses.
#   draft -> instructor_pending -> hod_pending -> incharge_pending -> dpgs_pending
#         -> coe_pending -> approved (locked); reverted returns to the creating instructor.
GRADESHEET_EDITABLE_STATUSES = ("draft", "reverted")
GRADESHEET_PENDING_STATUSES = ("instructor_pending", "hod_pending", "incharge_pending", "dpgs_pending", "coe_pending")


class GradeSheet(Base):
    __tablename__ = "ams_grade_sheets"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offering_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_course_offerings.id"))
    # LEGACY (pre-Gradesheet/Result task): free string, never validated. Superseded by
    # `gradesheet_type`; retained only so old rows/queries keep working.
    sheet_type: Mapped[str]        = mapped_column(String(30), default="final")
    status: Mapped[str]            = mapped_column(String(20), default="draft")  # draft / instructor_pending / hod_pending / incharge_pending / dpgs_pending / coe_pending / approved / reverted
    is_locked: Mapped[bool]        = mapped_column(Boolean, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # LEGACY (old Super Admin publish)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # ── Gradesheet/Result task ────────────────────────────────────────────────
    gradesheet_type: Mapped[str]   = mapped_column(String(20), default="new", server_default="new", nullable=False)  # new / repeat / revised / make_up
    related_sheet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="SET NULL"))
    total_theory_marks: Mapped[Decimal]     = mapped_column(Numeric(6, 2), default=Decimal("0"), server_default="0", nullable=False)
    theory_pass_marks: Mapped[Decimal]      = mapped_column(Numeric(6, 2), default=Decimal("0"), server_default="0", nullable=False)
    total_practical_marks: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"), server_default="0", nullable=False)
    practical_pass_marks: Mapped[Decimal]  = mapped_column(Numeric(6, 2), default=Decimal("0"), server_default="0", nullable=False)
    finalized_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))  # CoE approval time

    # Exactly one NEW gradesheet per offering (DB-enforced, race-safe); the
    # other three types may legitimately repeat and are related back via
    # `related_sheet_id`.
    __table_args__ = (
        Index("uq_gradesheet_new_per_offering", "offering_id", unique=True, postgresql_where=text("gradesheet_type = 'new'")),
    )

    offering: Mapped["CourseOffering"]    = relationship("CourseOffering", back_populates="grade_sheets")
    creator: Mapped["User | None"]        = relationship("User", foreign_keys=[created_by])
    entries: Mapped[list["GradeEntry"]]   = relationship("GradeEntry", back_populates="sheet", cascade="all, delete-orphan")
    # LEGACY approval tables (pre-Gradesheet/Result task) — retained for old rows, no longer written.
    approvals: Mapped[list["ApprovalStage"]] = relationship("ApprovalStage", back_populates="sheet", cascade="all, delete-orphan", order_by="ApprovalStage.stage")
    components: Mapped[list["GradesheetComponent"]] = relationship("GradesheetComponent", back_populates="sheet", cascade="all, delete-orphan", order_by="GradesheetComponent.sort_order")
    cycles: Mapped[list["GradesheetCycle"]] = relationship("GradesheetCycle", back_populates="sheet", cascade="all, delete-orphan", order_by="GradesheetCycle.cycle_number")


class GradesheetComponent(Base):
    """One configured assessment component of a Gradesheet (First Test / Mid
    Term / End Term / Practical). Components belong to the gradesheet — never
    hard-coded columns — so each sheet can configure its own subset and maxima.
    Only the components an instructor actually selected exist as rows."""
    __tablename__ = "ams_gradesheet_components"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sheet_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"), index=True)
    code: Mapped[str]              = mapped_column(String(20), nullable=False)
    name: Mapped[str]              = mapped_column(String(60), nullable=False)
    component_type: Mapped[str]    = mapped_column(String(12), nullable=False)  # theory / practical
    max_marks: Mapped[Decimal]     = mapped_column(Numeric(6, 2), nullable=False)
    sort_order: Mapped[int]        = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("sheet_id", "code", name="uq_gradesheet_component"),)

    sheet: Mapped["GradeSheet"] = relationship("GradeSheet", back_populates="components")


class GradeEntry(Base):
    __tablename__ = "ams_grade_entries"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sheet_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"))
    student_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_student_enrollments.id"))
    # LEGACY internal/external columns (pre-Gradesheet/Result task) — superseded by
    # per-component marks (`GradeEntryMark`); never written by the new workflow.
    internal_marks: Mapped[float | None] = mapped_column(Float)
    external_marks: Mapped[float | None] = mapped_column(Float)
    total_marks: Mapped[float | None]    = mapped_column(Float)   # Grand Total (theory + practical)
    grade_letter: Mapped[str | None]     = mapped_column(String(5))
    grade_points: Mapped[float | None]   = mapped_column(Float)
    is_absent: Mapped[bool]              = mapped_column(Boolean, default=False)
    remarks: Mapped[str | None]          = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    updated_at: Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # ── Gradesheet/Result task ────────────────────────────────────────────────
    attendance_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    theory_total: Mapped[Decimal | None]       = mapped_column(Numeric(7, 2))
    practical_total: Mapped[Decimal | None]    = mapped_column(Numeric(7, 2))
    marks_percent: Mapped[Decimal | None]      = mapped_column(Numeric(5, 2))

    __table_args__ = (UniqueConstraint("sheet_id", "student_id", name="uq_grade_entry"),)

    sheet: Mapped["GradeSheet"]     = relationship("GradeSheet", back_populates="entries")
    student: Mapped["User"]         = relationship("User", foreign_keys=[student_id])
    marks: Mapped[list["GradeEntryMark"]] = relationship("GradeEntryMark", back_populates="entry", cascade="all, delete-orphan")


class GradeEntryMark(Base):
    """Marks one student obtained in one configured component."""
    __tablename__ = "ams_grade_entry_marks"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_entries.id", ondelete="CASCADE"), index=True)
    component_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_gradesheet_components.id", ondelete="CASCADE"))
    marks: Mapped[Decimal | None]  = mapped_column(Numeric(6, 2))

    __table_args__ = (UniqueConstraint("entry_id", "component_id", name="uq_grade_entry_mark"),)

    entry: Mapped["GradeEntry"] = relationship("GradeEntry", back_populates="marks")
    component: Mapped["GradesheetComponent"] = relationship("GradesheetComponent")


class GradesheetCycle(Base):
    """One approval round of a Gradesheet. A revert closes the cycle (status
    "reverted"); the creator's resubmission opens a NEW cycle with fresh
    stages, so old signatures stay attached to their own historical cycle and
    can never appear against a newly submitted round."""
    __tablename__ = "ams_gradesheet_cycles"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sheet_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"), index=True)
    cycle_number: Mapped[int]      = mapped_column(Integer, nullable=False)
    status: Mapped[str]            = mapped_column(String(20), default="open")  # open / reverted / completed
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # Server-side deadline for the other instructors' approvals (submitted_at +
    # 24h). NULL when there are no other instructors. Authoritative — never
    # supplied by, or compared against, any client value.
    instructor_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("sheet_id", "cycle_number", name="uq_gradesheet_cycle"),)

    sheet: Mapped["GradeSheet"] = relationship("GradeSheet", back_populates="cycles")
    stages: Mapped[list["GradesheetStage"]] = relationship("GradesheetStage", back_populates="cycle", cascade="all, delete-orphan", order_by="GradesheetStage.sequence, GradesheetStage.created_at")


class GradesheetStage(Base):
    """One approval/signature slot in a cycle. "Approve = Sign": acting on the
    stage records the signature (`acted_at` IS the signed-at timestamp, plus the
    signer, role, department and IP). No OTP/eSign — the same workflow-approval
    convention the newer AMS modules (Thesis/Synopsis/Comprehensive Exam) use.

    Sequence groups parallel work: 1 = creating instructor (auto-signed on
    submit), 2 = every other assigned instructor (all must act, or be
    deemed-approved after the server-side deadline), 3 = HOD, 4 = Incharge
    Academic Cell, 5 = DPGS, 6 = Controller of Examination."""
    __tablename__ = "ams_gradesheet_stages"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_gradesheet_cycles.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int]          = mapped_column(Integer, nullable=False)
    stage_type: Mapped[str]        = mapped_column(String(20), nullable=False)  # creator / instructor / hod / incharge / dpgs / coe
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    status: Mapped[str]            = mapped_column(String(20), default="pending")  # pending / approved / deemed_approved / reverted / cancelled
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    acted_role: Mapped[str | None] = mapped_column(String(50))
    acted_department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # signed-at
    remark: Mapped[str | None]     = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(50))
    is_deemed: Mapped[bool]        = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    cycle: Mapped["GradesheetCycle"] = relationship("GradesheetCycle", back_populates="stages")
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])
    assigned_user: Mapped["User | None"] = relationship("User", foreign_keys=[assigned_user_id])


class ApprovalStage(Base):
    """LEGACY (pre-Gradesheet/Result task) — no longer written. Retained so old
    rows and the table remain intact; the live workflow uses GradesheetCycle/
    GradesheetStage."""
    __tablename__ = "ams_approval_stages"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sheet_id: Mapped[uuid.UUID]    = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"))
    stage: Mapped[int]             = mapped_column(Integer, nullable=False)
    role_required: Mapped[str]     = mapped_column(String(50))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    status: Mapped[str]            = mapped_column(String(20), default="pending")
    remarks: Mapped[str | None]    = mapped_column(Text)
    pin_verified: Mapped[bool]     = mapped_column(Boolean, default=False)
    otp_verified: Mapped[bool]     = mapped_column(Boolean, default=False)
    ip_address: Mapped[str | None] = mapped_column(String(50))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    sheet: Mapped["GradeSheet"]    = relationship("GradeSheet", back_populates="approvals")
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])


class DigitalSignature(Base):
    """LEGACY (pre-Gradesheet/Result task) — email-OTP signature rows of the old
    Gradesheet workflow. No longer written."""
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


# ── Student-wise semester result (CoE compilation) ────────────────────────────

RESULT_STATUSES = ("pass", "pass_with_backlogs")


class StudentSemesterResult(Base):
    """A student's compiled result for ONE semester, produced by the Controller
    of Examination from finalized (CoE-approved) course Gradesheets.
    `result_status` is the CoE's manual decision (Pass / Pass with Backlogs) —
    never inferred from GPA, grades, attendance or credit counts. Identity
    fields are snapshotted at compile time so a published result/Grade Card is
    stable. Deliberately holds NO promotion/next-class/admission-status data:
    AVFU has not confirmed any academic-progression rule (see BUSINESS_LOGIC.md)."""
    __tablename__ = "ams_student_semester_results"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), index=True)
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_semesters.id"), index=True)
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_academic_calendars.id"))
    result_status: Mapped[str]     = mapped_column(String(30), nullable=False)  # pass / pass_with_backlogs
    status: Mapped[str]            = mapped_column(String(20), default="compiled")  # compiled / published
    gpa: Mapped[Decimal | None]    = mapped_column(Numeric(12, 6))  # full-precision (truncated to 6 dp); displayed truncated to 3 dp
    total_credits: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("0"))
    total_grade_points: Mapped[Decimal] = mapped_column(Numeric(10, 3), default=Decimal("0"))
    total_credit_points: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    student_name: Mapped[str | None]     = mapped_column(String(300))
    student_roll: Mapped[str | None]     = mapped_column(String(50))
    college_name: Mapped[str | None]     = mapped_column(String(200))
    department_name: Mapped[str | None]  = mapped_column(String(200))
    degree_name: Mapped[str | None]      = mapped_column(String(200))
    semester_name: Mapped[str | None]    = mapped_column(String(100))
    academic_year: Mapped[str | None]    = mapped_column(String(20))
    exam_label: Mapped[str | None]       = mapped_column(String(100))  # e.g. "February 2025 Examination" — only when the semester carries exam dates
    compiled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    compiled_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    published_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    version: Mapped[int]           = mapped_column(Integer, default=1)  # bumped on every recompilation
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("student_id", "semester_id", name="uq_student_semester_result"),)

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    courses: Mapped[list["StudentSemesterResultCourse"]] = relationship(
        "StudentSemesterResultCourse", back_populates="result", cascade="all, delete-orphan", order_by="StudentSemesterResultCourse.sort_order",
    )


class StudentSemesterResultCourse(Base):
    """One course line of a compiled semester result — a snapshot of the source
    Gradesheet entry's outcome (grade, grade points, credit points)."""
    __tablename__ = "ams_student_semester_result_courses"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("ams_student_semester_results.id", ondelete="CASCADE"), index=True)
    offering_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_course_offerings.id"))
    course_id: Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("ams_courses.id"))
    gradesheet_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_sheets.id", ondelete="SET NULL"))
    entry_id: Mapped[uuid.UUID | None]      = mapped_column(UUID(as_uuid=True), ForeignKey("ams_grade_entries.id", ondelete="SET NULL"))
    course_number: Mapped[str]     = mapped_column(String(50))
    course_title: Mapped[str]      = mapped_column(String(300))
    department_name: Mapped[str | None] = mapped_column(String(200))
    credit_structure: Mapped[str | None] = mapped_column(String(20))  # e.g. "3(2+1)"
    credits: Mapped[Decimal]       = mapped_column(Numeric(6, 2), default=Decimal("0"))
    grade_letter: Mapped[str]      = mapped_column(String(5))
    grade_points: Mapped[Decimal]  = mapped_column(Numeric(8, 3))
    credit_points: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    marks_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    sort_order: Mapped[int]        = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("result_id", "offering_id", name="uq_result_course_offering"),)

    result: Mapped["StudentSemesterResult"] = relationship("StudentSemesterResult", back_populates="courses")


from app.models.user import User  # noqa: E402
from app.models.course import CourseOffering  # noqa: E402
from app.models.enrollment import StudentEnrollment  # noqa: E402
