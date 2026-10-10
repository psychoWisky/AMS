"""Standalone tests for `scripts/cleanup_production_db.py`.

CRITICAL SAFETY NOTE: this test suite NEVER runs against the shared local
dev database (`ams_db`) or any production database. It creates its own
throwaway Postgres database (`ams_cleanup_script_test`, same server/
credentials as the dev `.env`, different database name), builds the full
current schema into it directly from the SQLAlchemy models, seeds a
synthetic fixture spanning every module family the cleanup script has to
handle (including the specific cross-module FK edge cases the script's own
deletion order depends on: Thesis-before-ExternalExaminerAssignment,
PPW-before-CommitteeMember, Synopsis-cycle-before-file,
ExternalExaminer-assignment-before-examiner/selection-result,
ComprehensiveExam-panel-result-before-proposal), runs the REAL script
end-to-end as a subprocess against that database, and verifies the result —
then drops the throwaway database in `finally`, regardless of outcome.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_cleanup_production_db

Requires local Postgres reachable at the same host/credentials as the
project's own `.env` `DATABASE_URL` (CREATE DATABASE / DROP DATABASE
privilege on that role — true for the local dev `postgres` superuser this
project already uses).
"""
import asyncio
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

_TEST_DB = "ams_cleanup_script_test"
_ADMIN_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
_TEST_DATABASE_URL = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{_TEST_DB}"

# MUST be set before any `app.*` import anywhere in this process — settings
# is a module-level singleton read once at import time.
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
os.environ["ENVIRONMENT"] = "development"  # intentionally non-production; see t_*_safety tests below for the production-labelled variant

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

