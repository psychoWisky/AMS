"""schema sync: reconcile live DB with current ORM models

Revision ID: 0002_schema_sync
Revises: 0001_baseline
Create Date: 2026-08-31

Brings the live `ams_db` schema up to date with the SQLAlchemy models as of
this revision. Every operation here was verified against a prior read-only
audit of the live database plus a discarded `alembic revision --autogenerate`
draft used only for inspection (never applied).

Scope, in order:
  1. ams_departments.stream                          (nullable, no backfill —
     AVFU has not confirmed the stream taxonomy; existing rows stay NULL)
  2. ams_courses.category / credit_type               (nullable, no backfill)
  3. ams_course_offerings.department_id                (nullable UUID FK)
  4. Backfill department_id from ams_courses, plus a development-data-only
     assignment of the AGR601 offering to the AGRO department (AGR601's own
     course row has NULL department_id in seed data; this is dummy data, not
     an AVFU business rule)
  5. Redefine uq_offering_section to include department_id — only after the
     backfill, so the constraint is never rebuilt against all-NULL data
  6. Six nullable ams_users profile columns
  7. ams_users.must_change_password — NOT NULL, added with a temporary
     PostgreSQL server_default so the 14 existing rows are populated safely,
     then the server_default is dropped so the Python-side model default
     (`default=False`) remains the only source of truth going forward; the
     column itself and its now-fixed `false` values on existing rows are
     NOT removed
  8. Advisory Committee: revert_remark, reverted_at, CommitteeMember.remark
     (all nullable). Also widens ams_advisory_committees.status from
     VARCHAR(20) to VARCHAR(30) — a genuine additional drift discovered by
     autogenerate during this migration's preparation, not present in the
     original authoritative list: the model's current stage vocabulary
     includes "major_advisor_pending" (22 characters), which does not fit
     in the live column's current VARCHAR(20) limit. This reconciles the
     live schema with the ORM's declared String(30); it does not change
     any application behavior or invent new schema.
  9. Create ams_course_registrations (new table; no backfill needed)
 10. ams_student_enrollments.registration_id (nullable UUID FK, added only
     after ams_course_registrations exists; existing 72 rows are left NULL)
 11. Create ams_orientation_candidates (new table; no backfill needed)

No NOT NULL is introduced where the ORM declares nullable. No new indexes,
constraints, or cascade behavior beyond what the ORM already declares.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0002_schema_sync'
down_revision: Union[str, None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


AGRO_DEPARTMENT_ID = 'cb0d1c79-4abb-4634-a6f4-e845c17135e5'  # development-data only, not an AVFU rule


def upgrade() -> None:
    # 1. ams_departments.stream — nullable, no backfill (AVFU confirmation pending)
    op.add_column('ams_departments', sa.Column('stream', sa.String(length=20), nullable=True))

    # 2. ams_courses.category / credit_type — nullable, no backfill
    op.add_column('ams_courses', sa.Column('category', sa.String(length=30), nullable=True))
    op.add_column('ams_courses', sa.Column('credit_type', sa.String(length=20), nullable=True))

    # 3. ams_course_offerings.department_id — nullable UUID FK
    op.add_column('ams_course_offerings', sa.Column('department_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_course_offerings_department_id',
        'ams_course_offerings', 'ams_departments',
        ['department_id'], ['id'],
    )

    # 4. Backfill department_id from the owning course, then the AGR601 dev-data assignment
    op.execute("""
        UPDATE ams_course_offerings co
        SET department_id = c.department_id
        FROM ams_courses c
        WHERE co.course_id = c.id
          AND c.department_id IS NOT NULL
    """)
    op.execute(f"""
        UPDATE ams_course_offerings
        SET department_id = '{AGRO_DEPARTMENT_ID}'
        WHERE course_id = (
            SELECT id FROM ams_courses WHERE course_number = 'AGR601'
        )
    """)

    # 5. Redefine uq_offering_section — only after the backfill above
    op.drop_constraint('uq_offering_section', 'ams_course_offerings', type_='unique')
    op.create_unique_constraint(
        'uq_offering_section', 'ams_course_offerings',
        ['semester_id', 'course_id', 'department_id', 'section'],
    )

    # 6. ams_users nullable profile columns
    op.add_column('ams_users', sa.Column('date_of_birth', sa.Date(), nullable=True))
    op.add_column('ams_users', sa.Column('gender', sa.String(length=20), nullable=True))
    op.add_column('ams_users', sa.Column('blood_group', sa.String(length=10), nullable=True))
    op.add_column('ams_users', sa.Column('father_name', sa.String(length=200), nullable=True))
    op.add_column('ams_users', sa.Column('abc_id', sa.String(length=50), nullable=True))
    op.add_column('ams_users', sa.Column('address', sa.Text(), nullable=True))

    # 7. ams_users.must_change_password — NOT NULL via a temporary server default,
    #    dropped afterward so the Python-side default remains the single source of truth
    op.add_column(
        'ams_users',
        sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column('ams_users', 'must_change_password', server_default=None)

    # 8. Advisory Committee fields + the discovered status length fix
    op.add_column('ams_advisory_committees', sa.Column('revert_remark', sa.Text(), nullable=True))
    op.add_column('ams_advisory_committees', sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True))
    op.alter_column(
        'ams_advisory_committees', 'status',
        existing_type=sa.VARCHAR(length=20),
        type_=sa.String(length=30),
        existing_nullable=False,
    )
    op.add_column('ams_committee_members', sa.Column('remark', sa.Text(), nullable=True))

    # 9. ams_course_registrations — new table
    op.create_table(
        'ams_course_registrations',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('student_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('semester_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('calendar_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('stage', sa.String(length=30), nullable=False),
        sa.Column('revert_remark', sa.Text(), nullable=True),
        sa.Column('reverted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['ams_users.id'], name='fk_course_registrations_student_id'),
        sa.ForeignKeyConstraint(['semester_id'], ['ams_semesters.id'], name='fk_course_registrations_semester_id'),
        sa.ForeignKeyConstraint(['calendar_id'], ['ams_academic_calendars.id'], name='fk_course_registrations_calendar_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('student_id', 'semester_id', name='uq_course_registration'),
    )

    # 10. ams_student_enrollments.registration_id — nullable UUID FK, requires step 9 first
    op.add_column('ams_student_enrollments', sa.Column('registration_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        'fk_student_enrollments_registration_id',
        'ams_student_enrollments', 'ams_course_registrations',
        ['registration_id'], ['id'],
    )

    # 11. ams_orientation_candidates — new table
    op.create_table(
        'ams_orientation_candidates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('personal_email', sa.String(length=255), nullable=False),
        sa.Column('mobile', sa.String(length=20), nullable=True),
        sa.Column('entrance_exam_name', sa.String(length=200), nullable=True),
        sa.Column('entrance_exam_marks', sa.Float(), nullable=True),
        sa.Column('academic_year', sa.String(length=20), nullable=False),
        sa.Column('program_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attendance_status', sa.String(length=20), nullable=False),
        sa.Column('selection_status', sa.String(length=20), nullable=False),
        sa.Column('credential_status', sa.String(length=20), nullable=False),
        sa.Column('roll_no', sa.String(length=50), nullable=True),
        sa.Column('student_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('admission_application_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['program_id'], ['ams_programs.id'], name='fk_orientation_candidates_program_id'),
        sa.ForeignKeyConstraint(['student_user_id'], ['ams_users.id'], name='fk_orientation_candidates_student_user_id'),
        sa.ForeignKeyConstraint(['admission_application_id'], ['ams_admission_applications.id'], name='fk_orientation_candidates_admission_application_id'),
        sa.ForeignKeyConstraint(['created_by'], ['ams_users.id'], name='fk_orientation_candidates_created_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('personal_email', 'academic_year', 'program_id', name='uq_orientation_candidate_intake'),
        sa.UniqueConstraint('roll_no', name='uq_orientation_candidates_roll_no'),
        sa.UniqueConstraint('student_user_id', name='uq_orientation_candidates_student_user_id'),
    )


def downgrade() -> None:
    # NOTE: destructive on a populated database — dropping these tables/columns
    # discards any real data entered since this migration was applied. This
    # downgrade is provided for completeness and for use only against an
    # empty/fresh environment. It is not executed as part of this task.

    # 11. ams_orientation_candidates
    op.drop_table('ams_orientation_candidates')

    # 10. ams_student_enrollments.registration_id
    op.drop_constraint('fk_student_enrollments_registration_id', 'ams_student_enrollments', type_='foreignkey')
    op.drop_column('ams_student_enrollments', 'registration_id')

    # 9. ams_course_registrations
    op.drop_table('ams_course_registrations')

    # 8. Advisory Committee fields + status length revert
    op.drop_column('ams_committee_members', 'remark')
    op.alter_column(
        'ams_advisory_committees', 'status',
        existing_type=sa.String(length=30),
        type_=sa.VARCHAR(length=20),
        existing_nullable=False,
    )
    op.drop_column('ams_advisory_committees', 'reverted_at')
    op.drop_column('ams_advisory_committees', 'revert_remark')

    # 7. must_change_password
    op.drop_column('ams_users', 'must_change_password')

    # 6. ams_users nullable profile columns
    op.drop_column('ams_users', 'address')
    op.drop_column('ams_users', 'abc_id')
    op.drop_column('ams_users', 'father_name')
    op.drop_column('ams_users', 'blood_group')
    op.drop_column('ams_users', 'gender')
    op.drop_column('ams_users', 'date_of_birth')

    # 5. Restore original uq_offering_section
    op.drop_constraint('uq_offering_section', 'ams_course_offerings', type_='unique')
    op.create_unique_constraint(
        'uq_offering_section', 'ams_course_offerings',
        ['semester_id', 'course_id', 'section'],
    )

    # 3. ams_course_offerings.department_id (backfill in step 4 has no separate undo — dropping the column removes it)
    op.drop_constraint('fk_course_offerings_department_id', 'ams_course_offerings', type_='foreignkey')
    op.drop_column('ams_course_offerings', 'department_id')

    # 2. ams_courses
    op.drop_column('ams_courses', 'credit_type')
    op.drop_column('ams_courses', 'category')

    # 1. ams_departments.stream
    op.drop_column('ams_departments', 'stream')
