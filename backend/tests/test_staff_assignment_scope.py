"""Standalone HTTP-level tests: staff role/department membership comes from
`UserRoleAssignment`, never from the legacy `User.role` / `User.department_id`.

Covers the three fixed areas through the REAL FastAPI app (`httpx.ASGITransport`,
real JWTs/sessions, real role-assignment endpoints):
  1. Super Admin User Management  — GET /auth/users returns every real, paired
     assignment; role / department filters match by assignment; no duplicates.
  2. HOD Faculty Management       — GET /auth/users?role=faculty for a HOD is
     "has a FACULTY assignment in my ACTIVE department".
  3. Course Offering faculty      — POST /courses/offerings and POST
     /courses/offerings/{id}/faculty require an active user with FACULTY in the
     offering's department (HOD alone / Faculty elsewhere / Student -> 403).

Test users are created with LEGACY fields that deliberately disagree with their
assignments (e.g. a FACULTY@B user whose legacy department is A) — exactly the
situation that used to be invisible/ineligible. Assignments are added through the
real Super Admin endpoint, which never touches the legacy fields.

Runs against the local Postgres in `.env`. Everything created here is
`zztest_multirole_...` / `ZZTEST_MULTIROLE_...` and is deleted in `finally`
(offerings before users: OfferingFaculty.faculty_id has no ON DELETE).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_staff_assignment_scope
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import select, delete, event

from app.db.base import AsyncSessionLocal, engine
from app.main import app
from app.core.security import create_access_token
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Department
from app.models.course import Course, CourseOffering
from app.models.academic import Semester

_TAG = uuid.uuid4().hex[:6]
_EMAIL_PREFIX = "zztest_multirole_"
_COURSE_PREFIX = "ZZTEST_MULTIROLE_"


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
S: dict = {}          # shared fixtures
U: dict = {}          # name -> user id
TOK: dict = {}        # name -> access token
_SECTION = [0]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _call(method: str, url: str, token: str, **kw) -> httpx.Response:
    async with _client() as c:
        return await c.request(method, f"/api/v1{url}", headers={"Authorization": f"Bearer {token}"}, **kw)


async def _mk_user(name: str, legacy_role: UserRole, legacy_dept, *, active: bool = True) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_EMAIL_PREFIX}{name}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=name,
            role=legacy_role, department_id=legacy_dept, is_active=active, is_verified=True,
        )
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
    U[name] = uid
    return uid


async def _grant(name: str, role: str, dept=None) -> None:
    body = {"role": role} | ({"department_id": str(dept)} if dept else {})
    r = await _call("POST", f"/auth/users/{U[name]}/roles", S["super"], json=body)
    assert r.status_code == 201, f"grant {name} {role}@{dept}: {r.status_code} {r.text}"


async def _session(name: str) -> str:
    rt = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(id=rt, user_id=U[name], token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info="ZZTEST-multirole-scope"))
        await db.commit()
    TOK[name] = create_access_token(str(U[name]), {"sid": str(rt)})
    return TOK[name]


async def _list(token: str, **params) -> list[dict]:
    r = await _call("GET", "/auth/users", token, params=params)
    assert r.status_code == 200, f"GET /auth/users {params}: {r.status_code} {r.text}"
    return r.json()


def _ids(rows: list[dict], names: list[str]) -> dict[str, int]:
    """How many times each named test user appears in `rows`."""
    wanted = {str(U[n]): n for n in names}
    out = {n: 0 for n in names}
    for r in rows:
        if r["id"] in wanted:
            out[wanted[r["id"]]] += 1
    return out


def _present(rows, names): return {n for n, c in _ids(rows, names).items() if c > 0}


ALL = ["UA", "UB", "UC", "UD", "UE", "UF", "S_A", "S_B", "UM", "HOD_A", "HOD_B"]


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        depts = (await db.execute(select(Department).order_by(Department.code).limit(2))).scalars().all()
        S["A"], S["B"] = depts[0], depts[1]
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info="ZZTEST-multirole-scope"))
        sem = (await db.execute(select(Semester.id, Semester.calendar_id).limit(1))).one()
        S["sem"], S["cal"] = sem.id, sem.calendar_id
        await db.commit()
    S["super"] = create_access_token(str(admin_id), {"sid": str(rt)})
    A, B = S["A"].id, S["B"].id

    # LEGACY fields deliberately disagree with the assignments below.
    await _mk_user("UA", UserRole.FACULTY, A);  await _grant("UA", "faculty", A); await _grant("UA", "faculty", B)
    await _mk_user("UB", UserRole.HOD, B);      await _grant("UB", "hod", B);     await _grant("UB", "faculty", B)
    await _mk_user("UC", UserRole.HOD, A);      await _grant("UC", "hod", A);     await _grant("UC", "faculty", B)
    await _mk_user("UD", UserRole.HOD, B);      await _grant("UD", "hod", B)
    await _mk_user("UE", UserRole.FACULTY, B, active=False); await _grant("UE", "faculty", B)
    await _mk_user("UF", UserRole.FACULTY, A);  await _grant("UF", "faculty", A)
    await _mk_user("S_A", UserRole.STUDENT, A); await _grant("S_A", "student")
    await _mk_user("S_B", UserRole.STUDENT, B); await _grant("S_B", "student")
    await _mk_user("UM", UserRole.HOD, A);      await _grant("UM", "hod", A);     await _grant("UM", "hod", B)
    await _mk_user("HOD_A", UserRole.HOD, A);   await _grant("HOD_A", "hod", A)
    await _mk_user("HOD_B", UserRole.HOD, B);   await _grant("HOD_B", "hod", B)
    S["dpgs"] = False
    await _mk_user("G", UserRole.DPGS, None)
    r = await _call("POST", f"/auth/users/{U['G']}/roles", S["super"], json={"role": "dpgs"})
    S["dpgs"] = r.status_code == 201   # DPGS is single-holder; skip the global-role asserts if someone else holds it
    for n in ("HOD_A", "HOD_B", "UM"):
        await _session(n)
    # Two temporary courses (one per department) for the offering tests.
    for tag, dept in (("A", A), ("B", B)):
        r = await _call("POST", "/courses", S["super"], json={"course_number": f"{_COURSE_PREFIX}{tag}_{_TAG}".upper(), "title": f"Scope test {tag}", "department_id": str(dept)})
        assert r.status_code == 201, r.text
        S[f"course_{tag}"] = r.json()["id"]


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        cids = (await db.execute(select(Course.id).where(Course.course_number.like(f"{_COURSE_PREFIX}%")))).scalars().all()
        await db.execute(delete(CourseOffering).where(CourseOffering.course_id.in_(cids)))
        await db.execute(delete(Course).where(Course.id.in_(cids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == "ZZTEST-multirole-scope"))
        await db.execute(delete(User).where(User.email.like(f"{_EMAIL_PREFIX}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


# ── 1. Super Admin User Management ──────────────────────────────────────────

async def t_user_management_shows_real_paired_assignments():
    rows = await _list(S["super"])
    counts = _ids(rows, ALL)
    assert all(c == 1 for c in counts.values() if c) and counts["UE"] == 0, f"each active user exactly once (inactive excluded): {counts}"
    by_id = {r["id"]: r for r in rows}
    A, B = S["A"], S["B"]

    def pairs(name):
        return sorted((a["role"], a["department_id"], a["department_name"]) for a in by_id[str(U[name])]["assigned_role_assignments"])

    assert pairs("UA") == sorted([("faculty", str(A.id), A.name), ("faculty", str(B.id), B.name)]), pairs("UA")
    assert pairs("UB") == sorted([("faculty", str(B.id), B.name), ("hod", str(B.id), B.name)]), pairs("UB")
    assert pairs("UC") == sorted([("faculty", str(B.id), B.name), ("hod", str(A.id), A.name)]), "HOD@A and FACULTY@B must stay paired"
    assert pairs("UM") == sorted([("hod", str(A.id), A.name), ("hod", str(B.id), B.name)]), pairs("UM")
    assert pairs("UD") == [("hod", str(B.id), B.name)]
    assert pairs("S_B") == [("student", None, None)]
    if S["dpgs"]:
        assert pairs("G") == [("dpgs", None, None)], "a global role has no department and none is invented"
    # real persisted ids, never a fabricated "None"
    async with AsyncSessionLocal() as db:
        real = {str(i) for i in (await db.execute(select(UserRoleAssignment.id).where(UserRoleAssignment.user_id.in_([U[n] for n in ALL])))).scalars().all()}
    shown = {a["id"] for n in ALL if n != "UE" for a in by_id[str(U[n])]["assigned_role_assignments"]}
    assert shown <= real and "None" not in shown and len(shown) == len([1 for n in ALL if n != "UE" for _ in by_id[str(U[n])]["assigned_role_assignments"]]), "assignment ids must be the persisted ones"
    # legacy fields are unchanged and no session is implied
    ua = by_id[str(U["UA"])]
    assert ua["role"] == "faculty" and ua["department_id"] == str(A.id), "legacy primary fields must be untouched"
    assert ua["active_role"] is None and ua["active_role_assignment_id"] is None and ua["active_department_id"] is None
    assert ua["assigned_roles"] == ["faculty"]
    assert by_id[str(U["UB"])]["assigned_roles"] == ["faculty", "hod"]


async def t_role_filter_matches_any_assignment():
    rows = await _list(S["super"], role="faculty")
    assert _present(rows, ALL) == {"UA", "UB", "UC", "UF"}, _present(rows, ALL)
    assert all(c <= 1 for c in _ids(rows, ALL).values()), "no duplicate users (UA has two FACULTY assignments)"
    rows = await _list(S["super"], role="HOD")   # case-insensitive
    assert _present(rows, ALL) == {"UB", "UC", "UD", "UM", "HOD_A", "HOD_B"}, _present(rows, ALL)
    assert all(c <= 1 for c in _ids(rows, ALL).values()), "UM holds HOD twice but must appear once"
    rows = await _list(S["super"], role="student")
    assert _present(rows, ALL) == {"S_A", "S_B"}
    if S["dpgs"]:
        assert "G" in _present(await _list(S["super"], role="dpgs"), ["G"])
    r = await _call("GET", "/auth/users", S["super"], params={"role": "wizard"})
    assert r.status_code == 400, r.text


async def t_department_filter_uses_assignments():
    B, A = S["B"].id, S["A"].id
    assert _present(await _list(S["super"], role="faculty", department_id=str(B)), ALL) == {"UA", "UB", "UC"}
    assert _present(await _list(S["super"], role="faculty", department_id=str(A)), ALL) == {"UA", "UF"}
    assert _present(await _list(S["super"], role="hod", department_id=str(A)), ALL) == {"UC", "UM", "HOD_A"}
    assert _present(await _list(S["super"], department_id=str(B)), ALL) == {"UA", "UB", "UC", "UD", "UM", "HOD_B", "S_B"}, "any staff assignment in B, plus students whose own department is B"
    assert _present(await _list(S["super"], role="student", department_id=str(A)), ALL) == {"S_A"}


# ── 2. HOD Faculty Management ───────────────────────────────────────────────

async def t_hod_faculty_visibility_matrix():
    b = await _list(TOK["HOD_B"], role="faculty")
    a = await _list(TOK["HOD_A"], role="faculty")
    pb, pa = _present(b, ALL), _present(a, ALL)
    assert {"UA", "UB", "UC"} <= pb, f"HOD B must see FACULTY@B users (A: FAC@A+B, C: HOD@B+FAC@B, D: HOD@A+FAC@B): {pb}"
    assert not (pb & {"UD", "UE", "UF", "S_A", "S_B", "UM"}), f"HOD@B alone / inactive / FACULTY@A-only / students must not appear as Faculty to HOD B: {pb}"
    assert {"UA", "UF"} <= pa, pa
    assert not (pa & {"UB", "UC", "UD", "UE", "UM"}), f"HOD A must not see FACULTY@B users, nor HOD-only users, as Faculty: {pa}"
    assert all(c <= 1 for c in _ids(b, ALL).values()) and all(c <= 1 for c in _ids(a, ALL).values())


async def t_hod_scope_cannot_be_overridden_by_client():
    base = _present(await _list(TOK["HOD_B"], role="faculty"), ALL)
    forged = _present(await _list(TOK["HOD_B"], role="faculty", department_id=str(S["A"].id)), ALL)
    assert forged == base, f"department_id=<other dept> must not change a HOD's scope: {forged} vs {base}"
    assert "UF" not in forged
    assert _present(await _list(TOK["HOD_B"], role="hod", department_id=str(S["A"].id)), ALL) == {"UB", "UD", "UM", "HOD_B"}, "HOD list is B's HODs only"
    # Students keep their legacy-department scoping.
    st = _present(await _list(TOK["HOD_B"], role="student"), ALL)
    assert st == {"S_B"}, st
    anyrole = _present(await _list(TOK["HOD_B"]), ALL)
    assert {"UA", "UB", "UC", "UD", "UM", "HOD_B", "S_B"} <= anyrole and not (anyrole & {"UF", "S_A", "HOD_A"}), anyrole


async def t_hod_sees_only_assignments_in_own_department():
    rows = await _list(TOK["HOD_B"], role="faculty")
    by_id = {r["id"]: r for r in rows}
    ua = by_id[str(U["UA"])]["assigned_role_assignments"]
    assert [(a["role"], a["department_id"]) for a in ua] == [("faculty", str(S["B"].id))], "a colleague's assignments in other departments are not exposed to a HOD"
    uc = by_id[str(U["UC"])]["assigned_role_assignments"]
    assert [(a["role"], a["department_id"]) for a in uc] == [("faculty", str(S["B"].id))], "HOD@A of a FACULTY@B user stays hidden from HOD B"


async def t_multi_department_hod_uses_active_assignment():
    me = (await _call("GET", "/auth/me", TOK["UM"])).json()
    hod_a = next(a["id"] for a in me["assigned_role_assignments"] if a["department_id"] == str(S["A"].id))
    hod_b = next(a["id"] for a in me["assigned_role_assignments"] if a["department_id"] == str(S["B"].id))
    assert (await _call("POST", "/auth/switch-role", TOK["UM"], json={"assignment_id": hod_b})).status_code == 200
    as_b = _present(await _list(TOK["UM"], role="faculty"), ALL)
    assert {"UA", "UB", "UC"} <= as_b and "UF" not in as_b, as_b
    assert (await _call("POST", "/auth/switch-role", TOK["UM"], json={"assignment_id": hod_a})).status_code == 200
    as_a = _present(await _list(TOK["UM"], role="faculty"), ALL)
    assert {"UA", "UF"} <= as_a and not (as_a & {"UB", "UC"}), as_a


async def t_no_n_plus_one_queries():
    counts = []
    statements = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        for params in ({}, {"role": "faculty"}, {"role": "faculty", "department_id": str(S["B"].id)}):
            statements.clear()
            rows = await _list(S["super"], **params)
            counts.append((len(rows), len(statements)))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    n_all, q_all = counts[0]
    n_few, q_few = counts[-1]
    assert n_all > n_few + 5, counts
    assert q_all == q_few, f"query count must not grow with the number of users listed: {counts}"
    assert q_all <= 15, f"expected a small constant number of queries, got {counts}"


async def t_legacy_fields_never_rewritten():
    async with AsyncSessionLocal() as db:
        rows = {r.id: r for r in (await db.execute(select(User).where(User.id.in_(list(U.values()))))).scalars().all()}
    assert rows[U["UA"]].role == UserRole.FACULTY and rows[U["UA"]].department_id == S["A"].id
    assert rows[U["UB"]].role == UserRole.HOD and rows[U["UB"]].department_id == S["B"].id
    assert rows[U["UC"]].role == UserRole.HOD and rows[U["UC"]].department_id == S["A"].id


# ── 3. Course Offering faculty eligibility ──────────────────────────────────

async def _offering(token, dept, course_key, faculty: list[str]) -> httpx.Response:
    _SECTION[0] += 1
    ids = [str(U[n]) for n in faculty]
    return await _call("POST", "/courses/offerings", token, json={
        "calendar_id": str(S["cal"]), "semester_id": str(S["sem"]), "course_id": S[f"course_{course_key}"],
        "department_id": str(dept.id), "faculty_ids": ids, "leader_id": ids[0], "section": f"Z{_SECTION[0]}",
    })


async def _offering_count(section_prefix="Z") -> int:
    async with AsyncSessionLocal() as db:
        cids = [S["course_A"], S["course_B"]]
        return len((await db.execute(select(CourseOffering.id).where(CourseOffering.course_id.in_(cids)))).scalars().all())


async def t_offering_create_eligibility_matrix():
    B, A = S["B"], S["A"]
    for who in (["UA"], ["UB"], ["UC"]):
        r = await _offering(TOK["HOD_B"], B, "B", who)
        assert r.status_code == 201, f"{who} must be assignable for department B: {r.status_code} {r.text}"
    for who, why in ((["UF"], "Faculty@A only"), (["UD"], "HOD@B without FACULTY@B"), (["S_B"], "student of department B"),
                     (["UE"], "inactive FACULTY@B"), (["HOD_B"], "the HOD themself without FACULTY@B")):
        before = await _offering_count()
        r = await _offering(TOK["HOD_B"], B, "B", who)
        assert r.status_code == 403, f"{why} must be rejected, got {r.status_code} {r.text}"
        assert await _offering_count() == before, f"a rejected request ({why}) must not create anything"
    before = await _offering_count()
    r = await _offering(TOK["HOD_B"], B, "B", ["UA", "UF"])
    assert r.status_code == 403 and await _offering_count() == before, "one ineligible id in the list rejects the whole request"
    # department A: mirror image
    assert (await _offering(TOK["HOD_A"], A, "A", ["UA"])).status_code == 201, "FACULTY@A+B works in A too"
    assert (await _offering(TOK["HOD_A"], A, "A", ["UF"])).status_code == 201
    assert (await _offering(TOK["HOD_A"], A, "A", ["UB"])).status_code == 403, "HOD@B+FACULTY@B is not Faculty of A"
    assert (await _offering(TOK["HOD_A"], A, "A", ["UC"])).status_code == 403, "FACULTY@B is not Faculty of A even though UC is HOD@A"
    # existing department authorization is unchanged
    assert (await _offering(TOK["HOD_A"], B, "B", ["UA"])).status_code == 403, "HOD@A still cannot create offerings for department B"
    # Super Admin is not exempt from the eligibility rule
    assert (await _offering(S["super"], B, "B", ["UF"])).status_code == 403
    assert (await _offering(S["super"], B, "B", ["UA"])).status_code == 201


async def t_offering_assign_and_remove_faculty():
    B = S["B"]
    r = await _offering(TOK["HOD_B"], B, "B", ["UA"])
    assert r.status_code == 201, r.text
    oid = r.json()["id"]

    async def assign(token, who):
        return await _call("POST", f"/courses/offerings/{oid}/faculty", token, params={"faculty_id": str(U[who]), "role": "secondary"})

    for who in ("UF", "UD", "S_B", "UE", "HOD_B"):
        r = await assign(TOK["HOD_B"], who)
        assert r.status_code == 403, f"{who} must not be assignable to a department-B offering: {r.status_code} {r.text}"
    assert (await assign(TOK["HOD_A"], "UB")).status_code == 403, "HOD@A cannot assign into a department-B offering at all"
    assert (await assign(S["super"], "UF")).status_code == 403, "Super Admin is bound by the eligibility rule too"
    assert (await assign(TOK["HOD_B"], "UB")).status_code == 200, "HOD@B+FACULTY@B is assignable"
    assert (await assign(TOK["HOD_B"], "UC")).status_code == 200, "HOD@A+FACULTY@B is assignable to B"
    # removal keeps its (unchanged) department authorization
    r = await _call("DELETE", f"/courses/offerings/{oid}/faculty/{U['UB']}", TOK["HOD_A"])
    assert r.status_code == 403, "HOD@A cannot remove faculty from a department-B offering"
    r = await _call("DELETE", f"/courses/offerings/{oid}/faculty/{U['UB']}", TOK["HOD_B"])
    assert r.status_code == 204, r.text


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "USER MGMT: every user once; real, paired assignments; global role has no department; legacy fields intact; no session implied": t_user_management_shows_real_paired_assignments,
            "USER MGMT: role filter matches ANY assignment (Faculty, HOD, Student, DPGS), no duplicates, bad role -> 400": t_role_filter_matches_any_assignment,
            "USER MGMT: department filter is assignment-based (students by their own department)": t_department_filter_uses_assignments,
            "FACULTY PAGE: HOD B sees FAC@A+B, HOD@B+FAC@B, HOD@A+FAC@B; not HOD@B-only, FAC@A-only, inactive, students; HOD A the mirror image": t_hod_faculty_visibility_matrix,
            "FACULTY PAGE: a client-supplied department_id cannot widen a HOD's scope; student scoping unchanged": t_hod_scope_cannot_be_overridden_by_client,
            "FACULTY PAGE: a HOD sees only assignments in their own department": t_hod_sees_only_assignments_in_own_department,
            "FACULTY PAGE: a HOD@A+HOD@B user follows the ACTIVE assignment after switching": t_multi_department_hod_uses_active_assignment,
            "QUERY QUALITY: listing users issues a constant number of queries (no N+1)": t_no_n_plus_one_queries,
            "LEGACY: adding assignments never rewrote User.role / User.department_id": t_legacy_fields_never_rewritten,
            "OFFERING create: FACULTY@dept required (FAC@A+B ok, HOD@B+FAC@B ok, FAC@A/HOD-only/student/inactive/self-HOD -> 403), both directions, Super Admin bound too": t_offering_create_eligibility_matrix,
            "OFFERING assign/remove faculty: same eligibility on POST .../faculty; removal authorization unchanged": t_offering_assign_and_remove_faculty,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_EMAIL_PREFIX}%")))).scalars().all(),
                "courses": (await db.execute(select(Course.id).where(Course.course_number.like(f"{_COURSE_PREFIX}%")))).scalars().all(),
                "sessions": (await db.execute(select(RefreshToken.id).where(RefreshToken.device_info == "ZZTEST-multirole-scope"))).scalars().all(),
            }
            orphans = (await db.execute(select(UserRoleAssignment.id).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalars().all()
        RESULTS.record("cleanup: no ZZTEST_MULTIROLE user / course / session / assignment remains", not any(left.values()) and not orphans, str(left))
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
