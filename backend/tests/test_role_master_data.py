"""Standalone HTTP/DB-level tests for `ams_roles` <-> `UserRole` consistency
(role audit task). This is a READ-ONLY test file — it creates no ZZTEST
fixtures of its own, because the property under test ("every application
role has a matching DB role record") is a static property of the migrated
schema, not something a test needs to construct.

Verifies:
  * every `UserRole` enum member has a corresponding `ams_roles` row whose
    `code` equals the member's NAME (the confirmed lookup convention used by
    `_role_dict()` in departments.py)
  * `ams_roles.code` has no duplicates
  * `GET /admin/roles` (the real endpoint, via a real Super Admin session)
    surfaces all 10 roles with a numeric `user_count`
  * no `ams_user_role_assignments` or `ams_users` row references a role
    value that isn't a real `UserRole` member (sanity check — SQLAlchemy's
    `SAEnum` already makes this structurally impossible, but this asserts it
    directly against the live DB rather than assuming it)

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_role_master_data
"""
import asyncio
import sys

import httpx
from sqlalchemy import select, text

from app.db.base import AsyncSessionLocal
from app.main import app
from app.core.security import create_access_token
from app.models.user import User, UserRole, RefreshToken, Role


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
S: dict = {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _call(method: str, url: str, token: str | None = None, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with _client() as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _setup():
    async with AsyncSessionLocal() as db:
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        rt = RefreshToken(user_id=admin_id, token_hash="zztest_role_master_data_" + admin_id.hex[:16], is_revoked=False)
        db.add(rt)
        await db.commit()
        S["admin_rt"] = rt.id
    S["super"] = create_access_token(str(admin_id), {"sid": str(S["admin_rt"])})


async def _teardown():
    async with AsyncSessionLocal() as db:
        rt = await db.get(RefreshToken, S["admin_rt"])
        if rt:
            await db.delete(rt)
            await db.commit()


async def t_every_userrole_has_ams_roles_row():
    async with AsyncSessionLocal() as db:
        codes = set((await db.execute(select(Role.code))).scalars().all())
    missing = [m.name for m in UserRole if m.name not in codes]
    RESULTS.record("every UserRole member has a matching ams_roles row", not missing, f"missing: {missing}")


async def t_no_duplicate_role_codes():
    async with AsyncSessionLocal() as db:
        dupes = (await db.execute(text(
            "SELECT code FROM ams_roles GROUP BY code HAVING count(*) > 1"
        ))).scalars().all()
    RESULTS.record("no duplicate ams_roles.code values", not dupes, f"duplicates: {dupes}")


async def t_all_roles_are_system_and_active():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Role.code, Role.is_system, Role.is_active).where(Role.code.in_([m.name for m in UserRole]))
        )).all()
    bad = [r.code for r in rows if not (r.is_system and r.is_active)]
    RESULTS.record("every UserRole-backed ams_roles row is is_system=True/is_active=True", not bad, f"bad rows: {bad}")


async def t_admin_roles_endpoint_lists_all_ten():
    resp = await _call("GET", "/admin/roles", S["super"])
    ok = resp.status_code == 200
    codes = {r["code"] for r in resp.json()} if ok else set()
    expected = {m.name for m in UserRole}
    RESULTS.record(
        "GET /admin/roles returns all 10 application roles",
        ok and expected.issubset(codes),
        f"status={resp.status_code} missing={expected - codes}",
    )


async def t_admin_roles_endpoint_reports_user_counts():
    resp = await _call("GET", "/admin/roles", S["super"])
    by_code = {r["code"]: r["user_count"] for r in resp.json()}
    async with AsyncSessionLocal() as db:
        # Raw SQL against the ams_user_role enum column returns the enum LABEL
        # (the UserRole member NAME, e.g. 'FACULTY'), not its lowercase `.value`
        # — matches Role.code's own convention directly, no enum lookup needed.
        real_counts = dict((await db.execute(
            text("SELECT role::text, count(*) FROM ams_users WHERE is_active = TRUE GROUP BY role")
        )).all())
    mismatches = [
        code for code, count in by_code.items()
        if code in {m.name for m in UserRole} and count != real_counts.get(code, 0)
    ]
    RESULTS.record("GET /admin/roles user_count matches live ams_users counts", not mismatches, f"mismatches: {mismatches}")


async def t_no_orphan_role_values_in_assignments():
    """Sanity check: every role value actually stored in ams_users /
    ams_user_role_assignments is a real UserRole member value. SQLAlchemy's
    SAEnum column type makes this structurally guaranteed, but this asserts
    it directly against the live data rather than trusting that alone."""
    valid_values = {m.value.upper() for m in UserRole}
    async with AsyncSessionLocal() as db:
        user_roles = set((await db.execute(text("SELECT DISTINCT role::text FROM ams_users"))).scalars().all())
        assignment_roles = set((await db.execute(text("SELECT DISTINCT role::text FROM ams_user_role_assignments"))).scalars().all())
    bad = (user_roles | assignment_roles) - valid_values
    RESULTS.record("no orphan role values in ams_users/ams_user_role_assignments", not bad, f"orphans: {bad}")


async def t_cleanup_created_no_rows():
    async with AsyncSessionLocal() as db:
        leftover = (await db.execute(text(
            "SELECT count(*) FROM ams_refresh_tokens WHERE token_hash LIKE 'zztest_role_master_data_%'"
        ))).scalar_one()
    RESULTS.record("cleanup: no zztest_role_master_data_ refresh token remains", leftover == 0, f"leftover={leftover}")


async def main():
    await _setup()
    try:
        for name, fn in [
            ("every_userrole_has_ams_roles_row", t_every_userrole_has_ams_roles_row),
            ("no_duplicate_role_codes", t_no_duplicate_role_codes),
            ("all_roles_are_system_and_active", t_all_roles_are_system_and_active),
            ("admin_roles_endpoint_lists_all_ten", t_admin_roles_endpoint_lists_all_ten),
            ("admin_roles_endpoint_reports_user_counts", t_admin_roles_endpoint_reports_user_counts),
            ("no_orphan_role_values_in_assignments", t_no_orphan_role_values_in_assignments),
        ]:
            await fn()
    finally:
        await _teardown()
    await t_cleanup_created_no_rows()

    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        for name, detail in RESULTS.failed:
            print(f"  FAILED: {name} — {detail}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
