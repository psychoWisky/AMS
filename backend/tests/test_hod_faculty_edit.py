"""Standalone HTTP-level tests for:

  1. HOD: Edit Faculty (`PATCH /auth/faculty/{user_id}`, new) — an HOD may now
     edit the full staff profile of a Faculty member in their own ACTIVE
     department (same field set Super Admin edits via PATCH /auth/users/{id},
     minus role/department_id/is_active/abc_id), scoped to an actual FACULTY
     UserRoleAssignment in that exact department — never another department,
     never a non-Faculty target, never role/department/status.
  2. ABC ID restriction (`PATCH /auth/me`) — ABC ID is a student-only,
     student-viewable-only identifier: a non-student caller sending it on
     themselves via the shared self-service endpoint is rejected (403); a
     student caller still may set it.

Modelled on `tests/test_student_management.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `_Results` pass/fail tracker, a
`zztest_hfe_...` prefix on every created row, deleted in `finally`.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_hod_faculty_edit
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import delete, select

from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.user import Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_hfe_"
_DEVICE = "ZZTEST-hfe"


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _mk_user(name: str, role: UserRole, dept) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{name.lower()}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=name, role=role,
            department_id=dept.id if dept else None, is_active=True, is_verified=True,
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
    assert r.status_code == 201, f"grant {name} {role}: {r.status_code} {r.text}"


async def _session(name: str, active_assignment_id=None) -> str:
    rt = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(
            id=rt, user_id=U[name], token_hash=hashlib.sha256(str(rt).encode()).hexdigest(),
            device_info=_DEVICE, active_role_assignment_id=active_assignment_id,
        ))
        await db.commit()
    TOK[name] = create_access_token(str(U[name]), {"sid": str(rt)})
    return TOK[name]


async def _db_user(name: str) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == U[name]))).scalar_one()


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        d1, d2 = (await db.execute(select(Department).order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = d1, d2
        # A Programme with NO department links at all (mirrors
        # test_student_management.py's own "P_BAD" fixture) — used to exercise
        # update_faculty's program/department pair validation (400).
        S["P_BAD"] = (await db.execute(select(Program).where(Program.code == "MFSc"))).scalar_one()
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    S["super"] = create_access_token(str(admin_id), {"sid": str(rt)})

    # HOD1 (department D1), HOD2 (department D2); FAC1 is Faculty in D1 only;
    # FAC_MULTI is Faculty in D1 AND D2 (to confirm per-department scoping);
    # STU is a Student (never an editable target here, even in D1).
    # DEPT_MATE holds HOD in D1 (same department as hod1) but NO Faculty
    # assignment anywhere — tests that department membership via a
    # DIFFERENT role is not sufficient.
    # MULTI holds BOTH Faculty@D1 and Student — tests that the ABC ID
    # restriction follows the SESSION's active role, not a stale/legacy
    # User.role value or "holds the role somewhere" fact.
    await _mk_user("hod1", UserRole.HOD, S["D1"])
    await _mk_user("hod2", UserRole.HOD, S["D2"])
    await _mk_user("fac1", UserRole.FACULTY, S["D1"])
    await _mk_user("fac_multi", UserRole.FACULTY, S["D1"])
    await _mk_user("stu", UserRole.STUDENT, S["D1"])
    await _mk_user("dept_mate", UserRole.HOD, S["D1"])
    await _mk_user("multi", UserRole.FACULTY, S["D1"])
    await _grant("hod1", "hod", S["D1"].id)
    await _grant("hod2", "hod", S["D2"].id)
    await _grant("fac1", "faculty", S["D1"].id)
    await _grant("fac_multi", "faculty", S["D1"].id)
    await _grant("fac_multi", "faculty", S["D2"].id)
    await _grant("stu", "student")
    await _grant("dept_mate", "hod", S["D1"].id)
    await _grant("multi", "faculty", S["D1"].id)
    await _grant("multi", "student")

    # Sessions with an explicit active assignment, so `user.active_department_id`
    # is deterministic (never left to pick_default_assignment's own tie-break).
    async def _assignment_id(name: str, role: str, dept) -> uuid.UUID:
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(UserRoleAssignment.id).where(
                UserRoleAssignment.user_id == U[name], UserRoleAssignment.role == UserRole(role),
                UserRoleAssignment.department_id == (dept.id if dept else None),
            ))).scalar_one()

    await _session("hod1", await _assignment_id("hod1", "hod", S["D1"]))
    await _session("hod2", await _assignment_id("hod2", "hod", S["D2"]))
    await _session("fac1", await _assignment_id("fac1", "faculty", S["D1"]))
    await _session("stu", await _assignment_id("stu", "student", None))
    # Two SEPARATE sessions for the SAME "multi" account, one active as
    # Faculty and one active as Student — this is the whole point of the
    # active-role test below.
    TOK["multi_as_faculty"] = await _session("multi", await _assignment_id("multi", "faculty", S["D1"]))
    TOK["multi_as_student"] = await _session("multi", await _assignment_id("multi", "student", None))


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        ids = list(U.values())
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(ids)))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


_EDIT_BODY = {
    "first_name": "Edited", "middle_name": "Mid", "last_name": "Faculty", "designation": "Professor",
    "mobile": "9123456789", "title": "Dr.", "employee_id": f"ZZTEST-EMP-{_TAG}", "date_of_birth": "1985-03-04",
    "gender": "male", "blood_group": "b+", "father_name": "ZZTEST Father", "address": "ZZTEST address",
}


# ── 1. HOD can edit a Faculty member in their OWN department ───────────────

async def t_hod_edits_own_department_faculty():
    r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK["hod1"], json=_EDIT_BODY)
    assert r.status_code == 200, r.text
    d = r.json()
    exp = {**_EDIT_BODY, "gender": "Male", "blood_group": "B+"}
    for k, v in exp.items():
        assert d[k] == v, f"{k}: {d[k]!r} != {v!r}"
    assert "abc_id" not in d or d["abc_id"] is None, "abc_id must never surface/be settable here"


async def t_hod_cannot_edit_another_departments_faculty():
    before = (await _db_user("fac1")).first_name
    r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK["hod2"], json={"first_name": "Hacked"})
    assert r.status_code == 403, r.text
    assert (await _db_user("fac1")).first_name == before


async def t_hod_cannot_edit_a_student_or_another_hod():
    for target, actor in (("stu", "hod1"), ("hod2", "hod1")):
        r = await _call("PATCH", f"/auth/faculty/{U[target]}", TOK[actor], json={"first_name": "Hacked"})
        assert r.status_code == 403, f"{actor} -> {target}: {r.status_code} {r.text}"


async def t_hod_scoped_correctly_for_multi_department_faculty():
    """fac_multi holds FACULTY in BOTH D1 and D2 — hod1 (active dept D1) may
    edit them; hod2 (active dept D2) may ALSO edit them (same person, two
    legitimate department memberships) — but neither can affect the other
    department's standing, and the edit itself is a plain profile field."""
    r = await _call("PATCH", f"/auth/faculty/{U['fac_multi']}", TOK["hod1"], json={"first_name": "ViaHod1"})
    assert r.status_code == 200, r.text
    assert (await _db_user("fac_multi")).first_name == "ViaHod1"
    r = await _call("PATCH", f"/auth/faculty/{U['fac_multi']}", TOK["hod2"], json={"first_name": "ViaHod2"})
    assert r.status_code == 200, r.text
    assert (await _db_user("fac_multi")).first_name == "ViaHod2"


