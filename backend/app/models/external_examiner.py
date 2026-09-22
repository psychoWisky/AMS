"""External Examiner Selection (BUSINESS_LOGIC.md section AC).

Modelled directly on Synopsis's approval-cycle design (`app/models/synopsis.py`)
— one cycle per submission, one stage per approver — with one structural
difference required by the confirmed reuse rule: the examiner is a REUSABLE
identity, separate from both the per-student proposal (a historical snapshot)
and the AMS login account (created once, reused forever after):

    ExternalExaminer (reusable identity: email + optional User account)
            |
            +-- ExternalExaminerProposal.examiner_id (many proposals, many students, over time)
            |
            +-- ExternalExaminerAssignment.examiner_id (many assignments, over time)

Nothing here ever writes a CURRENT profile field (name/specialization/designation/
phone/institution) back onto `ExternalExaminer` — those live ONLY as an immutable
snapshot on each `ExternalExaminerProposal` row, so a later re-proposal of the same
person, or a correction by the Incharge, never rewrites an already-approved
student's historical record (confirmed requirement).

Approval workflow: Major Advisor -> HOD -> Incharge Academic Cell -> DPGS -> VC.
Every revert (from any of the four post-Major-Advisor stages) returns to the
Major Advisor — there is no per-stage alternate destination and no student
involvement anywhere in this module.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# draft -> major_advisor_pending -> hod_pending -> incharge_pending -> dpgs_pending
# -> vc_pending -> approved ; "reverted" is reachable from any pending state past
# the Major Advisor and always returns there (confirmed rule — no exceptions).
EXTERNAL_EXAMINER_SELECTION_STATUSES = (
    "draft", "major_advisor_pending", "hod_pending", "incharge_pending",
    "dpgs_pending", "vc_pending", "approved", "reverted",
)
EXTERNAL_EXAMINER_CYCLE_STATUSES = ("active", "approved", "reverted")
EXTERNAL_EXAMINER_STAGE_TYPES = ("major_advisor", "hod", "incharge_academic_cell", "dpgs", "vc")
EXTERNAL_EXAMINER_STAGE_STATUSES = ("pending", "approved", "reverted", "cancelled")
EXTERNAL_EXAMINER_ASSIGNMENT_STATUSES = ("active", "completed")

# PG -> 3 proposed, VC selects 1 ; PhD -> 5 proposed, VC selects 2 (confirmed, backend-enforced).
REQUIRED_PROPOSAL_COUNT = {"PG": 3, "PhD": 5}
REQUIRED_SELECTION_COUNT = {"PG": 1, "PhD": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ExternalExaminer(Base):
    """The reusable real-world examiner identity — one row per distinct person,
    identified by their (normalized, lowercased) email, regardless of how many
    students they are ever proposed/selected for. `user_id` is set once, the first
    time this examiner is actually selected by a VC (not at proposal time — a
    proposed-but-never-selected examiner never gets an AMS account). `ON DELETE
    SET NULL` so this identity, and every proposal/assignment that references it,
    survives even in the hypothetical future case of the linked account being
    removed (today the only lifecycle operation anywhere in AMS is deactivation,
    never deletion)."""
    __tablename__ = "ams_external_examiners"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped["User | None"] = relationship("User", foreign_keys=[user_id])


class ExternalExaminerSelection(Base):
    """One per student. `degree_level` is snapshotted from `Program.level` at
    creation (never recomputed later), which is what fixes the required proposal/
    selection counts for this student's workflow forever, independent of any
    later programme change."""
    __tablename__ = "ams_external_examiner_selections"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), unique=True, index=True)
    degree_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    cycles: Mapped[list["ExternalExaminerApprovalCycle"]] = relationship(
        "ExternalExaminerApprovalCycle", back_populates="selection", cascade="all, delete-orphan",
        order_by="ExternalExaminerApprovalCycle.cycle_number",
    )


class ExternalExaminerApprovalCycle(Base):
    """One submission/approval attempt. `vc_selection_completed_at` is set exactly
    once, in the same transaction as the `ExternalExaminerSelectionResult` rows —
    checked (`IS NULL`) before writing, so a retried VC-selection request is
    idempotent rather than creating a second result set or a second account."""
    __tablename__ = "ams_external_examiner_approval_cycles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    selection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_selections.id", ondelete="CASCADE"), index=True)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revert_remark: Mapped[str | None] = mapped_column(Text)
    vc_selection_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("selection_id", "cycle_number", name="uq_ext_examiner_cycle_number"),
        # At most one in-progress cycle per selection.
        Index("uq_ext_examiner_one_active_cycle", "selection_id", unique=True, postgresql_where=text("status = 'active'")),
    )

    selection: Mapped["ExternalExaminerSelection"] = relationship("ExternalExaminerSelection", back_populates="cycles")
    proposals: Mapped[list["ExternalExaminerProposal"]] = relationship(
        "ExternalExaminerProposal", back_populates="cycle", cascade="all, delete-orphan", order_by="ExternalExaminerProposal.slot_number",
    )
    stages: Mapped[list["ExternalExaminerApprovalStage"]] = relationship(
        "ExternalExaminerApprovalStage", back_populates="cycle", cascade="all, delete-orphan", order_by="ExternalExaminerApprovalStage.sequence",
    )
    results: Mapped[list["ExternalExaminerSelectionResult"]] = relationship(
        "ExternalExaminerSelectionResult", back_populates="cycle", cascade="all, delete-orphan",
    )


class ExternalExaminerProposal(Base):
    """One proposed slot (1..3 for PG, 1..5 for PhD) within a cycle. The six
    `*_snapshot` columns are the historical record of what was actually submitted/
    approved for THIS student — never rewritten because the reusable examiner's
    current details change later. `examiner_id` is resolved (or re-resolved, if the
    Incharge edits the email) against `ExternalExaminer.email`; it may legitimately
    be NULL if no matching identity exists yet (one is created only when this
    proposal is later selected by the VC)."""
    __tablename__ = "ams_external_examiner_proposals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_approval_cycles.id", ondelete="CASCADE"), index=True)
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    examiner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiners.id", ondelete="SET NULL"))

    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    specialization_snapshot: Mapped[str] = mapped_column(String(300), nullable=False)
    designation_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    email_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    institution_snapshot: Mapped[str] = mapped_column(String(300), nullable=False)

    edited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("cycle_id", "slot_number", name="uq_ext_examiner_proposal_slot"),)

    cycle: Mapped["ExternalExaminerApprovalCycle"] = relationship("ExternalExaminerApprovalCycle", back_populates="proposals")
    examiner: Mapped["ExternalExaminer | None"] = relationship("ExternalExaminer", foreign_keys=[examiner_id])
    editor: Mapped["User | None"] = relationship("User", foreign_keys=[edited_by])


class ExternalExaminerApprovalStage(Base):
    """One approver's stage within a cycle. Major Advisor is bound to the exact
    `CommitteeMember` row (SET NULL if that membership is later removed, so
    history survives); HOD/Incharge/DPGS/VC carry no person up front — whoever
    holds the role (and, for HOD, the student's department) in their ACTIVE
    session may act, re-checked live on every call, exactly PPW's/Synopsis's
    `_resolve_my_stage` pattern."""
    __tablename__ = "ams_external_examiner_approval_stages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_approval_cycles.id", ondelete="CASCADE"), index=True)
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

    __table_args__ = (UniqueConstraint("cycle_id", "sequence", name="uq_ext_examiner_stage_sequence"),)

    cycle: Mapped["ExternalExaminerApprovalCycle"] = relationship("ExternalExaminerApprovalCycle", back_populates="stages")
    committee_member: Mapped["CommitteeMember | None"] = relationship("CommitteeMember", foreign_keys=[committee_member_id])
    assignee: Mapped["User | None"] = relationship("User", foreign_keys=[assignee_id])
    approver: Mapped["User | None"] = relationship("User", foreign_keys=[approver_id])
    signatures: Mapped[list["ExternalExaminerSignature"]] = relationship(
        "ExternalExaminerSignature", back_populates="stage", cascade="all, delete-orphan",
    )


class ExternalExaminerSignature(Base):
    """OTP record for one signing attempt on one stage — same shape and semantics
    as `SynopsisSignature`/`PpwSignature` (single-use, expiring, scoped to this
    stage and this user). The Incharge Academic Cell stage never creates one (it
    is a workflow approval only, never a signature)."""
    __tablename__ = "ams_external_examiner_signatures"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_stage_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_approval_stages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    otp_code: Mapped[str | None] = mapped_column(String(10))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_used: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    stage: Mapped["ExternalExaminerApprovalStage"] = relationship("ExternalExaminerApprovalStage", back_populates="signatures")
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])


class ExternalExaminerSelectionResult(Base):
    """The VC's choice — one row per selected proposal, scoped to the cycle it was
    selected in (never the examiner). Confidentiality is enforced entirely by the
    serializer (who may see this table's contents), never by the schema."""
    __tablename__ = "ams_external_examiner_selection_results"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_approval_cycles.id", ondelete="CASCADE"), index=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_proposals.id"), nullable=False)

    __table_args__ = (UniqueConstraint("cycle_id", "proposal_id", name="uq_ext_examiner_result_proposal"),)

    cycle: Mapped["ExternalExaminerApprovalCycle"] = relationship("ExternalExaminerApprovalCycle", back_populates="results")
    proposal: Mapped["ExternalExaminerProposal"] = relationship("ExternalExaminerProposal", foreign_keys=[proposal_id])


class ExternalExaminerAssignment(Base):
    """One student's use of one reusable examiner — created once per selection
    result, the moment the VC confirms it. Deliberately NO unique constraint on
    `examiner_id` alone (an examiner may have any number of assignments, across
    any number of students, over time); `selection_result_id` IS unique, so a
    retried VC-selection request can never create a second assignment for the
    same result. `status` starts `active` and this module never transitions it to
    `completed` — that belongs to the future Thesis module. An account may only
    be deactivated once it has no `active` assignment."""
    __tablename__ = "ams_external_examiner_assignments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    examiner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiners.id"), index=True, nullable=False)
    student_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), nullable=False)
    selection_result_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_external_examiner_selection_results.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    examiner: Mapped["ExternalExaminer"] = relationship("ExternalExaminer", foreign_keys=[examiner_id])
    student: Mapped["User"] = relationship("User", foreign_keys=[student_id])
    selection_result: Mapped["ExternalExaminerSelectionResult"] = relationship("ExternalExaminerSelectionResult", foreign_keys=[selection_result_id])


from app.models.user import User, Department  # noqa: E402,F401
from app.models.research import CommitteeMember  # noqa: E402,F401
