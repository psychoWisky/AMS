"""Comprehensive Examination module (Application, Viva, Viva Report, PhD External Panel, External Viva Report)

Revision ID: 0038_comprehensive_exam
Revises: 0037_research_course_instructor
Create Date: 2026-09-26

Purely additive — ten new tables, nothing existing touched. See `app/models/comprehensive_exam.py`
for the full design rationale. Summary:

* `ams_comprehensive_exam_applications` — one per student (unique), snapshots `degree_level` and
  the Major/Minor completed-credit totals at submission time (never recomputed later).
* `ams_comprehensive_exam_application_courses` — frozen per-course rows (code/title/credits) used
  to build the generated application document's course table.
* `ams_comprehensive_exam_vivas` — one row per scheduled viva attempt; `viva_date` is the
  HOD-fixed, authoritative date printed on the report regardless of when the result is recorded.
* `ams_comprehensive_exam_viva_reports` / `..._viva_report_signatures` — modelled directly on
  `ams_final_certificates`/`ams_final_certificate_signatures` (PG-25(A)/Viva Voce Certificate).
* PhD External Panel: `ams_comp_exam_external_panel_selections/cycles/proposals/results` — a
  PARALLEL table family to `ams_external_examiner_*` (Thesis), never sharing rows with it, for
  three reasons: (1) `ams_external_examiner_selections.student_id` is globally unique, so reusing
  it would block the same student from ever having a real Thesis External Examiner Selection;
  (2) the panel size (5 proposed / 1 selected) differs from Thesis's PhD rule (5 proposed / 2
  selected); (3) the existing Thesis flow creates a real AMS login account for the selected
  examiner — this module's confirmed rule is the opposite (no account, ever). The reusable
  `ams_external_examiners` identity table itself IS reused directly (FK only, no new columns).
* `ams_comprehensive_exam_external_viva_reports` / `..._external_report_signatures` — the signed,
  physically-executed external report, versioned like `ams_thesis_documents` (never overwritten).

No existing table, column, or row is modified.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0038_comprehensive_exam'
down_revision: Union[str, None] = '0037_research_course_instructor'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        'ams_comprehensive_exam_applications',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False, unique=True),
        sa.Column('application_type', sa.String(60), nullable=False, server_default='holding_comprehensive_examination'),
        sa.Column('degree_level', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='generated'),
        sa.Column('major_credits_required', sa.Integer, nullable=False, server_default='20'),
        sa.Column('minor_credits_required', sa.Integer, nullable=False, server_default='8'),
        sa.Column('major_credits_completed', sa.Integer, nullable=False),
        sa.Column('minor_credits_completed', sa.Integer, nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('student_signed_at', sa.DateTime(timezone=True)),
        sa.Column('ma_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('ma_acted_at', sa.DateTime(timezone=True)),
        sa.Column('hod_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('hod_approved_at', sa.DateTime(timezone=True)),
        sa.Column('incharge_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('incharge_approved_at', sa.DateTime(timezone=True)),
        sa.Column('dpgs_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('dpgs_approved_at', sa.DateTime(timezone=True)),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('reverted_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('reverted_at', sa.DateTime(timezone=True)),
        sa.Column('revert_remark', sa.Text),
        sa.Column('stored_filename', sa.String(100)),
        sa.Column('original_filename', sa.String(255)),
        sa.Column('content_type', sa.String(100)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("degree_level IN ('PG', 'PhD')", name='ck_comp_exam_application_degree_level'),
    )
    op.create_index('ix_comp_exam_app_student', 'ams_comprehensive_exam_applications', ['student_id'])

    op.create_table(
        'ams_comprehensive_exam_application_courses',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('application_id', UUID, sa.ForeignKey('ams_comprehensive_exam_applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('classification', sa.String(10), nullable=False),
        sa.Column('course_number', sa.String(50), nullable=False),
        sa.Column('course_title', sa.String(300)),
        sa.Column('credits', sa.Integer, nullable=False),
        sa.Column('order_index', sa.Integer, nullable=False, server_default='0'),
        sa.CheckConstraint("classification IN ('major', 'minor')", name='ck_comp_exam_app_course_classification'),
    )
    op.create_index('ix_comp_exam_app_course_app', 'ams_comprehensive_exam_application_courses', ['application_id'])

    op.create_table(
        'ams_comprehensive_exam_vivas',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('application_id', UUID, sa.ForeignKey('ams_comprehensive_exam_applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('attempt_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('viva_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scheduled_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('result', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('result_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('result_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('application_id', 'attempt_number', name='uq_comp_exam_viva_attempt'),
    )
    op.create_index('ix_comp_exam_viva_app', 'ams_comprehensive_exam_vivas', ['application_id'])
    op.create_index(
        'uq_comp_exam_viva_one_pending', 'ams_comprehensive_exam_vivas', ['application_id'],
        unique=True, postgresql_where=sa.text("result = 'pending'"),
    )

    op.create_table(
        'ams_comprehensive_exam_viva_reports',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('viva_id', UUID, sa.ForeignKey('ams_comprehensive_exam_vivas.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('status', sa.String(20), nullable=False, server_default='generated'),
        sa.Column('generated_by_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ma_acted_at', sa.DateTime(timezone=True)),
        sa.Column('hod_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('hod_approved_at', sa.DateTime(timezone=True)),
        sa.Column('incharge_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('incharge_approved_at', sa.DateTime(timezone=True)),
        sa.Column('dpgs_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('dpgs_approved_at', sa.DateTime(timezone=True)),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('reverted_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('reverted_at', sa.DateTime(timezone=True)),
        sa.Column('revert_remark', sa.Text),
        sa.Column('stored_filename', sa.String(100)),
        sa.Column('original_filename', sa.String(255)),
        sa.Column('content_type', sa.String(100)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('viva_id', 'version_number', name='uq_comp_exam_viva_report_version'),
    )
    op.create_index('ix_comp_exam_viva_report_viva', 'ams_comprehensive_exam_viva_reports', ['viva_id'])
    op.create_index(
        'uq_comp_exam_viva_report_one_open', 'ams_comprehensive_exam_viva_reports', ['viva_id'],
        unique=True, postgresql_where=sa.text(
            "status IN ('generated', 'ma_pending', 'committee_pending', 'hod_pending', 'incharge_pending', 'dpgs_pending')"
        ),
    )

    op.create_table(
        'ams_comprehensive_exam_viva_report_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('report_id', UUID, sa.ForeignKey('ams_comprehensive_exam_viva_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('committee_member_id', UUID, sa.ForeignKey('ams_committee_members.id', ondelete='SET NULL')),
        sa.Column('faculty_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('role_snapshot', sa.String(30), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('signed_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('report_id', 'faculty_id', name='uq_comp_exam_viva_report_sig_faculty'),
    )
    op.create_index('ix_comp_exam_viva_report_sig_report', 'ams_comprehensive_exam_viva_report_signatures', ['report_id'])

    op.create_table(
        'ams_comp_exam_external_panel_selections',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('application_id', UUID, sa.ForeignKey('ams_comprehensive_exam_applications.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='draft'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_comp_exam_panel_sel_app', 'ams_comp_exam_external_panel_selections', ['application_id'])

    op.create_table(
        'ams_comp_exam_external_panel_cycles',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('selection_id', UUID, sa.ForeignKey('ams_comp_exam_external_panel_selections.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cycle_number', sa.Integer, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('ma_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('reverted_at', sa.DateTime(timezone=True)),
        sa.Column('revert_remark', sa.Text),
        sa.Column('hod_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('hod_approved_at', sa.DateTime(timezone=True)),
        sa.Column('incharge_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('incharge_approved_at', sa.DateTime(timezone=True)),
        sa.Column('dpgs_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('dpgs_approved_at', sa.DateTime(timezone=True)),
        sa.Column('vc_selection_completed_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('selection_id', 'cycle_number', name='uq_comp_exam_panel_cycle_number'),
    )
    op.create_index('ix_comp_exam_panel_cycle_sel', 'ams_comp_exam_external_panel_cycles', ['selection_id'])
    op.create_index(
        'uq_comp_exam_panel_one_active_cycle', 'ams_comp_exam_external_panel_cycles', ['selection_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        'ams_comp_exam_external_panel_proposals',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_comp_exam_external_panel_cycles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('slot_number', sa.Integer, nullable=False),
        sa.Column('examiner_id', UUID, sa.ForeignKey('ams_external_examiners.id', ondelete='SET NULL')),
        sa.Column('name_snapshot', sa.String(200), nullable=False),
        sa.Column('specialization_snapshot', sa.String(300), nullable=False),
        sa.Column('designation_snapshot', sa.String(200), nullable=False),
        sa.Column('email_snapshot', sa.String(255), nullable=False),
        sa.Column('phone_snapshot', sa.String(20), nullable=False),
        sa.Column('institution_snapshot', sa.String(300), nullable=False),
        sa.UniqueConstraint('cycle_id', 'slot_number', name='uq_comp_exam_panel_proposal_slot'),
    )
    op.create_index('ix_comp_exam_panel_proposal_cycle', 'ams_comp_exam_external_panel_proposals', ['cycle_id'])

    op.create_table(
        'ams_comp_exam_external_panel_results',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_comp_exam_external_panel_cycles.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('proposal_id', UUID, sa.ForeignKey('ams_comp_exam_external_panel_proposals.id'), nullable=False),
        sa.Column('selected_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('selected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('email_sent_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_comp_exam_panel_result_cycle', 'ams_comp_exam_external_panel_results', ['cycle_id'])

    op.create_table(
        'ams_comprehensive_exam_external_viva_reports',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('application_id', UUID, sa.ForeignKey('ams_comprehensive_exam_applications.id', ondelete='CASCADE'), nullable=False),
        sa.Column('version_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('status', sa.String(20), nullable=False, server_default='uploaded'),
        sa.Column('stored_filename', sa.String(100), nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=False),
        sa.Column('content_type', sa.String(100)),
        sa.Column('size_bytes', sa.Integer, nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('uploaded_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True)),
        sa.Column('hod_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('hod_approved_at', sa.DateTime(timezone=True)),
        sa.Column('incharge_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('incharge_approved_at', sa.DateTime(timezone=True)),
        sa.Column('dpgs_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('dpgs_approved_at', sa.DateTime(timezone=True)),
        sa.Column('approved_at', sa.DateTime(timezone=True)),
        sa.Column('reverted_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL')),
        sa.Column('reverted_at', sa.DateTime(timezone=True)),
        sa.Column('revert_remark', sa.Text),
        sa.UniqueConstraint('application_id', 'version_number', name='uq_comp_exam_ext_report_version'),
    )
    op.create_index('ix_comp_exam_ext_report_app', 'ams_comprehensive_exam_external_viva_reports', ['application_id'])
    op.create_index(
        'uq_comp_exam_ext_report_one_open', 'ams_comprehensive_exam_external_viva_reports', ['application_id'],
        unique=True, postgresql_where=sa.text(
            "status IN ('uploaded', 'committee_pending', 'hod_pending', 'incharge_pending', 'dpgs_pending')"
        ),
    )

    op.create_table(
        'ams_comprehensive_exam_external_report_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('report_id', UUID, sa.ForeignKey('ams_comprehensive_exam_external_viva_reports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('committee_member_id', UUID, sa.ForeignKey('ams_committee_members.id', ondelete='SET NULL')),
        sa.Column('faculty_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('role_snapshot', sa.String(30), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('signed_at', sa.DateTime(timezone=True)),
        sa.UniqueConstraint('report_id', 'faculty_id', name='uq_comp_exam_ext_report_sig_faculty'),
    )
    op.create_index('ix_comp_exam_ext_report_sig_report', 'ams_comprehensive_exam_external_report_signatures', ['report_id'])


def downgrade() -> None:
    op.drop_table('ams_comprehensive_exam_external_report_signatures')
    op.drop_table('ams_comprehensive_exam_external_viva_reports')
    op.drop_table('ams_comp_exam_external_panel_results')
    op.drop_table('ams_comp_exam_external_panel_proposals')
    op.drop_table('ams_comp_exam_external_panel_cycles')
    op.drop_table('ams_comp_exam_external_panel_selections')
    op.drop_table('ams_comprehensive_exam_viva_report_signatures')
    op.drop_table('ams_comprehensive_exam_viva_reports')
    op.drop_table('ams_comprehensive_exam_vivas')
    op.drop_table('ams_comprehensive_exam_application_courses')
    op.drop_table('ams_comprehensive_exam_applications')
