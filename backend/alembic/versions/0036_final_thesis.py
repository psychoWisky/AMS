"""Final Thesis: one-Final-per-student index, PG-25(A)/Viva Voce Certificate tables

Revision ID: 0036_final_thesis
Revises: 0035_pg25_ma_driven
Create Date: 2026-09-24

Final Thesis is implemented as a SEPARATE `ams_theses` row (`thesis_type='final'`) for the same
student, never a version/cycle of the Initial Thesis row — every document/cycle/stage/signature
table already keys off `thesis_id`, so this gives Final Thesis independent history for free with
no change to those tables. This migration adds only what's genuinely new:

* `uq_thesis_final_per_student` — a partial unique index mirroring `uq_thesis_initial_per_student`
  exactly, so "at most one Final Thesis per student" is database-enforced, not just checked in
  application code.
* `ams_final_certificates` / `ams_final_certificate_signatures` — the PG-25(A) and Viva Voce
  Certificate workflows (one shared, focused pair of tables, `kind` discriminates), reusing the
  exact attempt/version/revert/regenerate shape already proven by
  `ams_thesis_seminar_certificates` (migration `0035`), extended with the extra sequential
  Incharge Academic Cell / DPGS tail these two documents require that the Initial Thesis PG25
  workflow does not.

Purely additive — no existing table, column, or row is touched. `ams_thesis_documents.document_type`
is a plain `VARCHAR(40)` with no DB-level CHECK/enum constraint, so the four new Final-Thesis-only
document type strings (`pg05a`, `pg05b`, `pg25a_certificate`, `viva_voce_certificate`) need no
schema change at all — they are enforced at the Python/API layer only, exactly like every other
document type in this table.

Data-safety check performed before writing this migration: the live database currently has ZERO
`ams_theses` rows of any type (confirmed directly), so the new partial index has nothing to
validate against and cannot conflict with any existing data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0036_final_thesis'
down_revision: Union[str, None] = '0035_pg25_ma_driven'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_index(
        'uq_thesis_final_per_student', 'ams_theses', ['student_id'],
        unique=True, postgresql_where=sa.text("thesis_type = 'final'"),
    )

    op.create_table(
        'ams_final_certificates',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('thesis_id', UUID, sa.ForeignKey('ams_theses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('attempt_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('version_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('status', sa.String(20), nullable=False, server_default='generated'),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('generated_by_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('student_signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ma_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('ma_acted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('hod_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('hod_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('incharge_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('incharge_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dpgs_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('dpgs_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('pg25a', 'viva')", name='ck_final_certificate_kind'),
        sa.UniqueConstraint('thesis_id', 'kind', 'attempt_number', 'version_number', name='uq_final_certificate_attempt_version'),
    )
    op.create_index('ix_ams_final_certificates_thesis_id', 'ams_final_certificates', ['thesis_id'])
    op.create_index(
        'uq_finalcert_one_open_per_kind', 'ams_final_certificates', ['thesis_id', 'kind'],
        unique=True, postgresql_where=sa.text(
            "status IN ('generated', 'ma_pending', 'committee_pending', 'hod_pending', 'incharge_pending', 'dpgs_pending')"
        ),
    )

    op.create_table(
        'ams_final_certificate_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('certificate_id', UUID, sa.ForeignKey('ams_final_certificates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('committee_member_id', UUID, sa.ForeignKey('ams_committee_members.id', ondelete='SET NULL'), nullable=True),
        sa.Column('faculty_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('role_snapshot', sa.String(30), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('certificate_id', 'faculty_id', name='uq_final_certificate_signature_faculty'),
    )
    op.create_index('ix_ams_final_cert_sig_certificate_id', 'ams_final_certificate_signatures', ['certificate_id'])


def downgrade() -> None:
    op.drop_table('ams_final_certificate_signatures')
    op.drop_table('ams_final_certificates')
    op.drop_index('uq_thesis_final_per_student', table_name='ams_theses')