async def t_role_department_is_active_abc_id_are_not_accepted():
    """role/department_id/is_active/abc_id are not fields on this schema at
    all — sending them must be silently ignored (never a 422, never applied),
    unlike Super Admin's own PATCH /auth/users/{id}."""
    before = await _db_user("fac1")
    r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK["hod1"], json={
        "role": "hod", "department_id": str(S["D2"].id), "is_active": False, "abc_id": "ABC-SHOULD-NOT-APPLY",
        "mobile": "9000000000",
    })
    assert r.status_code == 200, r.text
    after = await _db_user("fac1")
    assert after.role == before.role and after.department_id == before.department_id and after.is_active is True
    assert after.abc_id is None
    assert after.mobile == "9000000000", "a legitimate field in the same request must still apply"


async def t_faculty_student_unauthenticated_cannot_call_endpoint():
    for who in ("fac1", "stu", None):
        r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK.get(who) if who else None, json={"first_name": "Hacked"})
        assert r.status_code in ((403,) if who else (401, 403)), f"{who}: {r.status_code}"


async def t_email_uniqueness_and_unknown_target():
    dupe = (await _db_user("hod1")).email
    r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK["hod1"], json={"email": dupe})
    assert r.status_code == 409, r.text
    r = await _call("PATCH", f"/auth/faculty/{uuid.uuid4()}", TOK["hod1"], json={"first_name": "X"})
    assert r.status_code == 404, r.text


