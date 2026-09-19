"""Standalone, dependency-free HTTP-level test script for the course
bulk-upload duplicate-warning preview/confirmation workflow.

Unlike `test_course_uniqueness.py` (which calls endpoint functions
directly), this file drives the REAL `POST /api/v1/courses/bulk-upload`
endpoint through the actual FastAPI app via `httpx.ASGITransport` — the
confirmation flow depends on real multipart/Form request handling
(`UploadFile` + `Form(...)` fields) that a direct function call can't
exercise faithfully.

Runs against the SAME local Postgres database configured in `.env`
(`DATABASE_URL`) — never production, no real HTTP server, no real network.
Every course/token this script creates/uses is scoped to a `ZZTEST_`
course-number prefix and cleaned up in a `finally` block per scenario. A
temporary `RefreshToken` row (needed to mint a real access token for an
existing Super Admin test user) is deleted immediately after use.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_course_bulk_upload_confirmation
"""
import asyncio
import hashlib
import io
import sys
import uuid

import httpx
import openpyxl
from sqlalchemy import select, delete

from app.db.base import AsyncSessionLocal
from app.main import app
from app.core.security import create_access_token, create_bulk_upload_confirmation_token
from app.models.course import Course
from app.models.user import User, UserRole, RefreshToken, Department

_MARKER = "ZZTEST_"


def _fake_number(tag: str) -> str:
    return f"{_MARKER}{tag}_{uuid.uuid4().hex[:6]}".upper()


def _make_workbook(rows: list[list[str]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Course Number", "Course Title", "Programme", "Course Type", "Credit Type", "Theory Credit", "Practical Credit", "Status", "Department"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        status = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail and not ok else ""
        print(f"{status}: {name}{suffix}")


RESULTS = _Results()

_DEPT_A_CODE: str | None = None
_TOKEN: str | None = None
_RT_ID: uuid.UUID | None = None


async def _setup() -> None:
    global _DEPT_A_CODE, _TOKEN, _RT_ID
    async with AsyncSessionLocal() as db:
        _DEPT_A_CODE = (await db.execute(select(Department.code).order_by(Department.code).limit(1))).scalar_one()
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        _RT_ID = uuid.uuid4()
        db.add(RefreshToken(
            id=_RT_ID, user_id=admin_id,
            token_hash=hashlib.sha256(str(_RT_ID).encode()).hexdigest(), device_info="course-bulk-upload-test",
        ))
        await db.commit()
        _TOKEN = create_access_token(str(admin_id), {"sid": str(_RT_ID)})


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(RefreshToken).where(RefreshToken.id == _RT_ID))
        await db.commit()


async def _cleanup_by_numbers(numbers: list[str]) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Course).where(Course.course_number.in_(numbers)))
        await db.commit()


async def _post_bulk_upload(content: bytes, *, confirm_warnings: bool | None = None, confirmation_token: str | None = None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("test.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {}
        if confirm_warnings is not None:
            data["confirm_warnings"] = "true" if confirm_warnings else "false"
        if confirmation_token is not None:
            data["confirmation_token"] = confirmation_token
        return await client.post(
            "/api/v1/courses/bulk-upload",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            files=files, data=data,
        )


async def test_warning_without_confirmation_creates_nothing():
    name = "Test 13 — warning present, no confirmation -> 409, requires_confirmation, zero courses created"
    n1, n2 = _fake_number("c13a"), _fake_number("c13b")
    try:
        content = _make_workbook([
            [n1, "Research (Semester II)", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE],
        ])
        # Pre-seed an existing course so the same code + different title triggers a warning.
        async with AsyncSessionLocal() as db:
            dept_id = (await db.execute(select(Department.id).where(Department.code == _DEPT_A_CODE))).scalar_one()
            db.add(Course(course_number=n1, title="Research (Semester IV)", department_id=dept_id))
            await db.commit()

        r = await _post_bulk_upload(content)
        body = r.json()
        assert r.status_code == 409, f"expected 409, got {r.status_code}: {body}"
        assert body.get("requires_confirmation") is True
        assert body.get("imported_count") == 0
        assert isinstance(body.get("warnings"), list) and len(body["warnings"]) >= 1
        assert body.get("confirmation_token")

        async with AsyncSessionLocal() as db:
            new_rows = (await db.execute(select(Course).where(Course.course_number == n1, Course.title == "Research (Semester II)"))).scalars().all()
        assert new_rows == [], "no course should have been created before confirmation"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1, n2])


