"""Standalone HTTP-level tests for `GET /research/committees/eligible-students`
(the Advisory Committee "Propose Major Advisor" student picker fix).

Modelled directly on `tests/test_research_course.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions via `create_access_token`/
`RefreshToken` rows, a `record(name, ok, detail)` pass/fail tracker, a
`_run(name, fn)` wrapper, and a `_setup()`/`_teardown()` pair using a unique
`_TAG = uuid.uuid4().hex[:6]` and `zztest_acs_...` prefix on every created
row so cleanup is unambiguous.

Root cause under test: `GET /auth/users` deliberately excludes every student
account (`staff_user_clause()`), so the Advisory Committee frontend's old
`/auth/users`-based student dropdown was always empty. The fix is a new,
narrow, department-scoped endpoint — this file verifies it is scoped to the
AUTHENTICATED HOD's own department (never a client-supplied value), that a
`department_id` query-param manipulation attempt has no effect (the endpoint
accepts no such parameter at all), and that non-HOD/non-admin roles remain
blocked exactly as every other department-scoped endpoint in this app.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_advisory_committee_students
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import Department, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_acs_"
_DEVICE = "ZZTEST-acs"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120) as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _session(user_id, assignment_id=None) -> str:
    async with AsyncSessionLocal() as db:
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=user_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(),
                             device_info=_DEVICE, active_role_assignment_id=assignment_id))
        await db.commit()
    return create_access_token(str(user_id), {"sid": str(rt)})


async def _mk_user(key, role, dept):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"ACS{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_acs_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        await db.commit()
    d1, d2 = S["D1"], S["D2"]

    await _mk_user("hod_a", UserRole.HOD, d1)
    await _mk_user("hod_b", UserRole.HOD, d2)
    await _mk_user("faculty_a", UserRole.FACULTY, d1)
    await _mk_user("sa", UserRole.SUPER_ADMIN, None)
    await _mk_user("sx_a1", UserRole.STUDENT, d1)
    await _mk_user("sx_a2", UserRole.STUDENT, d1)
    await _mk_user("sx_b1", UserRole.STUDENT, d2)
    await _mk_user("sx_a_inactive", UserRole.STUDENT, d1)
    async with AsyncSessionLocal() as db:
        u = await db.get(User, U["sx_a_inactive"])
        u.is_active = False
        await db.commit()

    for k in ("hod_a", "hod_b", "faculty_a", "sa"):
        TOK[k] = await _session(U[k])
    # A plain, unauthenticated STUDENT session too, to test the STUDENT role is blocked.
    TOK["sx_a1"] = await _session(U["sx_a1"])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.commit()


# ── tests ────────────────────────────────────────────────────────────────────

async def t_hod_a_sees_own_department_students():
    r = await _call("GET", "/research/committees/eligible-students", TOK["hod_a"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(U["sx_a1"]) in ids and str(U["sx_a2"]) in ids, ids
    assert str(U["sx_a_inactive"]) not in ids, "inactive students must never be listed"


async def t_hod_a_never_sees_department_b_students():
    r = await _call("GET", "/research/committees/eligible-students", TOK["hod_a"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(U["sx_b1"]) not in ids, ids


async def t_hod_a_department_id_query_param_has_no_effect():
    """The endpoint accepts no department_id parameter at all — HOD A
    attempting to pass Department B's id must not gain Department B's
    students, and must not error out either; it is simply ignored, HOD A's
    own department remains authoritative."""
    r = await _call("GET", "/research/committees/eligible-students", TOK["hod_a"], params={"department_id": str(S["D2"].id)})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(U["sx_b1"]) not in ids, "a client-supplied department_id must never override the authenticated HOD's own department"
    assert str(U["sx_a1"]) in ids, "HOD A's own students must still be returned despite the manipulated query param"


async def t_hod_b_sees_only_department_b_students():
    r = await _call("GET", "/research/committees/eligible-students", TOK["hod_b"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert ids == {str(U["sx_b1"])}, ids


async def t_faculty_role_blocked():
    r = await _call("GET", "/research/committees/eligible-students", TOK["faculty_a"])
    assert r.status_code == 403, r.text


async def t_student_role_blocked():
    r = await _call("GET", "/research/committees/eligible-students", TOK["sx_a1"])
    assert r.status_code == 403, r.text


async def t_super_admin_sees_all_departments_unscoped():
    r = await _call("GET", "/research/committees/eligible-students", TOK["sa"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(U["sx_a1"]) in ids and str(U["sx_b1"]) in ids, ids
    assert str(U["sx_a_inactive"]) not in ids


async def t_response_shape_minimal():
    r = await _call("GET", "/research/committees/eligible-students", TOK["hod_a"])
    row = next(row for row in r.json() if row["id"] == str(U["sx_a1"]))
    assert set(row.keys()) == {"id", "full_name", "department_id"}, row.keys()
    assert row["department_id"] == str(S["D1"].id)


async def t_auth_users_still_excludes_students():
    """Regression guard: this fix must not have touched /auth/users's
    existing staff-only behavior."""
    r = await _call("GET", "/auth/users", TOK["hod_a"])
    assert r.status_code == 200, r.text
    assert all(row["role"] != "student" for row in r.json()), "/auth/users must still exclude every student account"


async def t_students_endpoint_authorization_unchanged():
    """Regression guard: GET /students must still be Super-Admin/VC-only —
    this fix must not have broadened it to HOD."""
    r = await _call("GET", "/students", TOK["hod_a"])
    assert r.status_code == 403, r.text


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "HOD A sees their own department's active students (inactive excluded)": t_hod_a_sees_own_department_students,
            "HOD A never sees Department B's students": t_hod_a_never_sees_department_b_students,
            "HOD A + department_id=B query param manipulation has no effect": t_hod_a_department_id_query_param_has_no_effect,
            "HOD B sees only Department B's students": t_hod_b_sees_only_department_b_students,
            "FACULTY role is blocked (403)": t_faculty_role_blocked,
            "STUDENT role is blocked (403)": t_student_role_blocked,
            "SUPER_ADMIN sees students across all departments, unscoped": t_super_admin_sees_all_departments_unscoped,
            "Response shape is minimal (id/full_name/department_id only)": t_response_shape_minimal,
            "Regression: GET /auth/users still excludes all students": t_auth_users_still_excludes_students,
            "Regression: GET /students authorization unchanged (still blocks HOD)": t_students_endpoint_authorization_unchanged,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_ACS user / session row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