async def t_hod_cannot_edit_non_faculty_same_department_via_other_role():
    """dept_mate holds HOD in D1 — the SAME department as hod1 — but no
    Faculty assignment anywhere. Department membership via a different role
    must not be sufficient; only an actual FACULTY assignment counts."""
    before = (await _db_user("dept_mate")).first_name
    r = await _call("PATCH", f"/auth/faculty/{U['dept_mate']}", TOK["hod1"], json={"first_name": "Hacked"})
    assert r.status_code == 403, r.text
    assert (await _db_user("dept_mate")).first_name == before


async def t_hod_faculty_edit_invalid_college_and_program_id():
    """Invalid/non-existent college_id and a program/department pair that
    doesn't exist must be rejected exactly as Super Admin's own PATCH
    /auth/users/{id} rejects them — nothing changed either way."""
    before = await _db_user("fac1")
    r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK["hod1"], json={"college_id": str(uuid.uuid4())})
    assert r.status_code == 400, r.text
    r = await _call("PATCH", f"/auth/faculty/{U['fac1']}", TOK["hod1"], json={"program_id": str(S["P_BAD"].id)})
    assert r.status_code == 400, r.text
    after = await _db_user("fac1")
    assert after.college_id == before.college_id and after.program_id == before.program_id


# ── 2. ABC ID is student-only, student-viewable-only ───────────────────────

async def t_non_student_cannot_set_abc_id_on_self():
    for who in ("hod1", "fac1"):
        before = (await _db_user(who)).abc_id
        r = await _call("PATCH", "/auth/me", TOK[who], json={"abc_id": "ABC-SHOULD-BE-REJECTED"})
        assert r.status_code == 403, f"{who}: {r.status_code} {r.text}"
        assert (await _db_user(who)).abc_id == before


async def t_student_can_still_set_abc_id_on_self():
    r = await _call("PATCH", "/auth/me", TOK["stu"], json={"abc_id": "ABC-STU-OK"})
    assert r.status_code == 200, r.text
    assert (await _db_user("stu")).abc_id == "ABC-STU-OK"


async def t_non_student_other_fields_via_me_still_work():
    """The abc_id guard must not break the shared endpoint's other fields."""
    r = await _call("PATCH", "/auth/me", TOK["hod1"], json={"father_name": "ZZTEST HOD Father"})
    assert r.status_code == 200, r.text
    assert (await _db_user("hod1")).father_name == "ZZTEST HOD Father"


