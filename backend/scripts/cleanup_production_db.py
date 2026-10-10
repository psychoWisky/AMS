"""
AMS PRODUCTION DATABASE CLEANUP SCRIPT (rewritten against schema HEAD = 0045)
==============================================================================

Purpose
-------
AVFU will reuse the existing SIQES production database for a fresh round of
testing. AVFU's explicit requirement: **only the designated Super Admin
account and all current system role definitions remain, together with the
minimum supporting records strictly necessary for authentication,
authorization, and application startup. Everything else is cleared.**

This script removes ALL operational, business, and organizational data
(most of it originally loaded from a development dump, plus anything created
since) while preserving exactly these items, each kept only because it is
demonstrably required — not merely because an earlier version of this
script happened to keep it:

  1. The existing Super Admin account — same id, same email, same password
     hash, same role, same active flag. Never deleted, never recreated,
     never demoted/promoted. Required for authentication.
  2. The Super Admin's own `ams_user_role_assignments` row — required for
     authorization (`require_roles()`/`get_current_user` read this table,
     never a blanket "is Super Admin" flag). Verified to remain valid
     (role=SUPER_ADMIN, department_id=NULL) and never cascade-deleted by any
     of the organizational-data deletions below.
  3. ALL current `ams_roles` master-data rows (the cosmetic "Administration
     -> Roles" screen data) — every one of the 11 roles the application
     actually defines today. None are deleted, per AVFU's explicit
     instruction, even though (see below) this table is not itself read by
     any authorization check.
  4. Alembic migration history/schema (`alembic_version`, every table
     definition, the `ams_user_role` Postgres enum type) — required for the
     application to start at all; never touched.

Nothing else is preserved. In particular — and this corrects an earlier
version of this rewrite's own classification, per AVFU's explicit
correction — `ams_courses` and `ams_designations` are BOTH fully cleared:

  * `ams_courses` — courses are department-linked curriculum data; keeping
    them while Departments/Colleges/Programmes are wiped would leave
    dangling, meaningless course rows pointing at organizational structure
    that no longer exists. There is no authentication/authorization/startup
    dependency on any `Course` row (confirmed: nothing in `app/core/
    dependencies.py` or anywhere in the auth path reads this table). All
    rows, and every table that references a Course, are deleted.
  * `ams_designations` — confirmed by direct inspection (`app/models/
    user.py::Designation`'s own docstring) that `User.designation` is a
    PLAIN STRING column, not a foreign key to this table. Nothing in the
    schema, nothing in authentication, and nothing in authorization reads
    or requires a row here. It is UI convenience master data for the HOD
    "Add Faculty" designation dropdown only — not a system dependency. All
    rows are deleted. (Functional consequence, not a safety concern: that
    dropdown will be empty until a Super Admin re-adds designations via the
    existing `POST /admin/designations` endpoint — an ordinary post-cleanup
    setup step, not something this script needs to do for them.)

================================================================================
WHY THIS IS A FULL REWRITE, NOT A PATCH OF THE PREVIOUS SCRIPT
================================================================================
The previous two-pass script was written against an EARLIER version of the
AMS schema and is now dangerously stale. Confirmed by direct inspection of
`app/models/*.py` (19 files) and all 45 files under `alembic/versions/`
(HEAD = `0045_research_assignment_type`):

  * The app has 58 tables today. The previous script's hardcoded list
    (`ALL_TABLES`) covered only 32 — it has NO knowledge of the entire
    Thesis, Synopsis, Progress Report, External Examiner Selection,
    Comprehensive Examination, Student Migration, or multi-role/
    multi-department module families (26 tables, all built in later
    revisions). Running the old script would have left every one of those
    tables' dummy/dev data completely untouched, defeating "AVFU wants a
    clean environment."
  * The previous script's `UserRole` assumptions are simply wrong today.
    It believed the enum has 8 members (SUPER_ADMIN, ACADEMIC_ADMIN, HOD,
    FACULTY, STUDENT, REGISTRAR, EXAMINER, RESEARCH_SUPERVISOR) and tried to
    hard-delete the `ams_roles` rows for ACADEMIC_ADMIN/REGISTRAR/EXAMINER/
    RESEARCH_SUPERVISOR. Today's real enum (`app/models/user.py::UserRole`)
    has 11 DIFFERENT members: SUPER_ADMIN, VICE_CHANCELLOR, DPGS,
    INCHARGE_ACADEMIC_CELL, REGISTRAR, HOD, FACULTY, STUDENT,
    EXTERNAL_EXAMINER, LIBRARIAN, CONTROLLER_OF_EXAMINATION.
    ACADEMIC_ADMIN/EXAMINER/RESEARCH_SUPERVISOR were removed entirely by
    migration `0015_remove_legacy_roles` and no longer exist as `ams_roles`
    rows either. **`REGISTRAR` is NOT the old dummy role** — migration
    `0015` removed the original dummy REGISTRAR, and `0030_registrar_role`
    later added a brand-new, genuine, single-holder REGISTRAR role for the
    Student Migration module. Had the previous script actually run against
    the current schema, it would have tried to delete the master-data row
    for this real, currently-functioning role. (In practice its own
    post-cleanup verification — expecting exactly 4 `ams_roles` rows to
    remain — would have failed and the transaction would have rolled back,
    so this would have been a safe failure, not silent corruption; but it
    would never have completed successfully.)
  * Confirmed directly from `app/models/user.py::Role`'s own docstring and
    `app/core/dependencies.py::require_roles`/`get_current_user`: `ams_roles`
    is NOT the authorization mechanism and never has been. It has no foreign
    key from `User.role`, is read by nothing except the Super Admin
    "Administration -> Roles" screen, and `require_roles()` checks
    `user.active_role` (resolved from `UserRoleAssignment`/the `UserRole`
    enum) directly. The correct action for `ams_roles` today is simply:
    leave all 11 rows alone. There is no "unwanted legacy role" left to
    remove — that cleanup already happened, permanently, via `0015`.

Full classification of every one of the 58 tables, the exact deletion order,
and the FK/cascade reasoning behind it are written up in
`docs/PRODUCTION_DB_CLEANUP.md` (added alongside this rewrite) — read that
before running this file against any real database.

This is a MANUAL, administrative operation. It is:
  * NOT an Alembic migration.
  * NOT imported or executed by the application at startup (confirmed:
    `app/main.py` has no startup/lifespan hook that touches the database at
    all — the app boots without running migrations or seeding anything).
  * NOT safe to run more than once with materially different intent —
    review every constant below before every use.

Usage
-----
    cd backend
    python scripts/cleanup_production_db.py                              # inspect only (default)
    python scripts/cleanup_production_db.py --dry-run                    # inspect only (explicit)
    python scripts/cleanup_production_db.py --confirm-production-cleanup # PERFORMS the cleanup
    python scripts/cleanup_production_db.py --confirm-production-cleanup --also-delete-files
        # ALSO removes the exact per-record upload directories for every
        # Thesis/Synopsis/Progress Report/Migration Application/
        # Comprehensive Exam application/Admission Application row that was
        # deleted from the database. Filesystem deletes are NEVER part of
        # the database transaction (they cannot be — there is no rollback
        # for a deleted file) and only run AFTER the DB transaction has
        # committed successfully. Restricted to exactly the per-id
        # directories under settings.UPLOAD_DIR that this run's own deleted
        # rows named — never a blind UPLOAD_DIR wipe, never any other path.

    --preserve-admission-applications
        Keep `ams_admission_applications` rows instead of deleting them
        (default: deleted — this table has no migration-level seed data, so
        every existing row is either dev-dump or test data). `program_id`/
        `reviewed_by` are nulled instead (both nullable) since
        Programs/Users are being cleared regardless.

Environment variables (read via the application's own `app.core.config`):
    DATABASE_URL   — required, the SAME variable the application itself
                     reads. Never hardcoded here. Point this at the real
                     SIQES production database before running with
                     --confirm-production-cleanup.

Safety model
------------
  * Without --confirm-production-cleanup: no INSERT/UPDATE/DELETE is ever
    executed. The script only runs SELECTs and prints a plan (dry-run).
  * With --confirm-production-cleanup: the script prints the same plan,
    then requires the operator to type an exact confirmation phrase
    interactively before touching the database.
  * If the target looks like a local/development database (host in
    {localhost, 127.0.0.1, ::1, 0.0.0.0} or empty/local-socket, or
    `ENVIRONMENT` != "production"), a loud warning is printed before the
    plan. This is a WARNING, not a hard block, specifically so this exact
    script can be exercised end-to-end against a disposable, isolated test
    database during review/testing (see docs/PRODUCTION_DB_CLEANUP.md's
    testing section) — it must never be used to justify running this
    against the shared local dev database or any database holding real
    data someone still needs.
  * Exactly one existing Super Admin matching `REQUIRED_SUPERADMIN_EMAIL`
    with role SUPER_ADMIN must be found, or the script aborts without
    changing anything. Multiple matches, zero matches, or a role mismatch
    all abort.
  * All destructive statements run inside a single database transaction.
    Post-cleanup verification runs INSIDE that same transaction; if any
    check fails, the transaction is rolled back and nothing is kept.
  * Never uses DROP TABLE / DROP SCHEMA / DROP DATABASE / TRUNCATE ... CASCADE.
  * Never deletes Alembic's `alembic_version` table or row.
  * Never alters the `ams_user_role` Postgres enum type.
  * Never disables/bypasses foreign-key constraints.
  * File deletion (opt-in, see --also-delete-files above) is the ONE part
    of this script's work that is NOT transactional and CANNOT be rolled
    back. It only ever runs after the database transaction has already
    committed, and only ever targets the exact per-record directories of
    rows this same run actually deleted from the database.

Refresh tokens / sessions
--------------------------
ALL rows in `ams_refresh_tokens` are deleted, including the Super Admin's
own — same conclusion as the previous pass: the safest production baseline
is 0 refresh tokens. The Super Admin's existing session(s) will stop working
and they will need to log in again after cleanup. Their account (id/password
hash/role/active flag) is completely unaffected — only sessions are
invalidated.

Take a production database backup BEFORE running this with
--confirm-production-cleanup. This script does not create one — see
docs/PRODUCTION_DB_CLEANUP.md for the exact backup/restore commands. This
script's cleanup is reversible ONLY by restoring that backup; it implements
no rollback of its own beyond the single transaction described above, and
NONE at all for any filesystem deletion performed with --also-delete-files.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow running as `python scripts/cleanup_production_db.py` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings

REQUIRED_SUPERADMIN_EMAIL = "superadmin@avfu.ac.in"
REQUIRED_SUPERADMIN_ROLE = "SUPER_ADMIN"

# Every role the CURRENT application defines (app/models/user.py::UserRole,
# member NAMES — `ams_roles.code` matches these exactly, confirmed by every
# seed migration: 0005, 0017, 0039, 0041). ALL of these `ams_roles` rows are
# preserved; none are deleted. This list exists only so the printed plan can
# show it was actually checked against the live enum, not assumed.
CURRENT_SYSTEM_ROLES = (
    "SUPER_ADMIN", "VICE_CHANCELLOR", "DPGS", "INCHARGE_ACADEMIC_CELL",
    "REGISTRAR", "HOD", "FACULTY", "STUDENT", "EXTERNAL_EXAMINER",
    "LIBRARIAN", "CONTROLLER_OF_EXAMINATION",
)

CONFIRMATION_PHRASE = "DELETE PRODUCTION DATA"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# Upload-directory modules whose per-record subdirectory is named exactly
# after the DB row's own id (confirmed by direct inspection of
# `_doc_dir`/`_file_dir`-equivalent helpers in each endpoint file: thesis.py,
# synopsis.py, progress_report.py, migration.py, comprehensive_exam.py,
# admission.py). Used ONLY by --also-delete-files, and only ever with ids
# this run's own deletions actually produced.
_UPLOAD_SUBDIRS = {
    "ams_theses": "thesis",
    "ams_synopses": "synopsis",
    "ams_progress_reports": "progress-report",
    "ams_migration_applications": "migration",
    "ams_comprehensive_exam_applications": "comprehensive-exam",
    # admission.py nests one level deeper: UPLOAD_DIR/admissions/<id>/ —
    # same per-id isolation property, different literal subdir spelling.
    "ams_admission_applications": "admissions",
}

# Every table in the current schema (Base.metadata, 58 tables — see
# docs/PRODUCTION_DB_CLEANUP.md for the full per-table classification and
# reasoning). Kept here purely for the printed report / completeness check;
# the actual DELETE/UPDATE statements in `run_cleanup` are explicit,
# hand-ordered from the real FK graph, independent of this list's order.
TABLE_CLASSIFICATION: list[tuple[str, str, str]] = [
    ("ams_users", "PRESERVE SELECTIVELY", "Keep only the one designated Super Admin row; every other user removed."),
    ("ams_roles", "PRESERVE", "Cosmetic master data matching the CURRENT 11-value UserRole enum exactly (confirmed via app/models/user.py + all seed migrations). Not the authorization mechanism. Nothing deleted."),
    ("ams_designations", "DELETE ALL DATA", "User.designation is a PLAIN STRING, not an FK to this table (confirmed by inspection) — no authentication/authorization/startup dependency exists. UI convenience data only; cleared per AVFU's explicit instruction not to preserve master data without a concrete system dependency."),
    ("ams_courses", "DELETE ALL DATA", "Department-linked curriculum data; deleted entirely, after every table that references a Course row (offerings, availability, PPW courses, result courses) is already empty. No authentication/authorization/startup dependency exists."),
    ("ams_colleges", "DELETE ALL DATA", "Dev-dump/test master data; cleared after every NO-ACTION referencer is empty."),
    ("ams_departments", "DELETE ALL DATA", "Same; every NO-ACTION referencer (courses, course_availability, orientation_candidates, course_offerings, user_role_assignments, users.department_id) cleared or deleted first."),
    ("ams_programs", "DELETE ALL DATA", "Same; deleted before departments (its own legacy department_id FK)."),
    ("ams_program_departments", "DELETE ALL DATA", "CASCADEs from programs/departments; deleted explicitly first for accurate reporting."),
    ("ams_college_programs", "DELETE ALL DATA", "CASCADEs from colleges/programs; deleted explicitly first."),
    ("ams_department_colleges", "DELETE ALL DATA", "CASCADEs from departments/colleges; deleted explicitly first."),
    ("ams_course_availability", "DELETE ALL DATA", "department_id is NOT NULL — cannot survive its department being deleted; deleted outright, before Course rows themselves."),
    ("ams_audit_logs", "DELETE ALL DATA", "Entire DB's audit history to date is dev/test activity."),
    ("ams_notifications", "DELETE DATA (scoped)", "Per-user notifications for removed users (CASCADE on user_id would also handle this; deleted explicitly first for accurate reporting)."),
    ("ams_email_outbox", "DELETE ALL DATA", "Transient email-send queue; no FK to users at all; cleared unconditionally."),
    ("ams_refresh_tokens", "DELETE ALL DATA", "ALL sessions cleared, including the Super Admin's own."),
    ("ams_user_role_assignments", "DELETE DATA (scoped)", "Every assignment for a removed user (CASCADE on user_id would also handle this; deleted explicitly BEFORE departments, since a HOD/FACULTY assignment's department_id is NO ACTION)."),
    ("ams_admission_applications", "DELETE ALL DATA (default)", "No migration seed exists for this table; every row is dev/test data. --preserve-admission-applications keeps rows and nulls program_id/reviewed_by instead."),
    ("ams_orientation_candidates", "DELETE ALL DATA", "Dummy/dev Orientation candidates."),
    ("ams_migration_applications", "DELETE ALL DATA", "Dummy Student Migration applications."),
    ("ams_comprehensive_exam_external_report_signatures", "DELETE (cascade)", "Cascades from ams_comprehensive_exam_external_viva_reports."),
    ("ams_comprehensive_exam_external_viva_reports", "DELETE ALL DATA", "Cascades from ams_comprehensive_exam_applications; deleted first (own cascading children)."),
    ("ams_comp_exam_external_panel_results", "DELETE ALL DATA", "Must precede ams_comp_exam_external_panel_proposals (proposal_id is a plain/NO-ACTION FK)."),
    ("ams_comp_exam_external_panel_proposals", "DELETE (cascade)", "Cascades from ams_comp_exam_external_panel_cycles."),
    ("ams_comp_exam_external_panel_cycles", "DELETE (cascade)", "Cascades from ams_comp_exam_external_panel_selections."),
    ("ams_comp_exam_external_panel_selections", "DELETE (cascade)", "Cascades from ams_comprehensive_exam_applications."),
    ("ams_comprehensive_exam_viva_report_signatures", "DELETE (cascade)", "Cascades from ams_comprehensive_exam_viva_reports."),
    ("ams_comprehensive_exam_viva_reports", "DELETE (cascade)", "Cascades from ams_comprehensive_exam_vivas."),
    ("ams_comprehensive_exam_vivas", "DELETE (cascade)", "Cascades from ams_comprehensive_exam_applications."),
    ("ams_comprehensive_exam_application_courses", "DELETE (cascade)", "Cascades from ams_comprehensive_exam_applications."),
    ("ams_comprehensive_exam_applications", "DELETE ALL DATA", "Root of the comprehensive-exam family; all children explicitly cleared above/cascaded."),
    ("ams_thesis_signatures", "DELETE (cascade)", "Cascades from ams_thesis_approval_stages."),
    ("ams_thesis_seminar_certificate_signatures", "DELETE (cascade)", "Cascades from ams_thesis_seminar_certificates."),
    ("ams_final_certificate_signatures", "DELETE (cascade)", "Cascades from ams_final_certificates."),
    ("ams_thesis_documents", "DELETE (cascade)", "Cascades from ams_theses."),
    ("ams_thesis_external_evaluations", "DELETE ALL DATA", "Cascades from ams_theses; deleted (as part of the Thesis family) BEFORE ams_external_examiner_assignments, since assignment_id is a plain/NO-ACTION FK."),
    ("ams_thesis_seminar_certificates", "DELETE (cascade)", "Cascades from ams_theses."),
    ("ams_final_certificates", "DELETE (cascade)", "Cascades from ams_theses."),
    ("ams_thesis_approval_stages", "DELETE (cascade)", "Cascades from ams_thesis_approval_cycles."),
    ("ams_thesis_approval_cycles", "DELETE (cascade)", "Cascades from ams_theses."),
    ("ams_theses", "DELETE ALL DATA", "Root of the Thesis family; must be fully gone before the External Examiner family is cleared."),
    ("ams_synopsis_signatures", "DELETE (cascade)", "Cascades from ams_synopsis_approval_stages."),
    ("ams_synopsis_approval_stages", "DELETE (cascade)", "Cascades from ams_synopsis_approval_cycles."),
    ("ams_synopsis_approval_cycles", "DELETE ALL DATA", "Must precede ams_synopsis_files (file_id is a plain/NO-ACTION FK)."),
    ("ams_synopsis_files", "DELETE ALL DATA", "Cascades from ams_synopses; deleted explicitly after approval_cycles."),
    ("ams_synopses", "DELETE ALL DATA", "Root of the Synopsis family."),
    ("ams_progress_report_signatures", "DELETE (cascade)", "Cascades from ams_progress_report_approval_stages."),
    ("ams_progress_report_proceedings", "DELETE (cascade)", "Cascades from ams_progress_report_approval_cycles."),
    ("ams_progress_report_approval_stages", "DELETE (cascade)", "Cascades from ams_progress_report_approval_cycles."),
    ("ams_progress_report_approval_cycles", "DELETE (cascade)", "Cascades from ams_progress_reports."),
    ("ams_progress_reports", "DELETE ALL DATA", "Root of the Progress Report family."),
    ("ams_external_examiner_signatures", "DELETE (cascade)", "Cascades from ams_external_examiner_approval_stages."),
    ("ams_external_examiner_assignments", "DELETE ALL DATA", "Must precede ams_external_examiners (examiner_id) AND ams_external_examiner_selection_results (selection_result_id) — both plain/NO-ACTION FKs."),
    ("ams_external_examiner_selection_results", "DELETE ALL DATA", "Must precede ams_external_examiner_proposals (proposal_id is a plain/NO-ACTION FK)."),
    ("ams_external_examiner_approval_stages", "DELETE (cascade)", "Cascades from ams_external_examiner_approval_cycles."),
    ("ams_external_examiner_proposals", "DELETE (cascade)", "Cascades from ams_external_examiner_approval_cycles."),
    ("ams_external_examiner_approval_cycles", "DELETE (cascade)", "Cascades from ams_external_examiner_selections."),
    ("ams_external_examiner_selections", "DELETE ALL DATA", "Root of the selection family."),
    ("ams_external_examiners", "DELETE ALL DATA", "The reusable examiner identity row; deleted only after assignments/proposals no longer reference it."),
    ("ams_ppw_signatures", "DELETE (cascade)", "Cascades from ams_ppw_approval_stages."),
    ("ams_ppw_approval_stages", "DELETE ALL DATA", "committee_member_id is a plain/NO-ACTION FK (the one PPW inconsistency vs. every sibling module's SET NULL) — must be fully gone before ams_committee_members."),
    ("ams_ppw_approval_cycles", "DELETE (cascade)", "Cascades from ams_ppw."),
    ("ams_ppw_courses", "DELETE (cascade)", "Cascades from ams_ppw."),
    ("ams_ppw", "DELETE ALL DATA", "Root of the PPW family; must be fully gone before Advisory Committee/Members (see ams_ppw_approval_stages above)."),
    ("ams_committee_members", "DELETE ALL DATA", "Deleted only after every PPW stage referencing it is gone."),
    ("ams_advisory_committees", "DELETE ALL DATA", "Cascades committee_members; deleted after the explicit committee_members pass above for accurate reporting."),
    ("ams_digital_signatures", "DELETE ALL DATA", "Legacy table; approval_stage_id is a plain/NO-ACTION FK into the LEGACY ams_approval_stages — must precede it."),
    ("ams_student_semester_result_courses", "DELETE ALL DATA", "offering_id/course_id are plain/NO-ACTION FKs — must precede ams_course_offerings (courses are preserved, so no issue there)."),
    ("ams_student_semester_results", "DELETE (cascade)", "Cascades ams_student_semester_result_courses; deleted after the explicit pass above."),
    ("ams_grade_entry_marks", "DELETE (cascade)", "Cascades from ams_grade_entries/ams_gradesheet_components."),
    ("ams_grade_entries", "DELETE ALL DATA", "enrollment_id is a plain/NO-ACTION FK — must precede ams_student_enrollments."),
    ("ams_gradesheet_stages", "DELETE (cascade)", "Cascades from ams_gradesheet_cycles."),
    ("ams_gradesheet_cycles", "DELETE (cascade)", "Cascades from ams_grade_sheets."),
    ("ams_gradesheet_components", "DELETE (cascade)", "Cascades from ams_grade_sheets."),
    ("ams_approval_stages", "DELETE (cascade)", "LEGACY table; cascades from ams_grade_sheets (ams_digital_signatures already cleared above)."),
    ("ams_grade_sheets", "DELETE ALL DATA", "offering_id is a plain/NO-ACTION FK — must precede ams_course_offerings."),
    ("ams_withdrawal_requests", "DELETE (cascade)", "Cascades from ams_student_enrollments; deleted explicitly first for accurate reporting."),
    ("ams_student_enrollments", "DELETE ALL DATA", "registration_id is a plain/NO-ACTION FK — must precede ams_course_registrations."),
    ("ams_course_registrations", "DELETE ALL DATA", "Must precede ams_semesters/ams_academic_calendars."),
    ("ams_offering_faculty", "DELETE (cascade)", "Cascades from ams_course_offerings."),
    ("ams_admit_cards", "DELETE ALL DATA", "Must precede ams_semesters."),
    ("ams_course_offerings", "DELETE ALL DATA", "Every referencer cleared above (grade_sheets, student_enrollments, result_courses, offering_faculty)."),
    ("ams_semesters", "DELETE ALL DATA", "Every referencer cleared above."),
    ("ams_academic_calendars", "DELETE ALL DATA", "Every referencer cleared above, including ams_semesters (CASCADE) and ams_users.academic_year_id (nulled)."),
]

ALL_TABLES = [t for t, _, _ in TABLE_CLASSIFICATION]


@dataclass
class Plan:
    superadmin_id: str
    other_user_ids: list[str] = field(default_factory=list)
    counts_before: dict[str, int] = field(default_factory=dict)
    expected_deletes: dict[str, int] = field(default_factory=dict)
    # id lists for --also-delete-files, populated only when building the
    # real (non-dry-run) plan inside the transaction, so the directories
    # named here are exactly the rows actually about to be deleted.
    upload_ids: dict[str, list[str]] = field(default_factory=dict)


def make_engine():
    # A dedicated, quiet engine (echo=False) independent of app.db.base's
    # global one, but sourced from the SAME settings.DATABASE_URL the
    # application itself uses — no hardcoded connection string.
    return create_async_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)


async def fetch_counts(conn) -> dict[str, int]:
    counts = {}
    for t in ALL_TABLES:
        counts[t] = (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
    return counts


async def describe_environment(conn) -> dict:
    db_name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    host = (await conn.execute(text("SELECT inet_server_addr()"))).scalar_one()
    port = (await conn.execute(text("SELECT inet_server_port()"))).scalar_one()
    try:
        alembic_rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:
        # A failed statement leaves the connection's transaction aborted at
        # the Postgres level even though Python caught the exception —
        # every subsequent query on THIS connection would otherwise fail
        # with "current transaction is aborted" regardless of what it is.
        # Roll back explicitly so the rest of this function/connection's
        # callers keep working normally.
        await conn.rollback()
        alembic_rev = "<alembic_version table not found>"
    return {
        "database": db_name,
        "server_addr": str(host) if host else "<local socket>",
        "server_port": port,
        "alembic_revision": alembic_rev,
        "environment_setting": settings.ENVIRONMENT,
        "debug_setting": settings.DEBUG,
    }


def looks_non_production(env_info: dict) -> bool:
    if str(env_info["environment_setting"]).lower() != "production":
        return True
    if env_info["server_addr"] in _LOCAL_HOSTS or env_info["server_addr"] == "<local socket>":
        return True
    return False


async def find_superadmins(conn) -> list:
    rows = (await conn.execute(
        text("SELECT id, email, role, is_active FROM ams_users WHERE lower(email) = lower(:email)"),
        {"email": REQUIRED_SUPERADMIN_EMAIL},
    )).all()
    return rows


async def check_role_master_data(conn) -> list[str]:
    """Verify ams_roles still has exactly the rows matching the CURRENT
    UserRole enum (CURRENT_SYSTEM_ROLES) before/after cleanup — this script
    never deletes any of them, so this should always pass; a failure here
    means something OTHER than this script has already modified ams_roles
    in a way that doesn't match the live application, which is worth
    surfacing rather than silently proceeding."""
    problems = []
    rows = (await conn.execute(text("SELECT code FROM ams_roles"))).scalars().all()
    codes = set(rows)
    missing = [c for c in CURRENT_SYSTEM_ROLES if c not in codes]
    if missing:
        problems.append(f"ams_roles is missing row(s) for current system role(s): {', '.join(missing)} (this script does not create them — a Super Admin must add via POST /admin/roles, or re-run the relevant seed migration).")
    return problems


async def build_plan(conn, preserve_admission_applications: bool, collect_upload_ids: bool) -> Plan | None:
    """Read-only. Returns None (with a printed reason) if a precondition
    blocks the whole operation (missing/duplicate/ambiguous Super Admin)."""
    superadmins = await find_superadmins(conn)
    if len(superadmins) == 0:
        print(f"BLOCKED: no user found with email '{REQUIRED_SUPERADMIN_EMAIL}'. "
              "Refusing to proceed — this script never creates a Super Admin account.")
        return None
    if len(superadmins) > 1:
        print(f"BLOCKED: {len(superadmins)} accounts match '{REQUIRED_SUPERADMIN_EMAIL}':")
        for r in superadmins:
            print(f"    id={r.id} role={r.role} is_active={r.is_active}")
        print("Refusing to proceed — the protected Super Admin cannot be identified unambiguously.")
        return None

    sa = superadmins[0]
    if sa.role != REQUIRED_SUPERADMIN_ROLE:
        print(f"BLOCKED: '{REQUIRED_SUPERADMIN_EMAIL}' exists but its role is '{sa.role}', "
              f"not '{REQUIRED_SUPERADMIN_ROLE}'. Refusing to proceed without an explicit human decision.")
        return None
    if not sa.is_active:
        print(f"BLOCKED: '{REQUIRED_SUPERADMIN_EMAIL}' exists but is NOT active (is_active=False). "
              "Refusing to proceed — reactivating or choosing a different account is a human decision, not this script's.")
        return None

    other_ids = [str(r[0]) for r in (await conn.execute(
        text("SELECT id FROM ams_users WHERE id <> :sid"), {"sid": sa.id},
    )).all()]

    role_problems = await check_role_master_data(conn)
    if role_problems:
        print("BLOCKED: role master-data preconditions not satisfied:")
        for p in role_problems:
            print(f"  - {p}")
        print("Refusing to proceed — the preservation allowlist for role definitions is incomplete.")
        return None

    counts = await fetch_counts(conn)

    expected: dict[str, int] = {}
    for t in ALL_TABLES:
        if t in ("ams_users", "ams_roles"):
            continue  # handled specially below
        if t == "ams_admission_applications":
            expected[t] = 0 if preserve_admission_applications else counts[t]
            continue
        if t in ("ams_notifications", "ams_user_role_assignments"):
            continue  # scoped-by-user counts, computed below
        expected[t] = counts[t]

    expected["ams_users"] = len(other_ids)
    expected["ams_roles"] = 0  # nothing is ever deleted from ams_roles

    notif_n = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_notifications WHERE user_id = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    expected["ams_notifications"] = notif_n

    ura_n = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_user_role_assignments WHERE user_id = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    expected["ams_user_role_assignments"] = ura_n

    # Only the SURVIVING Super Admin row is ever touched by the UPDATE
    # below (it runs after every other user is already deleted) — scoped
    # to sa.id here too, so the dry-run count reflects exactly that, not
    # every user's row before deletion.
    users_fk_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_users WHERE id = :sid AND "
             "(department_id IS NOT NULL OR program_id IS NOT NULL OR college_id IS NOT NULL OR academic_year_id IS NOT NULL)"),
        {"sid": sa.id},
    )).scalar_one()
    admission_program_touched = admission_reviewed_touched = 0
    if preserve_admission_applications:
        admission_program_touched = (await conn.execute(
            text("SELECT COUNT(*) FROM ams_admission_applications WHERE program_id IS NOT NULL"),
        )).scalar_one()
        admission_reviewed_touched = (await conn.execute(
            text("SELECT COUNT(*) FROM ams_admission_applications WHERE reviewed_by = ANY(:ids)"), {"ids": other_ids},
        )).scalar_one() if other_ids else 0
    expected["_ams_users_fk_columns_nulled"] = users_fk_touched
    expected["_ams_admission_applications_program_id_nulled"] = admission_program_touched
    expected["_ams_admission_applications_reviewed_by_nulled"] = admission_reviewed_touched

    upload_ids: dict[str, list[str]] = {}
    if collect_upload_ids:
        for t in _UPLOAD_SUBDIRS:
            if t == "ams_admission_applications" and preserve_admission_applications:
                upload_ids[t] = []
                continue
            rows = (await conn.execute(text(f"SELECT id FROM {t}"))).scalars().all()
            upload_ids[t] = [str(r) for r in rows]

    plan = Plan(
        superadmin_id=str(sa.id), other_user_ids=other_ids, counts_before=counts,
        expected_deletes=expected, upload_ids=upload_ids,
    )
    return plan


def print_plan(env_info: dict, plan: Plan, preserve_admission_applications: bool, also_delete_files: bool) -> None:
    print("=" * 78)
    print("AMS PRODUCTION DATABASE CLEANUP — PLAN (no changes made yet)")
    print("=" * 78)
    print(f"Database               : {env_info['database']}")
    print(f"Server address         : {env_info['server_addr']}:{env_info['server_port']}")
    print(f"Alembic revision       : {env_info['alembic_revision']}  (script written against HEAD=0045_research_assignment_type)")
    print(f"ENVIRONMENT setting    : {env_info['environment_setting']}  (DEBUG={env_info['debug_setting']})")
    if looks_non_production(env_info):
        print()
        print("*** WARNING: this target does not look like a production database/")
        print("*** environment (local host and/or ENVIRONMENT != 'production').")
        print("*** Proceeding is only appropriate for reviewing/testing this script")
        print("*** against a DISPOSABLE, ISOLATED database, never the shared local")
        print("*** dev database and never anything with real data someone still needs.")
    print()
    print(f"Super Admin to keep    : {REQUIRED_SUPERADMIN_EMAIL}  (id={plan.superadmin_id})")
    print(f"Users to remove        : {len(plan.other_user_ids)}  (ALL other users)")
    print()
    print(f"ams_roles: 0 rows will be deleted. All {len(CURRENT_SYSTEM_ROLES)} current system roles are expected present "
          "and preserved untouched (checked above as a precondition):")
    for code in CURRENT_SYSTEM_ROLES:
        print(f"  {code}")
    print()
    print("Master data to DELETE entirely (including courses and designations — see module docstring):")
    for t in ("ams_colleges", "ams_departments", "ams_programs", "ams_program_departments",
              "ams_college_programs", "ams_department_colleges", "ams_course_availability",
              "ams_courses", "ams_designations"):
        print(f"  {t}")
    print()
    print("Rows expected to be deleted per table (0-row tables included for completeness):")
    for t, cls, _reason in TABLE_CLASSIFICATION:
        if t in plan.expected_deletes:
            n = plan.expected_deletes[t]
            print(f"  {t:48s} currently={plan.counts_before.get(t, '?'):>5}   to delete={n:>5}")
    print()
    print(f"  ams_users (the surviving Super Admin): department_id/program_id/college_id/academic_year_id -> NULL "
          f"if currently set ({plan.expected_deletes.get('_ams_users_fk_columns_nulled', 0)} row(s) affected) — "
          "id/email/password hash/role/is_active are never written by this script.")
    if preserve_admission_applications:
        print(f"  ams_admission_applications: rows PRESERVED; program_id -> NULL on "
              f"{plan.expected_deletes.get('_ams_admission_applications_program_id_nulled', 0)} row(s), "
              f"reviewed_by -> NULL on {plan.expected_deletes.get('_ams_admission_applications_reviewed_by_nulled', 0)} row(s).")
    else:
        print(f"  ams_admission_applications: {plan.expected_deletes.get('ams_admission_applications', 0)} row(s) WILL BE DELETED "
              "(default — pass --preserve-admission-applications to keep instead).")
    print()
    if also_delete_files:
        print("File cleanup (--also-delete-files): after a successful commit, the per-record upload")
        print("directory for every deleted row below will be removed (never any other path):")
        for t, subdir in _UPLOAD_SUBDIRS.items():
            ids = plan.upload_ids.get(t, [])
            print(f"  {len(ids):>5} director(y/ies) under {settings.UPLOAD_DIR}/{subdir}/<id>/   (table: {t})")
        print("  This step is NOT transactional and CANNOT be rolled back.")
    else:
        print("File cleanup: NOT requested (pass --also-delete-files to also remove the matching")
        print("per-record upload directories after a successful commit). Database-only by default.")
    print()
    print("Tables left completely untouched (PRESERVE, no data or schema change at all): "
          "ams_roles, alembic_version")
    print()
    print("Expected final counts:")
    print("  Users                        : 1  (the designated Super Admin)")
    print("  Roles (ams_roles)            : unchanged — all rows present before cleanup remain")
    print("  Colleges / Departments       : 0 / 0")
    print("  Programmes                   : 0")
    print("  Courses / Designations       : 0 / 0")
    print("  Every other table listed above: 0 (or exactly the count noted for admission applications)")
    print("=" * 78)


# ──────────────────────────────────────────────────────────────────────────
# Deletion — explicit, hand-ordered from the real FK graph (see
# docs/PRODUCTION_DB_CLEANUP.md for the full derivation). Children are always
# deleted before the parents/siblings they reference; cascades are relied on
# ONLY where a single parent's cascade cannot create a sibling-ordering
# conflict (confirmed per-case in the documentation, not assumed).
# ──────────────────────────────────────────────────────────────────────────

async def run_cleanup(conn, plan: Plan, preserve_admission_applications: bool) -> None:
    ids = plan.other_user_ids

    # ── Comprehensive Examination family ────────────────────────────────
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_external_report_signatures"))
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_external_viva_reports"))
    await conn.execute(text("DELETE FROM ams_comp_exam_external_panel_results"))  # before proposals (proposal_id NO ACTION)
    await conn.execute(text("DELETE FROM ams_comp_exam_external_panel_proposals"))
    await conn.execute(text("DELETE FROM ams_comp_exam_external_panel_cycles"))
    await conn.execute(text("DELETE FROM ams_comp_exam_external_panel_selections"))
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_viva_report_signatures"))
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_viva_reports"))
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_vivas"))
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_application_courses"))
    await conn.execute(text("DELETE FROM ams_comprehensive_exam_applications"))

    # ── Thesis family — fully gone BEFORE the External Examiner family
    # (ThesisExternalEvaluation.assignment_id -> ExternalExaminerAssignment.id
    # is a plain/NO-ACTION FK) ───────────────────────────────────────────
    await conn.execute(text("DELETE FROM ams_thesis_signatures"))
    await conn.execute(text("DELETE FROM ams_thesis_seminar_certificate_signatures"))
    await conn.execute(text("DELETE FROM ams_final_certificate_signatures"))
    await conn.execute(text("DELETE FROM ams_thesis_documents"))
    await conn.execute(text("DELETE FROM ams_thesis_external_evaluations"))
    await conn.execute(text("DELETE FROM ams_thesis_seminar_certificates"))
    await conn.execute(text("DELETE FROM ams_final_certificates"))
    await conn.execute(text("DELETE FROM ams_thesis_approval_stages"))
    await conn.execute(text("DELETE FROM ams_thesis_approval_cycles"))
    await conn.execute(text("DELETE FROM ams_theses"))

    # ── Synopsis family — approval_cycles before synopsis_files
    # (SynopsisApprovalCycle.file_id -> SynopsisFile.id is plain/NO-ACTION) ──
    await conn.execute(text("DELETE FROM ams_synopsis_signatures"))
    await conn.execute(text("DELETE FROM ams_synopsis_approval_stages"))
    await conn.execute(text("DELETE FROM ams_synopsis_approval_cycles"))
    await conn.execute(text("DELETE FROM ams_synopsis_files"))
    await conn.execute(text("DELETE FROM ams_synopses"))

    # ── Progress Report family ──────────────────────────────────────────
    await conn.execute(text("DELETE FROM ams_progress_report_signatures"))
    await conn.execute(text("DELETE FROM ams_progress_report_proceedings"))
    await conn.execute(text("DELETE FROM ams_progress_report_approval_stages"))
    await conn.execute(text("DELETE FROM ams_progress_report_approval_cycles"))
    await conn.execute(text("DELETE FROM ams_progress_reports"))

    # ── External Examiner family (AFTER Thesis — see above). Assignments
    # before BOTH examiners (examiner_id) and selection_results
    # (selection_result_id); selection_results before proposals
    # (proposal_id). All three are plain/NO-ACTION FKs. ──────────────────
    await conn.execute(text("DELETE FROM ams_external_examiner_signatures"))
    await conn.execute(text("DELETE FROM ams_external_examiner_assignments"))
    await conn.execute(text("DELETE FROM ams_external_examiner_selection_results"))
    await conn.execute(text("DELETE FROM ams_external_examiner_approval_stages"))
    await conn.execute(text("DELETE FROM ams_external_examiner_proposals"))
    await conn.execute(text("DELETE FROM ams_external_examiner_approval_cycles"))
    await conn.execute(text("DELETE FROM ams_external_examiner_selections"))
    await conn.execute(text("DELETE FROM ams_external_examiners"))

    # ── PPW family — fully gone BEFORE Advisory Committee/Members
    # (PpwApprovalStage.committee_member_id is the one plain/NO-ACTION FK to
    # CommitteeMember.id; every sibling module's equivalent is SET NULL) ──
    await conn.execute(text("DELETE FROM ams_ppw_signatures"))
    await conn.execute(text("DELETE FROM ams_ppw_approval_stages"))
    await conn.execute(text("DELETE FROM ams_ppw_approval_cycles"))
    await conn.execute(text("DELETE FROM ams_ppw_courses"))
    await conn.execute(text("DELETE FROM ams_ppw"))

    # ── Advisory Committee / Members (now safe) ─────────────────────────
    await conn.execute(text("DELETE FROM ams_committee_members"))
    await conn.execute(text("DELETE FROM ams_advisory_committees"))

    # ── Grading / Results — digital_signatures before the LEGACY
    # ams_approval_stages (approval_stage_id is plain/NO-ACTION); result
    # courses before course_offerings; grade_entries before
    # student_enrollments (enrollment_id is plain/NO-ACTION) ────────────
    await conn.execute(text("DELETE FROM ams_digital_signatures"))
    await conn.execute(text("DELETE FROM ams_student_semester_result_courses"))
    await conn.execute(text("DELETE FROM ams_student_semester_results"))
    await conn.execute(text("DELETE FROM ams_grade_entry_marks"))
    await conn.execute(text("DELETE FROM ams_grade_entries"))
    await conn.execute(text("DELETE FROM ams_gradesheet_stages"))
    await conn.execute(text("DELETE FROM ams_gradesheet_cycles"))
    await conn.execute(text("DELETE FROM ams_gradesheet_components"))
    await conn.execute(text("DELETE FROM ams_approval_stages"))  # legacy table; cascades would also clear it from grade_sheets below
    await conn.execute(text("DELETE FROM ams_grade_sheets"))

    # ── Enrollment / Registration — withdrawal_requests cascade from
    # enrollments but deleted explicitly first for reporting; enrollments
    # before registrations (registration_id is plain/NO-ACTION) ─────────
    await conn.execute(text("DELETE FROM ams_withdrawal_requests"))
    await conn.execute(text("DELETE FROM ams_student_enrollments"))
    await conn.execute(text("DELETE FROM ams_course_registrations"))

    # ── Scheduling ───────────────────────────────────────────────────────
    await conn.execute(text("DELETE FROM ams_offering_faculty"))
    await conn.execute(text("DELETE FROM ams_admit_cards"))
    await conn.execute(text("DELETE FROM ams_course_offerings"))

    # ── Semesters / Academic Calendars ──────────────────────────────────
    await conn.execute(text("DELETE FROM ams_semesters"))
    await conn.execute(text("DELETE FROM ams_academic_calendars"))

    # ── Orientation / Admissions / Migration ────────────────────────────
    await conn.execute(text("DELETE FROM ams_orientation_candidates"))
    if preserve_admission_applications:
        await conn.execute(
            text("UPDATE ams_admission_applications SET program_id = NULL WHERE program_id IS NOT NULL"),
        )
        await conn.execute(
            text("UPDATE ams_admission_applications SET reviewed_by = NULL WHERE reviewed_by = ANY(:ids)"),
            {"ids": ids},
        )
    else:
        await conn.execute(text("DELETE FROM ams_admission_applications"))
    await conn.execute(text("DELETE FROM ams_migration_applications"))

    # ── Audit / notifications / email queue / sessions ──────────────────
    await conn.execute(text("DELETE FROM ams_audit_logs"))
    await conn.execute(text("DELETE FROM ams_notifications WHERE user_id = ANY(:ids)"), {"ids": ids})
    await conn.execute(text("DELETE FROM ams_email_outbox"))
    await conn.execute(text("DELETE FROM ams_refresh_tokens"))

    # ── Role assignments for removed users — BEFORE departments, since a
    # HOD/FACULTY assignment's department_id is a plain/NO-ACTION FK. The
    # Super Admin's own assignment (department_id always NULL) is
    # untouched (it is never in `ids`). ──────────────────────────────────
    await conn.execute(text("DELETE FROM ams_user_role_assignments WHERE user_id = ANY(:ids)"), {"ids": ids})

    # ── Second pass: master data (Colleges/Departments/Programmes/M:N) ──
    # ams_courses is deleted entirely (department-linked curriculum data;
    # not preserved — see module docstring), after every table that could
    # reference a Course row is already empty: ams_course_offerings
    # (Scheduling, above), ams_ppw_courses (cascaded with the PPW family,
    # above), ams_student_semester_result_courses (Grading, above), and
    # ams_course_availability (CASCADE from Course anyway, but deleted
    # explicitly first below for accurate reporting, since its
    # department_id is NOT NULL and cannot survive the department deletion
    # that follows either way).
    await conn.execute(text("DELETE FROM ams_course_availability"))
    await conn.execute(text("DELETE FROM ams_courses"))
    await conn.execute(text(
        "UPDATE ams_users SET department_id = NULL, program_id = NULL, college_id = NULL, academic_year_id = NULL"
    ))
    await conn.execute(text("DELETE FROM ams_program_departments"))
    await conn.execute(text("DELETE FROM ams_college_programs"))
    await conn.execute(text("DELETE FROM ams_department_colleges"))
    await conn.execute(text("DELETE FROM ams_programs"))
    await conn.execute(text("DELETE FROM ams_departments"))
    await conn.execute(text("DELETE FROM ams_colleges"))

    # ams_designations — flat, dependent-free master data (confirmed:
    # User.designation is a plain string, never an FK to this table) —
    # deleted entirely, per AVFU's explicit instruction not to preserve
    # master data without a concrete system dependency. Safe at any point;
    # placed here for grouping with the other master-data deletions.
    await conn.execute(text("DELETE FROM ams_designations"))

    # ── Finally, the users themselves. ams_roles is NEVER touched. ──────
    await conn.execute(text("DELETE FROM ams_users WHERE id = ANY(:ids)"), {"ids": ids})


async def verify_after(conn, preserve_admission_applications: bool) -> list[str]:
    """Returns a list of problem descriptions; empty list means all checks passed."""
    problems = []

    users = (await conn.execute(text("SELECT id, email, role, is_active FROM ams_users"))).all()
    superadmin_id = None
    if len(users) != 1:
        problems.append(f"Expected exactly 1 user after cleanup, found {len(users)}.")
    elif users[0].email.lower() != REQUIRED_SUPERADMIN_EMAIL.lower():
        problems.append(f"The single remaining user is '{users[0].email}', not '{REQUIRED_SUPERADMIN_EMAIL}'.")
    elif users[0].role != REQUIRED_SUPERADMIN_ROLE:
        problems.append(f"Remaining user's role is '{users[0].role}', not '{REQUIRED_SUPERADMIN_ROLE}'.")
    elif not users[0].is_active:
        problems.append("Remaining Super Admin account is not active (is_active=False).")
    else:
        superadmin_id = users[0].id

    # "DELETE DATA (scoped)" tables (ams_notifications, ams_user_role_assignments)
    # must contain no row for anyone OTHER than the surviving Super Admin —
    # checked precisely, rather than assumed empty (the Super Admin's own
    # row(s) there are expected and correct).
    if superadmin_id is not None:
        for t, col in (("ams_notifications", "user_id"), ("ams_user_role_assignments", "user_id")):
            n = (await conn.execute(text(f"SELECT COUNT(*) FROM {t} WHERE {col} <> :sid"), {"sid": superadmin_id})).scalar_one()
            if n:
                problems.append(f"{n} row(s) remain in {t} for a user other than the surviving Super Admin.")

    problems.extend(await check_role_master_data(conn))
    role_rows = (await conn.execute(text("SELECT code, is_system FROM ams_roles"))).all()
    if any(not r.is_system for r in role_rows if r.code in CURRENT_SYSTEM_ROLES):
        problems.append("A required ams_roles row unexpectedly has is_system=False after cleanup (nothing in this script writes is_system).")

    # "DELETE DATA (scoped)" tables (ams_notifications, ams_user_role_assignments)
    # are only scoped to the REMOVED users — the surviving Super Admin's own
    # row(s) there are expected and correct, not a verification failure.
    empty_tables = [t for t, cls, _ in TABLE_CLASSIFICATION if cls not in ("PRESERVE", "PRESERVE SELECTIVELY", "DELETE DATA (scoped)")]
    if preserve_admission_applications:
        empty_tables = [t for t in empty_tables if t != "ams_admission_applications"]
    for t in empty_tables:
        n = (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
        if n:
            problems.append(f"Expected {t} to be empty after cleanup, found {n} row(s).")

    # Orphan checks — every NO-ACTION/plain FK that could realistically still
    # dangle given the deletion order above, re-verified rather than assumed.
    orphan_checks = {
        "ams_users.department_id": "SELECT COUNT(*) FROM ams_users a LEFT JOIN ams_departments d ON d.id=a.department_id WHERE a.department_id IS NOT NULL AND d.id IS NULL",
        "ams_users.program_id": "SELECT COUNT(*) FROM ams_users a LEFT JOIN ams_programs p ON p.id=a.program_id WHERE a.program_id IS NOT NULL AND p.id IS NULL",
        "ams_users.college_id": "SELECT COUNT(*) FROM ams_users a LEFT JOIN ams_colleges c ON c.id=a.college_id WHERE a.college_id IS NOT NULL AND c.id IS NULL",
        "ams_users.academic_year_id": "SELECT COUNT(*) FROM ams_users a LEFT JOIN ams_academic_calendars c ON c.id=a.academic_year_id WHERE a.academic_year_id IS NOT NULL AND c.id IS NULL",
    }
    if preserve_admission_applications:
        orphan_checks["ams_admission_applications.program_id"] = (
            "SELECT COUNT(*) FROM ams_admission_applications a LEFT JOIN ams_programs p ON p.id=a.program_id "
            "WHERE a.program_id IS NOT NULL AND p.id IS NULL"
        )
        orphan_checks["ams_admission_applications.reviewed_by"] = (
            "SELECT COUNT(*) FROM ams_admission_applications a LEFT JOIN ams_users u ON u.id=a.reviewed_by "
            "WHERE a.reviewed_by IS NOT NULL AND u.id IS NULL"
        )
    for label, sql in orphan_checks.items():
        n = (await conn.execute(text(sql))).scalar_one()
        if n:
            problems.append(f"{n} orphaned/dangling row(s) remain for {label}.")

    return problems


async def main_async(args: argparse.Namespace) -> int:
    engine = make_engine()
    try:
        async with engine.connect() as conn:
            env_info = await describe_environment(conn)
            plan = await build_plan(conn, args.preserve_admission_applications, collect_upload_ids=args.also_delete_files)
            if plan is None:
                return 2
            print_plan(env_info, plan, args.preserve_admission_applications, args.also_delete_files)

        if not args.confirm_production_cleanup:
            print()
            print("Dry run only — no changes were made.")
            print("Re-run with --confirm-production-cleanup to perform this cleanup for real.")
            return 0

        if looks_non_production(env_info):
            print()
            print("*** This target does not look like production. Proceeding because")
            print("*** --confirm-production-cleanup was explicitly passed (per the script's")
            print("*** documented safety rule). Make sure this is really what you intend.")

        print()
        print(f"Type the exact phrase to proceed: {CONFIRMATION_PHRASE}")
        typed = input("> ").strip()
        if typed != CONFIRMATION_PHRASE:
            print("Confirmation phrase did not match. Aborting — no changes made.")
            return 3

        committed_upload_ids: dict[str, list[str]] = {}
        async with engine.begin() as conn:
            # Re-check preconditions and re-plan INSIDE the transaction, in
            # case anything changed between the dry-run read above and now.
            plan = await build_plan(conn, args.preserve_admission_applications, collect_upload_ids=args.also_delete_files)
            if plan is None:
                raise RuntimeError("Preconditions no longer satisfied — aborting inside transaction.")

            await run_cleanup(conn, plan, args.preserve_admission_applications)

            problems = await verify_after(conn, args.preserve_admission_applications)
            if problems:
                print("Post-cleanup verification FAILED — rolling back, no changes will be kept:")
                for p in problems:
                    print(f"  - {p}")
                raise RuntimeError("Post-cleanup verification failed.")

            committed_upload_ids = plan.upload_ids
            print()
            print("Post-cleanup verification passed. Committing transaction...")

        async with engine.connect() as conn:
            counts_after = await fetch_counts(conn)
            env_after = await describe_environment(conn)
            print()
            print("=" * 78)
            print("DATABASE CLEANUP COMPLETE (committed)")
            print("=" * 78)
            print(f"Alembic revision (unchanged) : {env_after['alembic_revision']}")
            print(f"Users remaining               : {counts_after['ams_users']}")
            print(f"Refresh tokens remaining      : {counts_after['ams_refresh_tokens']}")
            print(f"Role rows (unchanged)         : {(await conn.execute(text('SELECT COUNT(*) FROM ams_roles'))).scalar_one()}")
            print(f"Designation rows remaining    : {counts_after['ams_designations']}  (expected 0)")
            print(f"Course rows remaining         : {counts_after['ams_courses']}  (expected 0)")
            print()
            print("The Super Admin's browser session(s) were invalidated along with all other")
            print("refresh tokens — they must log in again; their account itself is unaffected.")

        if args.also_delete_files:
            print()
            print("=" * 78)
            print("FILE CLEANUP (--also-delete-files) — NOT transactional, cannot be rolled back")
            print("=" * 78)
            removed, missing = 0, 0
            for table, subdir in _UPLOAD_SUBDIRS.items():
                for rid in committed_upload_ids.get(table, []):
                    target = Path(settings.UPLOAD_DIR) / subdir / rid
                    try:
                        resolved = target.resolve()
                        base = (Path(settings.UPLOAD_DIR) / subdir).resolve()
                        if base not in resolved.parents and resolved != base:
                            print(f"  SKIPPED (outside expected base): {target}")
                            continue
                    except OSError:
                        continue
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=False)
                        removed += 1
                    else:
                        missing += 1
            print(f"Removed {removed} per-record upload director(y/ies); {missing} already absent.")
        return 0
    finally:
        await engine.dispose()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Inspect and print the plan only (default behavior).")
    p.add_argument("--confirm-production-cleanup", action="store_true",
                   help="Actually perform the destructive cleanup (after a typed confirmation prompt).")
    p.add_argument("--preserve-admission-applications", action="store_true",
                   help="Keep ams_admission_applications rows instead of deleting them (default: deleted).")
    p.add_argument("--also-delete-files", action="store_true",
                   help="After a successful commit, also remove the per-record upload directories for every "
                        "Thesis/Synopsis/Progress Report/Migration Application/Comprehensive Exam/Admission "
                        "Application row deleted. NOT transactional; cannot be rolled back. Database-only by default.")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
