"""Gradesheet -> Approval -> CoE compilation -> Student Result workflow schema

Revision ID: 0042_gradesheet_workflow
Revises: 0041_seed_coe_role_data
Create Date: 2026-09-27

IMPORTANT repository condition: the original grading tables
(`ams_grade_sheets`, `ams_grade_entries`, `ams_approval_stages`,
`ams_digital_signatures`) were created by `seed.py`'s `Base.metadata.create_all()`
and are NOT created by any earlier migration (`0001_baseline` does zero DDL). A
database initialised from the current models (`create_all`) therefore already has
every column/table below. This migration is written to be safe in BOTH situations:
every step first checks the live schema and only adds what is missing, so it
upgrades an existing development database and is a harmless no-op on a fresh
`create_all` database. Nothing here drops or rewrites existing rows.

What changes (all additive):
  * `ams_grade_sheets`  + gradesheet_type, related_sheet_id, total/pass marks for
    theory & practical, finalized_at; partial unique index (exactly one NEW sheet
    per offering).
  * `ams_grade_entries` + attendance_percent, theory_total, practical_total,
    marks_percent.
  * NEW `ams_gradesheet_components`, `ams_grade_entry_marks` (configurable
    components + per-student marks), `ams_gradesheet_cycles`,
    `ams_gradesheet_stages` (approval cycles/signatures),
    `ams_student_semester_results`, `ams_student_semester_result_courses`.

The legacy internal/external columns and the old `ams_approval_stages` /
`ams_digital_signatures` tables are retained untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0042_gradesheet_workflow'
down_revision: Union[str, None] = '0041_seed_coe_role_data'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _tables(conn) -> set:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _add_column(conn, table: str, column: sa.Column) -> None:
    if column.name not in _columns(conn, table):
        op.add_column(table, column)


def upgrade() -> None:
    conn = op.get_bind()

    # ── ams_grade_sheets ──────────────────────────────────────────────────────
    _add_column(conn, "ams_grade_sheets", sa.Column("gradesheet_type", sa.String(20), nullable=False, server_default="new"))
    _add_column(conn, "ams_grade_sheets", sa.Column("related_sheet_id", UUID, sa.ForeignKey("ams_grade_sheets.id", ondelete="SET NULL"), nullable=True))
    for col in ("total_theory_marks", "theory_pass_marks", "total_practical_marks", "practical_pass_marks"):
        _add_column(conn, "ams_grade_sheets", sa.Column(col, sa.Numeric(6, 2), nullable=False, server_default="0"))
    _add_column(conn, "ams_grade_sheets", sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_gradesheet_new_per_offering "
        "ON ams_grade_sheets (offering_id) WHERE gradesheet_type = 'new'"
    )

    # ── ams_grade_entries ─────────────────────────────────────────────────────
    _add_column(conn, "ams_grade_entries", sa.Column("attendance_percent", sa.Numeric(5, 2), nullable=True))
    _add_column(conn, "ams_grade_entries", sa.Column("theory_total", sa.Numeric(7, 2), nullable=True))
    _add_column(conn, "ams_grade_entries", sa.Column("practical_total", sa.Numeric(7, 2), nullable=True))
    _add_column(conn, "ams_grade_entries", sa.Column("marks_percent", sa.Numeric(5, 2), nullable=True))

    existing = _tables(conn)

    if "ams_gradesheet_components" not in existing:
        op.create_table(
            "ams_gradesheet_components",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("sheet_id", UUID, sa.ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("code", sa.String(20), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("component_type", sa.String(12), nullable=False),
            sa.Column("max_marks", sa.Numeric(6, 2), nullable=False),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
            sa.UniqueConstraint("sheet_id", "code", name="uq_gradesheet_component"),
        )
        op.create_index("ix_ams_gradesheet_components_sheet_id", "ams_gradesheet_components", ["sheet_id"])

    if "ams_grade_entry_marks" not in existing:
        op.create_table(
            "ams_grade_entry_marks",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("entry_id", UUID, sa.ForeignKey("ams_grade_entries.id", ondelete="CASCADE"), nullable=False),
            sa.Column("component_id", UUID, sa.ForeignKey("ams_gradesheet_components.id", ondelete="CASCADE"), nullable=False),
            sa.Column("marks", sa.Numeric(6, 2), nullable=True),
            sa.UniqueConstraint("entry_id", "component_id", name="uq_grade_entry_mark"),
        )
        op.create_index("ix_ams_grade_entry_marks_entry_id", "ams_grade_entry_marks", ["entry_id"])

    if "ams_gradesheet_cycles" not in existing:
        op.create_table(
            "ams_gradesheet_cycles",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("sheet_id", UUID, sa.ForeignKey("ams_grade_sheets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("cycle_number", sa.Integer, nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            sa.Column("submitted_by", UUID, sa.ForeignKey("ams_users.id"), nullable=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("instructor_deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("sheet_id", "cycle_number", name="uq_gradesheet_cycle"),
        )
        op.create_index("ix_ams_gradesheet_cycles_sheet_id", "ams_gradesheet_cycles", ["sheet_id"])

    if "ams_gradesheet_stages" not in existing:
        op.create_table(
            "ams_gradesheet_stages",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("cycle_id", UUID, sa.ForeignKey("ams_gradesheet_cycles.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sequence", sa.Integer, nullable=False),
            sa.Column("stage_type", sa.String(20), nullable=False),
            sa.Column("assigned_user_id", UUID, sa.ForeignKey("ams_users.id"), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("approver_id", UUID, sa.ForeignKey("ams_users.id"), nullable=True),
            sa.Column("acted_role", sa.String(50), nullable=True),
            sa.Column("acted_department_id", UUID, sa.ForeignKey("ams_departments.id"), nullable=True),
            sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("remark", sa.Text, nullable=True),
            sa.Column("ip_address", sa.String(50), nullable=True),
            sa.Column("is_deemed", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ams_gradesheet_stages_cycle_id", "ams_gradesheet_stages", ["cycle_id"])

    if "ams_student_semester_results" not in existing:
        op.create_table(
            "ams_student_semester_results",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("student_id", UUID, sa.ForeignKey("ams_users.id"), nullable=False),
            sa.Column("semester_id", UUID, sa.ForeignKey("ams_semesters.id"), nullable=False),
            sa.Column("calendar_id", UUID, sa.ForeignKey("ams_academic_calendars.id"), nullable=True),
            sa.Column("result_status", sa.String(30), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="compiled"),
            sa.Column("gpa", sa.Numeric(12, 6), nullable=True),
            sa.Column("total_credits", sa.Numeric(8, 2), nullable=False, server_default="0"),
            sa.Column("total_grade_points", sa.Numeric(10, 3), nullable=False, server_default="0"),
            sa.Column("total_credit_points", sa.Numeric(12, 4), nullable=False, server_default="0"),
            sa.Column("student_name", sa.String(300), nullable=True),
            sa.Column("student_roll", sa.String(50), nullable=True),
            sa.Column("college_name", sa.String(200), nullable=True),
            sa.Column("department_name", sa.String(200), nullable=True),
            sa.Column("degree_name", sa.String(200), nullable=True),
            sa.Column("semester_name", sa.String(100), nullable=True),
            sa.Column("academic_year", sa.String(20), nullable=True),
            sa.Column("exam_label", sa.String(100), nullable=True),
            sa.Column("compiled_by", UUID, sa.ForeignKey("ams_users.id"), nullable=True),
            sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("published_by", UUID, sa.ForeignKey("ams_users.id"), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("version", sa.Integer, nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("student_id", "semester_id", name="uq_student_semester_result"),
        )
        op.create_index("ix_ams_student_semester_results_student_id", "ams_student_semester_results", ["student_id"])
        op.create_index("ix_ams_student_semester_results_semester_id", "ams_student_semester_results", ["semester_id"])

    if "ams_student_semester_result_courses" not in existing:
        op.create_table(
            "ams_student_semester_result_courses",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("result_id", UUID, sa.ForeignKey("ams_student_semester_results.id", ondelete="CASCADE"), nullable=False),
            sa.Column("offering_id", UUID, sa.ForeignKey("ams_course_offerings.id"), nullable=False),
            sa.Column("course_id", UUID, sa.ForeignKey("ams_courses.id"), nullable=False),
            sa.Column("gradesheet_id", UUID, sa.ForeignKey("ams_grade_sheets.id", ondelete="SET NULL"), nullable=True),
            sa.Column("entry_id", UUID, sa.ForeignKey("ams_grade_entries.id", ondelete="SET NULL"), nullable=True),
            sa.Column("course_number", sa.String(50), nullable=False),
            sa.Column("course_title", sa.String(300), nullable=False),
            sa.Column("department_name", sa.String(200), nullable=True),
            sa.Column("credit_structure", sa.String(20), nullable=True),
            sa.Column("credits", sa.Numeric(6, 2), nullable=False, server_default="0"),
            sa.Column("grade_letter", sa.String(5), nullable=False),
            sa.Column("grade_points", sa.Numeric(8, 3), nullable=False),
            sa.Column("credit_points", sa.Numeric(10, 3), nullable=False),
            sa.Column("marks_percent", sa.Numeric(5, 2), nullable=True),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
            sa.UniqueConstraint("result_id", "offering_id", name="uq_result_course_offering"),
        )
        op.create_index("ix_ams_student_semester_result_courses_result_id", "ams_student_semester_result_courses", ["result_id"])


def downgrade() -> None:
    conn = op.get_bind()
    existing = _tables(conn)
    for table in (
        "ams_student_semester_result_courses", "ams_student_semester_results",
        "ams_gradesheet_stages", "ams_gradesheet_cycles",
        "ams_grade_entry_marks", "ams_gradesheet_components",
    ):
        if table in existing:
            op.drop_table(table)
    op.execute("DROP INDEX IF EXISTS uq_gradesheet_new_per_offering")
    for col in ("marks_percent", "practical_total", "theory_total", "attendance_percent"):
        if col in _columns(conn, "ams_grade_entries"):
            op.drop_column("ams_grade_entries", col)
    for col in (
        "finalized_at", "practical_pass_marks", "total_practical_marks", "theory_pass_marks",
        "total_theory_marks", "related_sheet_id", "gradesheet_type",
    ):
        if col in _columns(conn, "ams_grade_sheets"):
            op.drop_column("ams_grade_sheets", col)