async def t_abc_id_follows_active_session_role_not_legacy_role():
    """`multi` holds BOTH Faculty@D1 and Student assignments on the SAME
    account (legacy `User.role` is FACULTY, set at creation). The ABC ID
    restriction must follow the SESSION's active role (server-validated,
    from the RefreshToken's active_role_assignment_id), never the legacy
    column and never anything client-supplied:
      - active role STUDENT -> may set/clear their own ABC ID.
      - active role FACULTY (the SAME account, a DIFFERENT session) -> may not,
        even though `User.role` is still literally FACULTY and never changes."""
    legacy_role = (await _db_user("multi")).role
    assert legacy_role == UserRole.FACULTY, "legacy User.role must stay FACULTY — this test is pointless otherwise"

    r = await _call("PATCH", "/auth/me", TOK["multi_as_student"], json={"abc_id": "ABC-MULTI-OK"})
    assert r.status_code == 200, r.text
    assert (await _db_user("multi")).abc_id == "ABC-MULTI-OK"
    assert r.json()["abc_id"] == "ABC-MULTI-OK", "the self-view response must include it when set while acting as Student"

    r = await _call("PATCH", "/auth/me", TOK["multi_as_faculty"], json={"abc_id": "ABC-MULTI-REJECTED"})
    assert r.status_code == 403, r.text
    assert (await _db_user("multi")).abc_id == "ABC-MULTI-OK", "a rejected write while acting as Faculty must not change the stored value"

    # GET /auth/me while acting as Faculty must NOT surface the value either
    # — reading is gated on the SAME active-role check as writing (the
    # account's own data is stored either way; the DB row is unaffected,
    # only the serializer's exposure of it follows the active session role).
    r = await _call("GET", "/auth/me", TOK["multi_as_faculty"])
    assert r.status_code == 200 and r.json()["abc_id"] is None, "ABC ID must not be exposed while the session's active role is Faculty"
    # Switching back to the Student session, the SAME value is visible again.
    r = await _call("GET", "/auth/me", TOK["multi_as_student"])
    assert r.status_code == 200 and r.json()["abc_id"] == "ABC-MULTI-OK"
    assert (await _db_user("multi")).role == UserRole.FACULTY, "the legacy role column is never touched by any of this"


async def t_abc_id_cross_user_blocked_even_for_multirole_account():
    """Another student can never read or write `multi`'s ABC ID, and `multi`
    cannot read or write another student's, through any endpoint."""
    r = await _call("PATCH", "/auth/me", TOK["stu"], json={"abc_id": "ABC-STU-ISOLATED"})
    assert r.status_code == 200, r.text
    assert (await _db_user("stu")).abc_id == "ABC-STU-ISOLATED"
    assert (await _db_user("multi")).abc_id != "ABC-STU-ISOLATED"
    # No endpoint accepts a target user id and returns/accepts abc_id for
    # anyone other than the caller themselves — confirmed structurally by
    # the serializer tests below (GET /auth/users never carries it) and by
    # PATCH /auth/faculty/{id} never accepting it (t_role_department_is_active_abc_id_are_not_accepted).


async def t_staff_directory_never_exposes_abc_id_even_if_present_in_db():
    """Defense-in-depth: even if a staff row somehow HAD a non-null abc_id
    (e.g. legacy data predating this restriction, set here directly at the
    DB level, bypassing every API write-path on purpose), GET /auth/users
    must still never surface it — the serializer itself must suppress it,
    not merely rely on "no writer ever sets it for staff"."""
    async with AsyncSessionLocal() as db:
        u = await db.get(User, U["fac1"])
        u.abc_id = "ABC-LEGACY-STAFF-DATA"
        await db.commit()
    try:
        r = await _call("GET", "/auth/users", TOK["hod1"], params={"role": "faculty"})
        assert r.status_code == 200, r.text
        row = next(x for x in r.json() if x["id"] == str(U["fac1"]))
        assert "abc_id" not in row or row["abc_id"] is None, "staff directory must never expose ABC ID, regardless of underlying data"
        r = await _call("GET", "/auth/users", S["super"])
        row = next(x for x in r.json() if x["id"] == str(U["fac1"]))
        assert "abc_id" not in row or row["abc_id"] is None, "even Super Admin's own staff listing must not expose it"
    finally:
        async with AsyncSessionLocal() as db:
            u = await db.get(User, U["fac1"])
            u.abc_id = None
            await db.commit()


