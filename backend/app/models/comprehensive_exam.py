"""Comprehensive Examination (PG "Internal Oral Comprehensive" / PhD "Pre Oral Comprehensive").

Modelled directly on `app/models/thesis.py`'s `FinalCertificate`/`FinalCertificateSignature`
(PG-25(A)/Viva Voce Certificate) for every approval-chain table here, and on
`app/models/external_examiner.py`'s reusable-identity/selection/cycle/proposal/stage/result
shape for the PhD External Panel — but as PARALLEL tables, never the same rows (see
`ComprehensiveExamExternalPanelSelection`'s docstring for why the existing Thesis External
Examiner tables cannot be reused directly).

No OTP/digital-signature step exists anywhere in this module (mirrors `FinalCertificate`'s own
plain approve/revert chain, not External Examiner Selection's OTP-signed one) — a smaller,
already-proven AMS pattern, since no OTP requirement was specified for Comprehensive Exam.

One application per student (`ComprehensiveExamApplication.student_id` unique) — mirrors
`AdvisoryCommittee.student_id`'s own one-per-student-ever shape. `degree_level` and the
Major/Minor credit totals are SNAPSHOTTED at submission time (never recomputed later) so an
already-generated application/document can never silently change because the student's
enrollment data changes afterward.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, CheckConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# generated -> ma_pending -> hod_pending -> incharge_pending -> dpgs_pending -> approved
# "reverted" is reachable from any pending stage and is terminal for that row (no regenerate
# flow exists for the application itself — a reverted application stays reverted).
COMPREHENSIVE_EXAM_APPLICATION_STATUSES = (
    "generated", "ma_pending", "hod_pending", "incharge_pending", "dpgs_pending", "approved", "reverted",
)
COMPREHENSIVE_EXAM_COURSE_CLASSIFICATIONS = ("major", "minor")
COMPREHENSIVE_EXAM_VIVA_RESULTS = ("pending", "satisfactory", "unsatisfactory")
# generated -> ma_pending -> committee_pending -> hod_pending -> incharge_pending -> dpgs_pending -> approved
# (identical shape to FinalCertificate's kind="viva" chain). ma_pending exists here because the
# report is generated automatically the instant the MA marks Satisfactory, then the SAME MA
# explicitly submits it (mirrors FinalCertificate's Viva kind: generated_by_id == ma_id, a
# single actor across generation and the first "submit" action).
COMPREHENSIVE_EXAM_REPORT_STATUSES = (
    "generated", "ma_pending", "committee_pending", "hod_pending", "incharge_pending", "dpgs_pending", "approved", "reverted",
)
# draft -> hod_pending -> incharge_pending -> dpgs_pending -> vc_pending -> approved
# (no separate "major_advisor_pending" stage: MA proposing IS the submit action, unlike
# Thesis External Examiner Selection's separate draft/submit steps).
COMPREHENSIVE_EXAM_PANEL_STAGE_TYPES = ("hod", "incharge_academic_cell", "dpgs", "vc")
COMPREHENSIVE_EXAM_PANEL_CYCLE_STATUSES = ("active", "approved", "reverted")
COMPREHENSIVE_EXAM_EXTERNAL_PANEL_SIZE = 5
COMPREHENSIVE_EXAM_EXTERNAL_SELECTION_COUNT = 1
# uploaded -> committee_pending -> hod_pending -> incharge_pending -> dpgs_pending -> approved
COMPREHENSIVE_EXAM_EXTERNAL_REPORT_STATUSES = (
    "uploaded", "committee_pending", "hod_pending", "incharge_pending", "dpgs_pending", "approved", "reverted",
)

_OPEN_APPLICATION_STATUSES = ("generated", "ma_pending", "hod_pending", "incharge_pending", "dpgs_pending")
_OPEN_REPORT_STATUSES = ("generated", "ma_pending", "committee_pending", "hod_pending", "incharge_pending", "dpgs_pending")
_OPEN_PANEL_CYCLE_STATUSES = ("active",)
_OPEN_EXTERNAL_REPORT_STATUSES = ("uploaded", "committee_pending", "hod_pending", "incharge_pending", "dpgs_pending")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ComprehensiveExamApplication(Base):
    """One per student, ever (`student_id` unique — mirrors `AdvisoryCommittee`). `degree_level`
    and every credit figure are snapshotted at creation from the REAL, live `Program.level` /
    `StudentEnrollment` data at that instant — never recomputed later, so an already-generated
    application can never silently change because the student's enrollment data changes
    afterward (see `ComprehensiveExamApplicationCourse` for the frozen per-course rows).

    `application_type` is a plain string, deliberately not a foreign key to a configurable
    "application type" table — only one value exists today
    ("holding_comprehensive_examination"). Keeping it as a string (not a hardcoded boolean/enum
    baked into columns) means a future dynamic, Super-Admin-managed application-type list can be
    introduced later by adding a lookup table and pointing this column at it, without a breaking
    schema change to this table."""
    __tablename__ = "ams_comprehensive_exam_applications"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), unique=True, index=True)
    application_type: Mapped[str] = mapped_column(String(60), nullable=False, default="holding_comprehensive_examination")
    degree_level: Mapped[str] = mapped_column(String(20), nullable=False)  # PG / PhD, snapshotted
    status: Mapped[str] = mapped_column(String(20), default="generated", nullable=False)

    major_credits_required: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    minor_credits_required: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    major_credits_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    minor_credits_completed: Mapped[int] = mapped_column(Integer, nullable=False)

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    student_signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ma_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    ma_acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hod_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    hod_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incharge_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    incharge_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpgs_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    dpgs_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)

    # Generated PDF, stored directly on this row (one application = one current document;
    # there is no multi-version concept for the application itself, unlike the Viva Report).
    stored_filename: Mapped[str | None] = mapped_column(String(100))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("degree_level IN ('PG', 'PhD')", name="ck_comp_exam_application_degree_level"),
    )

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    ma: Mapped["User | None"] = relationship("User", foreign_keys=[ma_id])
    hod_approver: Mapped["User | None"] = relationship("User", foreign_keys=[hod_approved_by])
    incharge_approver: Mapped["User | None"] = relationship("User", foreign_keys=[incharge_approved_by])
    dpgs_approver: Mapped["User | None"] = relationship("User", foreign_keys=[dpgs_approved_by])
    reverter: Mapped["User | None"] = relationship("User", foreign_keys=[reverted_by])
    courses: Mapped[list["ComprehensiveExamApplicationCourse"]] = relationship(
        "ComprehensiveExamApplicationCourse", back_populates="application", cascade="all, delete-orphan",
        order_by="ComprehensiveExamApplicationCourse.classification, ComprehensiveExamApplicationCourse.order_index",
    )
    vivas: Mapped[list["ComprehensiveExamViva"]] = relationship(
        "ComprehensiveExamViva", back_populates="application", cascade="all, delete-orphan",
        order_by="ComprehensiveExamViva.attempt_number",
    )


class ComprehensiveExamApplicationCourse(Base):
    """One frozen row per course counted toward Major/Minor eligibility at submission time —
    the "Courses successfully completed till date" column of the generated document. Snapshots
    `course_number`/`credits` directly (never a live FK-follow to `Course`/`CourseOffering`),
    so a later change to the course catalogue or the student's enrollment can never alter an
    already-generated application's printed course list."""
    __tablename__ = "ams_comprehensive_exam_application_courses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_applications.id", ondelete="CASCADE"), index=True)
    classification: Mapped[str] = mapped_column(String(10), nullable=False)  # major / minor
    course_number: Mapped[str] = mapped_column(String(50), nullable=False)
    course_title: Mapped[str | None] = mapped_column(String(300))
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("classification IN ('major', 'minor')", name="ck_comp_exam_app_course_classification"),
    )

    application: Mapped["ComprehensiveExamApplication"] = relationship("ComprehensiveExamApplication", back_populates="courses")


