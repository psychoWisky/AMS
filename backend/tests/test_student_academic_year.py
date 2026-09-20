"""Standalone HTTP-level tests for the student's Academic Year (`User.academic_year_id`).

A student's Academic Year is an explicit reference to the Super Admin-managed
AcademicCalendar. It is a SEPARATE field from `admission_year` and from registrations:

  * schema (migration 0024): nullable UUID column, FK -> ams_academic_calendars.id, index;
    no existing student was populated
  * PATCH /students/{id}: assign, re-assign, clear (null); unknown id -> 400; malformed -> 422;
    a rejected request changes nothing; any calendar status is accepted
  * the student payload carries `academic_year_id` and `academic_year` (the calendar's label)
    in list and detail; `latest_academic_year` no longer exists
  * `admission_year` is never touched or synchronised
  * security: only Super Admin (HOD / Faculty / Student / anonymous cannot; a student's own
    PATCH /auth/me cannot set it)
  * GET /students?academic_year_id= filters directly on the column, alone and combined with
    department / programme / college / semester, paginates, never duplicates
  * a calendar with assigned students cannot be deleted (400), and can once they are cleared

Real FastAPI app via `httpx.ASGITransport`, real JWT sessions. Everything created is
`ZZTEST_ACADEMIC_YEAR...` and deleted in `finally`; real students/calendars are never modified.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_student_academic_year
"""
import asyncio
import hashlib
import importlib.util
import pathlib
import sys
import uuid

import httpx
from sqlalchemy import select, delete, func, text

from app.db.base import AsyncSessionLocal
from app.main import app
from app.core.security import create_access_token
from app.models.academic import AcademicCalendar, Semester
from app.models.enrollment import CourseRegistration
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Department, Program, College

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_academic_year_"
_LABEL = "ZZTEST_ACADEMIC_YEAR"
_DEVICE = "ZZTEST-academic-year"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
S: dict = {}
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _call(method, url, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _session(user_id) -> str:
    async with AsyncSessionLocal() as db:
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=user_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    return create_access_token(str(user_id), {"sid": str(rt)})


async def _mk(key, role, dept, *, program=None, college=None, ay=None, admission_year=None, assignments=None) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"AY{key}", role=role, department_id=dept.id,
                 program_id=program.id if program else None, college_id=college, academic_year_id=ay, admission_year=admission_year,
                 student_roll=f"ZZTEST-AY-{key}-{_TAG}" if role == UserRole.STUDENT else None,
                 mobile="9876543210", gender="Male", is_active=True, is_verified=True)
        db.add(u)
        await db.flush()
        for r in (assignments if assignments is not None else [role]):
            db.add(UserRoleAssignment(user_id=u.id, role=r, department_id=dept.id if r in (UserRole.HOD, UserRole.FACULTY) else None))
        await db.commit()
        U[key] = u.id
    return u.id


async def _row(key) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == U[key]))).scalar_one()


def _state(u: User) -> tuple:
    return (u.email, u.first_name, u.mobile, u.student_roll, u.department_id, u.program_id, u.college_id, u.admission_year, u.academic_year_id, u.is_active)


async def _patch(key, body, token=None):
    return await _call("PATCH", f"/students/{U[key]}", token or S["super"], json=body)


async def _detail(key) -> dict:
    return (await _call("GET", f"/students/{U[key]}", S["super"])).json()


async def _list(**params) -> dict:
    r = await _call("GET", "/students", S["super"], params={"q": _PFX, "page_size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _keys(items) -> set:
    inv = {str(v): k for k, v in U.items()}
    return {inv[i["id"]] for i in items if i["id"] in inv}


async def _calendars_digest() -> tuple:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("select md5(c::text) from ams_academic_calendars c where c.name not like 'ZZTEST%' order by 1"))).scalars().all()
    return tuple(rows)


