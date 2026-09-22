"""external examiner selection: reusable examiners, selections, cycles, proposals, stages, results, assignments

Revision ID: 0027_external_examiners
Revises: 0026_vc_examiner_roles
Create Date: 2026-09-23

Purely additive: seven NEW tables, nothing else touched.

* `ams_external_examiners` — the reusable real-world examiner identity, keyed by a
  unique, lowercased email; `user_id` (SET NULL) is filled in only once the
  examiner is actually selected for the first time.
* `ams_external_examiner_selections` — one row per student; `degree_level` is
  snapshotted from `Program.level` at creation and never recomputed.
* `ams_external_examiner_approval_cycles` — one per submission; a partial unique
  index allows at most one ACTIVE cycle per selection; `vc_selection_completed_at`
  makes the VC-selection endpoint idempotent.
* `ams_external_examiner_proposals` — one row per proposed slot (1..3 or 1..5);
  the six `*_snapshot` columns are the immutable historical record of what was
  actually submitted, independent of the reusable examiner's current details.
* `ams_external_examiner_approval_stages` — one row per approver per cycle
  (history); the Advisory Committee link is `ON DELETE SET NULL` so removing a
  committee member later never destroys approval history.
* `ams_external_examiner_signatures` — OTP records, same shape as
  `ams_synopsis_signatures`/`ams_ppw_signatures`.
* `ams_external_examiner_selection_results` — the VC's choice, one row per
  selected proposal, scoped to the cycle (never the examiner).
* `ams_external_examiner_assignments` — one row per (examiner, student)
  assignment; deliberately NO unique constraint on `examiner_id` alone, since an
  examiner may have many assignments across many students over time; unique on
  `selection_result_id` so a retried VC-selection can never create a duplicate.

Downgrade drops all seven tables (and with them any External Examiner data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0027_external_examiners'
down_revision: Union[str, None] = '0026_vc_examiner_roles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        'ams_external_examiners',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('user_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('email', name='uq_external_examiner_email'),
    )
    op.create_index('ix_ams_external_examiners_email', 'ams_external_examiners', ['email'])

    op.create_table(
        'ams_external_examiner_selections',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('degree_level', sa.String(20), nullable=False),
        sa.Column('status', sa.String(30), nullable=False, server_default='draft'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('student_id', name='uq_ext_examiner_selection_student'),
    )
    op.create_index('ix_ams_external_examiner_selections_student_id', 'ams_external_examiner_selections', ['student_id'])

    op.create_table(
        'ams_external_examiner_approval_cycles',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('selection_id', UUID, sa.ForeignKey('ams_external_examiner_selections.id', ondelete='CASCADE'), nullable=False),
        sa.Column('cycle_number', sa.Integer, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text, nullable=True),
        sa.Column('vc_selection_completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('selection_id', 'cycle_number', name='uq_ext_examiner_cycle_number'),
    )
    op.create_index('ix_ams_external_examiner_approval_cycles_selection_id', 'ams_external_examiner_approval_cycles', ['selection_id'])
    op.create_index(
        'uq_ext_examiner_one_active_cycle', 'ams_external_examiner_approval_cycles', ['selection_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        'ams_external_examiner_proposals',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_external_examiner_approval_cycles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('slot_number', sa.Integer, nullable=False),
        sa.Column('examiner_id', UUID, sa.ForeignKey('ams_external_examiners.id', ondelete='SET NULL'), nullable=True),
        sa.Column('name_snapshot', sa.String(200), nullable=False),
        sa.Column('specialization_snapshot', sa.String(300), nullable=False),
        sa.Column('designation_snapshot', sa.String(200), nullable=False),
        sa.Column('email_snapshot', sa.String(255), nullable=False),
        sa.Column('phone_snapshot', sa.String(20), nullable=False),
        sa.Column('institution_snapshot', sa.String(300), nullable=False),
        sa.Column('edited_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('edited_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('cycle_id', 'slot_number', name='uq_ext_examiner_proposal_slot'),
    )
    op.create_index('ix_ams_external_examiner_proposals_cycle_id', 'ams_external_examiner_proposals', ['cycle_id'])
    op.create_index('ix_ams_external_examiner_proposals_examiner_id', 'ams_external_examiner_proposals', ['examiner_id'])

    op.create_table(
        'ams_external_examiner_approval_stages',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_external_examiner_approval_cycles.id', ondelete='CASCADE'), nullable=False),
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
        sa.UniqueConstraint('cycle_id', 'sequence', name='uq_ext_examiner_stage_sequence'),
    )
    op.create_index('ix_ams_external_examiner_approval_stages_cycle_id', 'ams_external_examiner_approval_stages', ['cycle_id'])

    op.create_table(
        'ams_external_examiner_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('approval_stage_id', UUID, sa.ForeignKey('ams_external_examiner_approval_stages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('otp_code', sa.String(10), nullable=True),
        sa.Column('otp_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('otp_used', sa.Boolean, nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip_address', sa.String(50), nullable=True),
        sa.Column('user_agent', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ams_external_examiner_signatures_approval_stage_id', 'ams_external_examiner_signatures', ['approval_stage_id'])

    op.create_table(
        'ams_external_examiner_selection_results',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_external_examiner_approval_cycles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('proposal_id', UUID, sa.ForeignKey('ams_external_examiner_proposals.id'), nullable=False),
        sa.UniqueConstraint('cycle_id', 'proposal_id', name='uq_ext_examiner_result_proposal'),
    )
    op.create_index('ix_ams_external_examiner_selection_results_cycle_id', 'ams_external_examiner_selection_results', ['cycle_id'])

    op.create_table(
        'ams_external_examiner_assignments',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('examiner_id', UUID, sa.ForeignKey('ams_external_examiners.id'), nullable=False),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('selection_result_id', UUID, sa.ForeignKey('ams_external_examiner_selection_results.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('selection_result_id', name='uq_ext_examiner_assignment_result'),
    )
    op.create_index('ix_ams_external_examiner_assignments_examiner_id', 'ams_external_examiner_assignments', ['examiner_id'])


def downgrade() -> None:
    op.drop_table('ams_external_examiner_assignments')
    op.drop_table('ams_external_examiner_selection_results')
    op.drop_table('ams_external_examiner_signatures')
    op.drop_table('ams_external_examiner_approval_stages')
    op.drop_table('ams_external_examiner_proposals')
    op.drop_table('ams_external_examiner_approval_cycles')
    op.drop_table('ams_external_examiner_selections')
    op.drop_table('ams_external_examiners')