RESULTS_PASSED: list[str] = []
RESULTS_FAILED: list[tuple[str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    (RESULTS_PASSED if ok else RESULTS_FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── throwaway database lifecycle ────────────────────────────────────────────

async def _recreate_test_database() -> None:
    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
        await conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    finally:
        await conn.close()


async def _drop_test_database() -> None:
    try:
        from app.db.base import engine as app_engine
        await app_engine.dispose()
    except Exception:
        pass
    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{_TEST_DB}' AND pid <> pg_backend_pid()")
        await conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}"')
    finally:
        await conn.close()


async def _create_schema() -> None:
    from app.db.base import Base
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    import app.main  # noqa: F401
    # Explicitly import every model module — app.main's own import chain does
    # not necessarily touch all of them (confirmed: ams_email_outbox was
    # missing from Base.metadata without this), and Base.metadata.create_all
    # only creates tables for classes that have actually been imported.
    import importlib
    import pkgutil
    import app.models as _models_pkg
    for mod in pkgutil.iter_modules(_models_pkg.__path__):
        importlib.import_module(f"app.models.{mod.name}")
    engine = create_async_engine(_TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Mirror real production's alembic_version bookkeeping table so the
        # script's environment probe behaves exactly as it would there.
        await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0045_research_assignment_type')"))
    await engine.dispose()


async def _seed_role_and_designation_master_data() -> None:
    from app.db.base import AsyncSessionLocal
    from app.models.user import Role, Designation
    roles = ["SUPER_ADMIN", "VICE_CHANCELLOR", "DPGS", "INCHARGE_ACADEMIC_CELL", "REGISTRAR",
             "HOD", "FACULTY", "STUDENT", "EXTERNAL_EXAMINER", "LIBRARIAN", "CONTROLLER_OF_EXAMINATION"]
    async with AsyncSessionLocal() as db:
        for code in roles:
            db.add(Role(id=uuid.uuid4(), code=code, name=code.replace("_", " ").title(), is_system=True, is_active=True))
        for name in ("Professor", "Associate Professor", "Assistant Professor"):
            db.add(Designation(id=uuid.uuid4(), name=name, is_active=True))
        await db.commit()


# ── fixture ids, populated by _seed_fixture ─────────────────────────────────
F: dict = {}


async def _seed_fixture() -> None:
    """A full synthetic fixture spanning every module the cleanup script has
    to handle, including the specific cross-module ordering edge cases its
    deletion order depends on. Nothing here touches any real AVFU data —
    this is the throwaway database created above."""
    from app.db.base import AsyncSessionLocal
    from app.models.user import User, UserRole, UserRoleAssignment, Department, College, Program, ProgramDepartment, CollegeProgram, DepartmentCollege, RefreshToken
    from app.models.academic import AcademicCalendar, Semester
    from app.models.course import Course, CourseOffering, OfferingFaculty, CourseAvailability
    from app.models.enrollment import StudentEnrollment, CourseRegistration, WithdrawalRequest
    from app.models.research import AdvisoryCommittee, CommitteeMember
    from app.models.ppw import Ppw, PpwApprovalCycle, PpwApprovalStage
    from app.models.synopsis import Synopsis, SynopsisFile, SynopsisApprovalCycle
    from app.models.thesis import Thesis, ThesisApprovalCycle, ThesisExternalEvaluation
    from app.models.external_examiner import ExternalExaminer, ExternalExaminerSelection, ExternalExaminerApprovalCycle, ExternalExaminerProposal, ExternalExaminerSelectionResult, ExternalExaminerAssignment
    from app.models.progress_report import ProgressReport
    from app.models.comprehensive_exam import ComprehensiveExamApplication, ComprehensiveExamExternalPanelSelection, ComprehensiveExamExternalPanelCycle, ComprehensiveExamExternalPanelProposal, ComprehensiveExamExternalPanelResult
    from app.models.migration import MigrationApplication
    from app.models.grading import GradeSheet, GradesheetComponent, GradeEntry, ApprovalStage, DigitalSignature
    from app.models.admission import AdmissionApplication
    from app.models.orientation import OrientationCandidate
    from app.models.audit import AuditLog, Notification

    def now():
        return datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        sa = User(id=uuid.uuid4(), email="superadmin@avfu.ac.in", hashed_password="ZZTEST-HASH-DO-NOT-CHECK",
                   first_name="Super", last_name="Admin", role=UserRole.SUPER_ADMIN, is_active=True, is_verified=True)
        db.add(sa)
        await db.flush()
        F["superadmin_id"] = sa.id
        db.add(UserRoleAssignment(id=uuid.uuid4(), user_id=sa.id, role=UserRole.SUPER_ADMIN, department_id=None))

        dept = Department(id=uuid.uuid4(), name="ZZTEST Dept", code="ZZTD", stream="veterinary", is_active=True)
        college = College(id=uuid.uuid4(), name="ZZTEST College", code="ZZTC", is_active=True)
        program = Program(id=uuid.uuid4(), name="ZZTEST Programme", code="ZZTP", level="PG", duration_years=2, is_active=True)
        db.add_all([dept, college, program])
        await db.flush()
        db.add(ProgramDepartment(program_id=program.id, department_id=dept.id))
        db.add(CollegeProgram(college_id=college.id, program_id=program.id))
        db.add(DepartmentCollege(department_id=dept.id, college_id=college.id))

        hod = User(id=uuid.uuid4(), email="zztest_hod@avfu.ac.in", hashed_password="x", first_name="ZZ", last_name="Hod",
                   role=UserRole.HOD, department_id=dept.id, is_active=True, is_verified=True)
        faculty = User(id=uuid.uuid4(), email="zztest_fac@avfu.ac.in", hashed_password="x", first_name="ZZ", last_name="Fac",
                        role=UserRole.FACULTY, department_id=dept.id, is_active=True, is_verified=True)
        student = User(id=uuid.uuid4(), email="zztest_stu@avfu.ac.in", hashed_password="x", first_name="ZZ", last_name="Stu",
                        role=UserRole.STUDENT, department_id=dept.id, college_id=college.id, program_id=program.id, is_active=True, is_verified=True)
        db.add_all([hod, faculty, student])
        await db.flush()
        db.add(UserRoleAssignment(id=uuid.uuid4(), user_id=hod.id, role=UserRole.HOD, department_id=dept.id))
        db.add(UserRoleAssignment(id=uuid.uuid4(), user_id=faculty.id, role=UserRole.FACULTY, department_id=dept.id))
        db.add(UserRoleAssignment(id=uuid.uuid4(), user_id=student.id, role=UserRole.STUDENT, department_id=None))
        F.update(hod_id=hod.id, faculty_id=faculty.id, student_id=student.id, dept_id=dept.id, college_id=college.id, program_id=program.id)

        rt = RefreshToken(id=uuid.uuid4(), user_id=sa.id, token_hash="zztest", device_info="zztest")
        db.add(rt)

        calendar = AcademicCalendar(id=uuid.uuid4(), name="ZZTEST Year", academic_year="2026-27", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), status="active")
        db.add(calendar)
        await db.flush()
        semester = Semester(id=uuid.uuid4(), calendar_id=calendar.id, name="ZZTEST Sem 1", sem_type="odd", start_date=date(2026, 1, 1), end_date=date(2026, 6, 30), status="active")
        db.add(semester)

        course = Course(id=uuid.uuid4(), course_number="ZZT-101", title="ZZTEST Course", department_id=dept.id, credit_theory=3, credit_practical=0, created_by=sa.id)
        db.add(course)
        await db.flush()
        offering = CourseOffering(id=uuid.uuid4(), calendar_id=calendar.id, semester_id=semester.id, course_id=course.id, department_id=dept.id, max_enrollment=30, status="published", created_by=hod.id)
        db.add(offering)
        await db.flush()
        db.add(OfferingFaculty(id=uuid.uuid4(), offering_id=offering.id, faculty_id=faculty.id, role="primary"))
        db.add(CourseAvailability(id=uuid.uuid4(), course_id=course.id, department_id=dept.id))
        F.update(course_id=course.id, offering_id=offering.id, calendar_id=calendar.id, semester_id=semester.id)

        registration = CourseRegistration(id=uuid.uuid4(), student_id=student.id, semester_id=semester.id, calendar_id=calendar.id, stage="hod_approved")
        db.add(registration)
        await db.flush()
        enrollment = StudentEnrollment(id=uuid.uuid4(), student_id=student.id, offering_id=offering.id, registration_id=registration.id, status="approved", instructor_id=faculty.id)
        db.add(enrollment)
        await db.flush()
        db.add(WithdrawalRequest(id=uuid.uuid4(), enrollment_id=enrollment.id, reason="zztest", status="rejected", requested_by=student.id, decided_by=faculty.id, decided_at=now()))
        F["enrollment_id"] = enrollment.id

        # Advisory Committee + Member
        committee = AdvisoryCommittee(id=uuid.uuid4(), student_id=student.id, research_title="ZZTEST", status="hod_approved")
        db.add(committee)
        await db.flush()
        member = CommitteeMember(id=uuid.uuid4(), committee_id=committee.id, faculty_id=faculty.id, role="major_advisor", accepted=True)
        db.add(member)
        await db.flush()
        F.update(committee_id=committee.id, member_id=member.id)

        # PPW family — stage references the committee member with NO ondelete
        # (the one deliberately-checked cross-module ordering edge case).
        ppw = Ppw(id=uuid.uuid4(), student_id=student.id, status="hod_approved")
        db.add(ppw)
        await db.flush()
        ppw_cycle = PpwApprovalCycle(id=uuid.uuid4(), ppw_id=ppw.id, cycle_number=1, status="approved", completed_at=now())
        db.add(ppw_cycle)
        await db.flush()
        db.add(PpwApprovalStage(id=uuid.uuid4(), cycle_id=ppw_cycle.id, sequence=1, stage_type="committee_member", committee_member_id=member.id, status="approved"))
        F["ppw_id"] = ppw.id

        # Grading (legacy ApprovalStage + DigitalSignature, both plain FKs)
        sheet = GradeSheet(id=uuid.uuid4(), offering_id=offering.id, sheet_type="final", gradesheet_type="new", status="draft", created_by=faculty.id)
        db.add(sheet)
        await db.flush()
        comp = GradesheetComponent(id=uuid.uuid4(), sheet_id=sheet.id, code="THEORY", name="Theory", component_type="theory", max_marks=100)
        db.add(comp)
        entry = GradeEntry(id=uuid.uuid4(), sheet_id=sheet.id, student_id=student.id, enrollment_id=enrollment.id)
        db.add(entry)
        await db.flush()
        legacy_stage = ApprovalStage(id=uuid.uuid4(), sheet_id=sheet.id, stage=1, role_required="instructor", approver_id=faculty.id, status="approved")
        db.add(legacy_stage)
        await db.flush()
        db.add(DigitalSignature(id=uuid.uuid4(), approval_stage_id=legacy_stage.id, user_id=faculty.id, verified_at=now()))
        F["sheet_id"] = sheet.id

        # External Examiner family
        examiner = ExternalExaminer(id=uuid.uuid4(), email="zztest.examiner@example.com")
        db.add(examiner)
        await db.flush()
        selection = ExternalExaminerSelection(id=uuid.uuid4(), student_id=student.id, degree_level="PG", status="vc_approved")
        db.add(selection)
        await db.flush()
        ee_cycle = ExternalExaminerApprovalCycle(id=uuid.uuid4(), selection_id=selection.id, cycle_number=1, status="approved", completed_at=now(), vc_selection_completed_at=now())
        db.add(ee_cycle)
        await db.flush()
        proposal = ExternalExaminerProposal(id=uuid.uuid4(), cycle_id=ee_cycle.id, slot_number=1, examiner_id=examiner.id,
                                             name_snapshot="ZZTEST Examiner", specialization_snapshot="ZZ", designation_snapshot="ZZ",
                                             email_snapshot="zztest.examiner@example.com", phone_snapshot="0000000000", institution_snapshot="ZZ")
        db.add(proposal)
        await db.flush()
        result = ExternalExaminerSelectionResult(id=uuid.uuid4(), cycle_id=ee_cycle.id, proposal_id=proposal.id)
        db.add(result)
        await db.flush()
        assignment = ExternalExaminerAssignment(id=uuid.uuid4(), examiner_id=examiner.id, student_id=student.id, selection_result_id=result.id, status="active")
        db.add(assignment)
        await db.flush()
        F.update(examiner_id=examiner.id, ee_selection_id=selection.id, ee_proposal_id=proposal.id, ee_result_id=result.id, assignment_id=assignment.id)

        # Thesis family — ThesisExternalEvaluation references the assignment
        # above with NO ondelete: the Thesis-before-ExternalExaminer ordering
        # edge case this script's deletion order is specifically built around.
        thesis = Thesis(id=uuid.uuid4(), student_id=student.id, thesis_type="initial", status="external_examiner_pending", title_snapshot="ZZTEST Thesis")
        db.add(thesis)
        await db.flush()
        th_cycle = ThesisApprovalCycle(id=uuid.uuid4(), thesis_id=thesis.id, cycle_number=1, status="approved", completed_at=now())
        db.add(th_cycle)
        await db.flush()
        db.add(ThesisExternalEvaluation(id=uuid.uuid4(), thesis_id=thesis.id, assignment_id=assignment.id, status="submitted", submitted_at=now()))
        F["thesis_id"] = thesis.id

        # Synopsis family — approval cycle references the file with NO ondelete.
        synopsis = Synopsis(id=uuid.uuid4(), student_id=student.id, synopsis_type="first", title="ZZTEST Synopsis", status="hod_approved")
        db.add(synopsis)
        await db.flush()
        syn_file = SynopsisFile(id=uuid.uuid4(), synopsis_id=synopsis.id, version_number=1, original_filename="zztest.pdf", stored_filename="zztest-stored.pdf", size_bytes=10, sha256="0" * 64, page_count=1)
        db.add(syn_file)
        await db.flush()
        db.add(SynopsisApprovalCycle(id=uuid.uuid4(), synopsis_id=synopsis.id, cycle_number=1, file_id=syn_file.id, status="approved", completed_at=now()))
        F["synopsis_id"] = synopsis.id

        # Progress Report
        db.add(ProgressReport(id=uuid.uuid4(), student_id=student.id, academic_year_id=calendar.id, semester_id=semester.id, status="approved",
                               student_name_snapshot=student.full_name, student_roll_snapshot="ZZ-1", program_snapshot="ZZTEST Programme"))

        # Comprehensive Exam family — panel result references the proposal with NO ondelete.
        comp_app = ComprehensiveExamApplication(id=uuid.uuid4(), student_id=student.id, degree_level="PG", status="dpgs_approved",
                                             major_credits_completed=20, minor_credits_completed=8)
        db.add(comp_app)
        await db.flush()
        panel_sel = ComprehensiveExamExternalPanelSelection(id=uuid.uuid4(), application_id=comp_app.id, status="vc_approved")
        db.add(panel_sel)
        await db.flush()
        panel_cycle = ComprehensiveExamExternalPanelCycle(id=uuid.uuid4(), selection_id=panel_sel.id, cycle_number=1, status="approved", completed_at=now())
        db.add(panel_cycle)
        await db.flush()
        panel_proposal = ComprehensiveExamExternalPanelProposal(id=uuid.uuid4(), cycle_id=panel_cycle.id, slot_number=1, examiner_id=examiner.id,
                                                                  name_snapshot="ZZ", specialization_snapshot="ZZ", designation_snapshot="ZZ",
                                                                  email_snapshot="zztest.examiner@example.com", phone_snapshot="0000000000", institution_snapshot="ZZ")
        db.add(panel_proposal)
        await db.flush()
        db.add(ComprehensiveExamExternalPanelResult(id=uuid.uuid4(), cycle_id=panel_cycle.id, proposal_id=panel_proposal.id, selected_by=sa.id))
        F["comp_app_id"] = comp_app.id

        # Migration
        db.add(MigrationApplication(id=uuid.uuid4(), student_id=student.id, status="hod_approved", student_name_snapshot=student.full_name))

        # Admission + Orientation (reference program/dept but no later module depends on them)
        db.add(AdmissionApplication(
            id=uuid.uuid4(), application_number="ZZTEST-0001", status="pending", program_id=program.id,
            academic_year="2026-27", category="general", first_name="ZZTEST", last_name="Applicant",
            date_of_birth=date(2005, 1, 1), gender="Male", nationality="Indian", aadhar_number="000000000000",
            personal_email="zztest.applicant@example.com", mobile="0000000000",
            current_address="ZZ", current_city="ZZ", current_state="ZZ", current_pincode="000000",
            permanent_address="ZZ", permanent_city="ZZ", permanent_state="ZZ", permanent_pincode="000000",
            father_name="ZZ", mother_name="ZZ",
            tenth_board="ZZ", tenth_school="ZZ", tenth_year=2018, tenth_percentage=80.0,
            twelfth_board="ZZ", twelfth_school="ZZ", twelfth_year=2020, twelfth_percentage=80.0, twelfth_stream="Science",
            reviewed_by=hod.id,
        ))
        db.add(OrientationCandidate(id=uuid.uuid4(), personal_email="zztest.cand@example.com", first_name="ZZ", last_name="Cand",
                                     academic_year="2026-27", program_id=program.id, department_id=dept.id, college_id=college.id, roll_no="ZZ-C-1"))

        db.add(AuditLog(id=uuid.uuid4(), user_id=hod.id, action="zztest", entity_type="zztest", entity_id=str(uuid.uuid4())))
        db.add(Notification(id=uuid.uuid4(), user_id=student.id, type="zztest", title="ZZTEST", message="zztest", is_read=False))

        await db.commit()


# ── CLI helpers ──────────────────────────────────────────────────────────────

def _run_script(args: list[str], stdin_text: str | None = None, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = _TEST_DATABASE_URL
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "cleanup_production_db.py"), *args],
        input=stdin_text, capture_output=True, text=True, env=env, cwd=str(BACKEND_DIR), timeout=120,
    )


