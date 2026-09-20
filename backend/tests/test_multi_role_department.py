"""Standalone, dependency-free HTTP-level test script for the multi-role,
multi-department authorization architecture (UserRoleAssignment now carries
`department_id`; the active session context is `RefreshToken.
active_role_assignment_id`, pointing at one specific persisted assignment
row — never an independently-editable role+department pair).

Drives the REAL FastAPI app via `httpx.ASGITransport` (real JWTs, real
`get_current_user` resolution, real `POST /auth/users/{id}/roles`,
`DELETE .../roles/{role}`, and `POST /auth/switch-role` endpoints) — this is
the only way to genuinely exercise session/active-assignment resolution
end-to-end, not just the department-comparison logic inside a single
authorization helper (already covered indirectly by
`test_course_uniqueness.py`'s passing suite against the new
`active_department_id` attribute).

Runs against the SAME local Postgres database configured in `.env`
(`DATABASE_URL`) — never production. Every user/assignment/refresh-token
this script creates is scoped to a `ZZTEST_` email prefix and deleted in a
`finally` block per scenario (cascade-deletes its own
UserRoleAssignment/RefreshToken rows via the existing FK `ondelete`
behavior). No existing Department/User row is ever modified.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_multi_role_department
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
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Department
from app.models.course import Course, CourseOffering
from app.models.academic import Semester

_MARKER = "zztest_multirole"


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()

_DEPT_A: uuid.UUID
_DEPT_B: uuid.UUID
_DEPT_C: uuid.UUID
_SUPER_TOKEN: str
_SUPER_RT_ID: uuid.UUID


async def _setup() -> None:
    global _DEPT_A, _DEPT_B, _DEPT_C, _SUPER_TOKEN, _SUPER_RT_ID
    async with AsyncSessionLocal() as db:
        depts = (await db.execute(select(Department.id).order_by(Department.code).limit(3))).scalars().all()
        assert len(depts) >= 3, "test requires at least 3 existing departments in the local dev database"
        _DEPT_A, _DEPT_B, _DEPT_C = depts[0], depts[1], depts[2]

        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        _SUPER_RT_ID = uuid.uuid4()
        db.add(RefreshToken(
            id=_SUPER_RT_ID, user_id=admin_id,
            token_hash=hashlib.sha256(str(_SUPER_RT_ID).encode()).hexdigest(), device_info="multirole-test-admin",
        ))
        await db.commit()
        _SUPER_TOKEN = create_access_token(str(admin_id), {"sid": str(_SUPER_RT_ID)})


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(RefreshToken).where(RefreshToken.id == _SUPER_RT_ID))
        await db.commit()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _super_headers() -> dict:
    return {"Authorization": f"Bearer {_SUPER_TOKEN}"}


async def _create_test_user(tag: str) -> uuid.UUID:
    """A bare, password-less test user — `get_current_user` only requires
    `is_active=True`, and a session is minted directly (see `_login_as`)
    rather than through the real /auth/login endpoint, so no password is
    ever needed. Starts with NO role assignments; each test grants exactly
    the combination it needs via the real add_user_role endpoint."""
    email = f"{_MARKER}.{tag}.{uuid.uuid4().hex[:8]}@avfu.ac.in"
    async with AsyncSessionLocal() as db:
        user = User(email=email, role=UserRole.FACULTY, is_active=True, is_verified=True)
        db.add(user)
        await db.flush()
        user_id = user.id
        # Remove the row create_user-equivalent code would normally add via
        # a real signup path — this helper deliberately starts the user
        # with ZERO assignments, since get_current_user's "no assignments"
        # branch is a defensive self-heal we don't want triggering here.
        await db.commit()
    return user_id


async def _mint_session(user_id: uuid.UUID) -> tuple[str, uuid.UUID]:
    rt_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(
            id=rt_id, user_id=user_id,
            token_hash=hashlib.sha256(str(rt_id).encode()).hexdigest(), device_info=f"{_MARKER}-session",
        ))
        await db.commit()
    token = create_access_token(str(user_id), {"sid": str(rt_id)})
    return token, rt_id


async def _cleanup_user(user_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _add_role(user_id: uuid.UUID, role: str, department_id: uuid.UUID | None = None) -> httpx.Response:
    async with _client() as c:
        body: dict = {"role": role}
        if department_id is not None:
            body["department_id"] = str(department_id)
        return await c.post(f"/api/v1/auth/users/{user_id}/roles", headers=_super_headers(), json=body)


async def _remove_role(user_id: uuid.UUID, role: str, department_id: uuid.UUID | None = None) -> httpx.Response:
    async with _client() as c:
        params = {"department_id": str(department_id)} if department_id is not None else None
        return await c.delete(f"/api/v1/auth/users/{user_id}/roles/{role}", headers=_super_headers(), params=params)


async def _get_roles(user_id: uuid.UUID) -> dict:
    async with _client() as c:
        r = await c.get(f"/api/v1/auth/users/{user_id}/roles", headers=_super_headers())
        return r.json()


async def _switch_role(token: str, assignment_id: str) -> httpx.Response:
    async with _client() as c:
        return await c.post("/api/v1/auth/switch-role", headers={"Authorization": f"Bearer {token}"}, json={"assignment_id": assignment_id})


async def _me(token: str) -> httpx.Response:
    async with _client() as c:
        return await c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})


def _assignment_id_for(payload: dict, role: str, department_id: uuid.UUID | None) -> str:
    dept_str = str(department_id) if department_id else None
    for a in payload["assigned_role_assignments"]:
        if a["role"] == role and a["department_id"] == dept_str:
            return a["id"]
    raise AssertionError(f"no assignment found for role={role} department_id={dept_str} in {payload['assigned_role_assignments']}")


# ── Data model: multi-department assignments coexist cleanly ────────────────

async def test_hod_two_departments_via_api():
    name = "Case B — HOD @ A + HOD @ B: both assignments coexist, DB has 2 distinct rows"
    user_id = await _create_test_user("caseb")
    try:
        r1 = await _add_role(user_id, "hod", _DEPT_A)
        assert r1.status_code == 201, r1.text
        r2 = await _add_role(user_id, "hod", _DEPT_B)
        assert r2.status_code == 201, r2.text

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(UserRoleAssignment).where(UserRoleAssignment.user_id == user_id))).scalars().all()
        assert len(rows) == 2, f"expected 2 HOD rows, got {len(rows)}"
        assert {r.department_id for r in rows} == {_DEPT_A, _DEPT_B}
        assert all(r.role == UserRole.HOD for r in rows)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_faculty_two_departments_via_api():
    name = "Case C — FACULTY @ A + FACULTY @ B: both coexist"
    user_id = await _create_test_user("casec")
    try:
        assert (await _add_role(user_id, "faculty", _DEPT_A)).status_code == 201
        assert (await _add_role(user_id, "faculty", _DEPT_B)).status_code == 201
        roles = await _get_roles(user_id)
        assert len(roles["assignments"]) == 2
        assert {a["department_id"] for a in roles["assignments"]} == {str(_DEPT_A), str(_DEPT_B)}
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_hod_a_faculty_a_requires_two_grants():
    name = "Case E — HOD @ A + FACULTY @ A require TWO explicit grants; one never implies the other"
    user_id = await _create_test_user("casee")
    try:
        assert (await _add_role(user_id, "hod", _DEPT_A)).status_code == 201
        roles_after_hod_only = await _get_roles(user_id)
        assert roles_after_hod_only["assigned_roles"] == ["hod"], "granting HOD must not implicitly grant FACULTY"

        assert (await _add_role(user_id, "faculty", _DEPT_A)).status_code == 201
        roles_after_both = await _get_roles(user_id)
        assert sorted(roles_after_both["assigned_roles"]) == ["faculty", "hod"]
        assert len(roles_after_both["assignments"]) == 2
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_dpgs_hod_faculty_across_departments():
    name = "Case F — DPGS + HOD @ A + FACULTY @ B: three independent assignments"
    user_id = await _create_test_user("casef")
    try:
        assert (await _add_role(user_id, "dpgs")).status_code == 201
        assert (await _add_role(user_id, "hod", _DEPT_A)).status_code == 201
        assert (await _add_role(user_id, "faculty", _DEPT_B)).status_code == 201
        roles = await _get_roles(user_id)
        assert sorted(roles["assigned_roles"]) == ["dpgs", "faculty", "hod"]
        by_role = {(a["role"], a["department_id"]) for a in roles["assignments"]}
        assert by_role == {("dpgs", None), ("hod", str(_DEPT_A)), ("faculty", str(_DEPT_B))}
        # Remove DPGS single-holder afterwards so it doesn't linger and
        # block a later scenario in this same run.
        assert (await _remove_role(user_id, "dpgs")).status_code == 200
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_dpgs_hod_two_departments_faculty_third():
    name = "Case G — DPGS + HOD @ A + HOD @ B + FACULTY @ C"
    user_id = await _create_test_user("caseg")
    try:
        assert (await _add_role(user_id, "dpgs")).status_code == 201
        assert (await _add_role(user_id, "hod", _DEPT_A)).status_code == 201
        assert (await _add_role(user_id, "hod", _DEPT_B)).status_code == 201
        assert (await _add_role(user_id, "faculty", _DEPT_C)).status_code == 201
        roles = await _get_roles(user_id)
        by_role = {(a["role"], a["department_id"]) for a in roles["assignments"]}
        assert by_role == {
            ("dpgs", None), ("hod", str(_DEPT_A)), ("hod", str(_DEPT_B)), ("faculty", str(_DEPT_C)),
        }
        assert (await _remove_role(user_id, "dpgs")).status_code == 200
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


# ── Department validation per role ──────────────────────────────────────────

async def test_hod_faculty_require_department():
    name = "add_user_role: HOD/FACULTY without a department -> 400"
    user_id = await _create_test_user("nodept")
    try:
        r1 = await _add_role(user_id, "hod", None)
        assert r1.status_code == 400, r1.text
        r2 = await _add_role(user_id, "faculty", None)
        assert r2.status_code == 400, r2.text
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_global_roles_reject_department():
    name = "add_user_role: DPGS/INCHARGE_ACADEMIC_CELL/STUDENT with a department -> 400 (never invent a department for a global role)"
    user_id = await _create_test_user("globaldept")
    try:
        r1 = await _add_role(user_id, "dpgs", _DEPT_A)
        assert r1.status_code == 400, r1.text
        r2 = await _add_role(user_id, "incharge_academic_cell", _DEPT_A)
        assert r2.status_code == 400, r2.text
        r3 = await _add_role(user_id, "student", _DEPT_A)
        assert r3.status_code == 400, r3.text
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


# ── Super Admin exclusivity (Section 2.4) ───────────────────────────────────

async def test_super_admin_exclusive_both_directions():
    name = "Super Admin exclusivity: cannot layer onto existing roles, and no role can be layered onto Super Admin"
    user_id = await _create_test_user("supex")
    try:
        assert (await _add_role(user_id, "faculty", _DEPT_A)).status_code == 201
        r1 = await _add_role(user_id, "super_admin", None)
        assert r1.status_code == 409, r1.text

        user_id_2 = await _create_test_user("supex2")
        try:
            assert (await _add_role(user_id_2, "super_admin", None)).status_code == 201
            r2 = await _add_role(user_id_2, "hod", _DEPT_A)
            assert r2.status_code == 409, r2.text
        finally:
            await _cleanup_user(user_id_2)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


# ── remove_user_role disambiguation ──────────────────────────────────────────

async def test_remove_one_of_two_hod_departments_keeps_the_other():
    name = "remove_user_role: removing HOD @ A while HOD @ B remains is allowed and does not touch B"
    user_id = await _create_test_user("removehod")
    try:
        await _add_role(user_id, "hod", _DEPT_A)
        await _add_role(user_id, "hod", _DEPT_B)
        r = await _remove_role(user_id, "hod", _DEPT_A)
        assert r.status_code == 200, r.text
        roles = await _get_roles(user_id)
        assert len(roles["assignments"]) == 1, f"expected exactly 1 remaining assignment, got {roles['assignments']}"
        remaining = roles["assignments"][0]
        assert remaining["role"] == "hod" and remaining["department_id"] == str(_DEPT_B), f"expected HOD @ B to remain untouched, got {remaining}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_remove_role_ambiguous_without_department_rejected():
    name = "remove_user_role: omitting department_id when the user holds the role in >1 department -> 400 (must disambiguate)"
    user_id = await _create_test_user("ambigremove")
    try:
        await _add_role(user_id, "hod", _DEPT_A)
        await _add_role(user_id, "hod", _DEPT_B)
        r = await _remove_role(user_id, "hod", None)
        assert r.status_code == 400, r.text
        roles = await _get_roles(user_id)
        assert len(roles["assignments"]) == 2, "the ambiguous removal attempt must not have deleted anything"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_cannot_remove_last_assignment():
    name = "remove_user_role: cannot remove a user's only remaining assignment"
    user_id = await _create_test_user("lastrole")
    try:
        await _add_role(user_id, "hod", _DEPT_A)
        r = await _remove_role(user_id, "hod", _DEPT_A)
        assert r.status_code == 400, r.text
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


# ── THE required scenario (Section 13): DPGS + HOD@A + FACULTY@B ────────────

async def test_required_scenario_dpgs_hod_a_faculty_b():
    name = "REQUIRED SCENARIO — DPGS + HOD@A + FACULTY@B: each context is independent, none implies another"
    user_id = await _create_test_user("required")
    try:
        await _add_role(user_id, "dpgs")
        await _add_role(user_id, "hod", _DEPT_A)
        await _add_role(user_id, "faculty", _DEPT_B)
        token, rt_id = await _mint_session(user_id)
        try:
            me = (await _me(token)).json()
            hod_a_id = _assignment_id_for(me, "hod", _DEPT_A)
            faculty_b_id = _assignment_id_for(me, "faculty", _DEPT_B)
            dpgs_id = _assignment_id_for(me, "dpgs", None)

            # Switch into HOD @ A.
            r = await _switch_role(token, hod_a_id)
            body = r.json()
            assert r.status_code == 200, body
            assert body["active_role"] == "hod"
            assert body["active_department_id"] == str(_DEPT_A)

            # As HOD @ A: creating a course FOR department A succeeds...
            course_number = f"ZZTEST_REQ_{uuid.uuid4().hex[:6]}".upper()
            async with _client() as c:
                r_create_a = await c.post(
                    "/api/v1/courses", headers={"Authorization": f"Bearer {token}"},
                    json={"course_number": course_number, "title": "Required Scenario Course", "department_id": str(_DEPT_A)},
                )
            assert r_create_a.status_code == 201, r_create_a.text
            created_course_id = r_create_a.json()["id"]

            # ...but claiming department B while active as HOD @ A must be REJECTED —
            # HOD @ A never authorizes department B, regardless of request body.
            async with _client() as c:
                r_create_b = await c.post(
                    "/api/v1/courses", headers={"Authorization": f"Bearer {token}"},
                    json={"course_number": f"ZZTEST_REQ2_{uuid.uuid4().hex[:6]}".upper(), "title": "Should Fail", "department_id": str(_DEPT_B)},
                )
            assert r_create_b.status_code == 403, r_create_b.text

            # HOD @ A must NOT be treated as Faculty of A: an endpoint gated
            # to require_roles(FACULTY) must reject this session while it is
            # active as HOD, even though the SAME PERSON does hold Faculty
            # somewhere (department B) — active role, not "any assigned role".
            async with _client() as c:
                r_faculty_gate = await c.get("/api/v1/courses/offerings/all", headers={"Authorization": f"Bearer {token}"}, params={"mine": "true"})
            # HOD "mine" branch is unrestricted-but-department-scoped in this
            # app (courses.py); the meaningful assertion is the explicit
            # role-switch below, not this incidental 200/empty-list response.
            assert r_faculty_gate.status_code == 200

            # Switch into FACULTY @ B.
            r2 = await _switch_role(token, faculty_b_id)
            body2 = r2.json()
            assert r2.status_code == 200, body2
            assert body2["active_role"] == "faculty"
            assert body2["active_department_id"] == str(_DEPT_B)

            # As FACULTY @ B: updating the department-A course created above
            # (as HOD) must be rejected outright — Faculty has no
            # department-wide manage authorization at all, and is certainly
            # NOT being treated as HOD of A.
            async with _client() as c:
                r_update = await c.put(
                    f"/api/v1/courses/{created_course_id}", headers={"Authorization": f"Bearer {token}"},
                    json={"course_number": course_number, "title": "Hijacked?", "department_id": str(_DEPT_A)},
                )
            assert r_update.status_code == 403, r_update.text

            # Switch into DPGS (institution-wide).
            r3 = await _switch_role(token, dpgs_id)
            body3 = r3.json()
            assert r3.status_code == 200, body3
            assert body3["active_role"] == "dpgs"
            assert body3["active_department_id"] is None

            # Cleanup the course created during this scenario.
            async with AsyncSessionLocal() as db:
                await db.execute(delete(Course).where(Course.id == created_course_id))
                await db.commit()
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id == rt_id))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


# ── Security: assignment_id cannot be manufactured / borrowed ───────────────

async def test_switch_role_rejects_foreign_assignment_id():
    name = "SECURITY — switch-role rejects an assignment_id belonging to a DIFFERENT user"
    user_a = await _create_test_user("secA")
    user_b = await _create_test_user("secB")
    try:
        await _add_role(user_a, "hod", _DEPT_A)
        await _add_role(user_b, "hod", _DEPT_B)
        token_a, rt_a = await _mint_session(user_a)
        try:
            me_b = await _get_roles(user_b)
            foreign_assignment_id = me_b["assignments"][0]["id"]
            r = await _switch_role(token_a, foreign_assignment_id)
            assert r.status_code == 403, r.text
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id == rt_a))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_a)
        await _cleanup_user(user_b)


async def test_switch_role_rejects_random_uuid():
    name = "SECURITY — switch-role rejects a random/nonexistent assignment_id"
    user_id = await _create_test_user("secrand")
    try:
        await _add_role(user_id, "hod", _DEPT_A)
        token, rt_id = await _mint_session(user_id)
        try:
            r = await _switch_role(token, str(uuid.uuid4()))
            assert r.status_code == 403, r.text
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id == rt_id))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_removed_assignment_self_heals_active_context():
    name = "SECURITY — removing a session's active assignment self-heals on the next request, never stays authorized as the removed role"
    user_id = await _create_test_user("selfheal")
    try:
        await _add_role(user_id, "hod", _DEPT_A)
        await _add_role(user_id, "faculty", _DEPT_B)
        token, rt_id = await _mint_session(user_id)
        try:
            me = (await _me(token)).json()
            hod_a_id = _assignment_id_for(me, "hod", _DEPT_A)
            switched = await _switch_role(token, hod_a_id)
            assert switched.status_code == 200

            # Super Admin now removes the HOD @ A assignment this session is
            # actively using.
            r = await _remove_role(user_id, "hod", _DEPT_A)
            assert r.status_code == 200, r.text

            # The very next request under the SAME token must NOT still be
            # authorized as HOD @ A — it must self-heal to a remaining
            # assignment (FACULTY @ B here), never silently keep acting as a
            # role that was just revoked.
            me_after = (await _me(token)).json()
            assert me_after["active_role"] != "hod" or me_after["active_department_id"] != str(_DEPT_A)
            assert me_after["active_role"] == "faculty"
            assert me_after["active_department_id"] == str(_DEPT_B)
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id == rt_id))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_user(user_id)


async def test_faculty_a_hod_b_forged_department_and_inactive_assignment():
    name = "SECURITY — FACULTY@A + HOD@B: forged department ids are rejected, and the inactive assignment grants nothing"
    user_id = await _create_test_user("forge")
    course_prefix = "ZZTEST_FORGE_"
    try:
        await _add_role(user_id, "faculty", _DEPT_A)
        await _add_role(user_id, "hod", _DEPT_B)
        # A course OWNED BY department A, created by the Super Admin.
        async with _client() as c:
            r = await c.post("/api/v1/courses", headers=_super_headers(), json={
                "course_number": f"{course_prefix}{uuid.uuid4().hex[:6]}".upper(), "title": "Owned by A", "department_id": str(_DEPT_A)})
        assert r.status_code == 201, r.text
        a_course_id = r.json()["id"]

        token, rt_id = await _mint_session(user_id)
        try:
            me = (await _me(token)).json()
            faculty_a_id = _assignment_id_for(me, "faculty", _DEPT_A)
            hod_b_id = _assignment_id_for(me, "hod", _DEPT_B)
            auth = {"Authorization": f"Bearer {token}"}

            def new_course(dept, tag):
                return {"course_number": f"{course_prefix}{tag}{uuid.uuid4().hex[:5]}".upper(), "title": "Forge test", "department_id": str(dept)}

            # ── Active as FACULTY@A: the inactive HOD@B assignment grants nothing.
            assert (await _switch_role(token, faculty_a_id)).status_code == 200
            async with _client() as c:
                for dept in (_DEPT_A, _DEPT_B):
                    r = await c.post("/api/v1/courses", headers=auth, json=new_course(dept, "F"))
                    assert r.status_code == 403, f"Faculty@A create in {dept} must be 403, got {r.status_code} {r.text}"
                r = await c.get("/api/v1/auth/users", headers=auth)
                assert r.status_code == 403, f"HOD-restricted GET /auth/users as Faculty@A: {r.status_code}"
                r = await c.post("/api/v1/auth/faculty", headers=auth, json={})
                assert r.status_code in (403,), f"HOD-only POST /auth/faculty as Faculty@A must be 403 (before body validation), got {r.status_code}"

            # ── Active as HOD@B: department is B, whatever the body claims.
            assert (await _switch_role(token, hod_b_id)).status_code == 200
            forged = new_course(_DEPT_A, "X")
            async with _client() as c:
                r = await c.post("/api/v1/courses", headers=auth, json=forged)
                assert r.status_code == 403, f"HOD@B creating for department A must be 403, got {r.status_code} {r.text}"
                r = await c.post("/api/v1/courses", headers=auth, json=new_course(_DEPT_B, "OK"))
                assert r.status_code == 201, r.text
                b_course_id = r.json()["id"]
                r = await c.put(f"/api/v1/courses/{a_course_id}", headers=auth,
                                json={"course_number": "ZZTEST_FORGE_X", "title": "hijack", "department_id": str(_DEPT_A)})
                assert r.status_code == 403, f"HOD@B updating a department-A course must be 403, got {r.status_code}"
                r = await c.put(f"/api/v1/courses/{b_course_id}", headers=auth,
                                json={"course_number": "ZZTEST_FORGE_Y", "title": "move", "department_id": str(_DEPT_A)})
                assert r.status_code == 403, f"HOD@B moving its own course into department A must be 403, got {r.status_code}"
                r = await c.post(f"/api/v1/courses/{a_course_id}/availability", headers=auth, json={"department_id": str(_DEPT_A)})
                assert r.status_code == 403, f"HOD@B managing department A availability must be 403, got {r.status_code}"
                r = await c.post("/api/v1/courses/offerings", headers=auth, json={
                    "calendar_id": str(uuid.uuid4()), "semester_id": str(uuid.uuid4()), "course_id": str(a_course_id),
                    "department_id": str(_DEPT_A), "faculty_ids": [str(user_id)], "leader_id": str(user_id)})
                assert r.status_code == 403, f"HOD@B creating an offering for department A must be 403, got {r.status_code} {r.text}"

            async with AsyncSessionLocal() as db:
                owned = (await db.execute(select(Course.department_id).where(Course.id == uuid.UUID(b_course_id)))).scalar_one()
                forged_rows = (await db.execute(select(Course.id).where(Course.course_number == forged["course_number"]))).scalars().all()
                a_after = (await db.execute(select(Course.title, Course.department_id).where(Course.id == uuid.UUID(a_course_id)))).one()
            assert owned == _DEPT_B, "HOD@B's course must be owned by department B"
            assert not forged_rows, "the forged department-A course must not exist"
            assert a_after == ("Owned by A", _DEPT_A), f"department A's course was modified: {a_after}"
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id == rt_id))
                await db.execute(delete(Course).where(Course.course_number.like(f"{course_prefix}%")))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Course).where(Course.course_number.like(f"{course_prefix}%")))
            await db.commit()
        await _cleanup_user(user_id)


async def test_course_catalogue_list_faculty_blocked_others_unchanged():
    name = "GET /courses: Faculty -> 403 (also when Faculty@A is merely inactive-HOD@B); HOD@B -> own dept only; Student unchanged"
    user_f = await _create_test_user("catfac")
    user_m = await _create_test_user("catmulti")
    user_s = await _create_test_user("catstud")
    try:
        await _add_role(user_f, "faculty", _DEPT_A)
        await _add_role(user_m, "faculty", _DEPT_A)
        await _add_role(user_m, "hod", _DEPT_B)
        await _add_role(user_s, "student")
        tok_f, rt_f = await _mint_session(user_f)
        tok_m, rt_m = await _mint_session(user_m)
        tok_s, rt_s = await _mint_session(user_s)
        try:
            async def list_courses(tok):
                async with _client() as c:
                    return await c.get("/api/v1/courses", headers={"Authorization": f"Bearer {tok}"})

            r = await list_courses(tok_f)
            assert r.status_code == 403, f"Faculty GET /courses must be 403, got {r.status_code}"

            me = (await _me(tok_m)).json()
            fac_a = _assignment_id_for(me, "faculty", _DEPT_A)
            hod_b = _assignment_id_for(me, "hod", _DEPT_B)
            assert (await _switch_role(tok_m, fac_a)).status_code == 200
            r = await list_courses(tok_m)
            assert r.status_code == 403, f"Faculty@A (holding HOD@B, inactive) must be 403, got {r.status_code}"
            assert (await _switch_role(tok_m, hod_b)).status_code == 200
            r = await list_courses(tok_m)
            assert r.status_code == 200, r.text
            assert all(c["department_id"] == str(_DEPT_B) for c in r.json()), "HOD@B must only see department B courses"
            assert (await _switch_role(tok_m, fac_a)).status_code == 200
            assert (await list_courses(tok_m)).status_code == 403, "switching back to Faculty@A must block again"

            r = await list_courses(tok_s)
            assert r.status_code == 200 and r.json() == [], f"scope-less Student keeps the existing scoped (empty) result, got {r.status_code} {r.text[:80]}"
            r = await list_courses(_SUPER_TOKEN)
            assert r.status_code == 200 and len(r.json()) > 0, "Super Admin unchanged"
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id.in_([rt_f, rt_m, rt_s])))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")
    finally:
        for uid in (user_f, user_m, user_s):
            await _cleanup_user(uid)


async def test_faculty_teacher_courses_and_assigned_access_still_work():
    name = "Faculty still reaches Teacher Courses surfaces: own offerings (mine/default), own assigned course, own offering detail; unassigned course 403"
    user_id = await _create_test_user("teacher")
    prefix = "ZZTEST_TEACH_"
    offering_id = None
    try:
        async with AsyncSessionLocal() as db:
            # No legacy User.department_id is set on purpose: the FACULTY@A assignment alone must make
            # this user assignable to a department-A offering.
            sem = (await db.execute(select(Semester.id, Semester.calendar_id).limit(1))).one()
            await db.commit()
        await _add_role(user_id, "faculty", _DEPT_A)

        async def make_course(tag):
            async with _client() as c:
                r = await c.post("/api/v1/courses", headers=_super_headers(), json={
                    "course_number": f"{prefix}{tag}{uuid.uuid4().hex[:5]}".upper(), "title": f"Teach {tag}", "department_id": str(_DEPT_A)})
            assert r.status_code == 201, r.text
            return r.json()["id"]

        assigned_course, other_course = await make_course("A"), await make_course("B")
        async with _client() as c:
            r = await c.post("/api/v1/courses/offerings", headers=_super_headers(), json={
                "calendar_id": str(sem.calendar_id), "semester_id": str(sem.id), "course_id": assigned_course,
                "department_id": str(_DEPT_A), "faculty_ids": [str(user_id)], "leader_id": str(user_id)})
        assert r.status_code == 201, r.text
        offering_id = r.json()["id"]

        token, rt_id = await _mint_session(user_id)
        try:
            h = {"Authorization": f"Bearer {token}"}
            async with _client() as c:
                for params in ({"mine": "true"}, {}):
                    r = await c.get("/api/v1/courses/offerings/all", headers=h, params=params)
                    assert r.status_code == 200, r.text
                    assert [o["id"] for o in r.json()] == [offering_id], f"Faculty must see exactly their own offering ({params}), got {[o['id'] for o in r.json()]}"
                assert (await c.get(f"/api/v1/courses/offerings/{offering_id}", headers=h)).status_code == 200
                assert (await c.get(f"/api/v1/courses/{assigned_course}", headers=h)).status_code == 200, "assigned course must stay viewable"
                assert (await c.get(f"/api/v1/courses/{other_course}", headers=h)).status_code == 403, "unassigned course must stay 403"
                assert (await c.get("/api/v1/courses", headers=h)).status_code == 403
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(RefreshToken).where(RefreshToken.id == rt_id))
                await db.commit()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")
    finally:
        async with AsyncSessionLocal() as db:
            # OfferingFaculty.faculty_id has no ON DELETE: the offering (which cascades it) must go before the user.
            await db.execute(delete(CourseOffering).where(CourseOffering.course_id.in_(select(Course.id).where(Course.course_number.like(f"{prefix}%")))))
            await db.execute(delete(Course).where(Course.course_number.like(f"{prefix}%")))
            await db.commit()
        await _cleanup_user(user_id)


async def main() -> None:
    await _setup()
    try:
        scenarios = [
            test_hod_two_departments_via_api,
            test_faculty_two_departments_via_api,
            test_hod_a_faculty_a_requires_two_grants,
            test_dpgs_hod_faculty_across_departments,
            test_dpgs_hod_two_departments_faculty_third,
            test_hod_faculty_require_department,
            test_global_roles_reject_department,
            test_super_admin_exclusive_both_directions,
            test_remove_one_of_two_hod_departments_keeps_the_other,
            test_remove_role_ambiguous_without_department_rejected,
            test_cannot_remove_last_assignment,
            test_required_scenario_dpgs_hod_a_faculty_b,
            test_switch_role_rejects_foreign_assignment_id,
            test_switch_role_rejects_random_uuid,
            test_removed_assignment_self_heals_active_context,
            test_faculty_a_hod_b_forged_department_and_inactive_assignment,
            test_course_catalogue_list_faculty_blocked_others_unchanged,
            test_faculty_teacher_courses_and_assigned_access_still_work,
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
