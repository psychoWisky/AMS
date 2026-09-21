"""synopsis module: synopses, files, approval cycles/stages, signatures

Revision ID: 0025_synopsis
Revises: 0024_user_academic_year
Create Date: 2026-09-22

Purely additive: five NEW tables and nothing else. No existing table, column or
row is touched.

* `ams_synopses` — one row per Synopsis. `synopsis_type` is "first" today
  ("revise" is reserved for a future Revise Synopsis, linked through
  `parent_synopsis_id`). The rule "a student has at most ONE First Synopsis" is a
  database-level PARTIAL UNIQUE INDEX (`uq_synopsis_first_per_student`,
  `WHERE synopsis_type = 'first'`), so it cannot be bypassed by any request.
* `ams_synopsis_files` — immutable uploaded-PDF versions (metadata only; the bytes
  are stored on disk under UPLOAD_DIR/synopsis/<id>/).
* `ams_synopsis_approval_cycles` — one per submission; carries the submitted file
  and, on the approved cycle only, the frozen final-document snapshot. A partial
  unique index allows at most one ACTIVE cycle per Synopsis.
* `ams_synopsis_approval_stages` — one per approver per cycle (history). The
  Advisory Committee link is `ON DELETE SET NULL` so removing a committee member
  later never destroys or blocks approval history.
* `ams_synopsis_signatures` — OTP records, same shape as `ams_ppw_signatures`.

Downgrade drops the five tables (and with them any Synopsis data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0025_synopsis'
down_revision: Union[str, None] = '0024_user_academic_year'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        'ams_synopses',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('synopsis_type', sa.String(20), nullable=False, server_default='first'),
        sa.Column('parent_synopsis_id', UUID, sa.ForeignKey('ams_synopses.id'), nullable=True),
        sa.Column('title', sa.Text, nullable=True),
        sa.Column('status', sa.String(30), nullable=False, server_default='draft'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("synopsis_type IN ('first', 'revise')", name='ck_synopsis_type'),
    )
    op.create_index('ix_ams_synopses_student_id', 'ams_synopses', ['student_id'])
    op.create_index(
        'uq_synopsis_first_per_student', 'ams_synopses', ['student_id'],
        unique=True, postgresql_where=sa.text("synopsis_type = 'first'"),
    )

    op.create_table(
        'ams_synopsis_files',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('synopsis_id', UUID, sa.ForeignKey('ams_synopses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version_number', sa.Integer, nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=False),
        sa.Column('stored_filename', sa.String(100), nullable=False),
        sa.Column('content_type', sa.String(100), nullable=True),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('page_count', sa.Integer, nullable=False),
        sa.Column('uploaded_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('synopsis_id', 'version_number', name='uq_synopsis_file_version'),
    )
    op.create_index('ix_ams_synopsis_files_synopsis_id', 'ams_synopsis_files', ['synopsis_id'])

    op.create_table(
        'ams_synopsis_approval_cycles',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('synopsis_id', UUID, sa.ForeignKey('ams_synopses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cycle_number', sa.Integer, nullable=False),
        sa.Column('file_id', UUID, sa.ForeignKey('ams_synopsis_files.id'), nullable=False),
        sa.Column('title_snapshot', sa.Text, nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text, nullable=True),
        sa.Column('snapshot', sa.JSON, nullable=True),
        sa.Column('frozen_pdf_filename', sa.String(100), nullable=True),
        sa.Column('frozen_pdf_sha256', sa.String(64), nullable=True),
        sa.Column('frozen_pdf_size', sa.Integer, nullable=True),
        sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('synopsis_id', 'cycle_number', name='uq_synopsis_cycle_number'),
    )
    op.create_index('ix_ams_synopsis_approval_cycles_synopsis_id', 'ams_synopsis_approval_cycles', ['synopsis_id'])
    op.create_index(
        'uq_synopsis_one_active_cycle', 'ams_synopsis_approval_cycles', ['synopsis_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        'ams_synopsis_approval_stages',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_synopsis_approval_cycles.id', ondelete='CASCADE'), nullable=False),
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
        sa.UniqueConstraint('cycle_id', 'sequence', name='uq_synopsis_stage_sequence'),
    )
    op.create_index('ix_ams_synopsis_approval_stages_cycle_id', 'ams_synopsis_approval_stages', ['cycle_id'])

    op.create_table(
        'ams_synopsis_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('approval_stage_id', UUID, sa.ForeignKey('ams_synopsis_approval_stages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('otp_code', sa.String(10), nullable=True),
        sa.Column('otp_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('otp_used', sa.Boolean, nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip_address', sa.String(50), nullable=True),
        sa.Column('user_agent', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ams_synopsis_signatures_approval_stage_id', 'ams_synopsis_signatures', ['approval_stage_id'])


def downgrade() -> None:
    op.drop_table('ams_synopsis_signatures')
    op.drop_table('ams_synopsis_approval_stages')
    op.drop_table('ams_synopsis_approval_cycles')
    op.drop_table('ams_synopsis_files')
    op.drop_table('ams_synopses')
