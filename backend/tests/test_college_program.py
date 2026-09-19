"""Standalone HTTP-level tests for the College<->Programme association
(`ams_college_programs`, managed at /admin/colleges/{id}/programs by Super
Admin) and a regression check that the independent Department<->Programme
association (`ams_program_departments`) is unaffected.

Drives the REAL FastAPI app via `httpx.ASGITransport` with real JWT sessions
(a real Super Admin from the local dev DB, plus throw-away HOD/Faculty/Student
users) against the local Postgres in `.env`. Every record created here is a
temporary `ZZTEST` College/Programme/Department/User; nothing pre-existing is
modified, and a before/after snapshot of every real association row is
asserted at the end.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_college_program
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import select, delete, text

from app.db.base import AsyncSessionLocal
from app.main import app
from app.core.security import create_access_token
from app.models.user import (
    User, UserRole, RefreshToken, College, Program, Department, CollegeProgram, ProgramDepartment,
)

_TAG = uuid.uuid4().hex[:6].upper()
_PREFIX = "ZZTEST"


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
S: dict = {}  # shared fixtures


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _call(method: str, url: str, token: str | None = None, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with _client() as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _snapshot_real() -> dict:
    """Content hash of every REAL (non-ZZTEST) association row + master rows."""
    async with AsyncSessionLocal() as db:
        out = {}
        for name, q in {
            "program_departments": "SELECT pd.program_id, pd.department_id FROM ams_program_departments pd JOIN ams_programs p ON p.id=pd.program_id WHERE p.code NOT LIKE 'ZZTEST%' ORDER BY 1,2",
            "college_programs": "SELECT college_id, program_id FROM ams_college_programs cp WHERE college_id IN (SELECT id FROM ams_colleges WHERE code NOT LIKE 'ZZTEST%') ORDER BY 1,2",
            "colleges": "SELECT id, name, code, is_active FROM ams_colleges WHERE code NOT LIKE 'ZZTEST%' ORDER BY id",
            "programs": "SELECT id, name, code, level, is_active FROM ams_programs WHERE code NOT LIKE 'ZZTEST%' ORDER BY id",
            "departments": "SELECT id, name, code, is_active FROM ams_departments WHERE code NOT LIKE 'ZZTEST%' ORDER BY id",
        }.items():
            rows = (await db.execute(text(q))).all()
            out[name] = (len(rows), hashlib.md5(repr(rows).encode()).hexdigest())
        return out


async def _mint_user(tag: str, role: str, department_id: uuid.UUID | None) -> tuple[uuid.UUID, str]:
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PREFIX.lower()}_cp.{tag}.{uuid.uuid4().hex[:6]}@avfu.ac.in", role=UserRole.FACULTY, is_active=True, is_verified=True)
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
    body = {"role": role} | ({"department_id": str(department_id)} if department_id else {})
    r = await _call("POST", f"/auth/users/{uid}/roles", S["super"], json=body)
    assert r.status_code == 201, r.text
    rt = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(id=rt, user_id=uid, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=f"{_PREFIX}-cp"))
        await db.commit()
    tok = create_access_token(str(uid), {"sid": str(rt)})
    me = (await _call("GET", "/auth/me", tok)).json()
    assert me["active_role"] == role, me
    return uid, tok


async def _count_links(college_id, program_id) -> int:
    async with AsyncSessionLocal() as db:
        return len((await db.execute(select(CollegeProgram.id).where(CollegeProgram.college_id == college_id, CollegeProgram.program_id == program_id))).scalars().all())


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=f"{_PREFIX}-cp-admin"))
        await db.commit()
        S["super_rt"] = rt
    S["super"] = create_access_token(str(admin_id), {"sid": str(S["super_rt"])})
    S["users"] = []
    for tag in ("C1", "C2"):
        r = await _call("POST", "/admin/colleges", S["super"], json={"name": f"{_PREFIX} College {tag}", "code": f"{_PREFIX}C{tag}{_TAG}"})
        assert r.status_code == 201, r.text
        S[f"col{tag}"] = uuid.UUID(r.json()["id"])
    for tag in ("P1", "P2"):
        r = await _call("POST", "/departments/programs", S["super"], json={"name": f"{_PREFIX} Programme {tag}", "code": f"{_PREFIX}{tag}{_TAG}", "level": "PG", "duration_years": 2})
        assert r.status_code == 201, r.text
        S[f"prog{tag}"] = uuid.UUID(r.json()["id"])
    r = await _call("POST", "/departments", S["super"], json={"name": f"{_PREFIX} Department", "code": f"{_PREFIX}D{_TAG}"})
    assert r.status_code == 201, r.text
    S["dept"] = uuid.UUID(r.json()["id"])
    for role in ("hod", "faculty", "student"):
        uid, tok = await _mint_user(role, role, S["dept"] if role in ("hod", "faculty") else None)
        S[f"tok_{role}"] = tok
        S["users"].append(uid)


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id.in_(S.get("users", []))))
        await db.execute(delete(College).where(College.code.like(f"{_PREFIX}%")))
        await db.execute(delete(Program).where(Program.code.like(f"{_PREFIX}%")))
        await db.execute(delete(Department).where(Department.code.like(f"{_PREFIX}%")))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info.like(f"{_PREFIX}-cp%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


def _url(college, program=None):
    return f"/admin/colleges/{college}/programs" + (f"/{program}" if program else "")


# ── Super Admin behaviour ────────────────────────────────────────────────────

async def t_super_admin_add_list_remove():
    c, p = S["colC1"], S["progP1"]
    r = await _call("GET", _url(c), S["super"]); assert r.status_code == 200 and r.json() == [], r.text
    r = await _call("POST", _url(c), S["super"], json={"program_id": str(p)}); assert r.status_code == 201, r.text
    r = await _call("GET", _url(c), S["super"])
    assert [x["program_id"] for x in r.json()] == [str(p)] and r.json()[0]["program_code"].startswith(_PREFIX), r.text
    r = await _call("GET", f"/departments/programs/{p}/colleges", S["super"])
    assert [x["college_id"] for x in r.json()] == [str(c)], r.text
    r = await _call("DELETE", _url(c, p), S["super"]); assert r.status_code == 204, r.text
    assert (await _call("GET", _url(c), S["super"])).json() == []
    assert (await _call("GET", f"/departments/programs/{p}/colleges", S["super"])).json() == []


async def t_many_to_many():
    c1, c2, p1, p2 = S["colC1"], S["colC2"], S["progP1"], S["progP2"]
    try:
        for c, p in ((c1, p1), (c2, p1), (c1, p2)):
            r = await _call("POST", _url(c), S["super"], json={"program_id": str(p)}); assert r.status_code == 201, r.text
        assert {x["college_id"] for x in (await _call("GET", f"/departments/programs/{p1}/colleges", S["super"])).json()} == {str(c1), str(c2)}
        assert {x["program_id"] for x in (await _call("GET", _url(c1), S["super"])).json()} == {str(p1), str(p2)}
        r = await _call("DELETE", _url(c1, p1), S["super"]); assert r.status_code == 204
        assert await _count_links(c2, p1) == 1 and await _count_links(c1, p2) == 1, "removing one association must not touch the others"
    finally:
        for c, p in ((c1, p1), (c2, p1), (c1, p2)):
            await _call("DELETE", _url(c, p), S["super"])


# ── Direct-API authorization ─────────────────────────────────────────────────

async def t_non_super_admin_cannot_write():
    c, p = S["colC1"], S["progP1"]
    r = await _call("POST", _url(c), S["super"], json={"program_id": str(p)}); assert r.status_code == 201
    try:
        for role in ("hod", "faculty", "student"):
            tok = S[f"tok_{role}"]
            r = await _call("POST", _url(c), tok, json={"program_id": str(S["progP2"])})
            assert r.status_code == 403, f"{role} POST must be 403, got {r.status_code}"
            r = await _call("DELETE", _url(c, p), tok)
            assert r.status_code == 403, f"{role} DELETE must be 403, got {r.status_code}"
            # Even with a real (pre-existing) college + programme id pair the request is refused.
            async with AsyncSessionLocal() as db:
                real_c = (await db.execute(select(College.id).where(College.code.notlike(f"{_PREFIX}%")).limit(1))).scalar_one()
                real_p = (await db.execute(select(Program.id).where(Program.code.notlike(f"{_PREFIX}%")).limit(1))).scalar_one()
            assert (await _call("POST", _url(real_c), tok, json={"program_id": str(real_p)})).status_code == 403
            assert (await _call("DELETE", _url(real_c, real_p), tok)).status_code == 403
        assert (await _call("POST", _url(c), None, json={"program_id": str(S["progP2"])})).status_code in (401, 403), "unauthenticated write"
        assert (await _call("DELETE", _url(c, p), None)).status_code in (401, 403), "unauthenticated delete"
        assert await _count_links(c, p) == 1, "the association must still exist after every forbidden DELETE"
        assert await _count_links(c, S["progP2"]) == 0, "no association may be created by a forbidden POST"
        # Reads mirror the existing College/Department reads: any authenticated user.
        assert (await _call("GET", _url(c), S["tok_hod"])).status_code == 200
    finally:
        await _call("DELETE", _url(c, p), S["super"])


# ── Validation ───────────────────────────────────────────────────────────────

async def t_validation():
    c, p, ghost = S["colC1"], S["progP1"], uuid.uuid4()
    assert (await _call("GET", _url(ghost), S["super"])).status_code == 404
    assert (await _call("POST", _url(ghost), S["super"], json={"program_id": str(p)})).status_code == 404, "invalid college on add"
    assert (await _call("POST", _url(c), S["super"], json={"program_id": str(ghost)})).status_code == 404, "invalid programme on add"
    assert (await _call("DELETE", _url(ghost, p), S["super"])).status_code == 404, "invalid college on remove"
    assert (await _call("DELETE", _url(c, ghost), S["super"])).status_code == 404, "invalid programme on remove"
    assert (await _call("GET", f"/departments/programs/{ghost}/colleges", S["super"])).status_code == 404
    assert (await _call("POST", _url(c), S["super"], json={})).status_code == 422, "missing program_id"
    assert (await _call("POST", _url(c), S["super"], json={"program_id": "not-a-uuid"})).status_code == 422
    assert (await _call("GET", "/admin/colleges/not-a-uuid/programs", S["super"])).status_code == 422
    # duplicate
    assert (await _call("POST", _url(c), S["super"], json={"program_id": str(p)})).status_code == 201
    try:
        r = await _call("POST", _url(c), S["super"], json={"program_id": str(p)})
        assert r.status_code == 409, r.text
        assert await _count_links(c, p) == 1, "duplicate must not create a second row"
    finally:
        await _call("DELETE", _url(c, p), S["super"])
    # removing a non-existent association (both ids valid) is an idempotent no-op
    r = await _call("DELETE", _url(c, p), S["super"]); assert r.status_code == 204, r.text


async def t_db_unique_constraint_backstop():
    c, p = S["colC1"], S["progP1"]
    from sqlalchemy.exc import IntegrityError
    async with AsyncSessionLocal() as db:
        db.add(CollegeProgram(college_id=c, program_id=p)); await db.commit()
    try:
        async with AsyncSessionLocal() as db:
            db.add(CollegeProgram(college_id=c, program_id=p))
            try:
                await db.commit()
                raise AssertionError("uq_college_program did not reject a duplicate row")
            except IntegrityError:
                await db.rollback()
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(CollegeProgram).where(CollegeProgram.college_id == c)); await db.commit()


# ── Data integrity + Department<->Programme regression ──────────────────────

async def t_integrity_and_department_programme_regression():
    c, p, d, tok = S["colC1"], S["progP1"], S["dept"], S["super"]
    # Department<->Programme: the existing behaviour, end to end.
    assert (await _call("GET", f"/departments/programs/{p}/departments", tok)).json() == []
    r = await _call("POST", f"/departments/programs/{p}/departments", tok, json={"department_id": str(d)}); assert r.status_code == 201, r.text
    assert (await _call("POST", f"/departments/programs/{p}/departments", tok, json={"department_id": str(d)})).status_code == 409, "duplicate dept-programme"
    assert (await _call("POST", f"/departments/programs/{p}/departments", tok, json={"department_id": str(uuid.uuid4())})).status_code == 404, "invalid department"
    assert (await _call("POST", f"/departments/programs/{uuid.uuid4()}/departments", tok, json={"department_id": str(d)})).status_code == 404, "invalid programme"
    assert (await _call("POST", f"/departments/programs/{p}/departments", S["tok_hod"], json={"department_id": str(d)})).status_code == 403
    assert [x["department_id"] for x in (await _call("GET", f"/departments/programs/{p}/departments", tok)).json()] == [str(d)]
    assert [x["program_id"] for x in (await _call("GET", f"/departments/{d}/programs", tok)).json()] == [str(p)]
    assert [x["id"] for x in (await _call("GET", f"/departments?program_id={p}", tok)).json()] == [str(d)]

    # Adding then removing a College<->Programme association changes nothing else.
    assert (await _call("POST", _url(c), tok, json={"program_id": str(p)})).status_code == 201
    assert (await _call("GET", f"/departments/programs/{p}/departments", tok)).json()[0]["department_id"] == str(d), "dept link changed by a college link"
    assert (await _call("DELETE", _url(c, p), tok)).status_code == 204
    async with AsyncSessionLocal() as db:
        assert await db.get(College, c) is not None, "college was deleted"
        assert await db.get(Program, p) is not None, "programme was deleted"
        assert await db.get(Department, d) is not None, "department was deleted"
        dept_links = (await db.execute(select(ProgramDepartment.id).where(ProgramDepartment.program_id == p, ProgramDepartment.department_id == d))).scalars().all()
    assert len(dept_links) == 1, "department-programme link must survive college-programme add/remove"

    # ...and removing the Department<->Programme link leaves the College link alone.
    assert (await _call("POST", _url(c), tok, json={"program_id": str(p)})).status_code == 201
    assert (await _call("DELETE", f"/departments/programs/{p}/departments/{d}", tok)).status_code == 204
    assert (await _call("DELETE", f"/departments/programs/{p}/departments/{d}", tok)).status_code == 204, "idempotent removal"
    assert await _count_links(c, p) == 1, "removing a department link must not remove the college link"
    assert (await _call("GET", f"/departments/programs/{p}/departments", tok)).json() == []
    assert (await _call("DELETE", _url(c, p), tok)).status_code == 204


async def main() -> None:
    before = await _snapshot_real()
    await _setup()
    try:
        for name, fn in {
            "Super Admin: list -> add -> list (both directions) -> remove": t_super_admin_add_list_remove,
            "Many-to-many: one programme in several colleges, one college with several programmes; removals are independent": t_many_to_many,
            "SECURITY: HOD/Faculty/Student/unauthenticated POST+DELETE -> 403/401, nothing created or removed, real ids too": t_non_super_admin_cannot_write,
            "Validation: unknown college/programme 404, malformed/missing 422, duplicate 409, missing association removal 204": t_validation,
            "DB: uq_college_program rejects a duplicate row": t_db_unique_constraint_backstop,
            "Integrity + REGRESSION: Department<->Programme unchanged; college link changes never delete/alter colleges, programmes, departments or dept links": t_integrity_and_department_programme_regression,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "colleges": (await db.execute(select(College.id).where(College.code.like(f"{_PREFIX}%")))).scalars().all(),
                "programs": (await db.execute(select(Program.id).where(Program.code.like(f"{_PREFIX}%")))).scalars().all(),
                "departments": (await db.execute(select(Department.id).where(Department.code.like(f"{_PREFIX}%")))).scalars().all(),
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_PREFIX.lower()}_cp%")))).scalars().all(),
            }
        RESULTS.record("cleanup: no ZZTEST college/programme/department/user remains", not any(left.values()), str(left))
        after = await _snapshot_real()
        RESULTS.record("cleanup: every real college/programme/department/association row unchanged", before == after, f"before={before} after={after}")
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
