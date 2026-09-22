"""Initial Thesis Management (BUSINESS_LOGIC.md section AD).

Modelled directly on Synopsis's approval-cycle design (`app/models/synopsis.py`)
— one cycle per submission, one stage per approver, OTP signature rows — with
three differences required by this module's confirmed business rules:

* **Initial vs Final.** `Thesis.thesis_type` is "initial" today; "final" is
  reserved for the future Final Thesis workflow (explicitly OUT OF SCOPE this
  phase). Exactly one "initial" Thesis per student is enforced by a PARTIAL
  UNIQUE INDEX in the database (mirrors `uq_synopsis_first_per_student`
  exactly), so it holds regardless of what the API does; "final" rows are
  outside that constraint and nothing here creates one.

* **Title sourced from PPW, snapshotted once.** The Thesis Title is the
  student's own `Ppw.research_title` (BUSINESS_LOGIC.md D.3/the PPW module) —
  NOT a second, independently-typed title. `Thesis.ppw_id` references the
  source PPW (informational, `ON DELETE SET NULL`); `Thesis.title_snapshot` is
  copied from `Ppw.research_title` exactly once, at Thesis creation, and is
  never resynced afterward. This prevents a later, unrelated PPW edit (the
  student could, in principle, still edit their PPW's `research_title` while
  it is a draft) from silently corrupting an already-created Thesis record —
  the same reasoning as Synopsis's own `title_snapshot` on each approval
  cycle. There is no Thesis Title input field anywhere in this module: the
  student never types an arbitrary title.

* **Multi-document, multi-examiner.** Unlike Synopsis's single uploaded PDF,
  a Thesis carries several distinct document categories (the thesis file
  itself, both plagiarism reports, and the four supporting-document uploads)
  — all versioned identically via one shared `ThesisDocument` table
  distinguished by `document_type`, and it must reference potentially SEVERAL
  external examiners' evaluations (PG: 1 assigned examiner; PhD: 2) via
  `ThesisExternalEvaluation`, one row per `ExternalExaminerAssignment`.

Examiner IDENTITY is never duplicated here: `ThesisExternalEvaluation` points
at the existing `ams_external_examiner_assignments` row (created by the
already-implemented External Examiner Selection module) — never at an email,
never at a fresh account. See `app/models/external_examiner.py`.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Float, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, CheckConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

THESIS_TYPES = ("initial", "final")

# draft -> major_advisor_pending -> hod_pending -> librarian_pending ->
# incharge_pending -> dpgs_pending -> external_examiner_pending ->
# dpgs_final_pending -> approved ; "reverted" is reachable from any pending
# state up to and including dpgs_pending and returns the Thesis to the
# STUDENT (Synopsis's revert convention, not External Examiner Selection's
# "always to Major Advisor" one — see thesis.py's module docstring for why).
# There is deliberately no revert path FROM external_examiner_pending or
# dpgs_final_pending in this phase (see thesis.py Section "Revert" / the
# implementation report's open questions).
THESIS_STATUSES = (
    "draft", "major_advisor_pending", "hod_pending", "librarian_pending",
    "incharge_pending", "dpgs_pending", "external_examiner_pending",
    "dpgs_final_pending", "approved", "reverted",
)
THESIS_CYCLE_STATUSES = ("active", "approved", "reverted")
# "dpgs" = approves and sends the Thesis for external evaluation.
# "dpgs_final" = approves the completed external evaluation (separate action,
# separate stage row — see module docstring). Only stages up to and including
# "dpgs" appear in the printed Signature table (business form convention,
# confirmed by inspection of the required table's exact 6 rows); "dpgs_final"
# is tracked identically for audit/history but is not itself a printed row.
THESIS_STAGE_TYPES = ("major_advisor", "hod", "librarian", "incharge_academic_cell", "dpgs", "dpgs_final")
THESIS_STAGE_STATUSES = ("pending", "approved", "reverted", "cancelled")

THESIS_DOCUMENT_TYPES = (
    "thesis_file", "plagiarism_student_report", "plagiarism_library_report",
    "payment_receipt", "seminar_proceedings", "clearance",
    "declaration_annexure1", "seminar_certificate_pg25", "certificate_i_pg27", "annexure_iv",
)
# Confidentiality (BUSINESS_LOGIC.md AD — "Plagiarism by Library"): this ONE document type is
# never visible/downloadable by the student, enforced in the endpoint layer's authorization,
# never merely by hiding a button. Every other document type is the student's own upload/view.
CONFIDENTIAL_DOCUMENT_TYPES = {"plagiarism_library_report"}

THESIS_EVALUATION_STATUSES = ("pending", "submitted", "approved")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Thesis(Base):
    __tablename__ = "ams_theses"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), index=True)
    thesis_type: Mapped[str] = mapped_column(String(20), nullable=False, default="initial")
    ppw_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_ppw.id", ondelete="SET NULL"))
    title_snapshot: Mapped[str | None] = mapped_column(Text)
    plagiarism_student_percent: Mapped[float | None] = mapped_column(Float)
    plagiarism_software_name: Mapped[str | None] = mapped_column(String(200))
    plagiarism_library_percent: Mapped[float | None] = mapped_column(Float)
    abstract: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("thesis_type IN ('initial', 'final')", name="ck_thesis_type"),
        # One INITIAL Thesis per student, enforced by the database (mirrors Synopsis's
        # uq_synopsis_first_per_student exactly). "final" rows are exempt — reserved for later.
        Index("uq_thesis_initial_per_student", "student_id", unique=True, postgresql_where=text("thesis_type = 'initial'")),
    )

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    ppw: Mapped["Ppw | None"] = relationship("Ppw", foreign_keys=[ppw_id])
    documents: Mapped[list["ThesisDocument"]] = relationship(
        "ThesisDocument", back_populates="thesis", cascade="all, delete-orphan",
        order_by="ThesisDocument.document_type, ThesisDocument.version_number",
    )
    cycles: Mapped[list["ThesisApprovalCycle"]] = relationship(
        "ThesisApprovalCycle", back_populates="thesis", cascade="all, delete-orphan", order_by="ThesisApprovalCycle.cycle_number",
    )
    evaluations: Mapped[list["ThesisExternalEvaluation"]] = relationship(
        "ThesisExternalEvaluation", back_populates="thesis", cascade="all, delete-orphan",
    )


class ThesisDocument(Base):
    """One uploaded document version, for any of the 10 confirmed document
    categories (`THESIS_DOCUMENT_TYPES`). Mirrors `SynopsisFile`'s versioning
    exactly: every upload is a new, immutable row (`version_number`); nothing
    is ever overwritten or deleted, so a replaced document's history survives.
    The stored file lives under `UPLOAD_DIR/thesis/<thesis_id>/<stored_filename>`
    (a server-generated name — the client never sees a filesystem path, only
    the authorized download endpoint). `is_confidential` is set true only for
    `plagiarism_library_report`, and is enforced by the endpoint's
    authorization check, never by the client."""
    __tablename__ = "ams_thesis_documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_theses.id", ondelete="CASCADE"), index=True)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(100), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_confidential: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("thesis_id", "document_type", "version_number", name="uq_thesis_document_version"),)

    thesis: Mapped["Thesis"] = relationship("Thesis", back_populates="documents")
    uploader: Mapped["User | None"] = relationship("User", foreign_keys=[uploaded_by])


class ThesisApprovalCycle(Base):
    """One submission/approval attempt. A new cycle is created on every
    (re)submission after a revert; a reverted cycle is kept forever (history)
    and never counts toward a later one — exactly Synopsis's convention."""
    __tablename__ = "ams_thesis_approval_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_theses.id", ondelete="CASCADE"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("thesis_id", "cycle_number", name="uq_thesis_cycle_number"),
        Index("uq_thesis_one_active_cycle", "thesis_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    thesis: Mapped["Thesis"] = relationship("Thesis", back_populates="cycles")
    stages: Mapped[list["ThesisApprovalStage"]] = relationship(
        "ThesisApprovalStage", back_populates="cycle", cascade="all, delete-orphan", order_by="ThesisApprovalStage.sequence",
    )


class ThesisApprovalStage(Base):
    """One approver's stage within a cycle. Major Advisor is bound to the exact
    `CommitteeMember` row (SET NULL if that membership is later removed, so
    history survives); HOD/Librarian/Incharge/DPGS carry no person up front —
    whoever holds the role (and, for HOD, the student's department) in their
    ACTIVE session may act, re-checked live on every call — the same
    `_find_my_stage` pattern used by PPW/Synopsis/External Examiner
    Selection. Librarian is explicitly NOT single-holder: ANY active
    Librarian may act on the pending Librarian stage (no per-library/
    department scoping — no evidence of one exists in the repository or
    business rules)."""
    __tablename__ = "ams_thesis_approval_stages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_thesis_approval_cycles.id", ondelete="CASCADE"), index=True)
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

    __table_args__ = (UniqueConstraint("cycle_id", "sequence", name="uq_thesis_stage_sequence"),)

    cycle: Mapped["ThesisApprovalCycle"] = relationship("ThesisApprovalCycle", back_populates="stages")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    assignee: Mapped["User | None"] = relationship("User", foreign_keys=[assignee_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])
    signatures: Mapped[list["ThesisSignature"]] = relationship("ThesisSignature", back_populates="stage", cascade="all, delete-orphan")


