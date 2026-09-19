"""Standalone, dependency-free test script for the course-code
duplicate-DETECTION fix (superseding the previous, incorrect
(department_id, course_number) uniqueness rule from migration 0019).

Corrected business rule under test:
    same department + same code + SAME normalized title      -> REJECT
    same department + same code + DIFFERENT normalized title -> ALLOWED
                                                                 (bulk upload
                                                                 additionally
                                                                 warns; see
                                                                 test_course_bulk_upload_confirmation.py)
    different department + same code (any title)             -> ALLOWED

No pytest — matches the existing convention in this `tests/` directory
(see `test_email_outbox.py`'s own docstring for why). Calls the real
endpoint functions (`create_course`, `update_course`,
`_validate_course_bulk_rows`) directly with hand-built request bodies and
lightweight fake `user` objects — no HTTP layer needed here, since these
are plain async functions that only read `user.active_role`/
`user.department_id`/`user.id`, exactly like FastAPI's dependency
injection would provide. (The full bulk-upload HTTP endpoint, including
its preview/confirmation flow, is covered separately in
`test_course_bulk_upload_confirmation.py`, which needs the real
multipart/Form request shape.)

Runs against the SAME local Postgres database configured in `.env`
(`DATABASE_URL`) — never production. Every course this script creates uses
a `ZZTEST_` course-number prefix and is deleted again at the end of each
scenario in a `finally` block (so a failed assertion still cleans up). No
existing Department or User row is ever modified — departments/users are
only READ to build valid foreign keys and fake request contexts.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_course_uniqueness
"""
import asyncio
import sys
import uuid
from types import SimpleNamespace
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, delete

from app.db.base import AsyncSessionLocal
from app.models.course import Course
from app.models.user import User, UserRole, Department
from app.api.v1.endpoints import courses as courses_module
from app.api.v1.endpoints.courses import CourseIn

_MARKER = "ZZTEST_"


def _fake_number(tag: str) -> str:
    return f"{_MARKER}{tag}_{uuid.uuid4().hex[:6]}".upper()


def _fake_user(user_id, department_id, active_role) -> SimpleNamespace:
    """Lightweight stand-in for the real `User` FastAPI would inject —
    `create_course`/`update_course`/`_authorize_department_manage` only
    ever read `.id`/`.active_department_id`/`.active_role`, so a plain
    namespace is sufficient and avoids needing to persist a throwaway user
    row. Multi-role/multi-department task (this revision) — `get_current_user`
    now derives authorization from the ACTIVE ASSIGNMENT's department
    (`active_department_id`), never the legacy scalar `department_id`; this
    fixture's `department_id` parameter feeds `active_department_id`
    directly, exactly mirroring what a real single-assignment session would
    resolve to."""
    return SimpleNamespace(id=user_id, department_id=department_id, active_department_id=department_id, active_role=active_role)


async def _cleanup_by_numbers(numbers: list[str]) -> None:
    if not numbers:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Course).where(Course.course_number.in_(numbers)))
        await db.commit()


def _course_in(course_number: str, department_id, title: str = "ZZTEST Course") -> CourseIn:
    return CourseIn(course_number=course_number, title=title, department_id=department_id)


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
        else:
            self.failed.append((name, detail))
        status = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail and not ok else ""
        print(f"{status}: {name}{suffix}")


RESULTS = _Results()

# Populated by `_setup` — two distinct REAL existing departments (read-only,
# never modified) and one real existing user id (borrowed only to satisfy
# Course.created_by's FK; that user's own row is never touched).
_DEPT_A: Optional[uuid.UUID] = None
_DEPT_B: Optional[uuid.UUID] = None
_SOME_USER_ID: Optional[uuid.UUID] = None


async def _setup() -> None:
    global _DEPT_A, _DEPT_B, _SOME_USER_ID
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Department.id).order_by(Department.code).limit(2))).scalars().all()
        assert len(rows) >= 2, "test requires at least 2 existing departments in the local dev database"
        _DEPT_A, _DEPT_B = rows[0], rows[1]

        user_row = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one_or_none()
        assert user_row is not None, "test requires at least 1 existing SUPER_ADMIN user in the local dev database"
        _SOME_USER_ID = user_row


async def _dept_code(db, department_id) -> str:
    return (await db.execute(select(Department.code).where(Department.id == department_id))).scalar_one()


# ── Database-level / migration checks ───────────────────────────────────────

