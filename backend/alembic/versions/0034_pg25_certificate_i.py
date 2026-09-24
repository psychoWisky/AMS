"""PG25 (Thesis Seminar Certificate) workflow + Certificate I cycle-scoping + Annexure-IV removal

Revision ID: 0034_pg25_certificate_i
Revises: 0033_pr_field_correction
Create Date: 2026-09-24

Confirmed Initial Thesis document requirements (this revision):

* **Annexure-IV removed.** It is no longer a required Initial Thesis document. Before writing
  this migration, the live database was inspected directly: `select count(*) from
  ams_thesis_documents where document_type = 'annexure_iv'` returned **0 rows** (no Thesis had
  even been created yet in this environment). There is therefore no legitimate data to
  preserve or migrate for this removal. `document_type` has never been a database-level
  CHECK-constrained/enum column (plain `VARCHAR(40)`), so no column/constraint change is
  needed to "remove" it — the removal is enforced at the Python/API layer
  (`THESIS_DOCUMENT_TYPES` in `app/models/thesis.py` no longer lists it; the upload/download/
  serialization endpoints in `app/api/v1/endpoints/thesis.py` no longer recognize it). This
  migration performs NO DELETE against `ams_thesis_documents` — if an `annexure_iv` row were
  ever found in a real environment before running this migration, it must be inspected and
  handled manually first; this migration deliberately does not assume it is safe to delete.

* **`ams_thesis_documents.cycle_id`** (new, nullable) — which `ThesisApprovalCycle` a generated
  document version belongs to. Populated only for `certificate_i_pg27` going forward, so a
  Certificate I generated for an earlier (reverted) submission cycle can never be mistaken for
  satisfying a later resubmission's requirement (Section 21 of the confirmed rules).

* **`ams_thesis_seminar_certificates`** / **`ams_thesis_seminar_certificate_signatures`** — the
  new, focused PG25 workflow (Sections 8-17): one certificate row per Thesis (unique on
  `thesis_id` — the seminar happens once, and this is also the idempotency guarantee against a
  duplicate "Satisfactory" click), and one signature row per required Advisory Committee member
  (snapshotted from the real `ams_committee_members` at HOD-Satisfactory time).

Purely additive — no existing table is altered destructively, no existing row is touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0034_pg25_certificate_i'
down_revision: Union[str, None] = '0033_pr_field_correction'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        'ams_thesis_documents',
        sa.Column('cycle_id', UUID, sa.ForeignKey('ams_thesis_approval_cycles.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('ix_ams_thesis_documents_cycle_id', 'ams_thesis_documents', ['cycle_id'])

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


def downgrade() -> None:
    op.drop_table('ams_thesis_seminar_certificate_signatures')
    op.drop_table('ams_thesis_seminar_certificates')
    op.drop_index('ix_ams_thesis_documents_cycle_id', table_name='ams_thesis_documents')
    op.drop_column('ams_thesis_documents', 'cycle_id')
