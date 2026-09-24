"""Corrective revision: PG25 (Thesis Seminar Certificate) is Major-Advisor-driven, not HOD-driven

Revision ID: 0035_pg25_ma_driven
Revises: 0034_pg25_certificate_i
Create Date: 2026-09-24

The confirmed AVFU workflow is: **the student's Major Advisor generates the Thesis Seminar
Certificate (PG25)** — not the HOD. Migration `0034_pg25_certificate_i` built the PG25 tables
around an HOD-driven, single-row-per-Thesis design (`ams_thesis_seminar_certificates.thesis_id`
UNIQUE, `recorded_by`/`hod_signed_at` = the HOD). This migration corrects that schema to support
the real workflow: Major-Advisor-driven Satisfactory/Unsatisfactory, repeated seminar attempts,
Major Advisor Submit+Sign, Advisory Committee approval, HOD FINAL approval, and Major-Advisor
regeneration after a revert — via an attempt/version-numbered row per Thesis instead of one
fixed row.

**Data-safety check performed before writing this migration**: the live database was queried
directly for existing rows in `ams_thesis_seminar_certificates` /
`ams_thesis_seminar_certificate_signatures` — **zero rows in both tables** (no Thesis has
reached PG25 in this environment yet). `upgrade()` re-verifies this at migration time and
refuses to proceed (raising, not silently dropping data) if either table is non-empty, so this
migration can never destroy real PG25 data — only an already-known-empty schema is replaced.

Both tables are dropped and recreated (children first) rather than incrementally ALTERed,
since there is no data to preserve and a full recreate is far less error-prone than a long
column-by-column ALTER sequence for a schema this different. `ams_thesis_documents` (the actual
generated PG25 PDF files) and every other Thesis/Certificate-I table are completely untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0035_pg25_ma_driven'
down_revision: Union[str, None] = '0034_pg25_certificate_i'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    conn = op.get_bind()
    cert_count = conn.execute(sa.text("SELECT count(*) FROM ams_thesis_seminar_certificates")).scalar_one()
    sig_count = conn.execute(sa.text("SELECT count(*) FROM ams_thesis_seminar_certificate_signatures")).scalar_one()
    if cert_count or sig_count:
        raise RuntimeError(
            f"Refusing to drop non-empty PG25 tables (certificates={cert_count}, signatures={sig_count}). "
            "This migration only supports an empty PG25 schema; write a data-preserving ALTER migration instead."
        )

    op.drop_table('ams_thesis_seminar_certificate_signatures')
    op.drop_table('ams_thesis_seminar_certificates')

    op.create_table(
        'ams_thesis_seminar_certificates',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('thesis_id', UUID, sa.ForeignKey('ams_theses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('attempt_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('version_number', sa.Integer, nullable=False, server_default='1'),
        sa.Column('status', sa.String(20), nullable=False, server_default='generated'),
        sa.Column('seminar_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ma_id', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ma_signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('hod_approved_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('hod_approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reverted_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revert_remark', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('thesis_id', 'attempt_number', 'version_number', name='uq_pg25_attempt_version'),
    )
    op.create_index('ix_ams_thesis_seminar_certificates_thesis_id', 'ams_thesis_seminar_certificates', ['thesis_id'])
    op.create_index(
        'uq_pg25_one_open_per_thesis', 'ams_thesis_seminar_certificates', ['thesis_id'],
        unique=True, postgresql_where=sa.text("status IN ('generated', 'committee_pending', 'hod_pending')"),
    )

    op.create_table(
        'ams_thesis_seminar_certificate_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('certificate_id', UUID, sa.ForeignKey('ams_thesis_seminar_certificates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('committee_member_id', UUID, sa.ForeignKey('ams_committee_members.id', ondelete='SET NULL'), nullable=True),
        sa.Column('faculty_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('role_snapshot', sa.String(30), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('certificate_id', 'faculty_id', name='uq_thesis_pg25_signature_faculty'),
    )
    op.create_index('ix_ams_thesis_pg25_sig_certificate_id', 'ams_thesis_seminar_certificate_signatures', ['certificate_id'])


def downgrade() -> None:
    op.drop_table('ams_thesis_seminar_certificate_signatures')
    op.drop_table('ams_thesis_seminar_certificates')

    op.create_table(
        'ams_thesis_seminar_certificates',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('thesis_id', UUID, sa.ForeignKey('ams_theses.id', ondelete='CASCADE'), nullable=False),
        sa.Column('seminar_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='awaiting_committee'),
        sa.Column('recorded_by', UUID, sa.ForeignKey('ams_users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('hod_signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('thesis_id', name='uq_thesis_seminar_certificate_thesis'),
    )
    op.create_table(
        'ams_thesis_seminar_certificate_signatures',
        sa.Column('id', UUID, primary_key=True),
        sa.Column('certificate_id', UUID, sa.ForeignKey('ams_thesis_seminar_certificates.id', ondelete='CASCADE'), nullable=False),
        sa.Column('committee_member_id', UUID, sa.ForeignKey('ams_committee_members.id', ondelete='SET NULL'), nullable=True),
        sa.Column('faculty_id', UUID, sa.ForeignKey('ams_users.id'), nullable=False),
        sa.Column('role_snapshot', sa.String(30), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('certificate_id', 'faculty_id', name='uq_thesis_pg25_signature_faculty'),
    )
    op.create_index('ix_ams_thesis_pg25_sig_certificate_id', 'ams_thesis_seminar_certificate_signatures', ['certificate_id'])