async def test_migration_chain_reaches_0020():
    name = "DB — alembic chain has passed 0020_course_number_not_unique and no unique constraint remains on ams_courses"
    try:
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            applied = set((await db.execute(text(
                "SELECT version_num FROM alembic_version"
            ))).scalars().all())
            # A later migration (e.g. 0021+) may legitimately be the actual
            # current head by the time this runs — this test only asserts
            # that 0020's own effect (course_number has no unique constraint)
            # is present, not that it is exactly the LATEST migration applied.
            cons = (await db.execute(text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'ams_courses'::regclass AND contype='u'"
            ))).scalars().all()
        assert applied, "expected at least one row in alembic_version"
        assert cons == [], f"expected zero unique constraints on ams_courses, found {cons}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))


async def test_department_id_course_number_duplicates_allowed_at_db_level():
    name = "DB — two rows with identical (department_id, course_number) but different titles insert cleanly (no constraint violation)"
    number = _fake_number("dbdup")
    try:
        async with AsyncSessionLocal() as db:
            db.add(Course(course_number=number, title="Research (Semester II)", department_id=_DEPT_A))
            db.add(Course(course_number=number, title="Research (Semester IV)", department_id=_DEPT_A))
            await db.commit()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == number))).scalars().all()
        assert len(rows) == 2, f"expected both rows to insert without a constraint violation, got {len(rows)}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


# ── create_course ────────────────────────────────────────────────────────────

async def test_create_same_dept_same_title_rejected():
    name = "create_course — same department + same code + SAME title -> REJECT"
    number = _fake_number("t1")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, admin)
        rejected = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, admin)
            except HTTPException as e:
                rejected = e.status_code == 409
        assert rejected, "expected the exact-duplicate create to raise HTTPException(409)"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_create_same_dept_same_code_whitespace_title_rejected():
    name = "create_course — title differing only by leading/trailing whitespace -> still REJECT"
    number = _fake_number("t2")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, admin)
        rejected = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.create_course(_course_in(number, _DEPT_A, "  Research  "), db, admin)
            except HTTPException as e:
                rejected = e.status_code == 409
        assert rejected, "expected a whitespace-only title difference to still be treated as an exact duplicate"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_create_same_dept_same_code_case_title_rejected():
    name = "create_course — title differing only by case -> still REJECT"
    number = _fake_number("t3")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, admin)
        rejected = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.create_course(_course_in(number, _DEPT_A, "RESEARCH"), db, admin)
            except HTTPException as e:
                rejected = e.status_code == 409
        assert rejected, "expected a case-only title difference to still be treated as an exact duplicate"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_create_same_dept_same_code_different_title_allowed():
    name = "create_course — same department + same code + DIFFERENT title -> ALLOWED (confirmed AVFU business case)"
    number = _fake_number("t4")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            c1 = await courses_module.create_course(_course_in(number, _DEPT_A, "Research (Semester II)"), db, admin)
        async with AsyncSessionLocal() as db:
            c2 = await courses_module.create_course(_course_in(number, _DEPT_A, "Research (Semester IV)"), db, admin)
        assert c1.id != c2.id
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == number))).scalars().all()
        assert len(rows) == 2, f"expected both differently-titled courses to exist, got {len(rows)}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_create_different_dept_same_code_same_title_allowed():
    name = "create_course — different department + same code + SAME title -> no relationship, ALLOWED"
    number = _fake_number("t5")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, admin)
        async with AsyncSessionLocal() as db:
            out_b = await courses_module.create_course(_course_in(number, _DEPT_B, "Research"), db, admin)
        assert out_b.department_id == _DEPT_B
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_create_different_dept_same_code_different_title_allowed():
    name = "create_course — different department + same code + different title -> ALLOWED"
    number = _fake_number("t6")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A, "Research (Semester II)"), db, admin)
        async with AsyncSessionLocal() as db:
            out_b = await courses_module.create_course(_course_in(number, _DEPT_B, "Something Else"), db, admin)
        assert out_b.department_id == _DEPT_B
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


# ── update_course ────────────────────────────────────────────────────────────

