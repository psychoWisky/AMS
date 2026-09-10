"""
AMS ONE-TIME PRODUCTION DATABASE CLEANUP SCRIPT
================================================

Purpose
-------
The AMS production database was created from a DEVELOPMENT DATABASE DUMP, so
it currently contains dummy/test data end-to-end. This script resets it to a
clean production baseline so the AVFU Super Admin can start entering real
data, while preserving:

  * the existing `superadmin@avfu.ac.in` account (same ID, same password
    hash, same role, same active flag — never deleted/recreated)
  * all required master/reference data (Colleges, Departments, Programmes,
    Courses, Course Availability config, Designations)
  * the Programme <-> Department many-to-many structure
    (`ams_program_departments`) — untouched
  * the `ams_roles` master-data table's ROWS (none deleted) and the
    `ams_user_role` Postgres enum's VALUES (none dropped) — see "Roles" below
    for what IS changed (a data-only `is_active` flip, not a schema/enum change)
  * Alembic migration history and the database schema — untouched

This is a MANUAL, ONE-TIME operational script. It is:
  * NOT an Alembic migration
  * NOT imported or executed by the application at startup
  * NOT safe to run more than once with meaningfully different intent —
    review its constants (below) before every use

Full classification of every table, the exact deletion order, and the
reasoning behind every decision are written up in the accompanying review
report delivered alongside this script — read that BEFORE running this file
against any real database.

Roles
-----
AMS does NOT store authorization roles as rows that can be "reduced to four".
`User.role` is backed by a native PostgreSQL enum type (`ams_user_role`) with
8 members (SUPER_ADMIN, ACADEMIC_ADMIN, HOD, FACULTY, STUDENT, REGISTRAR,
EXAMINER, RESEARCH_SUPERVISOR) defined by `app.models.user.UserRole`. Enum
values are NEVER dropped by this script — that would require rebuilding the
type, an explicitly-forbidden schema change, and it would also permanently
prevent those roles from being reintroduced later ("more roles may be added
later" — the task's own requirement).

Separately, `ams_roles` is a master-data table that feeds ONLY the Admin
"Role Management" screen (`GET/POST/PATCH/DELETE /admin/roles`) — it has NO
foreign key from `User.role` and does not gate what a real user's role can
be; `require_roles()` and every authorization check in the codebase read
`User.role` (the enum) directly, never this table. Its `is_active` column is
a genuine, pre-existing application feature (the Admin UI already has an
active/inactive toggle wired to `PATCH /admin/roles/{id}`) — this script
uses that exact same mechanism, purely as a data update, to set:
    is_active = True  for SUPER_ADMIN, STUDENT, HOD, FACULTY
    is_active = False for ACADEMIC_ADMIN, REGISTRAR, EXAMINER, RESEARCH_SUPERVISOR
No row is deleted, `is_system` is untouched, and every one of these 4 roles
can be flipped back to active later (through the same Admin UI, no script
needed) the moment AMS development actually needs them again.

IMPORTANT KNOWN LIMITATION (reported, not silently worked around): the
"Add User" role dropdown in `frontend/lib/utils.ts` (`ROLES`/`ADMIN_ROLES`)
is a HARDCODED list, completely independent of `ams_roles.is_active`. This
script's `ams_roles` update makes the 4 unwanted roles disappear from the
Admin "Role Management" screen, but it will NOT remove them from the "Add
User" dropdown — that is frontend application source code, which no one has
approved changing as part of this database cleanup task. This is called out
explicitly in the review report; fixing it (if desired) is a separate,
small, explicitly-approved frontend change, not something this script does.

Usage
-----
    cd backend
    python scripts/cleanup_production_db.py                              # inspect only (default)
    python scripts/cleanup_production_db.py --dry-run                    # inspect only (explicit)
    python scripts/cleanup_production_db.py --confirm-production-cleanup # PERFORMS the cleanup

Add `--preserve-admission-applications` to keep `ams_admission_applications`
rows instead of deleting them (the default is now to DELETE them — the whole
database, including this table, came from a development dump, so its one
existing row is dummy/development applicant data, not real production data).

Environment variables (read via the application's own `app.core.config`):
    DATABASE_URL   — required, same variable the application already uses.

Safety model
------------
  * Without --confirm-production-cleanup: no INSERT/UPDATE/DELETE is ever
    executed. The script only runs SELECTs and prints a plan.
  * With --confirm-production-cleanup: the script prints the same plan, then
    requires the operator to type an exact confirmation phrase interactively
    before touching the database.
  * If the target looks like a local/development database (host in
    {localhost, 127.0.0.1, ::1} or `ENVIRONMENT` != "production"), a loud
    warning is printed. This is a WARNING, not a hard block, so this exact
    script can still be exercised end-to-end against a disposable local
    database during review/testing.
  * Exactly one existing `superadmin@avfu.ac.in` with role SUPER_ADMIN must
    be found, or the script stops without changing anything.
  * All destructive statements run inside a single database transaction.
    Post-cleanup verification runs INSIDE that same transaction; if any
    check fails, the transaction is rolled back and nothing is kept.
  * Never uses DROP TABLE / DROP SCHEMA / DROP DATABASE / TRUNCATE ... CASCADE.
  * Never deletes Alembic's `alembic_version` table or row.
  * Never alters the `ams_user_role` Postgres enum type.

Refresh tokens / sessions
--------------------------
ALL rows in `ams_refresh_tokens` are deleted, including the Super Admin's own
— the task's own explicit conclusion is "the safest production baseline is
generally 0 refresh tokens". This means the Super Admin's existing browser
session(s) inherited from the development dump will stop working and they
will need to log in again after cleanup. Their account (id/password
hash/role/active flag) is completely unaffected — only their sessions are
invalidated.

Take a production `pg_dump` backup BEFORE running this with
--confirm-production-cleanup. This script does not create one — that is the
server operator's responsibility with proper production backup tooling. This
script's cleanup is reversible ONLY by restoring that backup; it implements
no rollback of its own beyond the single transaction described above.
"""
from __future__ import annotations