async def test_warning_with_valid_confirmation_inserts():
    name = "Test 14 — warning present, explicit valid confirmation -> insert succeeds"
    n1 = _fake_number("c14")
    try:
        content = _make_workbook([[n1, "Research (Semester II)", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])
        async with AsyncSessionLocal() as db:
            dept_id = (await db.execute(select(Department.id).where(Department.code == _DEPT_A_CODE))).scalar_one()
            db.add(Course(course_number=n1, title="Research (Semester IV)", department_id=dept_id))
            await db.commit()

        preview = await _post_bulk_upload(content)
        assert preview.status_code == 409
        token = preview.json()["confirmation_token"]

        confirmed = await _post_bulk_upload(content, confirm_warnings=True, confirmation_token=token)
        body = confirmed.json()
        assert confirmed.status_code == 201, f"expected 201, got {confirmed.status_code}: {body}"
        assert body["imported_count"] == 1

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == n1))).scalars().all()
        assert len(rows) == 2, f"expected the original + the newly confirmed course, got {len(rows)}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1])


async def test_hard_duplicate_with_confirmation_still_rejected():
    name = "Test 15 — hard duplicate present, even with confirm_warnings=true -> still rejected, no insert"
    n1 = _fake_number("c15")
    try:
        async with AsyncSessionLocal() as db:
            dept_id = (await db.execute(select(Department.id).where(Department.code == _DEPT_A_CODE))).scalar_one()
            db.add(Course(course_number=n1, title="Research", department_id=dept_id))
            await db.commit()

        content = _make_workbook([[n1, "Research", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])
        # Even a client blindly asserting confirmation (no real token) must not help.
        r = await _post_bulk_upload(content, confirm_warnings=True, confirmation_token="not-a-real-token")
        body = r.json()
        assert r.status_code == 400, f"expected 400 (hard duplicate), got {r.status_code}: {body}"
        assert body["success"] is False
        assert any("Duplicate course" in f["error"] for f in body["errors"])

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == n1))).scalars().all()
        assert len(rows) == 1, "no additional course should have been created"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1])


async def test_tampered_file_does_not_reuse_stale_confirmation():
    name = "Test 16 — confirmation token from file A cannot be reused to push through a different file B"
    n1, n2 = _fake_number("c16a"), _fake_number("c16b")
    try:
        async with AsyncSessionLocal() as db:
            dept_id = (await db.execute(select(Department.id).where(Department.code == _DEPT_A_CODE))).scalar_one()
            db.add(Course(course_number=n1, title="Research (Semester IV)", department_id=dept_id))
            await db.commit()

        file_a = _make_workbook([[n1, "Research (Semester II)", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])
        preview = await _post_bulk_upload(file_a)
        assert preview.status_code == 409
        token_for_a = preview.json()["confirmation_token"]

        # A DIFFERENT file (different course entirely) submitted with A's token.
        file_b = _make_workbook([[n2, "Something Unrelated", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])
        r = await _post_bulk_upload(file_b, confirm_warnings=True, confirmation_token=token_for_a)
        body = r.json()
        # File B has no warnings/findings of its own, but the token doesn't
        # match its hash, so `confirmed` must be False -- since B has no
        # warnings either, it should just succeed as an ordinary (unconfirmed
        # but also unconditional) upload. The real assertion is: A's own
        # course must NOT have been silently created via B's request.
        assert r.status_code == 201, f"file B alone has no duplicates and should upload normally, got {r.status_code}: {body}"
        async with AsyncSessionLocal() as db:
            a_rows = (await db.execute(select(Course).where(Course.course_number == n1, Course.title == "Research (Semester II)"))).scalars().all()
        assert a_rows == [], "file A's course must NOT have been created using file B's request + A's stale token"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1, n2])


async def test_race_condition_final_revalidation_rejects():
    name = "Test 17 — race condition: exact duplicate created between preview and confirm is caught at final commit"
    n1 = _fake_number("c17")
    try:
        content = _make_workbook([[n1, "Research (Semester II)", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])

        # Preview BEFORE any conflicting course exists -> no warnings, would
        # normally insert immediately. We instead force the two-step path by
        # first creating an unrelated-titled course to get a token, matching
        # the realistic "warning was shown, user pauses" scenario.
        async with AsyncSessionLocal() as db:
            dept_id = (await db.execute(select(Department.id).where(Department.code == _DEPT_A_CODE))).scalar_one()
            db.add(Course(course_number=n1, title="Some Other Title", department_id=dept_id))
            await db.commit()

        preview = await _post_bulk_upload(content)
        assert preview.status_code == 409
        token = preview.json()["confirmation_token"]

        # While the (simulated) user is still deciding, ANOTHER actor creates
        # the EXACT course this upload is trying to add.
        async with AsyncSessionLocal() as db:
            db.add(Course(course_number=n1, title="Research (Semester II)", department_id=dept_id))
            await db.commit()

        confirmed = await _post_bulk_upload(content, confirm_warnings=True, confirmation_token=token)
        body = confirmed.json()
        assert confirmed.status_code == 400, f"expected the final revalidation to reject as a hard duplicate, got {confirmed.status_code}: {body}"
        assert any("Duplicate course" in f["error"] for f in body["errors"])

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == n1, Course.title == "Research (Semester II)"))).scalars().all()
        assert len(rows) == 1, "only the concurrently-created course should exist -- the confirmed upload must not have duplicated it"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1])


