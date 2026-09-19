"""Standalone, dependency-free test script for the course-code
duplicate-DETECTION fix (superseding the previous, incorrect
(department_id, course_number) uniqueness rule from migration 0019).

Corrected business rule under test:
    same department + same code + SAME normalized title
        + SAME Programme Level (Course.program_level: UG/PG/PhD)   -> REJECT
    same department + same code + SAME normalized title
        + DIFFERENT Programme Level                                -> ALLOWED
    same department + same code + DIFFERENT normalized title       -> ALLOWED
                                                                 (bulk upload
                                                                 additionally
                                                                 warns; see
                                                                 test_course_bulk_upload_confirmation.py)
    different department + same code (any title)                   -> ALLOWED
("Programme" here is the Programme LEVEL on Course, not the ams_programs master
table — Course has no relationship to that table.) Tests whose names start
`test_level_` cover the Programme Level part of the rule; the rest predate it
and use the default level (UG) on both sides, so they still hold unchanged.

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
from app.models.course import Course, CourseOffering
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


def _course_in(course_number: str, department_id, title: str = "ZZTEST Course", program_level: str = "UG") -> CourseIn:
    return CourseIn(course_number=course_number, title=title, department_id=department_id, program_level=program_level)


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


# ── Programme Level (Course.program_level) is part of the duplicate key ──────
#
# Confirmed AVFU rule: a course is a duplicate only when department + code +
# normalized title + Programme Level (UG/PG/PhD — the existing
# `Course.program_level` field, NOT the ams_programs master table) all match.

_TITLE = "Animal Nutrition"


async def _create(number, dept, title, level, user):
    async with AsyncSessionLocal() as db:
        return await courses_module.create_course(_course_in(number, dept, title, level), db, user)


async def _expect_409(coro) -> bool:
    try:
        await coro
    except HTTPException as e:
        return e.status_code == 409
    return False


async def _row(course_id):
    async with AsyncSessionLocal() as db:
        return await db.get(Course, course_id)


async def test_level_create_same_level_rejected():
    name = "LEVEL create — same dept + code + title + SAME level (PG/PG) -> REJECT"
    number = _fake_number("lv1")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, _TITLE, "PG", admin)
        assert await _expect_409(_create(number, _DEPT_A, _TITLE, "PG", admin)), "expected HTTPException(409)"
        async with AsyncSessionLocal() as db:
            n = len((await db.execute(select(Course.id).where(Course.course_number == number))).scalars().all())
        assert n == 1, f"the rejected duplicate must not be stored, found {n} rows"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_create_different_level_allowed():
    name = "LEVEL create — same dept + code + title, DIFFERENT level (PG then PhD) -> BOTH ALLOWED, distinct ids"
    number = _fake_number("lv2")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        pg = await _create(number, _DEPT_A, _TITLE, "PG", admin)
        phd = await _create(number, _DEPT_A, _TITLE, "PhD", admin)
        assert pg.id != phd.id
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course.program_level).where(Course.course_number == number).order_by(Course.program_level))).scalars().all()
        assert rows == ["PG", "PhD"], f"expected one PG and one PhD row, got {rows}"
        # ...but a second PhD row is again a duplicate.
        assert await _expect_409(_create(number, _DEPT_A, _TITLE, "PhD", admin))
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_create_different_department_allowed():
    name = "LEVEL create — different department + same code + title + level -> ALLOWED (codes are reusable across departments)"
    number = _fake_number("lv3")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, _TITLE, "PG", admin)
        await _create(number, _DEPT_B, _TITLE, "PG", admin)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_create_same_level_different_title_allowed():
    name = "LEVEL create — same dept + code + level, DIFFERENT title -> ALLOWED (existing rule preserved)"
    number = _fake_number("lv4")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, "Animal Nutrition I", "PG", admin)
        await _create(number, _DEPT_A, "Animal Nutrition II", "PG", admin)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_create_title_normalization():
    name = "LEVEL create — whitespace/case title variant collides at the SAME level, and is allowed at a DIFFERENT level"
    number = _fake_number("lv5")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, _TITLE, "PG", admin)
        assert await _expect_409(_create(number, _DEPT_A, "  animal NUTRITION  ", "PG", admin)), "normalized variant at same level must collide"
        await _create(number, _DEPT_A, "  animal NUTRITION  ", "PhD", admin)  # different level -> fine
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_input_canonicalized_and_validated():
    name = "LEVEL create — 'pg'/' phd ' are canonicalized (so casing cannot bypass the rule); an unknown level is rejected"
    number = _fake_number("lv6")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        assert CourseIn(course_number=number, title=_TITLE, program_level="pg").program_level == "PG"
        assert CourseIn(course_number=number, title=_TITLE, program_level=" phd ").program_level == "PhD"
        await _create(number, _DEPT_A, _TITLE, "PG", admin)
        assert await _expect_409(_create(number, _DEPT_A, _TITLE, "pg", admin)), "'pg' must be treated as PG, not as a new level"
        from pydantic import ValidationError
        try:
            CourseIn(course_number=number, title=_TITLE, program_level="Masters")
            raise AssertionError("an unknown Programme Level must be rejected")
        except ValidationError:
            pass
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_update_same_level_collision_rejected():
    name = "LEVEL update — updating B (602/PG) into A's full key (601/title/PG) -> REJECT, B unchanged"
    n1, n2 = _fake_number("lv7a"), _fake_number("lv7b")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(n1, _DEPT_A, _TITLE, "PG", admin)
        b = await _create(n2, _DEPT_A, _TITLE, "PG", admin)
        async with AsyncSessionLocal() as db:
            assert await _expect_409(courses_module.update_course(b.id, _course_in(n1, _DEPT_A, _TITLE, "PG"), db, admin))
        fresh = await _row(b.id)
        assert fresh.course_number == n2 and fresh.program_level == "PG", "the rejected update must not change B"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1, n2])


async def test_level_update_cross_level_allowed_and_blocked_into_existing():
    name = "LEVEL update — PG -> PhD allowed when that combination is free; blocked when a PhD twin already exists"
    number = _fake_number("lv8")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        a = await _create(number, _DEPT_A, _TITLE, "PG", admin)
        async with AsyncSessionLocal() as db:
            await courses_module.update_course(a.id, _course_in(number, _DEPT_A, _TITLE, "PhD"), db, admin)
        assert (await _row(a.id)).program_level == "PhD", "free cross-level update must be applied"

        # Now a PG twin is created; moving the PhD row back to PG would collide with it.
        twin = await _create(number, _DEPT_A, _TITLE, "PG", admin)
        async with AsyncSessionLocal() as db:
            assert await _expect_409(courses_module.update_course(a.id, _course_in(number, _DEPT_A, _TITLE, "PG"), db, admin)), \
                "changing level into an existing duplicate must be rejected"
        assert (await _row(a.id)).program_level == "PhD" and (await _row(twin.id)).program_level == "PG"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_update_self_no_false_positive():
    name = "LEVEL update — a no-op update (same dept/code/title/level) never collides with itself, even with a PhD twin present"
    number = _fake_number("lv9")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        pg = await _create(number, _DEPT_A, _TITLE, "PG", admin)
        await _create(number, _DEPT_A, _TITLE, "PhD", admin)
        async with AsyncSessionLocal() as db:
            await courses_module.update_course(pg.id, _course_in(number, _DEPT_A, _TITLE, "PG"), db, admin)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_rbac_department_rules_unchanged():
    name = "LEVEL RBAC — HOD@A still cannot create/move a course into department B at ANY level; can create its own cross-level twin"
    number = _fake_number("lv10")
    hod_a = _fake_user(_SOME_USER_ID, _DEPT_A, UserRole.HOD)
    try:
        for level in ("UG", "PG", "PhD"):
            async with AsyncSessionLocal() as db:
                try:
                    await courses_module.create_course(_course_in(number, _DEPT_B, _TITLE, level), db, hod_a)
                    raise AssertionError(f"HOD@A created a {level} course in department B")
                except HTTPException as e:
                    assert e.status_code == 403, e.status_code
        await _create(number, _DEPT_A, _TITLE, "PG", hod_a)
        await _create(number, _DEPT_A, _TITLE, "PhD", hod_a)
        mine = await _row((await _find(number, "PG")))
        async with AsyncSessionLocal() as db:
            try:
                await courses_module.update_course(mine.id, _course_in(number, _DEPT_B, _TITLE, "PG"), db, hod_a)
                raise AssertionError("HOD@A moved a course into department B")
            except HTTPException as e:
                assert e.status_code == 403, e.status_code
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def _find(number, level):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Course.id).where(Course.course_number == number, Course.program_level == level))).scalar_one()


def _brow(n, number, title, level, dept_code):
    return {"row": n, "values": {"Course Number": number, "Course Title": title, "Programme": level, "Department": dept_code}}


async def _bulk(rows):
    async with AsyncSessionLocal() as db:
        return await courses_module._validate_course_bulk_rows(rows, db, force_department_id=None)


async def test_level_bulk_within_file_different_level_allowed():
    name = "LEVEL bulk — same file: same dept+code+title with PG and PhD rows -> BOTH VALID, no finding, no warning"
    number = _fake_number("bl1")
    try:
        async with AsyncSessionLocal() as db:
            code = await _dept_code(db, _DEPT_A)
        findings, warnings, valid = await _bulk([_brow(2, number, _TITLE, "PG", code), _brow(3, number, _TITLE, "PhD", code)])
        assert findings == [] and warnings == [], f"findings={findings} warnings={warnings}"
        assert sorted(r["program_level"] for r in valid) == ["PG", "PhD"]
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_bulk_within_file_same_level_duplicate():
    name = "LEVEL bulk — same file: two PG rows with the same dept+code+title -> both flagged as duplicates"
    number = _fake_number("bl2")
    try:
        async with AsyncSessionLocal() as db:
            code = await _dept_code(db, _DEPT_A)
        findings, warnings, valid = await _bulk([_brow(2, number, _TITLE, "PG", code), _brow(3, number, " animal nutrition ", "pg", code)])
        assert valid == [] and len(findings) == 2, f"findings={findings} valid={valid}"
        assert all("Duplicate" in f["error"] for f in findings)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_bulk_existing_db_different_level_allowed_same_level_duplicate():
    name = "LEVEL bulk — existing DB PG course: uploading the PhD twin -> allowed (no finding/warning); uploading the PG twin -> DUPLICATE"
    number = _fake_number("bl3")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, _TITLE, "PG", admin)
        async with AsyncSessionLocal() as db:
            code = await _dept_code(db, _DEPT_A)
        findings, warnings, valid = await _bulk([_brow(2, number, _TITLE, "PhD", code)])
        assert findings == [] and warnings == [] and len(valid) == 1, f"PhD twin: findings={findings} warnings={warnings}"
        findings, warnings, valid = await _bulk([_brow(2, number, _TITLE, "PG", code)])
        assert valid == [] and any("Duplicate course" in f["error"] and "(PG)" in f["error"] for f in findings), findings
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_bulk_different_title_still_warns_regardless_of_level():
    name = "LEVEL bulk — same code + DIFFERENT title still produces the confirmation WARNING (existing behaviour preserved), at any level"
    number = _fake_number("bl4")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, _TITLE, "PG", admin)
        async with AsyncSessionLocal() as db:
            code = await _dept_code(db, _DEPT_A)
        for level in ("PG", "PhD"):
            findings, warnings, valid = await _bulk([_brow(2, number, "Animal Nutrition Advanced", level, code)])
            assert findings == [] and len(valid) == 1 and len(warnings) == 1, f"{level}: findings={findings} warnings={warnings}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_bulk_invalid_level_reports_only_its_own_finding():
    name = "LEVEL bulk — an invalid Programme value yields ONLY the Programme finding (no spurious duplicate finding)"
    number = _fake_number("bl5")
    admin = _fake_user(_SOME_USER_ID, None, UserRole.SUPER_ADMIN)
    try:
        await _create(number, _DEPT_A, _TITLE, "UG", admin)  # would be a same-title duplicate at the default level
        async with AsyncSessionLocal() as db:
            code = await _dept_code(db, _DEPT_A)
        findings, warnings, valid = await _bulk([_brow(2, number, _TITLE, "Masters", code)])
        assert [f["column"] for f in findings] == ["Programme"], findings
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


# ── Database / offering identity ────────────────────────────────────────────

async def test_level_db_allows_same_dept_code_title_rows_no_new_constraint():
    name = "LEVEL DB — two rows with identical dept+code+title but different level insert cleanly; ams_courses still has no unique constraint"
    number = _fake_number("db1")
    try:
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            for level in ("PG", "PhD"):
                db.add(Course(course_number=number, title=_TITLE, department_id=_DEPT_A, program_level=level, created_by=_SOME_USER_ID))
            await db.commit()
            cons = (await db.execute(text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'ams_courses'::regclass AND contype='u'"
            ))).scalars().all()
        assert cons == [], f"the application-level rule must not be mirrored by a DB constraint, found {cons}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([number])


async def test_level_offerings_reference_distinct_courses_by_id():
    name = "LEVEL offerings — PG and PhD twins can each be offered in the same semester/department; offerings resolve to the exact course row and expose its level"
    from sqlalchemy.orm import selectinload
    from app.models.academic import Semester
    number = _fake_number("of1")
    offering_ids = []
    try:
        async with AsyncSessionLocal() as db:
            sem = (await db.execute(select(Semester).limit(1))).scalars().first()
            course_ids = {}
            for level in ("PG", "PhD"):
                c = Course(course_number=number, title=_TITLE, department_id=_DEPT_A, program_level=level, created_by=_SOME_USER_ID)
                db.add(c)
                await db.flush()
                course_ids[level] = c.id
                o = CourseOffering(calendar_id=sem.calendar_id, semester_id=sem.id, course_id=c.id, department_id=_DEPT_A, created_by=_SOME_USER_ID)
                db.add(o)
                await db.flush()
                offering_ids.append(o.id)
            await db.commit()
        async with AsyncSessionLocal() as db:
            offs = (await db.execute(
                select(CourseOffering).options(
                    selectinload(CourseOffering.course), selectinload(CourseOffering.semester),
                    selectinload(CourseOffering.department), selectinload(CourseOffering.faculty_assignments),
                ).where(CourseOffering.id.in_(offering_ids))
            )).scalars().all()
            dicts = {d["program_level"]: d for d in (courses_module._offering_dict(o, 0) for o in offs)}
        assert set(dicts) == {"PG", "PhD"}, f"offerings must expose their course's level, got {set(dicts)}"
        assert dicts["PG"]["course_id"] == str(course_ids["PG"]) and dicts["PhD"]["course_id"] == str(course_ids["PhD"])
        assert dicts["PG"]["course_number"] == dicts["PhD"]["course_number"] == number and dicts["PG"]["id"] != dicts["PhD"]["id"]
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(offering_ids)))
            await db.commit()
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
        test_level_create_same_level_rejected,
        test_level_create_different_level_allowed,
        test_level_create_different_department_allowed,
        test_level_create_same_level_different_title_allowed,
        test_level_create_title_normalization,
        test_level_input_canonicalized_and_validated,
        test_level_update_same_level_collision_rejected,
        test_level_update_cross_level_allowed_and_blocked_into_existing,
        test_level_update_self_no_false_positive,
        test_level_rbac_department_rules_unchanged,
        test_level_bulk_within_file_different_level_allowed,
        test_level_bulk_within_file_same_level_duplicate,
        test_level_bulk_existing_db_different_level_allowed_same_level_duplicate,
        test_level_bulk_different_title_still_warns_regardless_of_level,
        test_level_bulk_invalid_level_reports_only_its_own_finding,
        test_level_db_allows_same_dept_code_title_rows_no_new_constraint,
        test_level_offerings_reference_distinct_courses_by_id,
    ]
    for scenario in scenarios:
        await scenario()
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