class ComprehensiveExamViva(Base):
    """One scheduled viva attempt. `attempt_number` advances by 1 every time the Major Advisor
    marks the PRIOR attempt Unsatisfactory and the HOD fixes a new date — unlimited attempts,
    no cap enforced (confirmed business rule, this revision). `viva_date` is the HOD-fixed date
    ONLY — the authoritative date printed on the generated Viva Report regardless of when the
    Major Advisor actually records the result; treated as immutable once a
    `ComprehensiveExamVivaReport` row has been generated against this viva (no UPDATE path
    exists past that point in the endpoint layer)."""
    __tablename__ = "ams_comprehensive_exam_vivas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_applications.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    viva_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    result: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending/satisfactory/unsatisfactory
    result_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("application_id", "attempt_number", name="uq_comp_exam_viva_attempt"),
        # At most one "open" (result still pending) viva attempt per application at a time.
        Index("uq_comp_exam_viva_one_pending", "application_id", unique=True, postgresql_where=text("result = 'pending'")),
    )

    application: Mapped["ComprehensiveExamApplication"] = relationship("ComprehensiveExamApplication", back_populates="vivas")
    scheduler: Mapped["User | None"] = relationship("User", foreign_keys=[scheduled_by])
    result_recorder: Mapped["User | None"] = relationship("User", foreign_keys=[result_by])
    report: Mapped["ComprehensiveExamVivaReport | None"] = relationship(
        "ComprehensiveExamVivaReport", back_populates="viva", uselist=False, cascade="all, delete-orphan",
    )


