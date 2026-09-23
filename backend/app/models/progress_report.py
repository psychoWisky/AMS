"""Student Progress Report (BUSINESS_LOGIC.md section AF).

Modelled directly on Synopsis's approval-cycle design (`app/models/synopsis.py`)
— one cycle per submission, one stage per approver, OTP signature rows — with
one structural difference required by this module's confirmed revert rules:

* **Synopsis**: EVERY revert, from any stage, ends the cycle and returns the
  whole thing to the student; a resubmission always starts a brand-new cycle
  with a brand-new set of stages.
* **Progress Report**: only a MAJOR ADVISOR revert returns to the student and
  ends the cycle. A revert from a Committee Member, HOD, Incharge Academic
  Cell or DPGS goes back exactly ONE step in the chain (Committee -> Major
  Advisor; HOD -> Committee; Incharge -> HOD; DPGS -> Incharge) WITHOUT ending
  the cycle or creating a new one — the SAME cycle re-opens a fresh "round" of
  stage(s) for whoever must act again. Because an already-decided
  `ProgressReportApprovalStage` row is never mutated or deleted (full history
  must survive every partial revert, not just a full one), a "fresh round" is
  represented by NEW stage rows with a higher `sequence` value. At any moment,
  the one LIVE row for a given (stage_type, committee_member_id) pair is
  simply the one with the highest `sequence` — see `progress_report.py`'s
  `_live_stages` helper. This is still a single, bounded per-cycle mechanism,
  not a generic workflow engine: only 5 stage_types exist, in a fixed order,
  and only the module's own endpoint code ever creates a new round.

Snapshots (`student_name_snapshot`, `student_roll_snapshot`,
`program_snapshot`) are copied from the student's own `User` record once, at
creation — the same reasoning as `Thesis.title_snapshot` — so a later profile
change never alters a historical report. `research_title` is confirmed to be
INDEPENDENTLY entered by the student on this module — it is never sourced
from PPW, Synopsis, or the Advisory Committee (a deliberate, confirmed
divergence from Thesis's PPW-sourced title).

`session_year`/`session_semester` are NEVER stored — they are pure,
deterministic functions of `semester_completed` (see `_session_label` in
`progress_report.py`), computed only at serialization time, so there is
nothing for a client to submit or desynchronize.
"""
import uuid
from datetime import date, datetime, timezone
from sqlalchemy import String, Text, Integer, Boolean, Date, DateTime, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# "submitted" is kept in the vocabulary for completeness/documentation parity with the
# confirmed requirement, but is never actually persisted by this module's own code — a
# submission transitions the record directly to "major_advisor_pending" (there is no
# separate first-approver stage distinct from Major Advisor, exactly like Synopsis).
PROGRESS_REPORT_STATUSES = (
    "draft", "submitted", "major_advisor_pending", "committee_pending", "hod_pending",
    "incharge_academic_cell_pending", "dpgs_pending", "approved", "reverted",
)
PROGRESS_REPORT_CYCLE_STATUSES = ("active", "approved", "reverted")
PROGRESS_REPORT_STAGE_TYPES = ("major_advisor", "committee_member", "hod", "incharge_academic_cell", "dpgs")
PROGRESS_REPORT_STAGE_STATUSES = ("pending", "approved", "reverted", "cancelled")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProgressReport(Base):
    __tablename__ = "ams_progress_reports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), index=True)
    academic_year_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_academic_calendars.id"))
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_semesters.id"))
    status: Mapped[str] = mapped_column(String(40), default="draft", nullable=False)

    # Server-derived snapshots (never client-supplied, never resynced afterward).
    student_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    student_roll_snapshot: Mapped[str | None] = mapped_column(String(50))
    program_snapshot: Mapped[str | None] = mapped_column(String(200))

    # Reporting period.
    period_from: Mapped[date | None] = mapped_column(Date)
    period_to: Mapped[date | None] = mapped_column(Date)

    # Semester Completed drives session_year/session_semester (computed, never stored — see module docstring).
    semester_completed: Mapped[int | None] = mapped_column(Integer)

    # Course/credit fields — manually entered by the student; never derived/overwritten server-side
    # (Section 7 of the confirmed requirements — no curriculum master-data system exists or is added).
    total_courses: Mapped[int | None] = mapped_column(Integer)
    total_credits_programme: Mapped[int | None] = mapped_column(Integer)
    current_semester_courses: Mapped[int | None] = mapped_column(Integer)
    current_semester_credits: Mapped[int | None] = mapped_column(Integer)
    courses_completed_till_date: Mapped[int | None] = mapped_column(Integer)
    credits_completed_till_date: Mapped[int | None] = mapped_column(Integer)

    # Research — independently entered here, never sourced from PPW/Synopsis/Advisory Committee.
    research_title: Mapped[str | None] = mapped_column(Text)
    research_progress: Mapped[str | None] = mapped_column(Text)

    leave_availed: Mapped[str | None] = mapped_column(Text)
    fellowship_stipend: Mapped[str | None] = mapped_column(Text)

    # Student-entered (CORRECTED — a prior revision mistakenly modelled these as Major Advisor
    # fields; AVFU clarified they belong to the student). `expected_completion` is a strict
    # Yes/No choice, stored as a plain Boolean (True = Yes) — reusing the existing repository
    # convention of a boolean column for a closed two-value field, not a free-text/enum
    # redesign. `completion_delay_reason` is required only when `expected_completion is False`;
    # enforced server-side (see `_validate_completion_fields` in the endpoint layer), never
    # trusted from client-side conditional rendering alone.
    expected_completion: Mapped[bool | None] = mapped_column(Boolean)
    completion_delay_reason: Mapped[str | None] = mapped_column(Text)

    # Major Advisor-only fields (CORRECTED — these three, not the completion fields above, are
    # the actual Major Advisor fields) — never student-editable, editable only by the current
    # live Major Advisor while the report is at the Major Advisor stage (enforced in the
    # endpoint layer, same as before this correction).
    advisory_remark: Mapped[str | None] = mapped_column(Text)
    overall_progress: Mapped[str | None] = mapped_column(Text)
    student_conduct: Mapped[str | None] = mapped_column(Text)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        # ONE Progress Report per student per (academic year, semester) — EVER, regardless of
        # status (draft/submitted/approved/reverted all count) — a plain (non-partial) unique
        # constraint, unlike Migration's "one ACTIVE at a time" partial index: a reverted report
        # is resubmitted in place, never superseded by a second row for the same semester.
        UniqueConstraint("student_id", "academic_year_id", "semester_id", name="uq_progress_report_student_semester"),
    )

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    academic_year: Mapped["AcademicCalendar"] = relationship("AcademicCalendar", foreign_keys=[academic_year_id])
    semester: Mapped["Semester"] = relationship("Semester", foreign_keys=[semester_id])
    cycles: Mapped[list["ProgressReportApprovalCycle"]] = relationship(
        "ProgressReportApprovalCycle", back_populates="report", cascade="all, delete-orphan", order_by="ProgressReportApprovalCycle.cycle_number",
    )