async def _mk_calendar(label: str, status: str = "draft", start="2098-06-01", end="2099-05-31") -> dict:
    r = await _call("POST", "/academic/calendars", S["super"], json={"name": f"{_LABEL} {label}", "academic_year": f"ZZTESTAY {label}", "start_date": start, "end_date": end})
    assert r.status_code == 201, r.text
    cal = r.json()
    if status != "draft":
        assert (await _call("PATCH", f"/academic/calendars/{cal['id']}/status", S["super"], params={"status": status})).status_code == 200
    return cal


async def _setup() -> None:
    S["cal_digest_before"] = await _calendars_digest()
    async with AsyncSessionLocal() as db:
        S["D1"], S["D2"] = (await db.execute(select(Department).order_by(Department.code).limit(2))).scalars().all()
        S["P1"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        S["P2"] = (await db.execute(select(Program).where(Program.code == "Ph.D(V)"))).scalar_one()
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        for k, code in (("C1", "ZZTESTAYC1"), ("C2", "ZZTESTAYC2")):
            c = College(name=f"{_LABEL} College {k}", code=f"{code}{_TAG.upper()}")
            db.add(c)
            await db.flush()
            S[k] = c.id
        sems = (await db.execute(select(Semester).order_by(Semester.start_date).limit(2))).scalars().all()
        S["semX"], S["semY"], S["calReg"] = sems[0].id, sems[1].id, sems[0].calendar_id
        await db.commit()
    S["super"] = await _session(admin_id)
    S["cal1"] = await _mk_calendar(f"A {_TAG}", "draft")                       # draft
    S["cal2"] = await _mk_calendar(f"B {_TAG}", "closed", "2097-06-01", "2098-05-31")  # closed
    d1, d2, p1, p2, c1, c2 = S["D1"], S["D2"], S["P1"], S["P2"], S["C1"], S["C2"]
    await _mk("hod", UserRole.HOD, d1)
    await _mk("fac", UserRole.FACULTY, d1)
    # students: A(D1,P1,C1) B(D2,P1,C1) C(D1,P2,C2) -> cal1;  E(D1,P1,C1) -> cal2;  F(D1,P1,C1) unassigned; X: admission_year sample
    await _mk("A", UserRole.STUDENT, d1, program=p1, college=c1, ay=uuid.UUID(S["cal1"]["id"]), admission_year=2026)
    await _mk("B", UserRole.STUDENT, d2, program=p1, college=c1, ay=uuid.UUID(S["cal1"]["id"]))
    await _mk("C", UserRole.STUDENT, d1, program=p2, college=c2, ay=uuid.UUID(S["cal1"]["id"]))
    await _mk("E", UserRole.STUDENT, d1, program=p1, college=c1, ay=uuid.UUID(S["cal2"]["id"]))
    await _mk("F", UserRole.STUDENT, d1, program=p1, college=c1, admission_year=2026)
    for k in ("hod", "fac", "A"):
        TOK[k] = await _session(U[k])
    async with AsyncSessionLocal() as db:   # registrations in a REAL calendar: A twice (two semesters), C once — unrelated to the assigned year
        for key, sem in (("A", S["semX"]), ("A", S["semY"]), ("C", S["semX"])):
            db.add(CourseRegistration(student_id=U[key], semester_id=sem, calendar_id=S["calReg"]))
        await db.commit()


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        ids = select(User.id).where(User.email.like(f"{_PFX}%"))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.student_id.in_(ids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(ids)))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.execute(delete(AcademicCalendar).where(AcademicCalendar.name.like(f"{_LABEL}%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── schema / migration ─────────────────────────────────────────────────────

async def t_schema_and_no_backfill():
    async with AsyncSessionLocal() as db:
        col = (await db.execute(text("select data_type, is_nullable from information_schema.columns where table_name='ams_users' and column_name='academic_year_id'"))).one()
        assert col == ("uuid", "YES"), col
        fk = (await db.execute(text("""select confrelid::regclass::text, pg_get_constraintdef(oid) from pg_constraint
                                        where conrelid='ams_users'::regclass and contype='f' and conname='fk_users_academic_year_id'"""))).one()
        assert fk[0] == "ams_academic_calendars" and "(id)" in fk[1] and "ON DELETE" not in fk[1], fk
        assert (await db.execute(text("select count(*) from pg_indexes where tablename='ams_users' and indexname='ix_ams_users_academic_year_id'"))).scalar_one() == 1
        real_assigned = (await db.execute(text("select count(*) from ams_users where academic_year_id is not null and email not like :p"), {"p": f"{_PFX}%"})).scalar_one()
        assert real_assigned == 0, "the migration must not populate any existing student"
    spec = importlib.util.spec_from_file_location("mig0024", pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0024_user_academic_year.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.revision == "0024_user_academic_year" and mig.down_revision == "0023_backfill_student_college"
    assert mig.upgrade.__code__.co_names and "drop_column" in mig.downgrade.__code__.co_names, "a real (schema) downgrade"


# ── API ────────────────────────────────────────────────────────────────────

async def t_assign_reassign_and_payload():
    cal1, cal2 = S["cal1"], S["cal2"]
    # F starts unassigned
    d = await _detail("F")
    assert d["academic_year_id"] is None and d["academic_year"] is None
    r = await _patch("F", {"academic_year_id": cal1["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["academic_year_id"] == cal1["id"] and body["academic_year"] == cal1["academic_year"], body
    d = await _detail("F")
    assert (d["academic_year_id"], d["academic_year"]) == (cal1["id"], cal1["academic_year"])
    item = next(i for i in (await _list())["items"] if i["id"] == str(U["F"]))
    assert (item["academic_year_id"], item["academic_year"]) == (cal1["id"], cal1["academic_year"]), "the list payload carries it too — no extra request per student"
    assert "latest_academic_year" not in item and "latest_semester" in item
    # re-assign to another calendar; any status (draft, closed) is accepted
    r = await _patch("F", {"academic_year_id": cal2["id"]})
    assert r.status_code == 200 and r.json()["academic_year_id"] == cal2["id"], "a closed calendar is selectable"
    assert (await _row("F")).academic_year_id == uuid.UUID(cal2["id"])


async def t_invalid_ids_rejected_and_nothing_changes():
    before = _state(await _row("A"))
    for bad, code in ((str(uuid.uuid4()), 400), ("not-a-uuid", 422), (12345, 422)):
        r = await _patch("A", {"academic_year_id": bad})
        assert r.status_code == code, (bad, r.status_code, r.text)
        assert _state(await _row("A")) == before, "rejected: nothing changed"
    # atomic: a valid field in the same request is not applied when the calendar is unknown
    r = await _patch("A", {"mobile": "9111111111", "academic_year_id": str(uuid.uuid4())})
    assert r.status_code == 400 and "academic year" in r.json()["detail"].lower(), r.text
    assert _state(await _row("A")) == before, "the valid mobile edit must not be applied"
    assert (await _patch("A", {"academic_year_id": S["cal1"]["id"], "mobile": "9111111111"})).status_code == 200
    assert (await _row("A")).mobile == "9111111111"


async def t_clearing():
    assert (await _patch("B", {"academic_year_id": None})).status_code == 200
    d = await _detail("B")
    assert d["academic_year_id"] is None and d["academic_year"] is None, "null clears it (an unassigned student is valid)"
    assert (await _row("B")).academic_year_id is None
    assert (await _patch("B", {"academic_year_id": S["cal1"]["id"]})).status_code == 200, "and it can be assigned again"
    r = await _patch("B", {"mobile": "9222222222"})
    assert r.status_code == 200 and r.json()["academic_year_id"] == S["cal1"]["id"], "an unrelated edit leaves it alone"


async def t_admission_year_is_independent():
    a0 = await _row("A")
    assert a0.admission_year == 2026
    assert (await _patch("A", {"academic_year_id": S["cal2"]["id"]})).status_code == 200
    a1 = await _row("A")
    assert a1.admission_year == 2026 and a1.academic_year_id == uuid.UUID(S["cal2"]["id"]), "assigning an Academic Year must not touch admission_year"
    assert (await _patch("A", {"admission_year": 2030})).status_code == 200
    a2 = await _row("A")
    assert a2.admission_year == 2030 and a2.academic_year_id == uuid.UUID(S["cal2"]["id"]), "changing admission_year must not touch the Academic Year"
    d = await _detail("A")
    assert d["admission_year"] == 2030 and d["academic_year"] == S["cal2"]["academic_year"], "both are returned, with different meanings"
    assert (await _patch("A", {"admission_year": 2026, "academic_year_id": S["cal1"]["id"]})).status_code == 200   # restore
    assert (await _patch("F", {"academic_year_id": None})).status_code == 200


async def t_security():
    before = _state(await _row("F"))
    body = {"academic_year_id": S["cal1"]["id"]}
    for who in ("hod", "fac", "A"):
        assert (await _patch("F", body, TOK[who])).status_code == 403, who
    assert (await _call("PATCH", f"/students/{U['F']}", None, json=body)).status_code in (401, 403)
    assert _state(await _row("F")) == before, "nothing was modified"
    # a student's own profile edit cannot set it, and a student cannot use the Students endpoint on themselves
    a_before = (await _row("A")).academic_year_id
    r = await _call("PATCH", "/auth/me", TOK["A"], json={"academic_year_id": S["cal2"]["id"], "address": "ZZTEST self edit"})
    assert r.status_code in (200, 422), r.text
    assert (await _row("A")).academic_year_id == a_before, "PATCH /auth/me must not change the Academic Year"
    assert (await _call("PATCH", f"/students/{U['A']}", TOK["A"], json=body)).status_code == 403
    # staff (User Management) cannot be given one either; and a staff id is not a student
    assert (await _call("PATCH", f"/students/{U['hod']}", S["super"], json=body)).status_code == 404


# ── filters ────────────────────────────────────────────────────────────────

async def t_filter_alone_and_combined():
    c1, c2 = S["cal1"]["id"], S["cal2"]["id"]
    d1, d2, p1, p2, col1, col2 = str(S["D1"].id), str(S["D2"].id), str(S["P1"].id), str(S["P2"].id), str(S["C1"]), str(S["C2"])
    assert _keys((await _list(academic_year_id=c1))["items"]) == {"A", "B", "C"}
    assert _keys((await _list(academic_year_id=c2))["items"]) == {"E"}
    assert _keys((await _list(academic_year_id=str(uuid.uuid4())))["items"]) == set()
    assert _keys((await _list(academic_year_id=c1, department_id=d1))["items"]) == {"A", "C"}
    assert _keys((await _list(academic_year_id=c1, department_id=d2))["items"]) == {"B"}
    assert _keys((await _list(academic_year_id=c1, program_id=p1))["items"]) == {"A", "B"}
    assert _keys((await _list(academic_year_id=c1, program_id=p2))["items"]) == {"C"}
    assert _keys((await _list(academic_year_id=c1, college_id=col1))["items"]) == {"A", "B"}
    assert _keys((await _list(academic_year_id=c1, college_id=col2))["items"]) == {"C"}
    assert _keys((await _list(academic_year_id=c1, department_id=d1, program_id=p1, college_id=col1))["items"]) == {"A"}
    assert _keys((await _list(academic_year_id=c1, department_id=d2, program_id=p2))["items"]) == set()
    # semester stays a separate concept: registrations are in a real calendar, unrelated to the assigned year
    sx, sy = str(S["semX"]), str(S["semY"])
    assert _keys((await _list(semester_id=sx))["items"]) == {"A", "C"}
    assert _keys((await _list(academic_year_id=c1, semester_id=sx))["items"]) == {"A", "C"}
    assert _keys((await _list(academic_year_id=c1, semester_id=sy))["items"]) == {"A"}
    assert _keys((await _list(academic_year_id=c2, semester_id=sx))["items"]) == set(), "E is assigned cal2 but has no registration in semX"
    assert _keys((await _list(academic_year_id=c1, semester_id=sx, department_id=d1, program_id=p2, college_id=col2))["items"]) == {"C"}
    items = (await _list(academic_year_id=c1, semester_id=sx))["items"]
    assert len({i["id"] for i in items}) == len(items), "A has two registrations but appears once"
    # the derived registration year never leaks into the filter: A is registered in calReg, yet calReg matches nobody here
    assert _keys((await _list(academic_year_id=str(S["calReg"])))["items"]) == set()


async def t_filter_pagination_no_duplicates():
    c1 = S["cal1"]["id"]
    p1 = await _list(academic_year_id=c1, page_size=2, page=1)
    p2 = await _list(academic_year_id=c1, page_size=2, page=2)
    assert p1["total"] == p2["total"] == 3 and len(p1["items"]) == 2 and len(p2["items"]) == 1
    ids = [i["id"] for i in p1["items"] + p2["items"]]
    assert len(set(ids)) == 3, "pages do not overlap"
    assert _keys(p1["items"] + p2["items"]) == {"A", "B", "C"}


# ── calendar delete guard ──────────────────────────────────────────────────

async def t_calendar_delete_guard():
    cal = await _mk_calendar(f"DEL {_TAG}", "draft", "2093-06-01", "2094-05-31")
    assert (await _patch("F", {"academic_year_id": cal["id"]})).status_code == 200
    r = await _call("DELETE", f"/academic/calendars/{cal['id']}", S["super"])
    assert r.status_code == 400 and "assigned to students" in r.json()["detail"], r.text
    assert (await _row("F")).academic_year_id == uuid.UUID(cal["id"]), "the student keeps their assignment"
    assert (await _call("GET", f"/academic/calendars/{cal['id']}", S["super"])).status_code == 200, "the calendar still exists"
    assert (await _patch("F", {"academic_year_id": None})).status_code == 200
    assert (await _call("DELETE", f"/academic/calendars/{cal['id']}", S["super"])).status_code == 204, "once unassigned it can be deleted"


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "SCHEMA: nullable uuid column, FK -> ams_academic_calendars.id (no ON DELETE), index; no existing student populated; migration chain": t_schema_and_no_backfill,
            "API: assign / re-assign (draft and closed calendars); payload has academic_year_id + academic_year in detail and list": t_assign_reassign_and_payload,
            "API: unknown calendar -> 400, malformed -> 422; nothing changed; atomic with other fields": t_invalid_ids_rejected_and_nothing_changes,
            "API: null clears the Academic Year; it can be assigned again; unrelated edits leave it alone": t_clearing,
            "ADMISSION YEAR: independent of the Academic Year in both directions; both returned": t_admission_year_is_independent,
            "SECURITY: HOD / Faculty / Student / anonymous cannot set it (403/401); /auth/me cannot; staff id is 404": t_security,
            "FILTER: academic_year_id alone and combined with department, programme, college, semester; no duplicates": t_filter_alone_and_combined,
            "FILTER: pagination with the Academic Year filter — totals right, pages do not overlap": t_filter_pagination_no_duplicates,
            "CALENDAR DELETE: refused (400) while students are assigned, allowed once cleared": t_calendar_delete_guard,
        }.items():
            await _run(name, fn)
        record("existing (non-ZZTEST) calendars are unchanged", await _calendars_digest() == S["cal_digest_before"])
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one(),
                "calendars": (await db.execute(select(func.count()).select_from(AcademicCalendar).where(AcademicCalendar.name.like(f"{_LABEL}%")))).scalar_one(),
                "colleges": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "registrations": (await db.execute(select(func.count()).select_from(CourseRegistration).where(~CourseRegistration.student_id.in_(select(User.id))))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_ACADEMIC_YEAR user / calendar / college / session / registration / assignment remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