class ComprehensiveExamVivaReport(Base):
    """Internal Viva Report — generated the instant the Major Advisor marks a viva attempt
    Satisfactory (`generated_by_id == ma_id`, mirrors `FinalCertificate`'s own Viva `kind`
    exactly). `version_number` advances only when the Major Advisor regenerates after a revert
    (the reverted predecessor is kept forever, never reused/resurrected — identical convention
    to `FinalCertificate`). One viva attempt has at most one report row family (1:1 via
    `viva_id`, versions distinguished by `version_number`)."""
    __tablename__ = "ams_comprehensive_exam_viva_reports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    viva_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_vivas.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), default="generated", nullable=False)
    generated_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ma_acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hod_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    hod_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incharge_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    incharge_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpgs_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    dpgs_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)

    stored_filename: Mapped[str | None] = mapped_column(String(100))
    original_filename: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("viva_id", "version_number", name="uq_comp_exam_viva_report_version"),
        Index(
            "uq_comp_exam_viva_report_one_open", "viva_id", unique=True,
            postgresql_where=text("status IN ('generated', 'ma_pending', 'committee_pending', 'hod_pending', 'incharge_pending', 'dpgs_pending')"),
        ),
    )

    viva: Mapped["ComprehensiveExamViva"] = relationship("ComprehensiveExamViva", back_populates="report")
    generator: Mapped["User | None"] = relationship("User", foreign_keys=[generated_by_id])
    hod_approver: Mapped["User | None"] = relationship("User", foreign_keys=[hod_approved_by])
    incharge_approver: Mapped["User | None"] = relationship("User", foreign_keys=[incharge_approved_by])
    dpgs_approver: Mapped["User | None"] = relationship("User", foreign_keys=[dpgs_approved_by])
    reverter: Mapped["User | None"] = relationship("User", foreign_keys=[reverted_by])
    signatures: Mapped[list["ComprehensiveExamVivaReportSignature"]] = relationship(
        "ComprehensiveExamVivaReportSignature", back_populates="report", cascade="all, delete-orphan",
    )


class ComprehensiveExamVivaReportSignature(Base):
    """One required Advisory-Committee-member signature on ONE Viva Report row — identical
    shape/semantics to `FinalCertificateSignature`. The required signer set is snapshotted at
    generation time from the student's REAL, live `AdvisoryCommittee`/`CommitteeMember` rows:
    every accepted member EXCLUDING `major_advisor` (handled separately as the report's own
    `generated_by_id`/`ma_acted_at`)."""
    __tablename__ = "ams_comprehensive_exam_viva_report_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_viva_reports.id", ondelete="CASCADE"), index=True)
    committee_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_committee_members.id", ondelete="SET NULL"))
    faculty_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), nullable=False)
    role_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("report_id", "faculty_id", name="uq_comp_exam_viva_report_sig_faculty"),)

    report: Mapped["ComprehensiveExamVivaReport"] = relationship("ComprehensiveExamVivaReport", back_populates="signatures")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    faculty: Mapped["User"] = relationship("User", foreign_keys=[faculty_id])


# ── PhD External Panel (parallel to, never sharing rows with, ExternalExaminer*) ────────────

