"""programme department many-to-many

Revision ID: 0009_program_department_m2m
Revises: 0008_ppw_approval_workflow
Create Date: 2026-09-09

Programme <-> Department many-to-many redesign (Phase 1 — additive only, per
this task's explicit instruction). Confirmed business rule: one Programme can
have multiple Departments/specializations, and one Department (e.g. CSE) can
belong to multiple Programmes (e.g. both B.Tech and M.Tech) — the opposite
cardinality from the existing `ams_programs.department_id` column, which this
migration does NOT drop (that is a separate, later "migration 2" per the
task's explicit instruction, only after all application code has stopped
reading it).

Adds:
- ams_program_departments — association table, modeled directly on the
  existing ams_course_availability convention (own `id` PK, not a composite
  PK; UniqueConstraint on the pair; a supporting index on the reverse-lookup
  column). Backfilled from every existing non-null
  ams_programs.department_id value, so the current 5 Programme rows'
  relationships to Agronomy/Veterinary Medicine become association rows
  without duplicating or deleting either Department.
- ams_orientation_candidates.department_id — nullable FK, so an Orientation
  candidate can eventually record both Programme and Department. Left NULL
  for the one existing candidate whose original Department intent is not
  recoverable from the data (not guessed, per instruction).

Does NOT modify ams_programs.department_id, ams_departments, ams_users, any
Course/CourseAvailability/CourseOffering table, any PPW table, or any prior
migration (0001-0008). No existing row in any table is deleted, recreated, or
has its primary key changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0009_program_department_m2m'
down_revision: Union[str, None] = '0008_ppw_approval_workflow'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ams_program_departments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('program_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('department_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['program_id'], ['ams_programs.id'], name='fk_program_departments_program_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['department_id'], ['ams_departments.id'], name='fk_program_departments_department_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('program_id', 'department_id', name='uq_program_department'),
    )
    # Supports "which Programmes is this Department under" (reverse lookup,
    # e.g. the Admin Department-side association panel) — program_id itself
    # is already the leading column of the unique constraint above, so a
    # separate index for the forward direction is unnecessary, mirroring
    # ams_course_availability's own single-supporting-index convention.
    op.create_index('ix_program_departments_department_id', 'ams_program_departments', ['department_id'])

    # Backfill: one association row per existing non-null Program.department_id.
    # Read-then-insert via a plain SQL SELECT so this works regardless of ORM
    # model state at migration time (standard Alembic data-migration practice
    # in this project — see 0002_schema_sync.py's own data-copy steps).
    conn = op.get_bind()
    existing = conn.execute(sa.text(
        "SELECT id, department_id FROM ams_programs WHERE department_id IS NOT NULL"
    )).fetchall()
    for program_id, department_id in existing:
        conn.execute(
            sa.text(
                "INSERT INTO ams_program_departments (id, program_id, department_id, created_at) "
                "VALUES (gen_random_uuid(), :program_id, :department_id, now())"
            ),
            {"program_id": program_id, "department_id": department_id},
        )

    op.add_column('ams_orientation_candidates', sa.Column('department_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_orientation_candidates_department_id', 'ams_orientation_candidates',
        'ams_departments', ['department_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_orientation_candidates_department_id', 'ams_orientation_candidates', type_='foreignkey')
    op.drop_column('ams_orientation_candidates', 'department_id')

    op.drop_index('ix_program_departments_department_id', table_name='ams_program_departments')
    op.drop_table('ams_program_departments')
