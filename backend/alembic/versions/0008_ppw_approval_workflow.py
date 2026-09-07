"""ppw approval workflow

Revision ID: 0008_ppw_approval_workflow
Revises: 0007_course_availability
Create Date: 2026-09-04

PPW Phase 2 — Advisory Committee Approval Workflow. Adds three new,
PPW-specific tables:

- ams_ppw_approval_cycles: one submission/approval attempt per PPW. A partial
  unique index enforces at most one 'active' cycle per PPW at a time. Prior
  cycles are never deleted (audit requirement) — only superseded.
- ams_ppw_approval_stages: one specific person's stage within a cycle
  (major_advisor / committee_member / hod), FK'd to the EXACT
  ams_committee_members row for the first two stage types (never a bare role
  match), so only that exact person can authorize the action.
- ams_ppw_signatures: PPW-specific OTP/signature record, deliberately
  SEPARATE from ams_digital_signatures (which is Gradesheet-specific via its
  FK to ams_approval_stages) per explicit instruction not to couple PPW to
  Gradesheet.

Also widens ams_ppw.status from VARCHAR(20) to VARCHAR(30): the Phase 1
column was sized for "draft"/"submitted" only, but the Phase 2 state
"major_advisor_pending" is 22 characters, exceeding the Phase 1 width. No
data loss — this is a widen-only ALTER.

Does not modify 0001-0007, ams_courses, ams_course_offerings,
ams_course_availability, ams_advisory_committees, ams_committee_members,
ams_grade_sheets, ams_approval_stages, or ams_digital_signatures.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0008_ppw_approval_workflow'
down_revision: Union[str, None] = '0007_course_availability'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('ams_ppw', 'status', existing_type=sa.String(length=20), type_=sa.String(length=30))

    op.create_table(
        'ams_ppw_approval_cycles',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('ppw_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('cycle_number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ppw_id'], ['ams_ppw.id'], name='fk_ppw_cycle_ppw_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ppw_id', 'cycle_number', name='uq_ppw_cycle_number'),
    )
    op.create_index('ix_ppw_cycle_ppw_id', 'ams_ppw_approval_cycles', ['ppw_id'])
    # Enforces "no two active cycles" at the database level, not just in
    # application code (Section 13's explicit concurrency requirement).
    op.create_index(
        'uq_ppw_cycle_one_active', 'ams_ppw_approval_cycles', ['ppw_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        'ams_ppw_approval_stages',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('cycle_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('stage_type', sa.String(length=30), nullable=False),
        sa.Column('committee_member_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('approver_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('remark', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['cycle_id'], ['ams_ppw_approval_cycles.id'], name='fk_ppw_stage_cycle_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['committee_member_id'], ['ams_committee_members.id'], name='fk_ppw_stage_committee_member_id'),
        sa.ForeignKeyConstraint(['approver_id'], ['ams_users.id'], name='fk_ppw_stage_approver_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cycle_id', 'committee_member_id', name='uq_ppw_stage_committee_member'),
    )
    op.create_index('ix_ppw_stage_cycle_id', 'ams_ppw_approval_stages', ['cycle_id'])
    # Supports "find my pending stage" lookups (approval OTP/approve/revert
    # endpoints) keyed by the exact committee member / approver.
    op.create_index('ix_ppw_stage_committee_member_id', 'ams_ppw_approval_stages', ['committee_member_id'])
    op.create_index('ix_ppw_stage_approver_id', 'ams_ppw_approval_stages', ['approver_id'])

    op.create_table(
        'ams_ppw_signatures',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('approval_stage_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('otp_code', sa.String(length=10), nullable=True),
        sa.Column('otp_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('otp_used', sa.Boolean(), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip_address', sa.String(length=50), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['approval_stage_id'], ['ams_ppw_approval_stages.id'], name='fk_ppw_signature_stage_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['ams_users.id'], name='fk_ppw_signature_user_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ppw_signature_stage_id', 'ams_ppw_signatures', ['approval_stage_id'])
    op.create_index('ix_ppw_signature_user_id', 'ams_ppw_signatures', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_ppw_signature_user_id', table_name='ams_ppw_signatures')
    op.drop_index('ix_ppw_signature_stage_id', table_name='ams_ppw_signatures')
    op.drop_table('ams_ppw_signatures')

    op.drop_index('ix_ppw_stage_approver_id', table_name='ams_ppw_approval_stages')
    op.drop_index('ix_ppw_stage_committee_member_id', table_name='ams_ppw_approval_stages')
    op.drop_index('ix_ppw_stage_cycle_id', table_name='ams_ppw_approval_stages')
    op.drop_table('ams_ppw_approval_stages')

    op.drop_index('uq_ppw_cycle_one_active', table_name='ams_ppw_approval_cycles')
    op.drop_index('ix_ppw_cycle_ppw_id', table_name='ams_ppw_approval_cycles')
    op.drop_table('ams_ppw_approval_cycles')

    op.alter_column('ams_ppw', 'status', existing_type=sa.String(length=30), type_=sa.String(length=20))
