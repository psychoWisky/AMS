"""Student Progress Report: reports, approval cycles/stages, signatures, proceedings

Revision ID: 0032_progress_report
Revises: 0031_migration_applications
Create Date: 2026-09-25

Purely additive: five NEW tables and nothing else. No existing table, column or
row is touched.

* `ams_progress_reports` — one row per student per (academic year, semester),
  enforced by a plain (non-partial) UNIQUE constraint
  (`uq_progress_report_student_semester`) — unlike Migration's "one ACTIVE at
  a time" partial index, a Progress Report is reused/resubmitted in place
  after a revert, so exactly one row may ever exist for a given semester,
  regardless of status.
* `ams_progress_report_approval_cycles` — one per submission; a partial
  unique index allows at most one ACTIVE cycle per report. Unlike Synopsis, a
  cycle here can survive multiple partial (mid-chain) reverts — only a Major
  Advisor revert or DPGS approval ends it.
* `ams_progress_report_approval_stages` — one row per approver-attempt per
  cycle (history; a decided/superseded row is never mutated — a "fresh
  round" after a partial revert is a new row with a higher sequence).
* `ams_progress_report_signatures` — OTP records, same shape as
  `ams_synopsis_signatures`.
* `ams_progress_report_proceedings` — the Major Advisor's uploaded Proceedings
  PDF, versioned per cycle, same shape as `ams_synopsis_files`.

Downgrade drops the five tables (and with them any Progress Report data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0032_progress_report'
down_revision: Union[str, None] = '0031_migration_applications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        'ams_progress_reports',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('academic_year_id', UUID, sa.ForeignKey('ams_academic_calendars.id'), nullable=False),
        sa.Column('semester_id', UUID, sa.ForeignKey('ams_semesters.id'), nullable=False),
        sa.Column('status', sa.String(40), nullable=False, server_default='draft'),

        sa.Column('student_name_snapshot', sa.String(200), nullable=False),
        sa.Column('student_roll_snapshot', sa.String(50), nullable=True),
        sa.Column('program_snapshot', sa.String(200), nullable=True),

        sa.Column('period_from', sa.Date, nullable=True),
        sa.Column('period_to', sa.Date, nullable=True),
        sa.Column('semester_completed', sa.Integer, nullable=True),

        sa.Column('total_courses', sa.Integer, nullable=True),
        sa.Column('total_credits_programme', sa.Integer, nullable=True),
        sa.Column('current_semester_courses', sa.Integer, nullable=True),
        sa.Column('current_semester_credits', sa.Integer, nullable=True),
        sa.Column('courses_completed_till_date', sa.Integer, nullable=True),
        sa.Column('credits_completed_till_date', sa.Integer, nullable=True),

        sa.Column('research_title', sa.Text, nullable=True),
        sa.Column('research_progress', sa.Text, nullable=True),
        sa.Column('leave_availed', sa.Text, nullable=True),
        sa.Column('fellowship_stipend', sa.Text, nullable=True),

        sa.Column('advisor_completion_expected', sa.Boolean, nullable=True),
        sa.Column('advisor_reason', sa.Text, nullable=True),

        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('student_id', 'academic_year_id', 'semester_id', name='uq_progress_report_student_semester'),
    )
    op.create_index('ix_ams_progress_reports_student_id', 'ams_progress_reports', ['student_id'])

    op.create_table(
        'ams_progress_report_approval_cycles',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('report_id', UUID, sa.ForeignKey('ams_progress_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cycle_number', sa.Integer, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('report_id', 'cycle_number', name='uq_progress_report_cycle_number'),
    )
    op.create_index('ix_ams_progress_report_approval_cycles_report_id', 'ams_progress_report_approval_cycles', ['report_id'])
    op.create_index(
        'uq_progress_report_one_active_cycle', 'ams_progress_report_approval_cycles', ['report_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        'ams_progress_report_approval_stages',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_progress_report_approval_cycles.id', ondelete='CASCADE'), nullable=False),
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
        sa.UniqueConstraint('cycle_id', 'sequence', name='uq_progress_report_stage_sequence'),
    )
    op.create_index('ix_ams_progress_report_approval_stages_cycle_id', 'ams_progress_report_approval_stages', ['cycle_id'])

    op.create_table(
        'ams_progress_report_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('approval_stage_id', UUID, sa.ForeignKey('ams_progress_report_approval_stages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('otp_code', sa.String(10), nullable=True),
        sa.Column('otp_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('otp_used', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip_address', sa.String(50), nullable=True),
        sa.Column('user_agent', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ams_progress_report_signatures_approval_stage_id', 'ams_progress_report_signatures', ['approval_stage_id'])

    op.create_table(
        'ams_progress_report_proceedings',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_progress_report_approval_cycles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version_number', sa.Integer, nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=False),
        sa.Column('stored_filename', sa.String(100), nullable=False),
        sa.Column('content_type', sa.String(100), nullable=True),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('uploaded_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('cycle_id', 'version_number', name='uq_progress_report_proceedings_version'),
    )
    op.create_index('ix_ams_progress_report_proceedings_cycle_id', 'ams_progress_report_proceedings', ['cycle_id'])


def downgrade() -> None:
    op.drop_table('ams_progress_report_proceedings')
    op.drop_table('ams_progress_report_signatures')
    op.drop_table('ams_progress_report_approval_stages')
    op.drop_table('ams_progress_report_approval_cycles')
    op.drop_table('ams_progress_reports')
