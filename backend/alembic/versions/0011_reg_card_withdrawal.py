"""registration card lock state + withdrawal requests

Revision ID: 0011_reg_card_withdrawal
Revises: 0010_orientation_fields
Create Date: 2026-09-13

Course Registration / Registration Card / withdrawal task. Two additive
changes only:

1. `ams_course_registrations.card_submitted_at` (nullable DateTime) — marks
   the moment the student explicitly submitted the Registration Card,
   distinct from the pre-existing `submitted_at` (which marks when the
   registration ROW was first created, i.e. the student's first course
   selection for the semester). The new "all courses Course-Teacher approved
   but Registration Card not yet submitted" state itself
   (`CourseRegistration.stage == "card_pending"`) requires NO schema change —
   `stage` is already a plain, unconstrained String(30) column, exactly so
   later phases could add values without a migration touching the column
   type (see 0006/0008's own docstrings for this same reasoning).

2. New table `ams_withdrawal_requests` — one row per approved-course
   withdrawal request a student makes (reason required), decided by the
   Course Teacher (approve -> the linked StudentEnrollment becomes
   "withdrawn"; reject -> the enrollment is left untouched). A partial unique
   index enforces at most one PENDING request per enrollment at a time,
   mirroring 0008's proven `uq_ppw_cycle_one_active` pattern exactly. Rows
   are never deleted (full audit trail, including rejected attempts).

Does not modify 0001-0010, ams_course_offerings, ams_courses,
ams_advisory_committees, ams_committee_members, any PPW table, or any
existing column's type/nullability on ams_course_registrations or
ams_student_enrollments.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0011_reg_card_withdrawal'
down_revision: Union[str, None] = '0010_orientation_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ams_course_registrations',
        sa.Column('card_submitted_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        'ams_withdrawal_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('enrollment_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('requested_by', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('decided_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_remark', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['enrollment_id'], ['ams_student_enrollments.id'], name='fk_withdrawal_request_enrollment_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['requested_by'], ['ams_users.id'], name='fk_withdrawal_request_requested_by'),
        sa.ForeignKeyConstraint(['decided_by'], ['ams_users.id'], name='fk_withdrawal_request_decided_by'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_withdrawal_request_enrollment_id', 'ams_withdrawal_requests', ['enrollment_id'])
    # At most one PENDING request per enrollment (Section 11's explicit
    # "cannot create duplicate active withdrawal requests" requirement),
    # enforced at the database level, not just in application code.
    op.create_index(
        'uq_withdrawal_request_one_active', 'ams_withdrawal_requests', ['enrollment_id'],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index('uq_withdrawal_request_one_active', table_name='ams_withdrawal_requests')
    op.drop_index('ix_withdrawal_request_enrollment_id', table_name='ams_withdrawal_requests')
    op.drop_table('ams_withdrawal_requests')
    op.drop_column('ams_course_registrations', 'card_submitted_at')