class ProgressReportApprovalCycle(Base):
    """One submission attempt. Unlike Synopsis, a cycle here can survive MULTIPLE partial
    reverts (Committee->MA, HOD->Committee, Incharge->HOD, DPGS->Incharge) — it only ends
    (`status` becomes `approved` or `reverted`) on final DPGS approval or a MAJOR ADVISOR
    revert (the only revert destination that sends the report all the way back to the
    student). A resubmission after a Major-Advisor-level revert starts a brand new cycle,
    exactly like Synopsis."""
    __tablename__ = "ams_progress_report_approval_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_progress_reports.id", ondelete="CASCADE"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("report_id", "cycle_number", name="uq_progress_report_cycle_number"),
        Index("uq_progress_report_one_active_cycle", "report_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    report: Mapped["ProgressReport"] = relationship("ProgressReport", back_populates="cycles")
    stages: Mapped[list["ProgressReportApprovalStage"]] = relationship(
        "ProgressReportApprovalStage", back_populates="cycle", cascade="all, delete-orphan", order_by="ProgressReportApprovalStage.sequence",
    )
    proceedings: Mapped[list["ProgressReportProceedings"]] = relationship(
        "ProgressReportProceedings", back_populates="cycle", cascade="all, delete-orphan", order_by="ProgressReportProceedings.version_number",
    )


class ProgressReportApprovalStage(Base):
    """One approver's stage within a cycle. Major Advisor / committee-member stages are bound
    to the exact `CommitteeMember` row (SET NULL if that membership is later removed, so
    history survives); HOD/Incharge/DPGS carry no person up front — whoever holds the role
    (and, for HOD, the student's department) in their ACTIVE session may act, re-checked live
    on every call.

    A stage row, once decided (`approved`/`reverted`) or superseded (`cancelled`), is NEVER
    mutated again — a "fresh round" after a partial revert is a brand NEW row with a higher
    `sequence`. The one LIVE row for a given (stage_type, committee_member_id) pair at any
    moment is the one with the highest `sequence` (see `_live_stages` in the endpoint file)."""
    __tablename__ = "ams_progress_report_approval_stages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_progress_report_approval_cycles.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_type: Mapped[str] = mapped_column(String(30), nullable=False)
    role_label: Mapped[str] = mapped_column(String(60), nullable=False)
    committee_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_committee_members.id", ondelete="SET NULL"))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    acted_role: Mapped[str | None] = mapped_column(String(30))
    acted_department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("cycle_id", "sequence", name="uq_progress_report_stage_sequence"),)

    cycle: Mapped["ProgressReportApprovalCycle"] = relationship("ProgressReportApprovalCycle", back_populates="stages")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    assignee: Mapped["User | None"] = relationship("User", foreign_keys=[assignee_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])
    signatures: Mapped[list["ProgressReportSignature"]] = relationship(
        "ProgressReportSignature", back_populates="stage", cascade="all, delete-orphan",
    )


