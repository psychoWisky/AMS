"""Student Migration: migration applications

Revision ID: 0031_migration_applications
Revises: 0030_registrar_role
Create Date: 2026-09-24

Purely additive: one NEW table and nothing else.

`ams_migration_applications` — one row per Migration application. Modelled on
`ams_withdrawal_requests`'s lightweight shape (status + decided_by/decided_at/
decision_remark), not the heavy approval-cycle/stage/signature tables used by
PPW/Synopsis/External Examiner Selection/Thesis. A partial unique index
(`uq_migration_one_active_per_student`, `WHERE status IN ('draft',
'submitted')`) allows at most one ACTIVE application per student at a time,
while permitting unlimited historical (approved/rejected) rows and a new
application after a rejection or approval — deliberately NOT a one-ever
constraint (mirrors `uq_withdrawal_request_one_active`'s exact shape).

Downgrade drops the table (and with it any Migration data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0031_migration_applications'
down_revision: Union[str, None] = '0030_registrar_role'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        'ams_migration_applications',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('student_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='draft'),

        sa.Column('student_name_snapshot', sa.String(200), nullable=False),
        sa.Column('student_roll_snapshot', sa.String(50), nullable=True),
        sa.Column('degree_snapshot', sa.String(200), nullable=True),
        sa.Column('college_snapshot', sa.String(200), nullable=True),

        sa.Column('registration_no', sa.String(100), nullable=True),
        sa.Column('last_exam_name_and_roll', sa.Text, nullable=True),
        sa.Column('passed_from_institution', sa.Text, nullable=True),
        sa.Column('fee_payment_date', sa.Date, nullable=True),
        sa.Column('migration_reason', sa.Text, nullable=True),
        sa.Column('address', sa.Text, nullable=True),

        sa.Column('receipt_original_filename', sa.String(255), nullable=True),
        sa.Column('receipt_stored_filename', sa.String(100), nullable=True),
        sa.Column('receipt_content_type', sa.String(100), nullable=True),
        sa.Column('receipt_size_bytes', sa.Integer, nullable=True),
        sa.Column('receipt_sha256', sa.String(64), nullable=True),
        sa.Column('receipt_uploaded_at', sa.DateTime(timezone=True), nullable=True),

        sa.Column('decided_by', UUID, sa.ForeignKey('ams_users.id'), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_remark', sa.Text, nullable=True),

        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_ams_migration_applications_student_id', 'ams_migration_applications', ['student_id'])
    op.create_index(
        'uq_migration_one_active_per_student', 'ams_migration_applications', ['student_id'],
        unique=True, postgresql_where=sa.text("status IN ('draft', 'submitted')"),
    )


def downgrade() -> None:
    op.drop_table('ams_migration_applications')