class ThesisSignature(Base):
    """OTP record for one signing attempt on one stage (same shape/semantics as
    `SynopsisSignature`/`PpwSignature`/`ExternalExaminerSignature`: single-use,
    expiring, scoped to this stage and this user). The Incharge Academic Cell
    stage never creates one — workflow approval only, no signature, matching
    the established convention across every prior multi-stage workflow in
    this repository."""
    __tablename__ = "ams_thesis_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_thesis_approval_stages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    otp_code: Mapped[str | None] = mapped_column(String(10))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_used: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    stage: Mapped["ThesisApprovalStage"] = relationship("ThesisApprovalStage", back_populates="signatures")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])


class ThesisExternalEvaluation(Base):
    """One assigned External Examiner's evaluation of ONE thesis. Points at the
    existing `ExternalExaminerAssignment` row (created by the already-implemented
    External Examiner Selection module) — never at an email or a fresh identity;
    the examiner, their AMS account, and their assignment to this student are
    entirely owned by that module (`app/models/external_examiner.py`). Unique on
    `(thesis_id, assignment_id)` rather than `assignment_id` alone, so a FUTURE
    Final Thesis can create its own evaluation row against the very same
    assignment without any schema change here. Visible to the student ONLY once
    `status == 'approved'` (DPGS's final approval) — enforced in the endpoint
    layer's serializer, never merely by omitting a frontend link."""
    __tablename__ = "ams_thesis_external_evaluations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_theses.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_assignments.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    report_stored_filename: Mapped[str | None] = mapped_column(String(100))
    report_original_filename: Mapped[str | None] = mapped_column(String(255))
    report_content_type: Mapped[str | None] = mapped_column(String(100))
    report_size_bytes: Mapped[int | None] = mapped_column(Integer)
    report_sha256: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpgs_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dpgs_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("thesis_id", "assignment_id", name="uq_thesis_evaluation_assignment"),)

    thesis: Mapped["Thesis"] = relationship("Thesis", back_populates="evaluations")
    assignment: Mapped["ExternalExaminerAssignment"] = relationship("ExternalExaminerAssignment", foreign_keys=[assignment_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[dpgs_approved_by])


from app.models.user import User, Department  # noqa: E402,F401
from app.models.research import CommitteeMember  # noqa: E402,F401
from app.models.ppw import Ppw  # noqa: E402,F401
from app.models.external_examiner import ExternalExaminerAssignment  # noqa: E402,F401
