"""Standalone HTTP-level tests confirming Super Admin's existing administrative
password reset (`POST /auth/users/{user_id}/reset-password`) correctly covers
Student accounts — and ONLY Super Admin, never HOD/Faculty/Student (including
a student resetting their own password through this admin endpoint).

Root cause (Issue 2): the endpoint itself was already role-agnostic — it is
gated to SUPER_ADMIN only and targets whatever `user_id` is given, with no
role-based exclusion of students. The gap was purely a missing frontend entry
point: the Super Admin Students page (`/students`) had an Edit button but no
Reset Password button, unlike the staff Users page. This suite exercises the
SAME backend mechanism Issue 2 requires reuse of (no second/parallel
implementation) directly against a Student target.

Modelled on `tests/test_student_management.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `_Results` pass/fail tracker, a
`zztest_pwreset_...` prefix on every created row, deleted in `finally`.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_student_password_reset
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import delete, select

from app.core.security import create_access_token, verify_password
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.user import Department, RefreshToken, User, UserRole

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_pwreset_"
_DEVICE = "ZZTEST-pwreset"
_ORIGINAL_HASH = "zztest-not-a-real-hash"


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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with _client() as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _mk_user(name: str, role: UserRole, dept) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{name.lower()}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=name, role=role,
            department_id=dept.id if dept else None, is_active=True, is_verified=True,
            hashed_password=_ORIGINAL_HASH, must_change_password=False,
        )
        db.add(u)
        await db.flush()
        uid = u.id
        await db.commit()
    U[name] = uid
    return uid


async def _session(name: str) -> str:
    rt = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(id=rt, user_id=U[name], token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    TOK[name] = create_access_token(str(U[name]), {"sid": str(rt)})
    return TOK[name]


async def _db_user(name: str) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == U[name]))).scalar_one()


async def _reset(actor_token, target_name: str, new_password="ZZTestNewPw123", confirm_password=None):
    body = {"new_password": new_password, "confirm_password": confirm_password if confirm_password is not None else new_password}
    return await _call("POST", f"/auth/users/{U[target_name]}/reset-password", actor_token, json=body)


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        d1 = (await db.execute(select(Department).order_by(Department.code).limit(1))).scalar_one()
        S["D1"] = d1
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    rt = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    S["super"] = create_access_token(str(admin_id), {"sid": str(rt)})

    await _mk_user("stu1", UserRole.STUDENT, S["D1"])
    await _mk_user("stu2", UserRole.STUDENT, S["D1"])
    await _mk_user("hod", UserRole.HOD, S["D1"])
    await _mk_user("fac", UserRole.FACULTY, S["D1"])
    for n in ("stu1", "stu2", "hod", "fac"):
        await _session(n)


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


# ── 1. Super Admin can reset a Student's password via the SAME mechanism ───

async def t_super_admin_resets_student_password():
    r = await _reset(S["super"], "stu1", new_password="ZZTestBrandNew123")
    assert r.status_code == 200, r.text
    u = await _db_user("stu1")
    assert u.hashed_password != _ORIGINAL_HASH, "the hash must actually change"
    assert verify_password("ZZTestBrandNew123", u.hashed_password), "the NEW password must verify against the stored hash"
    assert not verify_password("anything-else", u.hashed_password)
    assert u.must_change_password is True, "forces the student to pick their own password on next login, same as every other role"
    # The plaintext password is never returned in the response body.
    assert "ZZTestBrandNew123" not in r.text and "password" not in r.json() and "new_password" not in r.json()
    # The student can immediately authenticate with the NEW password via the real login flow.
    login = await _call("POST", "/auth/login", None, json={"email": f"{_PFX}stu1_{_TAG}@avfu.ac.in", "password": "ZZTestBrandNew123"})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["must_change_password"] is True


async def t_reset_rejects_short_or_mismatched_password_and_changes_nothing():
    before = (await _db_user("stu2")).hashed_password
    for body, why in (
        ({"new_password": "short1", "confirm_password": "short1"}, "too short"),
        ({"new_password": "LongEnough123", "confirm_password": "Different123"}, "mismatch"),
    ):
        r = await _call("POST", f"/auth/users/{U['stu2']}/reset-password", S["super"], json=body)
        assert r.status_code == 422, f"{why}: {r.status_code} {r.text}"
    assert (await _db_user("stu2")).hashed_password == before, "a rejected reset must not change the stored hash"


async def t_reset_target_is_404_for_unknown_user_and_changes_nothing():
    before = (await _db_user("stu2")).hashed_password
    r = await _reset(S["super"], "stu2")
    assert r.status_code == 200, r.text
    after_valid = (await _db_user("stu2")).hashed_password
    assert after_valid != before
    r = await _call("POST", f"/auth/users/{uuid.uuid4()}/reset-password", S["super"], json={"new_password": "ZZTestXX123", "confirm_password": "ZZTestXX123"})
    assert r.status_code == 404, r.text


# ── 2. Authorization matrix: Super Admin only ───────────────────────────────

async def t_hod_cannot_reset_student_password():
    before = (await _db_user("stu1")).hashed_password
    r = await _reset(TOK["hod"], "stu1")
    assert r.status_code == 403, r.text
    assert (await _db_user("stu1")).hashed_password == before, "a rejected HOD attempt must not change the hash"


async def t_faculty_cannot_reset_student_password():
    before = (await _db_user("stu1")).hashed_password
    r = await _reset(TOK["fac"], "stu1")
    assert r.status_code == 403, r.text
    assert (await _db_user("stu1")).hashed_password == before


async def t_student_cannot_reset_another_students_password():
    before = (await _db_user("stu1")).hashed_password
    r = await _reset(TOK["stu2"], "stu1")
    assert r.status_code == 403, r.text
    assert (await _db_user("stu1")).hashed_password == before


async def t_student_cannot_reset_own_password_via_admin_endpoint():
    """A student's OWN password change must go through /auth/change-password
    (session-authorized, no target id), never through the Super-Admin-only
    admin-reset endpoint merely by naming themselves as the target."""
    before = (await _db_user("stu2")).hashed_password
    r = await _reset(TOK["stu2"], "stu2")
    assert r.status_code == 403, r.text
    assert (await _db_user("stu2")).hashed_password == before
    # The legitimate self-service path remains unaffected by this restriction.
    r = await _call("POST", "/auth/change-password", TOK["stu2"], json={"new_password": "ZZTestSelf123", "confirm_password": "ZZTestSelf123"})
    assert r.status_code == 200, r.text
    assert verify_password("ZZTestSelf123", (await _db_user("stu2")).hashed_password)


async def t_unauthenticated_cannot_reset_password():
    before = (await _db_user("stu1")).hashed_password
    r = await _reset(None, "stu1")
    assert r.status_code in (401, 403), r.text
    assert (await _db_user("stu1")).hashed_password == before


async def t_super_admin_can_also_still_reset_staff_password_unaffected():
    """Regression: extending coverage to Students must not have disturbed the
    pre-existing staff (HOD/Faculty) reset path."""
    r = await _reset(S["super"], "hod", new_password="ZZTestHodNew123")
    assert r.status_code == 200, r.text
    assert verify_password("ZZTestHodNew123", (await _db_user("hod")).hashed_password)


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "RESET: Super Admin resets a Student's password via the existing /auth/users/{id}/reset-password mechanism; hash changes, must_change_password set, student can log in with the new password, plaintext never returned": t_super_admin_resets_student_password,
            "RESET: short password / mismatched confirmation -> 422, hash unchanged": t_reset_rejects_short_or_mismatched_password_and_changes_nothing,
            "RESET: unknown target id -> 404, no other user's hash is affected": t_reset_target_is_404_for_unknown_user_and_changes_nothing,
            "SECURITY: HOD cannot reset a Student's password": t_hod_cannot_reset_student_password,
            "SECURITY: Faculty cannot reset a Student's password": t_faculty_cannot_reset_student_password,
            "SECURITY: a Student cannot reset ANOTHER Student's password": t_student_cannot_reset_another_students_password,
            "SECURITY: a Student cannot reset their OWN password via the admin endpoint (must use /auth/change-password)": t_student_cannot_reset_own_password_via_admin_endpoint,
            "SECURITY: unauthenticated request -> 401": t_unauthenticated_cannot_reset_password,
            "REGRESSION: Super Admin can still reset a staff (HOD) password through the same endpoint": t_super_admin_can_also_still_reset_staff_password_unaffected,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_PFX}%")))).scalars().all(),
                "sessions": (await db.execute(select(RefreshToken.id).where(RefreshToken.device_info == _DEVICE))).scalars().all(),
            }
        RESULTS.record("cleanup: no ZZTEST_PWRESET user / session remains", not any(left.values()), str(left))
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