async def t_non_student_self_view_never_shows_abc_id_even_if_present_in_db():
    """Same defense-in-depth, for the SELF-view path: a non-student's OWN
    GET /auth/me must not surface a legacy/anomalous abc_id value either —
    the field is meaningless for a non-student account."""
    async with AsyncSessionLocal() as db:
        u = await db.get(User, U["hod1"])
        u.abc_id = "ABC-LEGACY-HOD-DATA"
        await db.commit()
    try:
        r = await _call("GET", "/auth/me", TOK["hod1"])
        assert r.status_code == 200, r.text
        assert r.json().get("abc_id") is None, "a non-student's own profile must never surface an ABC ID value"
    finally:
        async with AsyncSessionLocal() as db:
            u = await db.get(User, U["hod1"])
            u.abc_id = None
            await db.commit()


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "HOD FACULTY EDIT: HOD edits a Faculty member's full profile in their own department": t_hod_edits_own_department_faculty,
            "HOD FACULTY EDIT: HOD cannot edit another department's faculty": t_hod_cannot_edit_another_departments_faculty,
            "HOD FACULTY EDIT: HOD cannot edit a Student or another HOD, even in their own department": t_hod_cannot_edit_a_student_or_another_hod,
            "HOD FACULTY EDIT: a multi-department Faculty is editable by either department's HOD": t_hod_scoped_correctly_for_multi_department_faculty,
            "HOD FACULTY EDIT: role/department_id/is_active/abc_id are silently ignored, not applied": t_role_department_is_active_abc_id_are_not_accepted,
            "HOD FACULTY EDIT: blocked for Faculty/Student/unauthenticated": t_faculty_student_unauthenticated_cannot_call_endpoint,
            "HOD FACULTY EDIT: duplicate email -> 409; unknown target -> 404": t_email_uniqueness_and_unknown_target,
            "HOD FACULTY EDIT: a non-Faculty target in the SAME department (via a different role) is still rejected": t_hod_cannot_edit_non_faculty_same_department_via_other_role,
            "HOD FACULTY EDIT: invalid college_id and an unmapped program/department pair -> 400, nothing changed": t_hod_faculty_edit_invalid_college_and_program_id,
            "ABC ID: a non-student (HOD/Faculty) cannot set it on themselves via PATCH /auth/me": t_non_student_cannot_set_abc_id_on_self,
            "ABC ID: a Student can still set their own via PATCH /auth/me": t_student_can_still_set_abc_id_on_self,
            "ABC ID: the guard does not block a non-student's other self-service fields": t_non_student_other_fields_via_me_still_work,
            "ABC ID: a multi-role (Faculty+Student) account's own ABC ID follows the SESSION's active role, never the legacy User.role": t_abc_id_follows_active_session_role_not_legacy_role,
            "ABC ID: cross-user exposure stays blocked for a multi-role account, in both directions": t_abc_id_cross_user_blocked_even_for_multirole_account,
            "ABC ID SERIALIZER: the staff directory (GET /auth/users) never exposes it, even for anomalous/legacy data": t_staff_directory_never_exposes_abc_id_even_if_present_in_db,
            "ABC ID SERIALIZER: a non-student's own self-view (GET /auth/me) never exposes it, even for anomalous/legacy data": t_non_student_self_view_never_shows_abc_id_even_if_present_in_db,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_PFX}%")))).scalars().all(),
                "sessions": (await db.execute(select(RefreshToken.id).where(RefreshToken.device_info == _DEVICE))).scalars().all(),
            }
        RESULTS.record("cleanup: no ZZTEST_HFE user / session remains", not any(left.values()), str(left))
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