class ProgressReportSignature(Base):
    """OTP record for one signing attempt on one stage (same shape/semantics as
    `SynopsisSignature`/`ThesisSignature`: single-use, expiring, scoped to this stage and this
    user). The Incharge Academic Cell stage never creates one — workflow approval only, no
    signature, matching the established convention across every prior workflow."""
    __tablename__ = "ams_progress_report_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_progress_report_approval_stages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    otp_code: Mapped[str | None] = mapped_column(String(10))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_used: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    stage: Mapped["ProgressReportApprovalStage"] = relationship("ProgressReportApprovalStage", back_populates="signatures")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])


class ProgressReportProceedings(Base):
    """The Major Advisor's uploaded Proceedings of the (offline) advisory committee meeting —
    one PDF per cycle, versioned/immutable exactly like `SynopsisFile` (a re-upload during a
    later Major-Advisor round is simply the next version; nothing is ever overwritten or
    deleted). Uploading/replacing is only permitted while the cycle's Major Advisor stage is
    the currently LIVE one (`report.status == 'major_advisor_pending'`) — a separate action
    from approving, per the confirmed requirement. Visible to Major Advisor, Advisory
    Committee members, HOD, Incharge Academic Cell, DPGS and Super Admin — explicitly NOT the
    student, enforced in the endpoint layer's authorization, never by hiding a button."""
    __tablename__ = "ams_progress_report_proceedings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_progress_report_approval_cycles.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(100), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("cycle_id", "version_number", name="uq_progress_report_proceedings_version"),)

    cycle: Mapped["ProgressReportApprovalCycle"] = relationship("ProgressReportApprovalCycle", back_populates="proceedings")
    uploader: Mapped["User | None"] = relationship("User", foreign_keys=[uploaded_by])


from app.models.user import User  # noqa: E402,F401
from app.models.academic import AcademicCalendar, Semester  # noqa: E402,F401
from app.models.research import CommitteeMember  # noqa: E402,F401