class ComprehensiveExamExternalPanelSelection(Base):
    """One per PhD application (`application_id` unique — NOT `student_id`, and NOT the same
    table as `ExternalExaminerSelection`). Deliberately a separate table family from Thesis's
    External Examiner Selection:
      - `ExternalExaminerSelection.student_id` is globally unique, so reusing it here would make
        it impossible for the same student to ALSO later have a real Thesis External Examiner
        Selection.
      - The panel size (5 proposed / 1 selected) differs from Thesis's PhD rule (5 proposed / 2
        selected) — sharing the same count constants/tables would silently corrupt one workflow
        or the other.
      - Most importantly, the existing Thesis flow creates a real AMS `User` login account for
        the selected examiner (`_resolve_or_create_examiner_account` in `external_examiner.py`)
        — this module's confirmed rule is the OPPOSITE (no account, ever) — see
        `ComprehensiveExamExternalPanelResult`'s docstring.
    The reusable `ExternalExaminer` IDENTITY table itself (keyed only by email, purpose-agnostic)
    IS reused directly — only the selection/cycle/proposal/stage/result shape is parallel."""
    __tablename__ = "ams_comp_exam_external_panel_selections"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_applications.id", ondelete="CASCADE"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    application: Mapped["ComprehensiveExamApplication"] = relationship("ComprehensiveExamApplication", foreign_keys=[application_id])
    cycles: Mapped[list["ComprehensiveExamExternalPanelCycle"]] = relationship(
        "ComprehensiveExamExternalPanelCycle", back_populates="selection", cascade="all, delete-orphan",
        order_by="ComprehensiveExamExternalPanelCycle.cycle_number",
    )


class ComprehensiveExamExternalPanelCycle(Base):
    __tablename__ = "ams_comp_exam_external_panel_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    selection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comp_exam_external_panel_selections.id", ondelete="CASCADE"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    ma_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)
    hod_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    hod_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incharge_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    incharge_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpgs_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    dpgs_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Idempotency guard for VC selection — set exactly once, in the same transaction as the
    # ComprehensiveExamExternalPanelResult row, checked (IS NULL) before writing. Mirrors
    # ExternalExaminerApprovalCycle.vc_selection_completed_at exactly.
    vc_selection_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("selection_id", "cycle_number", name="uq_comp_exam_panel_cycle_number"),
        Index("uq_comp_exam_panel_one_active_cycle", "selection_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    selection: Mapped["ComprehensiveExamExternalPanelSelection"] = relationship("ComprehensiveExamExternalPanelSelection", back_populates="cycles")
    ma: Mapped["User | None"] = relationship("User", foreign_keys=[ma_id])
    proposals: Mapped[list["ComprehensiveExamExternalPanelProposal"]] = relationship(
        "ComprehensiveExamExternalPanelProposal", back_populates="cycle", cascade="all, delete-orphan",
        order_by="ComprehensiveExamExternalPanelProposal.slot_number",
    )
    result: Mapped["ComprehensiveExamExternalPanelResult | None"] = relationship(
        "ComprehensiveExamExternalPanelResult", back_populates="cycle", uselist=False, cascade="all, delete-orphan",
    )


class ComprehensiveExamExternalPanelProposal(Base):
    """One of exactly 5 proposed slots. Snapshot fields mirror `ExternalExaminerProposal`'s six
    columns exactly (same information the Major Advisor already knows how to fill in)."""
    __tablename__ = "ams_comp_exam_external_panel_proposals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comp_exam_external_panel_cycles.id", ondelete="CASCADE"), index=True)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    examiner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiners.id", ondelete="SET NULL"))

    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    specialization_snapshot: Mapped[str] = mapped_column(String(300), nullable=False)
    designation_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    email_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    institution_snapshot: Mapped[str] = mapped_column(String(300), nullable=False)

    __table_args__ = (UniqueConstraint("cycle_id", "slot_number", name="uq_comp_exam_panel_proposal_slot"),)

    cycle: Mapped["ComprehensiveExamExternalPanelCycle"] = relationship("ComprehensiveExamExternalPanelCycle", back_populates="proposals")
    examiner: Mapped["ExternalExaminer | None"] = relationship("ExternalExaminer", foreign_keys=[examiner_id])


