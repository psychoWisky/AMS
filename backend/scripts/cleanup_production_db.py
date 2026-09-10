"""
AMS ONE-TIME PRODUCTION DATABASE CLEANUP SCRIPT
================================================

Purpose
-------
The AMS production database was created from a DEVELOPMENT DATABASE DUMP. A
first cleanup pass has already run successfully on production (users reduced
to 1, transactional/application data cleared). This SECOND-PASS update goes
further per an updated business requirement: it now also removes the
remaining development MASTER data (Colleges, Departments, Programmes, the
Programme<->Department M:N table) and HARD-DELETES the 4 non-required
`ams_roles` rows, rather than merely deactivating them. It still preserves:

  * the existing `superadmin@avfu.ac.in` account (same ID, same password
    hash, same role, same active flag — never deleted/recreated)
  * `ams_designations` — untouched (no FK involvement at all, see "FK
    findings" below; nothing in this task's requirement or the schema forces
    a change here)
  * `ams_courses` ROWS — untouched (kept as curriculum master data); only
    their now-dangling `department_id` FK is nulled (see "FK findings")
  * the `ams_user_role` Postgres enum's VALUES (none dropped) — all 8 remain
    valid at the DB level; only `ams_roles` ROWS for the 4 unwanted codes are
    deleted (see "Roles" below)
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

FK findings (inspected directly against app/models/ and the live schema,
not assumed) that shape this second-pass deletion order
--------------------------------------------------------
Tables now being fully cleared — ams_colleges, ams_departments, ams_programs,
ams_program_departments — are referenced by:
  * ams_program_departments.program_id/department_id  -> ON DELETE CASCADE
    (from both Program and Department) — deleting Program/Department alone
    would already cascade this table away; it is still deleted FIRST and
    explicitly, per the requested order, which is always FK-safe (child
    before parent).
  * ams_programs.department_id (legacy, unused by app code)     -> NO ACTION
  * ams_users.department_id / ams_users.program_id              -> NO ACTION
  * ams_courses.department_id                                    -> NO ACTION
  * ams_course_availability.department_id                        -> NO ACTION,
    and this column is NOT NULL at the schema level (unlike the others)
  * ams_orientation_candidates.{college,program,department}_id  -> NO ACTION
  * ams_admission_applications.program_id                        -> NO ACTION
  * ams_course_offerings.department_id                           -> NO ACTION
Every one of the last two rows' tables (orientation_candidates, admission_
applications, course_offerings) is already emptied by this script's existing
transactional-data cleanup (unconditionally, regardless of which pass this
is — see "existing production state" below), so by the time the new
master-data deletion runs, only three things can still legitimately hold a
reference and must be handled explicitly:
  1. `ams_courses.department_id` — nullable, so it is set to NULL for ALL
     rows (not just ones created by removed users — every course's
     department is being deleted). Course ROWS are never touched/deleted.
  2. `ams_course_availability.department_id` — NOT NULL, so a row here
     CANNOT be preserved once its department is deleted (nulling is not an
     option the schema allows). The minimum safe action is deleting these
     rows outright. Locally this table currently has 0 rows; the statement
     is still included, unconditionally, for production-safety in case any
     exist there.
  3. `ams_users.department_id` / `ams_users.program_id` — nullable, set to
     NULL for ALL remaining users (in practice just the Super Admin — was
     confirmed locally to have `department_id` set from seed data). This
     touches ONLY these two fields; the account's id/password hash/role/
     is_active are completely unaffected (verified by `verify_after`).
`ams_designations` has NO foreign key columns at all (flat master data,
confirmed by inspecting `app/models/user.py::Designation`) and no other
table references it — it is entirely unaffected by any of this and is left
alone, per the explicit instruction to preserve it unless the schema proves
otherwise (it does not).

Roles
-----
AMS does NOT store authorization roles as rows that can be "reduced to four"
at the schema level. `User.role` is backed by a native PostgreSQL enum type
(`ams_user_role`) with 8 members (SUPER_ADMIN, ACADEMIC_ADMIN, HOD, FACULTY,
STUDENT, REGISTRAR, EXAMINER, RESEARCH_SUPERVISOR) defined by
`app.models.user.UserRole`. Enum VALUES are NEVER dropped by this script —
that would require rebuilding the type, an explicitly-forbidden schema
change, and the task explicitly says the enum may keep all 8 historical
values.

Separately, `ams_roles` is a master-data table that feeds ONLY the Admin
"Role Management" screen (`GET/POST/PATCH/DELETE /admin/roles`) — it has NO
foreign key from `User.role` (confirmed by inspection — nothing anywhere
references `ams_roles.id`) and does not gate what a real user's role can be;
`require_roles()` and every authorization check in the codebase read
`User.role` (the enum) directly, never this table. Per this task's updated,
explicit instruction, the script now HARD-DELETES the 4 rows for
ACADEMIC_ADMIN/REGISTRAR/EXAMINER/RESEARCH_SUPERVISOR (not merely setting
`is_active=False` as the first-pass script did) and leaves the 4 required
rows (SUPER_ADMIN/STUDENT/HOD/FACULTY) completely untouched, including their
`is_system` flag. Because nothing references `ams_roles.id`, this delete is
unconditionally FK-safe. This is a genuine, harder-to-reverse change than the
first pass's toggle — re-adding one of the 4 deleted role rows later (if
"more roles are added" as the task anticipates) is a normal `POST
/admin/roles`-equivalent insert, not a schema change, so this remains
non-destructive to the schema/enum even though it is destructive to that
specific master-data row.

IMPORTANT KNOWN LIMITATION (carried over from the first pass, still true):
the "Add User" role dropdown in `frontend/lib/utils.ts` (`ROLES`/
`ADMIN_ROLES`) is a HARDCODED list, completely independent of `ams_roles`.
Deleting these 4 rows removes them from the Admin "Role Management" screen
but does not touch that dropdown — unrelated frontend source code, not
approved for change here.

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
Unchanged from the first-pass script — not part of this update.

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
# requirement). Enforced by this script:
#   1. Reported here for the printed plan/verification (the ams_user_role
#      enum itself is never touched — see module docstring, "Roles").
#   2. The 4 `ams_roles` ROWS below in `ROLES_TO_DELETE` are HARD-DELETED
#      (not merely deactivated — updated instruction, second pass). Nothing
#      references `ams_roles.id` (confirmed by inspection), so this is
#      unconditionally FK-safe. `is_system` is never written for any row.
REQUIRED_ROLES_TO_SUPPORT = ("SUPER_ADMIN", "STUDENT", "HOD", "FACULTY")
ROLES_TO_DELETE = ("ACADEMIC_ADMIN", "REGISTRAR", "EXAMINER", "RESEARCH_SUPERVISOR")

CONFIRMATION_PHRASE = "DELETE PRODUCTION DATA"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# All 32 AMS tables (from `Base.metadata`, `app.main` imported), classified.
# Kept here purely for the printed report — the actual DELETE/UPDATE
# statements below are independent, explicit, and hand-ordered from the real
# FK graph (see the review report for the full derivation).
TABLE_CLASSIFICATION: list[tuple[str, str, str]] = [
    # (table, classification, reason)
    ("ams_users", "PRESERVE SELECTIVELY", "Keep only superadmin@avfu.ac.in; remove every other (dummy/dev) user; department_id/program_id nulled."),
    ("ams_roles", "DELETE 4 ROWS", "Hard-delete ACADEMIC_ADMIN/REGISTRAR/EXAMINER/RESEARCH_SUPERVISOR rows (second-pass instruction); the 4 required rows are untouched, including is_system."),
    ("ams_colleges", "DELETE ALL DATA", "Second-pass instruction: dev-dump master data now cleared entirely; nothing references college_id once orientation_candidates is empty."),
    ("ams_departments", "DELETE ALL DATA", "Same; department_id FKs from courses/course_availability/users are cleared first (see module docstring FK findings)."),
    ("ams_designations", "PRESERVE", "Flat master data, no FK columns at all — unaffected by this cleanup, confirmed by inspection."),
    ("ams_programs", "DELETE ALL DATA", "Same as colleges/departments; must be deleted before departments (its own legacy department_id FK)."),
    ("ams_program_departments", "DELETE ALL DATA", "Second-pass instruction — deleted explicitly first (child-before-parent), though CASCADE from programs/departments would also remove it."),
    ("ams_courses", "PRESERVE SELECTIVELY", "Rows kept (curriculum master data, not in the delete list); created_by nulled for removed users, department_id nulled for ALL rows (its department is being deleted)."),
    ("ams_course_availability", "DELETE ALL DATA", "department_id is NOT NULL at the schema level — a row cannot survive its department being deleted, so nulling is not possible; deleting is the minimum safe action. 0 rows locally."),
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

    # Second-pass master-data deletion (all rows, unconditional).
    expected["ams_colleges"] = counts["ams_colleges"]
    expected["ams_departments"] = counts["ams_departments"]
    expected["ams_programs"] = counts["ams_programs"]
    expected["ams_program_departments"] = counts["ams_program_departments"]
    expected["ams_course_availability"] = counts["ams_course_availability"]

    role_rows_to_delete = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_roles WHERE code = ANY(:codes)"), {"codes": list(ROLES_TO_DELETE)},
    )).scalar_one()
    expected["ams_roles"] = role_rows_to_delete

    notif_n = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_notifications WHERE user_id = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    courses_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_courses WHERE created_by = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    courses_dept_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_courses WHERE department_id IS NOT NULL"),
    )).scalar_one()
    users_dept_prog_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_users WHERE department_id IS NOT NULL OR program_id IS NOT NULL"),
    )).scalar_one()
    admission_touched = (await conn.execute(
        text("SELECT COUNT(*) FROM ams_admission_applications WHERE reviewed_by = ANY(:ids)"), {"ids": other_ids},
    )).scalar_one() if other_ids else 0
    expected["ams_notifications"] = notif_n
    expected["_ams_courses_created_by_nulled"] = courses_touched
    expected["_ams_courses_department_id_nulled"] = courses_dept_touched
    expected["_ams_users_department_program_nulled"] = users_dept_prog_touched
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
    print()
    print("Roles to KEEP:")
    for code in REQUIRED_ROLES_TO_SUPPORT:
        print(f"  {code}")
    print()
    print("Roles to DELETE:")
    for code in ROLES_TO_DELETE:
        print(f"  {code}")
    print(f"(ams_roles rows to delete now: {plan.expected_deletes.get('ams_roles', 0)} of {len(ROLES_TO_DELETE)} expected codes found; "
          "is_system is never modified; the ams_user_role Postgres enum is untouched — all 8 values remain valid at the DB level.)")
    print()
    print("Master data to DELETE:")
    for t in ("ams_colleges", "ams_departments", "ams_programs", "ams_program_departments"):
        print(f"  {t}")
    print()
    print("Rows expected to be deleted per table:")
    always_show = {
        "ams_orientation_candidates", "ams_ppw", "ams_advisory_committees",
        "ams_digital_signatures", "ams_grade_sheets", "ams_student_enrollments",
        "ams_course_registrations", "ams_admit_cards", "ams_course_offerings",
        "ams_semesters", "ams_academic_calendars", "ams_users",
        "ams_notifications", "ams_refresh_tokens", "ams_audit_logs",
        "ams_admission_applications", "ams_colleges", "ams_departments",
        "ams_programs", "ams_program_departments", "ams_course_availability",
        "ams_roles",
    }
    for t, cls, reason in TABLE_CLASSIFICATION:
        if t in plan.expected_deletes:
            n = plan.expected_deletes[t]
            if n or t in always_show:
                print(f"  {t:32s} currently={plan.counts_before.get(t, '?'):>4}   to delete={n:>4}")
    print()
    print(f"  ams_courses: created_by will be set to NULL on {plan.expected_deletes.get('_ams_courses_created_by_nulled', 0)} row(s) "
          f"(removed-user rows); department_id will be set to NULL on {plan.expected_deletes.get('_ams_courses_department_id_nulled', 0)} row(s) "
          "(ALL rows with a department set — that department is being deleted). Course rows themselves are never deleted.")
    print(f"  ams_users: department_id/program_id will be set to NULL on {plan.expected_deletes.get('_ams_users_department_program_nulled', 0)} "
          "remaining row(s) (their department/programme is being deleted; id/password hash/role/is_active are untouched).")
    if preserve_admission_applications:
        print(f"  ams_admission_applications: rows PRESERVED (--preserve-admission-applications passed); "
              f"reviewed_by will be set to NULL on {plan.expected_deletes.get('_ams_admission_applications_reviewed_by_nulled', 0)} row(s).")
    else:
        print(f"  ams_admission_applications: {plan.expected_deletes.get('ams_admission_applications', 0)} row(s) WILL BE DELETED "
              "(default — dev-dump data; pass --preserve-admission-applications to keep instead).")
    print()
    print("Tables left completely untouched (PRESERVE, no data or schema change at all): "
          + ", ".join(t for t, cls, _ in TABLE_CLASSIFICATION if cls == "PRESERVE"))
    print()
    print("Expected final counts:")
    print("  Expected final users:                      1")
    print("  Expected final roles:                      4")
    print("  Expected final colleges:                   0")
    print("  Expected final departments:                0")
    print("  Expected final programmes:                 0")
    print("  Expected final programme-department links: 0")
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

    # ── Second pass: master data (Colleges/Departments/Programmes/M:N) ──────
    # By this point every table that could hold a NO-ACTION reference to
    # Programme/Department/College is already empty (orientation_candidates,
    # admission_applications [unless --preserve-admission-applications],
    # course_offerings — all cleared above). Three references remain and are
    # handled explicitly here, per the FK findings in the module docstring:

    # 17a. ams_courses.department_id — nullable; cleared for ALL rows (every
    # course's department is being deleted). Course rows themselves are never
    # touched/deleted — this is the "minimum safe change" for this table.
    await conn.execute(text("UPDATE ams_courses SET department_id = NULL"))

    # 17b. ams_course_availability — department_id is NOT NULL at the schema
    # level, so a row cannot survive its department being deleted; nulling is
    # not an option here. Deleting is the minimum safe action (0 rows
    # locally; included unconditionally for production-safety).
    await conn.execute(text("DELETE FROM ams_course_availability"))

    # 17c. ams_users.department_id / program_id — nullable; cleared for ALL
    # remaining users (in practice just the Super Admin, confirmed locally to
    # have department_id set from seed data). Only these two columns are
    # touched — id/password hash/role/is_active are never written here.
    await conn.execute(text("UPDATE ams_users SET department_id = NULL, program_id = NULL"))

    # 18. Programme<->Department M:N — deleted explicitly first (child before
    # parent), though ON DELETE CASCADE from both ams_programs and
    # ams_departments would also remove it once they're deleted below.
    await conn.execute(text("DELETE FROM ams_program_departments"))

    # 19. Programmes — must precede departments (its own legacy department_id
    # FK is ON DELETE NO ACTION).
    await conn.execute(text("DELETE FROM ams_programs"))

    # 20. Departments — every remaining NO ACTION reference (courses,
    # course_availability, users, programs, program_departments) has been
    # cleared above.
    await conn.execute(text("DELETE FROM ams_departments"))

    # 21. Colleges — orientation_candidates (its only referencer) is already
    # empty from step 13 above.
    await conn.execute(text("DELETE FROM ams_colleges"))

    # 22. Unwanted role rows — hard delete (second-pass instruction, not a
    # mere is_active flip). Nothing references ams_roles.id, so this is
    # unconditionally FK-safe; is_system is never written for any row.
    await conn.execute(text("DELETE FROM ams_roles WHERE code = ANY(:codes)"), {"codes": list(ROLES_TO_DELETE)})

    # 23. Finally, the users themselves.
    await conn.execute(text("DELETE FROM ams_users WHERE id = ANY(:ids)"), {"ids": ids})


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
        # Second-pass master-data orphan checks — with ams_departments/
        # ams_programs now at 0 rows, ANY remaining non-null reference would
        # show up here as orphaned, which doubles as proof the NULL-out steps
        # actually ran (not just "table is empty").
        "ams_courses.department_id": "SELECT COUNT(*) FROM ams_courses a LEFT JOIN ams_departments d ON d.id=a.department_id WHERE a.department_id IS NOT NULL AND d.id IS NULL",
        "ams_users.department_id": "SELECT COUNT(*) FROM ams_users a LEFT JOIN ams_departments d ON d.id=a.department_id WHERE a.department_id IS NOT NULL AND d.id IS NULL",
        "ams_users.program_id": "SELECT COUNT(*) FROM ams_users a LEFT JOIN ams_programs p ON p.id=a.program_id WHERE a.program_id IS NOT NULL AND p.id IS NULL",
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
        # Second-pass master data — must now be fully empty.
        "ams_colleges", "ams_departments", "ams_programs", "ams_program_departments",
        "ams_course_availability",
    ]
    if not preserve_admission_applications:
        empty_tables.append("ams_admission_applications")
    for t in empty_tables:
        n = (await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
        if n:
            problems.append(f"Expected {t} to be empty after cleanup, found {n} row(s).")

    # Roles master data — second pass: exactly 4 rows must remain, exactly
    # matching REQUIRED_ROLES_TO_SUPPORT, none of ROLES_TO_DELETE present,
    # is_system untouched (still True) for every surviving row.
    role_rows = (await conn.execute(text("SELECT code, is_active, is_system FROM ams_roles"))).all()
    role_codes = {r.code for r in role_rows}
    if len(role_rows) != len(REQUIRED_ROLES_TO_SUPPORT):
        problems.append(f"Expected exactly {len(REQUIRED_ROLES_TO_SUPPORT)} ams_roles rows after cleanup, found {len(role_rows)}.")
    for code in REQUIRED_ROLES_TO_SUPPORT:
        if code not in role_codes:
            problems.append(f"ams_roles row for required role '{code}' is missing.")
    for code in ROLES_TO_DELETE:
        if code in role_codes:
            problems.append(f"ams_roles row for '{code}' should have been deleted, but still exists.")
    if any(not r.is_system for r in role_rows):
        problems.append("A remaining ams_roles row unexpectedly has is_system=False after cleanup.")

    return problems


async def print_master_data_summary(conn) -> None:
    for label, sql in [
        ("Colleges (expected 0)", "SELECT COUNT(*) FROM ams_colleges"),
        ("Departments (expected 0)", "SELECT COUNT(*) FROM ams_departments"),
        ("Programmes (expected 0)", "SELECT COUNT(*) FROM ams_programs"),
        ("Programme<->Department links (expected 0)", "SELECT COUNT(*) FROM ams_program_departments"),
        ("Courses (preserved, rows kept)", "SELECT COUNT(*) FROM ams_courses"),
        ("Course availability (expected 0)", "SELECT COUNT(*) FROM ams_course_availability"),
        ("Designations (preserved, untouched)", "SELECT COUNT(*) FROM ams_designations"),
        ("Roles (expected 4)", "SELECT COUNT(*) FROM ams_roles"),
    ]:
        n = (await conn.execute(text(sql))).scalar_one()
        print(f"  {label:45s}: {n}")
    remaining_roles = (await conn.execute(text("SELECT code FROM ams_roles ORDER BY code"))).scalars().all()
    print(f"  {'Remaining role codes':45s}: {', '.join(remaining_roles)}")


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
            print("Master data final state:")
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