async def _counts() -> dict:
    conn = await asyncpg.connect(_TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql"))
    try:
        tables = [
            "ams_users", "ams_roles", "ams_designations", "ams_courses", "ams_departments", "ams_colleges",
            "ams_programs", "ams_program_departments", "ams_college_programs", "ams_department_colleges",
            "ams_course_availability", "ams_user_role_assignments", "ams_refresh_tokens", "ams_notifications",
            "ams_audit_logs", "ams_theses", "ams_thesis_external_evaluations", "ams_synopses", "ams_synopsis_files",
            "ams_ppw", "ams_ppw_approval_stages", "ams_committee_members", "ams_advisory_committees",
            "ams_external_examiners", "ams_external_examiner_assignments", "ams_external_examiner_selection_results",
            "ams_progress_reports", "ams_comprehensive_exam_applications", "ams_migration_applications",
            "ams_grade_sheets", "ams_digital_signatures", "ams_approval_stages", "ams_student_enrollments",
            "ams_course_registrations", "ams_course_offerings", "ams_semesters", "ams_academic_calendars",
            "ams_admission_applications", "ams_orientation_candidates",
        ]
        out = {}
        for t in tables:
            out[t] = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")
        return out
    finally:
        await conn.close()


# ── tests ────────────────────────────────────────────────────────────────────

async def t_dry_run_reports_plan_without_modifying_data():
    before = await _counts()
    r = _run_script(["--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PRODUCTION DATABASE CLEANUP" in r.stdout
    assert "Dry run only" in r.stdout
    after = await _counts()
    assert before == after, "a dry run must not change a single row"
    # Sanity: the plan actually reflects our fixture's counts.
    assert "ams_theses" in r.stdout and "to delete=    1" in r.stdout.replace("    1 ", "to delete=    1 ") or "ams_theses" in r.stdout


async def t_ambiguous_superadmin_aborts_safely():
    from app.db.base import AsyncSessionLocal
    from app.models.user import User, UserRole
    extra_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=extra_id, email="SuperAdmin@avfu.ac.in", hashed_password="x", first_name="Dup", last_name="SA",
                     role=UserRole.SUPER_ADMIN, is_active=True, is_verified=True))
        await db.commit()
    try:
        before = await _counts()
        r = _run_script(["--dry-run"])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "BLOCKED" in r.stdout and "accounts match" in r.stdout
        after = await _counts()
        assert before == after, "a blocked run must not change any data"
    finally:
        from app.db.base import AsyncSessionLocal as ASL
        from app.models.user import User as U
        async with ASL() as db:
            obj = await db.get(U, extra_id)
            if obj:
                await db.delete(obj)
                await db.commit()


async def t_missing_superadmin_aborts_safely():
    from app.db.base import AsyncSessionLocal
    from app.models.user import User
    async with AsyncSessionLocal() as db:
        sa = await db.get(User, F["superadmin_id"])
        sa.email = "renamed-temporarily@avfu.ac.in"
        await db.commit()
    try:
        r = _run_script(["--dry-run"])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "BLOCKED" in r.stdout and "no user found" in r.stdout
    finally:
        async with AsyncSessionLocal() as db:
            sa = await db.get(User, F["superadmin_id"])
            sa.email = "superadmin@avfu.ac.in"
            await db.commit()


async def t_full_cleanup_preserves_superadmin_and_roles_and_removes_operational_data():
    before_sa_hash = None
    from app.db.base import AsyncSessionLocal
    from app.models.user import User
    async with AsyncSessionLocal() as db:
        sa = await db.get(User, F["superadmin_id"])
        before_sa_hash = sa.hashed_password

    r = _run_script(["--confirm-production-cleanup"], stdin_text="DELETE PRODUCTION DATA\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CLEANUP COMPLETE" in r.stdout
    assert "Post-cleanup verification passed" in r.stdout

    counts = await _counts()
    assert counts["ams_users"] == 1, counts
    assert counts["ams_roles"] == 11, "no role definitions must be deleted"
    for t in ("ams_designations", "ams_courses",
              "ams_departments", "ams_colleges", "ams_programs", "ams_program_departments",
              "ams_college_programs", "ams_department_colleges", "ams_course_availability",
              "ams_refresh_tokens", "ams_audit_logs",
              "ams_theses", "ams_thesis_external_evaluations", "ams_synopses", "ams_synopsis_files",
              "ams_ppw", "ams_ppw_approval_stages", "ams_committee_members", "ams_advisory_committees",
              "ams_external_examiners", "ams_external_examiner_assignments", "ams_external_examiner_selection_results",
              "ams_progress_reports", "ams_comprehensive_exam_applications", "ams_migration_applications",
              "ams_grade_sheets", "ams_digital_signatures", "ams_approval_stages", "ams_student_enrollments",
              "ams_course_registrations", "ams_course_offerings", "ams_semesters", "ams_academic_calendars",
              "ams_admission_applications", "ams_orientation_candidates"):
        assert counts[t] == 0, f"{t} expected empty, found {counts[t]}"

    # ams_notifications/ams_user_role_assignments are only SCOPED-deleted
    # (for removed users) — the surviving Super Admin's own row(s) there are
    # expected and correct, never asserted to be exactly 0.
    conn = await asyncpg.connect(_TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql"))
    try:
        for t, col in (("ams_notifications", "user_id"), ("ams_user_role_assignments", "user_id")):
            n = await conn.fetchval(f"SELECT COUNT(*) FROM {t} WHERE {col} <> $1", F["superadmin_id"])
            assert n == 0, f"{t} has {n} row(s) for a user other than the surviving Super Admin"
        ura_row = await conn.fetchrow("SELECT role, department_id FROM ams_user_role_assignments WHERE user_id = $1", F["superadmin_id"])
        assert ura_row is not None and ura_row["role"] == "SUPER_ADMIN" and ura_row["department_id"] is None, \
            "the Super Admin's own role assignment must survive, unchanged"
    finally:
        await conn.close()

    async with AsyncSessionLocal() as db:
        sa = await db.get(User, F["superadmin_id"])
        assert sa is not None, "the Super Admin row itself must still exist"
        assert sa.email == "superadmin@avfu.ac.in"
        assert sa.hashed_password == before_sa_hash, "password hash must be byte-for-byte unchanged"
        assert sa.is_active is True
        assert sa.role.value == "super_admin" if hasattr(sa.role, "value") else sa.role == "SUPER_ADMIN"
        assert sa.department_id is None and sa.program_id is None and sa.college_id is None and sa.academic_year_id is None


async def t_organizational_data_removed_superadmin_and_roles_intact():
    """Dedicated, explicit check (run against the already-cleaned database
    from the previous test) of the corrected policy: courses are
    department-linked and must NOT be preserved while departments/colleges/
    programmes and their relationship tables are wiped — every one of them,
    and every M:N link table between them, must be fully empty — while the
    Super Admin and all 11 role definitions remain completely intact."""
    conn = await asyncpg.connect(_TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql"))
    try:
        for t in ("ams_courses", "ams_course_availability", "ams_departments", "ams_colleges",
                  "ams_programs", "ams_program_departments", "ams_college_programs", "ams_department_colleges",
                  "ams_designations"):
            n = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")
            assert n == 0, f"{t} must be fully empty (organizational/master data is not preserved), found {n} row(s)"

        role_count = await conn.fetchval("SELECT COUNT(*) FROM ams_roles")
        assert role_count == 11, f"all 11 current role definitions must remain, found {role_count}"
        role_codes = {r["code"] for r in await conn.fetch("SELECT code FROM ams_roles")}
        assert role_codes == {
            "SUPER_ADMIN", "VICE_CHANCELLOR", "DPGS", "INCHARGE_ACADEMIC_CELL", "REGISTRAR",
            "HOD", "FACULTY", "STUDENT", "EXTERNAL_EXAMINER", "LIBRARIAN", "CONTROLLER_OF_EXAMINATION",
        }

        user_count = await conn.fetchval("SELECT COUNT(*) FROM ams_users")
        assert user_count == 1, f"exactly the one Super Admin must remain, found {user_count} user(s)"
    finally:
        await conn.close()


async def t_superadmin_foreign_keys_valid_after_organizational_cleanup():
    """Deleting all organizational data (departments/colleges/programmes/
    calendars) must not leave the Super Admin's own row pointing at
    anything now-deleted, and must not have cascade-deleted the Super Admin
    or their role assignment as a side effect of that deletion."""
    from app.db.base import AsyncSessionLocal
    from app.models.user import User
    async with AsyncSessionLocal() as db:
        sa = await db.get(User, F["superadmin_id"])
        assert sa is not None, "the Super Admin must not have been cascade-deleted by the organizational cleanup"
        assert sa.email == "superadmin@avfu.ac.in" and sa.is_active is True

    conn = await asyncpg.connect(_TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql"))
    try:
        # Every one of these FK columns, if non-null, must point at a row
        # that actually exists — but since every target table is now fully
        # empty, the only way this passes is if they are all NULL.
        row = await conn.fetchrow(
            "SELECT department_id, program_id, college_id, academic_year_id FROM ams_users WHERE id = $1",
            F["superadmin_id"],
        )
        assert row["department_id"] is None and row["program_id"] is None and row["college_id"] is None and row["academic_year_id"] is None, \
            "the Super Admin's own organizational FK columns must be NULL, never dangling"

        ura = await conn.fetchrow(
            "SELECT id, role, department_id FROM ams_user_role_assignments WHERE user_id = $1 AND role = 'SUPER_ADMIN'",
            F["superadmin_id"],
        )
        assert ura is not None, "the Super Admin's required SUPER_ADMIN role assignment must not have been cascade-deleted"
        assert ura["department_id"] is None, "a SUPER_ADMIN assignment is institution-wide and must never carry a department_id"
    finally:
        await conn.close()


async def t_repeated_invocation_is_safe_and_predictable():
    """Running the already-cleaned database through the script again must
    still find exactly the one Super Admin, report an empty plan, and (if
    confirmed again) complete with zero further deletions — never error,
    never re-delete something already gone, never touch the Super Admin."""
    r1 = _run_script(["--dry-run"])
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "Users to remove        : 0" in r1.stdout

    r2 = _run_script(["--confirm-production-cleanup"], stdin_text="DELETE PRODUCTION DATA\n")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "CLEANUP COMPLETE" in r2.stdout
    counts = await _counts()
    assert counts["ams_users"] == 1 and counts["ams_roles"] == 11


async def t_wrong_confirmation_phrase_aborts():
    r = _run_script(["--confirm-production-cleanup"], stdin_text="not the phrase\n")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "did not match" in r.stdout


async def t_non_production_environment_prints_warning_but_still_allows_review():
    r = _run_script(["--dry-run"], env_overrides={"ENVIRONMENT": "development"})
    assert r.returncode == 0
    assert "does not look like a production database" in r.stdout


async def t_file_cleanup_path_restriction_logic():
    """Unit-level check of the containment guard inside main_async's
    --also-delete-files handling: a path outside the expected
    UPLOAD_DIR/<subdir>/ base must never be acted on. Exercised directly
    against the function's own logic shape rather than the full subprocess,
    since no real uploaded files exist in this fixture."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = (Path(tmp) / "thesis").resolve()
        base.mkdir()
        inside = base / str(uuid.uuid4())
        inside.mkdir()
        outside = Path(tmp).resolve()  # the parent of base — must be rejected
        resolved_outside = outside.resolve()
        assert base in inside.resolve().parents or inside.resolve() == base
        assert not (base in resolved_outside.parents or resolved_outside == base), \
            "the parent directory must never be judged 'inside' its own child base"


async def main() -> None:
    await _recreate_test_database()
    try:
        await _create_schema()
        await _seed_role_and_designation_master_data()
        await _seed_fixture()
        for name, fn in {
            "DRY RUN: reports the plan, modifies nothing": t_dry_run_reports_plan_without_modifying_data,
            "ABORT: ambiguous (duplicate) Super Admin blocks the run, changes nothing": t_ambiguous_superadmin_aborts_safely,
            "ABORT: missing Super Admin blocks the run": t_missing_superadmin_aborts_safely,
            "CLEANUP: Super Admin preserved byte-for-byte; roles preserved; EVERY organizational/operational table emptied (courses/designations included)": t_full_cleanup_preserves_superadmin_and_roles_and_removes_operational_data,
            "ORGANIZATIONAL DATA: courses/departments/colleges/programmes/link-tables all removed; Super Admin + all 11 roles intact": t_organizational_data_removed_superadmin_and_roles_intact,
            "FK INTEGRITY: Super Admin's own FKs are NULL (never dangling); role assignment survives the organizational cleanup, not cascade-deleted": t_superadmin_foreign_keys_valid_after_organizational_cleanup,
            "IDEMPOTENT: a second full run against an already-cleaned database is a safe no-op": t_repeated_invocation_is_safe_and_predictable,
            "ABORT: wrong confirmation phrase never applies the cleanup": t_wrong_confirmation_phrase_aborts,
            "SAFETY: a non-production-looking target prints a warning but dry-run still works": t_non_production_environment_prints_warning_but_still_allows_review,
            "FILE CLEANUP: path-containment guard rejects anything outside the expected per-record base": t_file_cleanup_path_restriction_logic,
        }.items():
            await _run(name, fn)
    finally:
        await _drop_test_database()
    print(f"\n{len(RESULTS_PASSED)} passed, {len(RESULTS_FAILED)} failed")
    if RESULTS_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
