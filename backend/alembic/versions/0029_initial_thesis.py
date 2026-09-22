"""Initial Thesis Management: theses, documents, approval cycles/stages, signatures, external evaluations

Revision ID: 0029_initial_thesis
Revises: 0028_librarian_role
Create Date: 2026-09-23

Purely additive: six NEW tables and nothing else. No existing table, column or
row is touched (in particular, `ams_external_examiner_assignments` is only
referenced by a new foreign key, never altered).

* `ams_theses` — one row per Thesis. `thesis_type` is "initial" today ("final"
  is reserved for the future Final Thesis workflow, explicitly out of scope
  this phase). "A student has at most ONE Initial Thesis" is a database-level
  PARTIAL UNIQUE INDEX (`uq_thesis_initial_per_student`, `WHERE thesis_type =
  'initial'`), mirroring `uq_synopsis_first_per_student` exactly. `ppw_id`
  references the source PPW (`ON DELETE SET NULL`); `title_snapshot` is
  copied from `Ppw.research_title` once, at creation.
* `ams_thesis_documents` — immutable uploaded-document versions for any of the
  10 confirmed document categories (thesis file, both plagiarism reports, and
  4 supporting documents), metadata only (bytes on disk under
  UPLOAD_DIR/thesis/<thesis_id>/).
* `ams_thesis_approval_cycles` — one per submission; a partial unique index
  allows at most one ACTIVE cycle per Thesis.
* `ams_thesis_approval_stages` — one per approver per cycle (history). Major
  Advisor's Advisory Committee link is `ON DELETE SET NULL`.
* `ams_thesis_signatures` — OTP records, same shape as `ams_synopsis_signatures`.
* `ams_thesis_external_evaluations` — one row per assigned External Examiner
  (`ams_external_examiner_assignments.id`) evaluating this Thesis; unique on
  (thesis_id, assignment_id) rather than assignment_id alone, so a future
  Final Thesis can reuse the same assignment without a schema change.

Downgrade drops the six tables (and with them any Thesis data). It does NOT
touch `ams_external_examiner_assignments` itself.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0029_initial_thesis'
down_revision: Union[str, None] = '0028_librarian_role'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        'ams_theses',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('thesis_type', sa.String(20), nullable=False, server_default='initial'),
        sa.Column('ppw_id', UUID, sa.ForeignKey('ams_ppw.id', ondelete='SET NULL'), nullable=True),
        sa.Column('title_snapshot', sa.Text, nullable=True),
        sa.Column('plagiarism_student_percent', sa.Float, nullable=True),
        sa.Column('plagiarism_software_name', sa.String(200), nullable=True),
        sa.Column('plagiarism_library_percent', sa.Float, nullable=True),
        sa.Column('abstract', sa.Text, nullable=True),
        sa.Column('status', sa.String(30), nullable=False, server_default='draft'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("thesis_type IN ('initial', 'final')", name='ck_thesis_type'),
    )
    op.create_index('ix_ams_theses_student_id', 'ams_theses', ['student_id'])
    op.create_index(
        'uq_thesis_initial_per_student', 'ams_theses', ['student_id'],
        unique=True, postgresql_where=sa.text("thesis_type = 'initial'"),
    )

    op.create_table(
        'ams_thesis_documents',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('thesis_id', UUID, sa.ForeignKey('ams_theses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('document_type', sa.String(40), nullable=False),
        sa.Column('version_number', sa.Integer, nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=False),
        sa.Column('stored_filename', sa.String(100), nullable=False),
        sa.Column('content_type', sa.String(100), nullable=True),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('is_confidential', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('uploaded_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('thesis_id', 'document_type', 'version_number', name='uq_thesis_document_version'),
    )
    op.create_index('ix_ams_thesis_documents_thesis_id', 'ams_thesis_documents', ['thesis_id'])

    op.create_table(
        'ams_thesis_approval_cycles',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('thesis_id', UUID, sa.ForeignKey('ams_theses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cycle_number', sa.Integer, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('thesis_id', 'cycle_number', name='uq_thesis_cycle_number'),
    )
    op.create_index('ix_ams_thesis_approval_cycles_thesis_id', 'ams_thesis_approval_cycles', ['thesis_id'])
    op.create_index(
        'uq_thesis_one_active_cycle', 'ams_thesis_approval_cycles', ['thesis_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        'ams_thesis_approval_stages',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_thesis_approval_cycles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sequence', sa.Integer, nullable=False),
        sa.Column('stage_type', sa.String(30), nullable=False),
        sa.Column('role_label', sa.String(60), nullable=False),
        sa.Column('committee_member_id', UUID, sa.ForeignKey('ams_committee_members.id', ondelete='SET NULL'), nullable=True),
        sa.Column('assignee_id', UUID, sa.ForeignKey('ams_users.id'), nullable=True),
        sa.Column('approver_id', UUID, sa.ForeignKey('ams_users.id'), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('acted_role', sa.String(30), nullable=True),
        sa.Column('acted_department_id', UUID, sa.ForeignKey('ams_departments.id'), nullable=True),
        sa.Column('acted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('remark', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('cycle_id', 'sequence', name='uq_thesis_stage_sequence'),
    )
    op.create_index('ix_ams_thesis_approval_stages_cycle_id', 'ams_thesis_approval_stages', ['cycle_id'])

    op.create_table(
        'ams_thesis_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('approval_stage_id', UUID, sa.ForeignKey('ams_thesis_approval_stages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('otp_code', sa.String(10), nullable=True),
        sa.Column('otp_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('otp_used', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip_address', sa.String(50), nullable=True),
        sa.Column('user_agent', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ams_thesis_signatures_approval_stage_id', 'ams_thesis_signatures', ['approval_stage_id'])

    op.create_table(
        'ams_thesis_external_evaluations',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('thesis_id', UUID, sa.ForeignKey('ams_theses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('assignment_id', UUID, sa.ForeignKey('ams_external_examiner_assignments.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('report_stored_filename', sa.String(100), nullable=True),
        sa.Column('report_original_filename', sa.String(255), nullable=True),
        sa.Column('report_content_type', sa.String(100), nullable=True),
        sa.Column('report_size_bytes', sa.Integer, nullable=True),
        sa.Column('report_sha256', sa.String(64), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dpgs_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dpgs_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('thesis_id', 'assignment_id', name='uq_thesis_evaluation_assignment'),
    )
    op.create_index('ix_ams_thesis_external_evaluations_thesis_id', 'ams_thesis_external_evaluations', ['thesis_id'])
    op.create_index('ix_ams_thesis_external_evaluations_assignment_id', 'ams_thesis_external_evaluations', ['assignment_id'])


def downgrade() -> None:
    op.drop_table('ams_thesis_external_evaluations')
    op.drop_table('ams_thesis_signatures')
    op.drop_table('ams_thesis_approval_stages')
    op.drop_table('ams_thesis_approval_cycles')
    op.drop_table('ams_thesis_documents')
    op.drop_table('ams_theses')
