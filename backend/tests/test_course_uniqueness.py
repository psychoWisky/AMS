"""Standalone, dependency-free test script for the course-code
department-scoping fix (course_number uniqueness is now per-department,
not global).

No pytest — matches the existing convention in this `tests/` directory
(see `test_email_outbox.py`'s own docstring for why). Calls the real
endpoint functions (`create_course`, `update_course`,
`_validate_course_bulk_rows`) directly with hand-built request bodies and
lightweight fake `user` objects — no HTTP layer needed, since these are
plain async functions that only read `user.active_role`/`user.department_id`/
`user.id`, exactly like FastAPI's dependency injection would provide.

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
from app.models.user import User, UserRole
from app.api.v1.endpoints import courses as courses_module
from app.api.v1.endpoints.courses import CourseIn

_MARKER = "ZZTEST_"


def _fake_number(tag: str) -> str:
    return f"{_MARKER}{tag}_{uuid.uuid4().hex[:6]}".upper()


def _fake_user(user_id, department_id, active_role) -> SimpleNamespace:
    """Lightweight stand-in for the real `User` FastAPI would inject —
    `create_course`/`update_course`/`_authorize_department_manage` only
    ever read `.id`/`.department_id`/`.active_role`, so a plain namespace
    is sufficient and avoids needing to persist a throwaway user row."""
    return SimpleNamespace(id=user_id, department_id=department_id, active_role=active_role)


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
        # Any two real, distinct department ids work — this script never
        # writes to ams_departments itself, only reads it.
        from app.models.user import Department
        rows = (await db.execute(select(Department.id).order_by(Department.code).limit(2))).scalars().all()
        assert len(rows) >= 2, "test requires at least 2 existing departments in the local dev database"
        _DEPT_A, _DEPT_B = rows[0], rows[1]

        user_row = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one_or_none()
        assert user_row is not None, "test requires at least 1 existing SUPER_ADMIN user in the local dev database"
        _SOME_USER_ID = user_row


async def test_same_department_duplicate_rejected():
    name = "Test 1 — same department, same course code: second create is REJECTED"
    number = _fake_number("cs101")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A), db, admin)
        rejected = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.create_course(_course_in(number, _DEPT_A), db, admin)
            except HTTPException as e:
                rejected = e.status_code == 409
        assert rejected, "expected the second same-department create to raise HTTPException(409)"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_cross_department_same_code_allowed():
    name = "Test 2 — different departments, same course code: both ALLOWED"
    number = _fake_number("cs101b")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            out_a = await courses_module.create_course(_course_in(number, _DEPT_A), db, admin)
        async with AsyncSessionLocal() as db:
            out_b = await courses_module.create_course(_course_in(number, _DEPT_B), db, admin)
        assert out_a.department_id == _DEPT_A
        assert out_b.department_id == _DEPT_B
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == number))).scalars().all()
        assert len(rows) == 2, f"expected 2 courses (one per department), got {len(rows)}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_bulk_duplicate_within_file():
    name = "Test 3 — bulk upload: duplicate (same dept, same code) within the file is flagged"
    number = _fake_number("cs103")
    try:
        async with AsyncSessionLocal() as db:
            dept_code = (await _dept_code(db, _DEPT_A))
            rows = [
                {"row": 2, "values": {"Course Number": number, "Course Title": "A", "Department": dept_code}},
                {"row": 3, "values": {"Course Number": number, "Course Title": "B", "Department": dept_code}},
            ]
            findings, valid = await courses_module._validate_course_bulk_rows(rows, db, force_department_id=None)
        assert len(valid) == 0, "an all-or-nothing duplicate must produce zero valid rows"
        assert any("Duplicate course number" in f["error"] for f in findings), f"expected a duplicate finding, got {findings}"
        assert len(findings) == 2, f"expected both offending rows flagged, got {findings}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_bulk_same_code_across_departments():
    name = "Test 4 — bulk upload: same code across two departments in the same file is ACCEPTED"
    number = _fake_number("cs104")
    try:
        async with AsyncSessionLocal() as db:
            code_a = await _dept_code(db, _DEPT_A)
            code_b = await _dept_code(db, _DEPT_B)
            rows = [
                {"row": 2, "values": {"Course Number": number, "Course Title": "A", "Department": code_a}},
                {"row": 3, "values": {"Course Number": number, "Course Title": "B", "Department": code_b}},
            ]
            findings, valid = await courses_module._validate_course_bulk_rows(rows, db, force_department_id=None)
        assert findings == [], f"expected no findings, got {findings}"
        assert len(valid) == 2, f"expected both rows accepted, got {len(valid)}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_existing_db_record_blocks_same_department_only():
    name = "Test 5 — existing DB record blocks bulk-upload only for the SAME department"
    number = _fake_number("cs105")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            await courses_module.create_course(_course_in(number, _DEPT_A), db, admin)

        async with AsyncSessionLocal() as db:
            code_a = await _dept_code(db, _DEPT_A)
            code_b = await _dept_code(db, _DEPT_B)
            rows_same_dept = [{"row": 2, "values": {"Course Number": number, "Course Title": "X", "Department": code_a}}]
            findings_same, valid_same = await courses_module._validate_course_bulk_rows(rows_same_dept, db, force_department_id=None)
            rows_other_dept = [{"row": 2, "values": {"Course Number": number, "Course Title": "X", "Department": code_b}}]
            findings_other, valid_other = await courses_module._validate_course_bulk_rows(rows_other_dept, db, force_department_id=None)

        assert any("already exists" in f["error"] for f in findings_same), f"expected a rejection for the same department, got {findings_same}"
        assert valid_same == []
        assert findings_other == [], f"expected the other department's upload to be accepted, got {findings_other}"
        assert len(valid_other) == 1
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_update_self_and_conflict():
    name = "Test 6 — update: a course keeps its own code without a false duplicate, but cannot take another course's code in the same department"
    n1, n2 = _fake_number("cs106a"), _fake_number("cs106b")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        async with AsyncSessionLocal() as db:
            c1 = await courses_module.create_course(_course_in(n1, _DEPT_A), db, admin)
        async with AsyncSessionLocal() as db:
            c2 = await courses_module.create_course(_course_in(n2, _DEPT_A), db, admin)

        # No-op update: c1 keeps its own code/department — must NOT false-positive.
        async with AsyncSessionLocal() as db:
            result = await courses_module.update_course(c1.id, _course_in(n1, _DEPT_A, title="Renamed"), db, admin)
        assert result == {"message": "Updated."}

        # Conflict: c1 tries to take c2's code within the SAME department.
        conflict = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.update_course(c1.id, _course_in(n2, _DEPT_A), db, admin)
            except HTTPException as e:
                conflict = e.status_code == 409
        assert conflict, "expected updating c1 to c2's code (same department) to raise HTTPException(409)"

        # c1's own row must be unaffected by the rejected attempt.
        async with AsyncSessionLocal() as db:
            fresh_c1 = await db.get(Course, c1.id)
        assert fresh_c1.course_number == n1, "the rejected update must not have changed c1's course_number"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1, n2])


async def test_rbac_hod_cannot_create_in_other_department():
    name = "Test 7 — RBAC: HOD of dept A cannot create a course in dept B via a manipulated department_id"
    number = _fake_number("cs107")
    hod_dept_a = _fake_user(_SOME_USER_ID, _DEPT_A, UserRole.HOD)
    try:
        blocked = False
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.create_course(_course_in(number, _DEPT_B), db, hod_dept_a)
            except HTTPException as e:
                blocked = e.status_code == 403
        assert blocked, "expected an HOD supplying a foreign department_id to be rejected with 403"

        async with AsyncSessionLocal() as db:
            leftover = (await db.execute(select(Course).where(Course.course_number == number))).scalars().all()
        assert leftover == [], "the blocked attempt must not have created any course row"

        # Sanity: the SAME HOD creating within their OWN department must still work —
        # proves the fix didn't also break the legitimate path.
        async with AsyncSessionLocal() as db:
            created = await courses_module.create_course(_course_in(number, _DEPT_A), db, hod_dept_a)
        assert created.department_id == _DEPT_A
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def _dept_code(db, department_id) -> str:
    from app.models.user import Department
    return (await db.execute(select(Department.code).where(Department.id == department_id))).scalar_one()


async def main() -> None:
    await _setup()
    scenarios = [
        test_same_department_duplicate_rejected,
        test_cross_department_same_code_allowed,
        test_bulk_duplicate_within_file,
        test_bulk_same_code_across_departments,
        test_existing_db_record_blocks_same_department_only,
        test_update_self_and_conflict,
        test_rbac_hod_cannot_create_in_other_department,
    ]
    for scenario in scenarios:
        await scenario()
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
