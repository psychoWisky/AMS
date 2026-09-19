"""Standalone HTTP-level tests for Academic Calendar semester create/update:
Registration Start / Exam Start / Exam End / Result Declaration are optional,
and PUT /academic/semesters/{id} is a partial update (omitted fields are
preserved, an explicit null clears an optional date, required columns can
never be nulled).

Drives the REAL FastAPI app via `httpx.ASGITransport` with a real JWT/session
for an existing Super Admin, against the local dev database in `.env`. Every
semester created here is named `ZZTEST_SEM...` under an EXISTING calendar and
is deleted in `finally`; no existing semester/calendar is modified (a
before/after snapshot of all pre-existing semesters is asserted at the end).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_semester_optional_dates
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import select, delete

from app.db.base import AsyncSessionLocal
from app.main import app
from app.core.security import create_access_token
from app.models.user import User, UserRole, RefreshToken
from app.models.academic import Semester, AcademicCalendar

_PREFIX = "ZZTEST_SEM"
_DATES = ("registration_start", "exam_start", "exam_end", "result_declaration")
_ALL_DATES = {
    "registration_start": "2026-01-01", "exam_start": "2026-02-01",
    "exam_end": "2026-02-15", "result_declaration": "2026-03-01",
}


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
_CAL_ID: uuid.UUID
_RT_ID: uuid.UUID
_HEADERS: dict


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _base(name: str, **extra) -> dict:
    return {"calendar_id": str(_CAL_ID), "name": f"{_PREFIX}_{name}", "sem_type": "even",
            "start_date": "2026-01-01", "end_date": "2026-06-30", **extra}


async def _post(body: dict) -> httpx.Response:
    async with _client() as c:
        return await c.post("/api/v1/academic/semesters", headers=_HEADERS, json=body)


async def _put(sem_id: str, body: dict) -> httpx.Response:
    async with _client() as c:
        return await c.put(f"/api/v1/academic/semesters/{sem_id}", headers=_HEADERS, json=body)


async def _get(sem_id: str) -> dict:
    async with _client() as c:
        return (await c.get(f"/api/v1/academic/semesters/{sem_id}", headers=_HEADERS)).json()


async def _snapshot_existing() -> list[tuple]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Semester).where(~Semester.name.like(f"{_PREFIX}%")).order_by(Semester.id)
        )).scalars().all()
        return [
            (str(s.id), str(s.calendar_id), s.name, s.sem_type, s.start_date, s.end_date, s.registration_start,
             s.registration_end, s.exam_start, s.exam_end, s.result_declaration, s.status, str(s.holidays))
            for s in rows
        ]


async def _run(name: str, fn) -> None:
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


# ── Create ───────────────────────────────────────────────────────────────────

async def t_create_no_dates():
    r = await _post(_base("c_none"))
    assert r.status_code == 201, r.text
    b = r.json()
    assert all(b[k] is None for k in _DATES), b


async def t_create_all_dates():
    r = await _post(_base("c_all", **_ALL_DATES))
    assert r.status_code == 201, r.text
    assert {k: r.json()[k] for k in _DATES} == _ALL_DATES


async def t_create_partial_dates():
    r = await _post(_base("c_part", registration_start="2026-01-05", exam_end="2026-02-20"))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["registration_start"] == "2026-01-05" and b["exam_end"] == "2026-02-20"
    assert b["exam_start"] is None and b["result_declaration"] is None


async def t_create_explicit_null_dates():
    r = await _post(_base("c_null", **{k: None for k in _DATES}))
    assert r.status_code == 201, r.text


async def t_create_invalid_date_rejected():
    r = await _post(_base("c_bad", exam_start="not-a-date"))
    assert r.status_code == 422, r.text
    r = await _post(_base("c_bad2", registration_start=""))
    assert r.status_code == 422, "empty-string date must still be rejected by the API (frontend must send null/omit)"


async def t_create_required_fields_still_required():
    for missing in ("calendar_id", "name", "start_date", "end_date"):
        body = _base("c_req")
        del body[missing]
        r = await _post(body)
        assert r.status_code == 422, f"missing {missing} should be 422, got {r.status_code}"
        assert any(e["loc"][-1] == missing and e["type"] == "missing" for e in r.json()["detail"]), r.text


# ── Update ───────────────────────────────────────────────────────────────────

async def t_update_name_only_preserves_everything():
    sem = (await _post(_base("u_name", **_ALL_DATES))).json()
    r = await _put(sem["id"], {"name": f"{_PREFIX}_u_name_renamed"})
    assert r.status_code == 200, r.text
    after = await _get(sem["id"])
    assert after["name"] == f"{_PREFIX}_u_name_renamed"
    for k in _DATES:
        assert after[k] == _ALL_DATES[k], f"{k} changed: {after[k]}"
    assert after["sem_type"] == "even", f"omitted sem_type was reset to {after['sem_type']}"
    assert after["start_date"] == "2026-01-01" and after["end_date"] == "2026-06-30"
    assert after["calendar_id"] == str(_CAL_ID)


async def t_update_frontend_style_name_only_no_calendar_id():
    """The original bug: body without calendar_id -> 422 'missing'."""
    sem = (await _post(_base("u_fe"))).json()
    r = await _put(sem["id"], {"name": f"{_PREFIX}_u_fe2"})
    assert r.status_code == 200, r.text


async def t_update_single_optional_date():
    sem = (await _post(_base("u_one", **_ALL_DATES))).json()
    r = await _put(sem["id"], {"exam_start": "2026-02-03"})
    assert r.status_code == 200, r.text
    after = await _get(sem["id"])
    assert after["exam_start"] == "2026-02-03"
    assert after["registration_start"] == _ALL_DATES["registration_start"]
    assert after["exam_end"] == _ALL_DATES["exam_end"]
    assert after["result_declaration"] == _ALL_DATES["result_declaration"]
    assert after["name"] == f"{_PREFIX}_u_one"


async def t_update_semester_without_dates():
    sem = (await _post(_base("u_nodates"))).json()
    r = await _put(sem["id"], {"name": f"{_PREFIX}_u_nodates2", "sem_type": "odd"})
    assert r.status_code == 200, r.text
    after = await _get(sem["id"])
    assert after["sem_type"] == "odd" and all(after[k] is None for k in _DATES)


async def t_update_explicit_null_clears_only_that_date():
    sem = (await _post(_base("u_clear", **_ALL_DATES))).json()
    r = await _put(sem["id"], {"exam_end": None})
    assert r.status_code == 200, r.text
    after = await _get(sem["id"])
    assert after["exam_end"] is None, after
    assert after["registration_start"] == _ALL_DATES["registration_start"]
    assert after["exam_start"] == _ALL_DATES["exam_start"]
    assert after["result_declaration"] == _ALL_DATES["result_declaration"]


async def t_update_explicit_null_on_required_column_rejected():
    sem = (await _post(_base("u_reqnull", **_ALL_DATES))).json()
    for field in ("name", "start_date", "end_date", "sem_type", "calendar_id"):
        r = await _put(sem["id"], {field: None})
        assert r.status_code == 422, f"{field}=null should be 422, got {r.status_code}: {r.text}"
    after = await _get(sem["id"])
    assert after["name"] == f"{_PREFIX}_u_reqnull" and after["start_date"] == "2026-01-01"


async def t_update_invalid_date_rejected_and_nothing_changed():
    sem = (await _post(_base("u_bad", **_ALL_DATES))).json()
    r = await _put(sem["id"], {"name": f"{_PREFIX}_u_bad_x", "exam_start": "garbage"})
    assert r.status_code == 422, r.text
    r = await _put(sem["id"], {"exam_start": ""})
    assert r.status_code == 422, r.text
    after = await _get(sem["id"])
    assert after["name"] == f"{_PREFIX}_u_bad" and after["exam_start"] == _ALL_DATES["exam_start"]


async def t_update_unknown_semester_404():
    r = await _put(str(uuid.uuid4()), {"name": "x"})
    assert r.status_code == 404, r.text


async def t_update_requires_super_admin():
    sem = (await _post(_base("u_auth"))).json()
    async with _client() as c:
        r = await c.put(f"/api/v1/academic/semesters/{sem['id']}", json={"name": "x"})
    assert r.status_code in (401, 403), r.status_code


# ── Regression: existing listing/status endpoints unaffected ─────────────────

async def t_regression_list_and_status():
    sem = (await _post(_base("r_list", **_ALL_DATES))).json()
    async with _client() as c:
        lst = await c.get(f"/api/v1/academic/calendars/{_CAL_ID}/semesters", headers=_HEADERS)
        assert lst.status_code == 200 and any(s["id"] == sem["id"] for s in lst.json())
        st = await c.patch(f"/api/v1/academic/semesters/{sem['id']}/status", headers=_HEADERS, params={"status": "active"})
        assert st.status_code == 200, st.text
    assert (await _get(sem["id"]))["status"] == "active"


async def main() -> None:
    global _CAL_ID, _RT_ID, _HEADERS
    async with AsyncSessionLocal() as db:
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        _CAL_ID = (await db.execute(select(AcademicCalendar.id).order_by(AcademicCalendar.id).limit(1))).scalar_one()
        _RT_ID = uuid.uuid4()
        db.add(RefreshToken(id=_RT_ID, user_id=admin_id, token_hash=hashlib.sha256(str(_RT_ID).encode()).hexdigest(), device_info="zztest-semester"))
        await db.commit()
    _HEADERS = {"Authorization": f"Bearer {create_access_token(str(admin_id), {'sid': str(_RT_ID)})}"}
    before = await _snapshot_existing()
    try:
        scenarios = {
            "create: no dates -> 201, all four null": t_create_no_dates,
            "create: all four dates -> 201": t_create_all_dates,
            "create: only some dates -> 201": t_create_partial_dates,
            "create: explicit null dates -> 201": t_create_explicit_null_dates,
            "create: invalid / empty-string date still rejected (422)": t_create_invalid_date_rejected,
            "create: calendar_id/name/start_date/end_date still required": t_create_required_fields_still_required,
            "update: name only preserves all dates, sem_type, start/end, calendar": t_update_name_only_preserves_everything,
            "update: name-only body without calendar_id -> 200 (original 422 bug)": t_update_frontend_style_name_only_no_calendar_id,
            "update: single optional date changes only that date": t_update_single_optional_date,
            "update: semester without dates (name + sem_type only)": t_update_semester_without_dates,
            "update: explicit null clears only that optional date": t_update_explicit_null_clears_only_that_date,
            "update: explicit null on a required column -> 422, nothing changed": t_update_explicit_null_on_required_column_rejected,
            "update: invalid/empty date -> 422, nothing partially applied": t_update_invalid_date_rejected_and_nothing_changed,
            "update: unknown semester -> 404": t_update_unknown_semester_404,
            "update: unauthenticated -> 401/403": t_update_requires_super_admin,
            "regression: list + status endpoints still work": t_regression_list_and_status,
        }
        for name, fn in scenarios.items():
            await _run(name, fn)
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Semester).where(Semester.name.like(f"{_PREFIX}%")))
            await db.execute(delete(RefreshToken).where(RefreshToken.id == _RT_ID))
            await db.commit()
        async with AsyncSessionLocal() as db:
            leftover = (await db.execute(select(Semester.id).where(Semester.name.like(f"{_PREFIX}%")))).scalars().all()
        after = await _snapshot_existing()
        RESULTS.record("cleanup: no ZZTEST semester remains", not leftover, f"leftover={leftover}")
        RESULTS.record("cleanup: pre-existing semesters byte-for-byte unchanged", before == after, "snapshot differs")
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