import argparse
import asyncio
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

# The four roles required at this stage of AMS development (task's explicit
# requirement). Enforced two ways by this script:
#   1. Reported here for the printed plan/verification (enum + master data
#      are never destructively changed to "only support" these four — see
#      module docstring, "Roles").
#   2. `ams_roles.is_active` is flipped for exactly these codes vs. the four
#      below in `ROLES_TO_DEACTIVATE` — a reversible, application-supported
#      data change, not a schema/enum change.
REQUIRED_ROLES_TO_SUPPORT = ("SUPER_ADMIN", "STUDENT", "HOD", "FACULTY")
ROLES_TO_DEACTIVATE = ("ACADEMIC_ADMIN", "REGISTRAR", "EXAMINER", "RESEARCH_SUPERVISOR")

CONFIRMATION_PHRASE = "DELETE PRODUCTION DATA"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# All 32 AMS tables (from `Base.metadata`, `app.main` imported), classified.
# Kept here purely for the printed report — the actual DELETE/UPDATE
# statements below are independent, explicit, and hand-ordered from the real
# FK graph (see the review report for the full derivation).
TABLE_CLASSIFICATION: list[tuple[str, str, str]] = [
    # (table, classification, reason)
    ("ams_users", "PRESERVE SELECTIVELY", "Keep only superadmin@avfu.ac.in; remove every other (dummy/dev) user."),
    ("ams_roles", "PRESERVE (data update)", "No row deleted; is_active flipped for 4 non-required system roles — reversible via the app's own toggle."),
    ("ams_colleges", "PRESERVE", "Institutional master data; nothing here reads as test data."),
    ("ams_departments", "PRESERVE", "Institutional master data."),
    ("ams_designations", "PRESERVE", "Institutional master data."),
    ("ams_programs", "PRESERVE", "Institutional master data."),
    ("ams_program_departments", "PRESERVE", "Programme<->Department M:N — explicitly must not be touched."),
    ("ams_courses", "PRESERVE SELECTIVELY", "Curriculum master data kept; created_by nulled if it pointed at a removed user."),
    ("ams_course_availability", "PRESERVE", "Cross-department course visibility config; references only Course/Department (both preserved)."),
    ("ams_audit_logs", "DELETE ALL DATA", "Entire DB came from a dev dump, so all audit history is dev/test activity; table/schema kept."),
    ("ams_admission_applications", "DELETE ALL DATA (default)", "Dev-dump data by definition; pass --preserve-admission-applications to keep instead."),
    ("ams_orientation_candidates", "DELETE ALL DATA", "Dummy/dev Orientation candidates — explicit business requirement to clear."),
    ("ams_ppw", "DELETE DATA (scoped)", "Every row belongs to a dummy student being removed; cascades ppw_courses/approval_cycles/approval_stages/signatures."),
    ("ams_ppw_courses", "DELETE (cascade)", "Cascades from ams_ppw."),
    ("ams_ppw_approval_cycles", "DELETE (cascade)", "Cascades from ams_ppw."),
    ("ams_ppw_approval_stages", "DELETE (cascade)", "Cascades from ams_ppw_approval_cycles."),
    ("ams_ppw_signatures", "DELETE (cascade)", "Cascades from ams_ppw_approval_stages."),
    ("ams_advisory_committees", "DELETE DATA (scoped)", "Every row belongs to a dummy student being removed; cascades committee_members."),
    ("ams_committee_members", "DELETE (cascade)", "Cascades from ams_advisory_committees."),
    ("ams_digital_signatures", "DELETE ALL DATA", "Dummy gradesheet OTP/signature records; must be cleared before grade_sheets (FK is ON DELETE NO ACTION)."),
    ("ams_grade_sheets", "DELETE ALL DATA", "Dummy grading data tied to dummy course offerings; cascades grade_entries + approval_stages."),
    ("ams_grade_entries", "DELETE (cascade)", "Cascades from ams_grade_sheets."),
    ("ams_approval_stages", "DELETE (cascade)", "Cascades from ams_grade_sheets."),
    ("ams_student_enrollments", "DELETE ALL DATA", "Dummy enrollments tied to dummy course offerings/semesters."),
    ("ams_course_registrations", "DELETE ALL DATA", "Dummy registrations."),
    ("ams_admit_cards", "DELETE ALL DATA", "Dummy admit cards tied to dummy semesters."),
    ("ams_course_offerings", "DELETE ALL DATA", "Dummy per-semester scheduling instances; cascades offering_faculty."),
    ("ams_offering_faculty", "DELETE (cascade)", "Cascades from ams_course_offerings."),
    ("ams_semesters", "DELETE ALL DATA", "Dummy/dev semester rows — the Super Admin will configure fresh real ones after cleanup."),
    ("ams_academic_calendars", "DELETE ALL DATA", "Dummy/dev calendar rows for the same reason; never fabricated here."),
    ("ams_notifications", "DELETE DATA (scoped)", "Per-user notifications for removed users; ON DELETE CASCADE would also handle this, deleted explicitly first for accurate reporting."),
    ("ams_refresh_tokens", "DELETE ALL DATA", "ALL sessions cleared, including the Super Admin's own — clean auth state, per explicit requirement."),
]