async def test_update_keeps_own_code_and_title_no_false_positive():
    name = "update_course — a no-op update (same code, same title) does not false-positive against itself"
    number = _fake_number("u1")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            c1 = await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, admin)
        async with AsyncSessionLocal() as db:
            result = await courses_module.update_course(c1.id, _course_in(number, _DEPT_A, "Research"), db, admin)
        assert result == {"message": "Updated."}
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_update_to_exact_duplicate_of_another_course_rejected():
    name = "update_course — updating title+code to exactly match another course in the same department -> REJECT"
    n1, n2 = _fake_number("u2a"), _fake_number("u2b")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(n1, _DEPT_A, "Research (Semester II)"), db, admin)
        async with AsyncSessionLocal() as db:
            c2 = await courses_module.create_course(_course_in(n2, _DEPT_A, "Something"), db, admin)

        conflict = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.update_course(c2.id, _course_in(n1, _DEPT_A, "Research (Semester II)"), db, admin)
            except HTTPException as e:
                conflict = e.status_code == 409
        assert conflict, "expected updating c2 into an exact duplicate of c1 to raise HTTPException(409)"

        async with AsyncSessionLocal() as db:
            fresh_c2 = await db.get(Course, c2.id)
        assert fresh_c2.course_number == n2, "the rejected update must not have changed c2 at all"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1, n2])


async def test_update_to_same_code_different_title_allowed():
    name = "update_course — updating to another course's CODE but keeping a different title -> ALLOWED"
    n1, n2 = _fake_number("u3a"), _fake_number("u3b")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(n1, _DEPT_A, "Research (Semester II)"), db, admin)
        async with AsyncSessionLocal() as db:
            c2 = await courses_module.create_course(_course_in(n2, _DEPT_A, "Something Else"), db, admin)

        async with AsyncSessionLocal() as db:
            result = await courses_module.update_course(c2.id, _course_in(n1, _DEPT_A, "Something Else"), db, admin)
        assert result == {"message": "Updated."}

        async with AsyncSessionLocal() as db:
            fresh_c2 = await db.get(Course, c2.id)
        assert fresh_c2.course_number == n1
        assert fresh_c2.title == "Something Else"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1, n2])


# ── Bulk-upload validation (_validate_course_bulk_rows) ─────────────────────

async def test_bulk_within_file_same_title_hard_reject():
    name = "bulk validate — same dept + same code + SAME title within file -> HARD REJECT"
    number = _fake_number("b1")
    try:
        async with AsyncSessionLocal() as db:
            code_a = await _dept_code(db, _DEPT_A)
            rows = [
                {"row": 12, "values": {"Course Number": number, "Course Title": "Research (Semester II)", "Department": code_a}},
                {"row": 18, "values": {"Course Number": number, "Course Title": "Research (Semester II)", "Department": code_a}},
            ]
            findings, warnings, valid = await courses_module._validate_course_bulk_rows(rows, db, force_department_id=None)
        assert valid == [], "an exact within-file duplicate must produce zero valid rows"
        assert len(findings) == 2, f"expected both offending rows flagged, got {findings}"
        assert warnings == [], f"an exact duplicate must not ALSO appear as a warning, got {warnings}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_bulk_within_file_different_title_warning():
    name = "bulk validate — same dept + same code + DIFFERENT title within file -> WARNING, both rows accepted"
    number = _fake_number("b2")
    try:
        async with AsyncSessionLocal() as db:
            code_a = await _dept_code(db, _DEPT_A)
            rows = [
                {"row": 12, "values": {"Course Number": number, "Course Title": "Research (Semester II)", "Department": code_a}},
                {"row": 18, "values": {"Course Number": number, "Course Title": "Research (Semester IV)", "Department": code_a}},
            ]
            findings, warnings, valid = await courses_module._validate_course_bulk_rows(rows, db, force_department_id=None)
        assert findings == [], f"expected no hard findings, got {findings}"
        assert len(valid) == 2, f"expected both differently-titled rows accepted, got {valid}"
        assert len(warnings) >= 1, "expected at least one duplicate-code warning"
        assert any(w["row"] == 18 for w in warnings), f"expected the second row to carry the warning, got {warnings}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_bulk_different_departments_same_code_no_warning():
    name = "bulk validate — same code across two different departments in the same file -> ALLOWED, no warning"
    number = _fake_number("b3")
    try:
        async with AsyncSessionLocal() as db:
            code_a = await _dept_code(db, _DEPT_A)
            code_b = await _dept_code(db, _DEPT_B)
            rows = [
                {"row": 12, "values": {"Course Number": number, "Course Title": "Research", "Department": code_a}},
                {"row": 18, "values": {"Course Number": number, "Course Title": "Research", "Department": code_b}},
            ]
            findings, warnings, valid = await courses_module._validate_course_bulk_rows(rows, db, force_department_id=None)
        assert findings == [], f"expected no findings, got {findings}"
        assert warnings == [], f"different departments must never produce a duplicate-code warning, got {warnings}"
        assert len(valid) == 2
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_bulk_existing_db_same_title_hard_reject_diff_title_warning():
    name = "bulk validate — existing DB course: same title -> HARD REJECT; different title -> WARNING only"
    number = _fake_number("b4")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A, "Research (Semester II)"), db, admin)

        async with AsyncSessionLocal() as db:
            code_a = await _dept_code(db, _DEPT_A)
            rows_same_title = [{"row": 2, "values": {"Course Number": number, "Course Title": "Research (Semester II)", "Department": code_a}}]
            findings_same, warnings_same, valid_same = await courses_module._validate_course_bulk_rows(rows_same_title, db, force_department_id=None)

            rows_diff_title = [{"row": 2, "values": {"Course Number": number, "Course Title": "Research (Semester IV)", "Department": code_a}}]
            findings_diff, warnings_diff, valid_diff = await courses_module._validate_course_bulk_rows(rows_diff_title, db, force_department_id=None)

        assert any("Duplicate course" in f["error"] for f in findings_same), f"expected a hard-duplicate finding, got {findings_same}"
        assert valid_same == []

        assert findings_diff == [], f"a different title must not be a hard finding, got {findings_diff}"
        assert len(valid_diff) == 1, "a different-titled row must still be accepted (pending confirmation upstream)"
        assert len(warnings_diff) == 1 and warnings_diff[0]["existing_course_id"] is not None
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