async def test_confirmation_token_bound_to_authenticated_user():
    name = "Test — a confirmation token signed for a DIFFERENT user id is rejected (falls back to preview)"
    n1 = _fake_number("c18")
    try:
        async with AsyncSessionLocal() as db:
            dept_id = (await db.execute(select(Department.id).where(Department.code == _DEPT_A_CODE))).scalar_one()
            db.add(Course(course_number=n1, title="Research (Semester IV)", department_id=dept_id))
            await db.commit()

        content = _make_workbook([[n1, "Research (Semester II)", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])
        wrong_user_token = create_bulk_upload_confirmation_token(
            str(uuid.uuid4()), hashlib.sha256(content).hexdigest(), 1,
        )
        r = await _post_bulk_upload(content, confirm_warnings=True, confirmation_token=wrong_user_token)
        body = r.json()
        assert r.status_code == 409 and body.get("requires_confirmation") is True, (
            f"a token signed for a different user must be treated as unconfirmed, got {r.status_code}: {body}"
        )
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Course).where(Course.course_number == n1, Course.title == "Research (Semester II)"))).scalars().all()
        assert rows == [], "no course should have been created using a token bound to a different user"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1])


async def test_no_warnings_uploads_normally_without_confirmation_fields():
    name = "no-warning fast path — an ordinary upload with no duplicates inserts immediately, no confirmation dance needed"
    n1 = _fake_number("c19")
    try:
        content = _make_workbook([[n1, "Brand New Course", "UG", "", "", "0", "0", "Active", _DEPT_A_CODE]])
        r = await _post_bulk_upload(content)
        body = r.json()
        assert r.status_code == 201, f"expected immediate success, got {r.status_code}: {body}"
        assert body["imported_count"] == 1
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1])


async def test_cross_programme_level_twins_upload_without_confirmation_same_level_rejected():
    name = "Programme Level — same dept+code+title at PG and PhD uploads immediately (201, no confirmation); re-uploading it -> HARD 400, nothing created"
    n1 = _fake_number("c20")
    try:
        content = _make_workbook([
            [n1, "Animal Nutrition", "PG", "", "", "3", "0", "Active", _DEPT_A_CODE],
            [n1, "Animal Nutrition", "PhD", "", "", "3", "0", "Active", _DEPT_A_CODE],
        ])
        r = await _post_bulk_upload(content)
        body = r.json()
        assert r.status_code == 201 and body["imported_count"] == 2, f"expected both levels accepted at once, got {r.status_code}: {body}"
        assert not body.get("requires_confirmation"), body

        # The identical file again: both rows are now exact duplicates (same level) -> hard rejection, even with a confirmation flag.
        r2 = await _post_bulk_upload(content, confirm_warnings=True, confirmation_token="not-a-real-token")
        b2 = r2.json()
        assert r2.status_code == 400 and b2["success"] is False and len(b2["errors"]) == 2, f"expected hard duplicate rejection, got {r2.status_code}: {b2}"
        async with AsyncSessionLocal() as db:
            count = len((await db.execute(select(Course.id).where(Course.course_number == n1))).scalars().all())
        assert count == 2, f"the rejected re-upload must not create anything, found {count} rows"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_numbers([n1])


async def main() -> None:
    await _setup()
    try:
        scenarios = [
            test_warning_without_confirmation_creates_nothing,
            test_warning_with_valid_confirmation_inserts,
            test_hard_duplicate_with_confirmation_still_rejected,
            test_tampered_file_does_not_reuse_stale_confirmation,
            test_race_condition_final_revalidation_rejects,
            test_confirmation_token_bound_to_authenticated_user,
            test_no_warnings_uploads_normally_without_confirmation_fields,
            test_cross_programme_level_twins_upload_without_confirmation_same_level_rejected,
        ]
        for scenario in scenarios:
            await scenario()
    finally:
        await _teardown()
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