class ComprehensiveExamExternalPanelResult(Base):
    """The VC's single choice (exactly 1 of 5). Creating this row NEVER creates or touches a
    `User` account — `ExternalExaminer.user_id` is left NULL for every examiner resolved through
    this table, permanently (confirmed rule: the external examiner is offline-only and never
    receives AMS login credentials). Contrast deliberately with
    `ExternalExaminerSelectionResult`/`_resolve_or_create_examiner_account`, which DOES create an
    account — that behavior must never be copied here."""
    __tablename__ = "ams_comp_exam_external_panel_results"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comp_exam_external_panel_cycles.id", ondelete="CASCADE"), unique=True, index=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comp_exam_external_panel_proposals.id"), nullable=False)
    selected_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    cycle: Mapped["ComprehensiveExamExternalPanelCycle"] = relationship("ComprehensiveExamExternalPanelCycle", back_populates="result")
    proposal: Mapped["ComprehensiveExamExternalPanelProposal"] = relationship("ComprehensiveExamExternalPanelProposal", foreign_keys=[proposal_id])
    selector: Mapped["User | None"] = relationship("User", foreign_keys=[selected_by])


class ComprehensiveExamExternalVivaReport(Base):
    """The signed, physically-executed external Viva Report — MA generates the blank/unsigned
    PDF, all parties sign on paper, MA uploads the scanned/signed PDF (`version_number`
    incrementing on every upload, exactly `ThesisDocument`'s "never overwrite, always a new
    version" contract), then submits it into the approval workflow. A revert returns the row to
    `reverted` (terminal for that version) — the Major Advisor uploads an entirely NEW version
    (`version_number + 1`) and resubmits; the reverted version's file and row are never deleted
    or reused."""
    __tablename__ = "ams_comprehensive_exam_external_viva_reports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_applications.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)

    stored_filename: Mapped[str] = mapped_column(String(100), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hod_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    hod_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incharge_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    incharge_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpgs_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    dpgs_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("application_id", "version_number", name="uq_comp_exam_ext_report_version"),
        Index(
            "uq_comp_exam_ext_report_one_open", "application_id", unique=True,
            postgresql_where=text("status IN ('uploaded', 'committee_pending', 'hod_pending', 'incharge_pending', 'dpgs_pending')"),
        ),
    )

    application: Mapped["ComprehensiveExamApplication"] = relationship("ComprehensiveExamApplication", foreign_keys=[application_id])
    uploader: Mapped["User | None"] = relationship("User", foreign_keys=[uploaded_by])
    hod_approver: Mapped["User | None"] = relationship("User", foreign_keys=[hod_approved_by])
    incharge_approver: Mapped["User | None"] = relationship("User", foreign_keys=[incharge_approved_by])
    dpgs_approver: Mapped["User | None"] = relationship("User", foreign_keys=[dpgs_approved_by])
    reverter: Mapped["User | None"] = relationship("User", foreign_keys=[reverted_by])
    signatures: Mapped[list["ComprehensiveExamExternalReportSignature"]] = relationship(
        "ComprehensiveExamExternalReportSignature", back_populates="report", cascade="all, delete-orphan",
    )


class ComprehensiveExamExternalReportSignature(Base):
    """Committee AND-gate for the external report — identical shape to
    `ComprehensiveExamVivaReportSignature`/`FinalCertificateSignature` (every accepted committee
    member excluding the Major Advisor)."""
    __tablename__ = "ams_comprehensive_exam_external_report_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_comprehensive_exam_external_viva_reports.id", ondelete="CASCADE"), index=True)
    committee_member_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_committee_members.id", ondelete="SET NULL"))
    faculty_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), nullable=False)
    role_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("report_id", "faculty_id", name="uq_comp_exam_ext_report_sig_faculty"),)

    report: Mapped["ComprehensiveExamExternalVivaReport"] = relationship("ComprehensiveExamExternalVivaReport", back_populates="signatures")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    faculty: Mapped["User"] = relationship("User", foreign_keys=[faculty_id])


from app.models.user import User  # noqa: E402,F401
from app.models.research import CommitteeMember  # noqa: E402,F401
from app.models.external_examiner import ExternalExaminer  # noqa: E402,F401