# ── RBAC ─────────────────────────────────────────────────────────────────────

async def test_rbac_hod_cannot_create_in_other_department():
    name = "RBAC — HOD of dept A cannot create a course in dept B via a manipulated department_id"
    number = _fake_number("r1")
    hod_dept_a = _fake_user(_SOME_USER_ID, _DEPT_A, UserRole.HOD)
    try:
        blocked = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.create_course(_course_in(number, _DEPT_B, "Research"), db, hod_dept_a)
            except HTTPException as e:
                blocked = e.status_code == 403
        assert blocked, "expected an HOD supplying a foreign department_id to be rejected with 403"

        async with AsyncSessionLocal() as db:
            leftover = (await db.execute(select(Course).where(Course.course_number == number))).scalars().all()
        assert leftover == [], "the blocked attempt must not have created any course row"

        # Sanity: the SAME HOD creating within their OWN department must still work.
        async with AsyncSessionLocal() as db:
            created = await courses_module.create_course(_course_in(number, _DEPT_A, "Research"), db, hod_dept_a)
        assert created.department_id == _DEPT_A
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_rbac_department_resolution_not_overridable_in_bulk():
    name = "RBAC — bulk validate: HOD's Excel Department column cannot override force_department_id"
    number = _fake_number("r2")
    try:
        async with AsyncSessionLocal() as db:
            code_b = await _dept_code(db, _DEPT_B)
            rows = [{"row": 2, "values": {"Course Number": number, "Course Title": "Research", "Department": code_b}}]
            findings, warnings, valid = await courses_module._validate_course_bulk_rows(rows, db, force_department_id=_DEPT_A)
        assert valid == [], "a row whose Department doesn't match the HOD's forced department must never be accepted"
        assert any("does not match the authenticated HOD" in f["error"] for f in findings), f"expected a department-mismatch finding, got {findings}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def main() -> None:
    await _setup()
    scenarios = [
        test_migration_chain_reaches_0020,
        test_department_id_course_number_duplicates_allowed_at_db_level,
        test_create_same_dept_same_title_rejected,
        test_create_same_dept_same_code_whitespace_title_rejected,
        test_create_same_dept_same_code_case_title_rejected,
        test_create_same_dept_same_code_different_title_allowed,
        test_create_different_dept_same_code_same_title_allowed,
        test_create_different_dept_same_code_different_title_allowed,
        test_update_keeps_own_code_and_title_no_false_positive,
        test_update_to_exact_duplicate_of_another_course_rejected,
        test_update_to_same_code_different_title_allowed,
        test_bulk_within_file_same_title_hard_reject,
        test_bulk_within_file_different_title_warning,
        test_bulk_different_departments_same_code_no_warning,
        test_bulk_existing_db_same_title_hard_reject_diff_title_warning,
        test_rbac_hod_cannot_create_in_other_department,
        test_rbac_department_resolution_not_overridable_in_bulk,
    ]
    for scenario in scenarios:
        await scenario()
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