@dataclass
class Plan:
    superadmin_id: str
    other_user_ids: list[str] = field(default_factory=list)
    counts_before: dict[str, int] = field(default_factory=dict)
    expected_deletes: dict[str, int] = field(default_factory=dict)


ALL_TABLES = [
    "ams_academic_calendars", "ams_admission_applications", "ams_admit_cards",
    "ams_advisory_committees", "ams_approval_stages", "ams_audit_logs",
    "ams_colleges", "ams_committee_members", "ams_course_availability",
    "ams_course_offerings", "ams_course_registrations", "ams_courses",
    "ams_departments", "ams_designations", "ams_digital_signatures",
    "ams_grade_entries", "ams_grade_sheets", "ams_notifications",
    "ams_offering_faculty", "ams_orientation_candidates", "ams_ppw",
    "ams_ppw_approval_cycles", "ams_ppw_approval_stages", "ams_ppw_courses",
    "ams_ppw_signatures", "ams_program_departments", "ams_programs",
    "ams_refresh_tokens", "ams_roles", "ams_semesters",
    "ams_student_enrollments", "ams_users",
]


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
    try:
        alembic_rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:
        alembic_rev = "<alembic_version table not found>"
    return {
        "database": db_name,
        "server_addr": str(host) if host else "<local socket>",
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


async def build_plan(conn, preserve_admission_applications: bool) -> Plan | None:
    """Read-only. Returns None (with a printed reason) if a precondition
    blocks the whole operation (missing/duplicate Super Admin)."""
    superadmins = await find_superadmins(conn)
    if len(superadmins) == 0:
        print(f"BLOCKED: no user found with email '{REQUIRED_SUPERADMIN_EMAIL}'. "
              "Refusing to proceed — this script never creates a Super Admin account.")
        return None
    if len(superadmins) > 1:
        print(f"BLOCKED: {len(superadmins)} accounts match '{REQUIRED_SUPERADMIN_EMAIL}':")
        for r in superadmins:
            print(f"    id={r.id} role={r.role} is_active={r.is_active}")
        print("Refusing to proceed — decide which one to keep before running this script again.")
        return None

    sa = superadmins[0]
    if sa.role != REQUIRED_SUPERADMIN_ROLE:
        print(f"BLOCKED: '{REQUIRED_SUPERADMIN_EMAIL}' exists but its role is '{sa.role}', "
              f"not '{REQUIRED_SUPERADMIN_ROLE}'. Refusing to proceed without your explicit decision.")
        return None

    other_ids = [str(r[0]) for r in (await conn.execute(
        text("SELECT id FROM ams_users WHERE id <> :sid"), {"sid": sa.id},
    )).all()]

    counts = await fetch_counts(conn)

    expected = {}
    expected["ams_users"] = len(other_ids)
    expected["ams_orientation_candidates"] = counts["ams_orientation_candidates"]
    expected["ams_ppw"] = counts["ams_ppw"]
    expected["ams_ppw_courses"] = counts["ams_ppw_courses"]
    expected["ams_ppw_approval_cycles"] = counts["ams_ppw_approval_cycles"]
    expected["ams_ppw_approval_stages"] = counts["ams_ppw_approval_stages"]
    expected["ams_ppw_signatures"] = counts["ams_ppw_signatures"]
    expected["ams_advisory_committees"] = counts["ams_advisory_committees"]
    expected["ams_committee_members"] = counts["ams_committee_members"]
    expected["ams_digital_signatures"] = counts["ams_digital_signatures"]
    expected["ams_grade_sheets"] = counts["ams_grade_sheets"]
    expected["ams_grade_entries"] = counts["ams_grade_entries"]
    expected["ams_approval_stages"] = counts["ams_approval_stages"]
    expected["ams_student_enrollments"] = counts["ams_student_enrollments"]
    expected["ams_course_registrations"] = counts["ams_course_registrations"]
    expected["ams_admit_cards"] = counts["ams_admit_cards"]
    expected["ams_course_offerings"] = counts["ams_course_offerings"]
    expected["ams_offering_faculty"] = counts["ams_offering_faculty"]
    expected["ams_semesters"] = counts["ams_semesters"]
    expected["ams_academic_calendars"] = counts["ams_academic_calendars"]
    expected["ams_audit_logs"] = counts["ams_audit_logs"]
    expected["ams_admission_applications"] = 0 if preserve_admission_applications else counts["ams_admission_applications"]
    # ALL refresh tokens are cleared now, including the Super Admin's own.
    expected["ams_refresh_tokens"] = counts["ams_refresh_tokens"]

    notif_n = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_notifications WHERE user_id = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    courses_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_courses WHERE created_by = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    admission_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_admission_applications WHERE reviewed_by = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    expected["ams_notifications"] = notif_n
    expected["_ams_courses_created_by_nulled"] = courses_touched
    expected["_ams_admission_applications_reviewed_by_nulled"] = admission_touched if preserve_admission_applications else 0

    plan = Plan(superadmin_id=str(sa.id), other_user_ids=other_ids, counts_before=counts, expected_deletes=expected)
    return plan


def print_plan(env_info: dict, plan: Plan, preserve_admission_applications: bool) -> None:
    print("=" * 78)
    print("AMS PRODUCTION DATABASE CLEANUP — PLAN (no changes made yet)")
    print("=" * 78)
    print(f"Database              : {env_info['database']}")
    print(f"Server address         : {env_info['server_addr']}")
    print(f"Alembic revision       : {env_info['alembic_revision']}")
    print(f"ENVIRONMENT setting    : {env_info['environment_setting']}  (DEBUG={env_info['debug_setting']})")
    if looks_non_production(env_info):
        print()
        print("*** WARNING: this target does not look like a production database/")
        print("*** environment (local host and/or ENVIRONMENT != 'production').")
        print("*** Proceeding is only appropriate for reviewing/testing this script")
        print("*** against a DISPOSABLE database, never the shared local dev DB.")
    print()
    print(f"Super Admin to keep    : {REQUIRED_SUPERADMIN_EMAIL}  (id={plan.superadmin_id})")
    print(f"Users to remove        : {len(plan.other_user_ids)}  (ALL other users — this is a full dev-dump reset)")
    print(f"Roles kept ACTIVE (ams_roles master data): {', '.join(REQUIRED_ROLES_TO_SUPPORT)}")
    print(f"Roles set INACTIVE (ams_roles master data, rows NOT deleted): {', '.join(ROLES_TO_DEACTIVATE)}")
    print("(ams_user_role Postgres enum is untouched — all 8 values remain valid at the DB level.)")
    print()
    print("Rows expected to be deleted per table:")
    always_show = {
        "ams_orientation_candidates", "ams_ppw", "ams_advisory_committees",
        "ams_digital_signatures", "ams_grade_sheets", "ams_student_enrollments",
        "ams_course_registrations", "ams_admit_cards", "ams_course_offerings",
        "ams_semesters", "ams_academic_calendars", "ams_users",
        "ams_notifications", "ams_refresh_tokens", "ams_audit_logs",
        "ams_admission_applications",
    }
    for t, cls, reason in TABLE_CLASSIFICATION:
        if t in plan.expected_deletes:
            n = plan.expected_deletes[t]
            if n or t in always_show:
                print(f"  {t:32s} currently={plan.counts_before.get(t, '?'):>4}   to delete={n:>4}")
    print()
    print(f"  ams_courses: created_by will be set to NULL on {plan.expected_deletes.get('_ams_courses_created_by_nulled', 0)} row(s) "
          "(rows themselves preserved).")
    if preserve_admission_applications:
        print(f"  ams_admission_applications: rows PRESERVED (--preserve-admission-applications passed); "
              f"reviewed_by will be set to NULL on {plan.expected_deletes.get('_ams_admission_applications_reviewed_by_nulled', 0)} row(s).")
    else:
        print(f"  ams_admission_applications: {plan.expected_deletes.get('ams_admission_applications', 0)} row(s) WILL BE DELETED "
              "(default — dev-dump data; pass --preserve-admission-applications to keep instead).")
    print()
    print("Tables left completely untouched (PRESERVE, no data or schema change at all): "
          + ", ".join(t for t, cls, _ in TABLE_CLASSIFICATION if cls == "PRESERVE"))
    print("=" * 78)


async def run_cleanup(conn, plan: Plan, preserve_admission_applications: bool) -> None:
    ids = plan.other_user_ids

    # 1. PPW tree (cascades ppw_courses / ppw_approval_cycles / ppw_approval_stages / ppw_signatures)
    await conn.execute(text("DELETE FROM ams_ppw WHERE student_id = ANY(:ids)"), {"ids": ids})

    # 2. Advisory committees (cascades committee_members)
    await conn.execute(text("DELETE FROM ams_advisory_committees WHERE student_id = ANY(:ids)"), {"ids": ids})

    # 3. Digital signatures — must precede grade_sheets (FK is ON DELETE NO ACTION)
    await conn.execute(text("DELETE FROM ams_digital_signatures"))

    # 4. Grade sheets (cascades grade_entries + approval_stages)
    await conn.execute(text("DELETE FROM ams_grade_sheets"))

    # 5. Student enrollments — must follow grade_sheets (grade_entries.enrollment_id NO ACTION)
    await conn.execute(text("DELETE FROM ams_student_enrollments"))

    # 6. Course registrations — must follow student_enrollments (registration_id NO ACTION)
    await conn.execute(text("DELETE FROM ams_course_registrations"))

    # 7. Admit cards — must precede semesters (semester_id NO ACTION)
    await conn.execute(text("DELETE FROM ams_admit_cards"))

    # 8. Course offerings (cascades offering_faculty) — must follow grade_sheets/enrollments
    await conn.execute(text("DELETE FROM ams_course_offerings"))

    # 9. Semesters — must follow admit_cards/course_offerings/course_registrations
    await conn.execute(text("DELETE FROM ams_semesters"))

    # 10. Academic calendars — must follow semesters/course_offerings/course_registrations
    await conn.execute(text("DELETE FROM ams_academic_calendars"))

    # 11. Courses preserved — only null the dangling audit FK
    await conn.execute(text("UPDATE ams_courses SET created_by = NULL WHERE created_by = ANY(:ids)"), {"ids": ids})

    # 12. Admission applications — deleted by default now (dev-dump data);
    # --preserve-admission-applications keeps rows and only nulls reviewed_by.
    if preserve_admission_applications:
        await conn.execute(
            text("UPDATE ams_admission_applications SET reviewed_by = NULL WHERE reviewed_by = ANY(:ids)"),
            {"ids": ids},
        )
    else:
        await conn.execute(text("DELETE FROM ams_admission_applications"))

    # 13. Orientation candidates — explicit business requirement; must precede users
    await conn.execute(text("DELETE FROM ams_orientation_candidates"))

    # 14. Per-user notifications (ON DELETE CASCADE would also handle this once
    # users are removed; done explicitly first for accurate row-count reporting).
    await conn.execute(text("DELETE FROM ams_notifications WHERE user_id = ANY(:ids)"), {"ids": ids})

    # 15. ALL refresh tokens — including the Super Admin's own (clean auth
    # state; they will need to log in again after this).
    await conn.execute(text("DELETE FROM ams_refresh_tokens"))

    # 16. Audit logs — entire DB is from a dev dump, so all audit history here
    # is dev/test activity. Table/schema untouched, rows cleared.
    await conn.execute(text("DELETE FROM ams_audit_logs"))

    # 17. Finally, the users themselves.
    await conn.execute(text("DELETE FROM ams_users WHERE id = ANY(:ids)"), {"ids": ids})

    # 18. ams_roles — DATA update only (is_active flip), no row deleted, no
    # is_system change, no schema/enum change. Reversible via the app's own
    # existing role-management toggle.
    await conn.execute(
        text("UPDATE ams_roles SET is_active = true WHERE code = ANY(:codes)"),
        {"codes": list(REQUIRED_ROLES_TO_SUPPORT)},
    )
    await conn.execute(
        text("UPDATE ams_roles SET is_active = false WHERE code = ANY(:codes)"),
        {"codes": list(ROLES_TO_DEACTIVATE)},
    )


async def verify_after(conn, preserve_admission_applications: bool) -> list[str]:
    """Returns a list of problem descriptions; empty list means all checks passed."""
    problems = []

    users = (await conn.execute(text("SELECT id, email, role, is_active FROM ams_users"))).all()
    if len(users) != 1:
        problems.append(f"Expected exactly 1 user after cleanup, found {len(users)}.")
    elif users[0].email.lower() != REQUIRED_SUPERADMIN_EMAIL.lower():
        problems.append(f"The single remaining user is '{users[0].email}', not '{REQUIRED_SUPERADMIN_EMAIL}'.")
    elif users[0].role != REQUIRED_SUPERADMIN_ROLE:
        problems.append(f"Remaining user's role is '{users[0].role}', not '{REQUIRED_SUPERADMIN_ROLE}'.")
    elif not users[0].is_active:
        problems.append("Remaining Super Admin account is not active (is_active=False).")

    # Orphan checks — every NO ACTION FK to ams_users must now be satisfiable.
    orphan_checks = {
        "ams_admit_cards.student_id": "SELECT COUNT(*) FROM ams_admit_cards a LEFT JOIN ams_users u ON u.id=a.student_id WHERE u.id IS NULL",
        "ams_advisory_committees.student_id": "SELECT COUNT(*) FROM ams_advisory_committees a LEFT JOIN ams_users u ON u.id=a.student_id WHERE u.id IS NULL",
        "ams_committee_members.faculty_id": "SELECT COUNT(*) FROM ams_committee_members a LEFT JOIN ams_users u ON u.id=a.faculty_id WHERE u.id IS NULL",
        "ams_course_registrations.student_id": "SELECT COUNT(*) FROM ams_course_registrations a LEFT JOIN ams_users u ON u.id=a.student_id WHERE u.id IS NULL",
        "ams_digital_signatures.user_id": "SELECT COUNT(*) FROM ams_digital_signatures a LEFT JOIN ams_users u ON u.id=a.user_id WHERE u.id IS NULL",
        "ams_grade_entries.student_id": "SELECT COUNT(*) FROM ams_grade_entries a LEFT JOIN ams_users u ON u.id=a.student_id WHERE u.id IS NULL",
        "ams_orientation_candidates.student_user_id": "SELECT COUNT(*) FROM ams_orientation_candidates a LEFT JOIN ams_users u ON u.id=a.student_user_id WHERE a.student_user_id IS NOT NULL AND u.id IS NULL",
        "ams_ppw.student_id": "SELECT COUNT(*) FROM ams_ppw a LEFT JOIN ams_users u ON u.id=a.student_id WHERE u.id IS NULL",
        "ams_student_enrollments.student_id": "SELECT COUNT(*) FROM ams_student_enrollments a LEFT JOIN ams_users u ON u.id=a.student_id WHERE u.id IS NULL",
        "ams_admission_applications.reviewed_by": "SELECT COUNT(*) FROM ams_admission_applications a LEFT JOIN ams_users u ON u.id=a.reviewed_by WHERE a.reviewed_by IS NOT NULL AND u.id IS NULL",
        "ams_courses.created_by": "SELECT COUNT(*) FROM ams_courses a LEFT JOIN ams_users u ON u.id=a.created_by WHERE a.created_by IS NOT NULL AND u.id IS NULL",
    }
    for label, sql in orphan_checks.items():
        n = (await conn.execute(text(sql))).scalar_one()
        if n:
            problems.append(f"{n} orphaned/dangling row(s) remain for {label}.")

    # Tables that must now be fully empty.
    empty_tables = [
        "ams_orientation_candidates", "ams_ppw", "ams_ppw_courses", "ams_ppw_approval_cycles",
        "ams_ppw_approval_stages", "ams_ppw_signatures", "ams_advisory_committees",
        "ams_committee_members", "ams_digital_signatures", "ams_grade_sheets", "ams_grade_entries",
        "ams_approval_stages", "ams_student_enrollments", "ams_course_registrations",
        "ams_admit_cards", "ams_course_offerings", "ams_offering_faculty", "ams_semesters",
        "ams_academic_calendars", "ams_refresh_tokens", "ams_notifications", "ams_audit_logs",
    ]
    if not preserve_admission_applications:
        empty_tables.append("ams_admission_applications")
    for t in empty_tables:
        n = (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
        if n:
            problems.append(f"Expected {t} to be empty after cleanup, found {n} row(s).")

    # Roles master data: rows preserved (count unchanged, checked by caller via
    # counts_before/after), correct active/inactive split, is_system untouched.
    role_rows = (await conn.execute(text("SELECT code, is_active, is_system FROM ams_roles"))).all()
    role_state = {r.code: (r.is_active, r.is_system) for r in role_rows}
    for code in REQUIRED_ROLES_TO_SUPPORT:
        if code not in role_state:
            problems.append(f"ams_roles row for required role '{code}' is missing.")
        elif role_state[code][0] is not True:
            problems.append(f"ams_roles row for '{code}' should be active, is not.")
    for code in ROLES_TO_DEACTIVATE:
        if code not in role_state:
            problems.append(f"ams_roles row for '{code}' is missing (should still exist, just inactive).")
        elif role_state[code][0] is not False:
            problems.append(f"ams_roles row for '{code}' should be inactive, is not.")
    if any(not is_system for _, (_, is_system) in role_state.items()):
        problems.append("An ams_roles system row unexpectedly has is_system=False after cleanup.")

    return problems


async def print_master_data_summary(conn) -> None:
    for label, sql in [
        ("Colleges", "SELECT COUNT(*) FROM ams_colleges"),
        ("Departments", "SELECT COUNT(*) FROM ams_departments"),
        ("Programmes", "SELECT COUNT(*) FROM ams_programs"),
        ("Programme<->Department links", "SELECT COUNT(*) FROM ams_program_departments"),
        ("Courses", "SELECT COUNT(*) FROM ams_courses"),
        ("Designations", "SELECT COUNT(*) FROM ams_designations"),
        ("Roles (master data rows, all preserved)", "SELECT COUNT(*) FROM ams_roles"),
    ]:
        n = (await conn.execute(text(sql))).scalar_one()
        print(f"  {label:40s}: {n}")
    active = (await conn.execute(text("SELECT code FROM ams_roles WHERE is_active ORDER BY code"))).scalars().all()
    inactive = (await conn.execute(text("SELECT code FROM ams_roles WHERE NOT is_active ORDER BY code"))).scalars().all()
    print(f"  {'Active roles':40s}: {', '.join(active)}")
    print(f"  {'Inactive roles (rows kept)':40s}: {', '.join(inactive)}")


async def main_async(args: argparse.Namespace) -> int:
    engine = make_engine()
    try:
        async with engine.connect() as conn:
            env_info = await describe_environment(conn)
            plan = await build_plan(conn, args.preserve_admission_applications)
            if plan is None:
                return 2
            print_plan(env_info, plan, args.preserve_admission_applications)

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

        async with engine.begin() as conn:
            # Re-check preconditions and re-plan INSIDE the transaction, in
            # case anything changed between the dry-run read above and now.
            plan = await build_plan(conn, args.preserve_admission_applications)
            if plan is None:
                raise RuntimeError("Preconditions no longer satisfied — aborting inside transaction.")

            await run_cleanup(conn, plan, args.preserve_admission_applications)

            problems = await verify_after(conn, args.preserve_admission_applications)
            if problems:
                print("Post-cleanup verification FAILED — rolling back, no changes will be kept:")
                for p in problems:
                    print(f"  - {p}")
                raise RuntimeError("Post-cleanup verification failed.")

            print()
            print("Post-cleanup verification passed. Committing transaction...")

        async with engine.connect() as conn:
            counts_after = await fetch_counts(conn)
            env_after = await describe_environment(conn)
            print()
            print("=" * 78)
            print("CLEANUP COMPLETE")
            print("=" * 78)
            print(f"Alembic revision (unchanged) : {env_after['alembic_revision']}")
            print(f"Users remaining               : {counts_after['ams_users']}")
            print(f"Refresh tokens remaining      : {counts_after['ams_refresh_tokens']}")
            print(f"Orientation candidates        : {counts_after['ams_orientation_candidates']}")
            print("Master data preserved:")
            await print_master_data_summary(conn)
            print()
            print("The Super Admin's browser session(s) were invalidated along with all other")
            print("refresh tokens — they must log in again; their account itself is unaffected.")
        return 0
    finally:
        await engine.dispose()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Inspect and print the plan only (default behavior).")
    p.add_argument("--confirm-production-cleanup", action="store_true",
                   help="Actually perform the destructive cleanup (after a typed confirmation prompt).")
    p.add_argument("--preserve-admission-applications", action="store_true",
                   help="Keep ams_admission_applications rows instead of deleting them (default: deleted, since the whole DB is dev-dump data).")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
