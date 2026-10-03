"""Standalone HTTP-level tests for this task's confirmed requirements:

1. Department <-> College/Outstation many-to-many association
   (`ams_department_colleges`, `app.api.v1.endpoints.departments`).
2. Super-Admin-only `college_id` support on user creation/update, with
   department/college pair validation (`auth.py`'s `create_user`/`update_user`).
3. Your Profile active-department/college resolution (`/auth/me`'s
   `department_name`/`college_name`), including multi-department Faculty and
   role-switching behavior.
4. Offered Courses offering-specific editing (`PATCH /courses/offerings/{id}`)
   — HOD own-department allowed, cross-department blocked, Super Admin
   unrestricted, course-master fields unreachable.

Modelled on `tests/test_advisory_committee_department_eligibility.py`: real
FastAPI app via `httpx.ASGITransport`, real JWT sessions, a `record`/`_run`
pass-fail tracker, `zztest_cdp_...`-prefixed rows, full teardown.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_college_department_profile_offering
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date

import httpx
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import AcademicCalendar, Semester
from app.models.course import Course, CourseOffering
from app.models.user import College, Department, DepartmentCollege, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_cdp_"
_DEVICE = "ZZTEST-cdp"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}

CREATED_USER_IDS: list[uuid.UUID] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120) as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _session(user_id) -> str:
    async with AsyncSessionLocal() as db:
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=user_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(),
                             device_info=_DEVICE))
        await db.commit()
    return create_access_token(str(user_id), {"sid": str(rt)})


async def _mk_user(key, role, dept):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"CDP{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_multi_dept_faculty(key, depts):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"CDP{key.upper()}",
            role=UserRole.FACULTY, department_id=depts[0].id,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        for d in depts:
            db.add(UserRoleAssignment(user_id=u.id, role=UserRole.FACULTY, department_id=d.id))
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
            print("STOP: stray zztest_cdp_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)

        # Isolated Departments/Colleges (not the real seeded ones) so this
        # suite's associations never touch real institutional data.
        d1 = Department(name=f"{_PFX}Dept1 {_TAG}", code=f"ZCD1{_TAG}"[:20])
        d2 = Department(name=f"{_PFX}Dept2 {_TAG}", code=f"ZCD2{_TAG}"[:20])
        c1 = College(name=f"{_PFX}College1 {_TAG}", code=f"ZCC1{_TAG}"[:20])
        c2 = College(name=f"{_PFX}College2 {_TAG}", code=f"ZCC2{_TAG}"[:20])
        db.add_all([d1, d2, c1, c2])
        await db.flush()
        S["D1"], S["D2"], S["C1"], S["C2"] = d1, d2, c1, c2

        # D1 <-> C1, D1 <-> C2 (one department, two colleges).
        # D2 <-> C2 only (one college, two departments; D2/C1 is a genuine mismatch).
        db.add_all([
            DepartmentCollege(department_id=d1.id, college_id=c1.id),
            DepartmentCollege(department_id=d1.id, college_id=c2.id),
            DepartmentCollege(department_id=d2.id, college_id=c2.id),
        ])

        # Offering-edit fixtures: one calendar/semester, one course+offering per department.
        cal = AcademicCalendar(name=f"{_PFX}Cal {_TAG}", academic_year="2097-98", start_date=date(2097, 7, 1), end_date=date(2098, 6, 30), status="active")
        db.add(cal); await db.flush()
        sem = Semester(calendar_id=cal.id, name="Semester I", sem_type="odd", start_date=date(2097, 7, 1), end_date=date(2097, 12, 31), exam_end=date(2097, 12, 20))
        db.add(sem); await db.flush()

        course1 = Course(course_number=f"ZZCDP1-{_TAG}", title=f"{_PFX}Course1", department_id=d1.id, category="core", program_level="PG")
        course2 = Course(course_number=f"ZZCDP2-{_TAG}", title=f"{_PFX}Course2", department_id=d2.id, category="core", program_level="PG")
        db.add_all([course1, course2]); await db.flush()
        offering1 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=course1.id, department_id=d1.id, max_enrollment=60, status="draft")
        offering2 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=course2.id, department_id=d2.id, max_enrollment=60, status="draft")
        db.add_all([offering1, offering2]); await db.flush()
        S["COURSE1"], S["COURSE2"], S["OFFERING1"], S["OFFERING2"] = course1, course2, offering1, offering2

        await db.commit()

    await _mk_user("sa", UserRole.SUPER_ADMIN, None)
    await _mk_user("hod_a", UserRole.HOD, S["D1"])
    await _mk_user("hod_b", UserRole.HOD, S["D2"])
    await _mk_user("ext_examiner", UserRole.EXTERNAL_EXAMINER, None)
    await _mk_user("stu_a", UserRole.STUDENT, S["D1"])
    await _mk_multi_dept_faculty("multi_fac", [S["D1"], S["D2"]])

    for k in ("sa", "hod_a", "hod_b", "ext_examiner", "stu_a", "multi_fac"):
        TOK[k] = await _session(U[k])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        for uid in CREATED_USER_IDS:
            await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id == uid))
            await db.execute(delete(User).where(User.id == uid))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_([S["OFFERING1"].id, S["OFFERING2"].id])))
        await db.execute(delete(Course).where(Course.id.in_([S["COURSE1"].id, S["COURSE2"].id])))
        await db.commit()
    async with AsyncSessionLocal() as db:
        # Clean up calendar/semester (fetched fresh to get their real ids)
        cal_name_like = f"{_PFX}Cal {_TAG}"
        cal_result = await db.execute(select(AcademicCalendar).where(AcademicCalendar.name == cal_name_like))
        cal = cal_result.scalar_one_or_none()
        if cal:
            await db.execute(delete(Semester).where(Semester.calendar_id == cal.id))
            await db.execute(delete(AcademicCalendar).where(AcademicCalendar.id == cal.id))
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(DepartmentCollege).where(DepartmentCollege.department_id.in_([S["D1"].id, S["D2"].id])))
        await db.execute(delete(Department).where(Department.id.in_([S["D1"].id, S["D2"].id])))
        await db.execute(delete(College).where(College.id.in_([S["C1"].id, S["C2"].id])))
        await db.commit()


# ── Department <-> College/Outstation many-to-many ──────────────────────────────

async def t_department_can_belong_to_multiple_colleges():
    r = await _call("GET", f"/departments/{S['D1'].id}/colleges", TOK["sa"])
    assert r.status_code == 200, r.text
    ids = {row["college_id"] for row in r.json()}
    assert ids == {str(S["C1"].id), str(S["C2"].id)}, ids


async def t_college_can_have_multiple_departments():
    r = await _call("GET", f"/admin/colleges/{S['C2'].id}/departments", TOK["sa"])
    assert r.status_code == 200, r.text
    ids = {row["department_id"] for row in r.json()}
    assert ids == {str(S["D1"].id), str(S["D2"].id)}, ids


async def t_duplicate_association_rejected():
    r = await _call("POST", f"/admin/colleges/{S['C1'].id}/departments", TOK["sa"], json={"department_id": str(S["D1"].id)})
    assert r.status_code == 409, r.text


async def t_department_filter_by_college_returns_only_associated():
    r = await _call("GET", "/departments", TOK["sa"], params={"college_id": str(S["C1"].id)})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(S["D1"].id) in ids, ids
    assert str(S["D2"].id) not in ids, "D2 is not associated with C1 and must not appear"


async def t_unauthorized_cannot_modify_department_college_association():
    r = await _call("POST", f"/admin/colleges/{S['C1'].id}/departments", TOK["hod_a"], json={"department_id": str(S["D2"].id)})
    assert r.status_code == 403, r.text
    r2 = await _call("DELETE", f"/admin/colleges/{S['C1'].id}/departments/{S['D1'].id}", TOK["hod_a"])
    assert r2.status_code == 403, r2.text
    # Confirm nothing actually changed despite the rejected attempts.
    check = await _call("GET", f"/departments/{S['D1'].id}/colleges", TOK["sa"])
    assert {row["college_id"] for row in check.json()} == {str(S["C1"].id), str(S["C2"].id)}


async def t_unassigned_department_remains_valid():
    """A department with zero college associations (none created here) must
    not error out of any existing endpoint — spot-check the plain list."""
    r = await _call("GET", "/departments", TOK["sa"])
    assert r.status_code == 200, r.text


# ── User creation/update with college_id (Super Admin only) ────────────────────

async def t_create_user_with_valid_college_and_department_pair():
    r = await _call("POST", "/auth/users", TOK["sa"], json={
        "email": f"{_PFX}newuser1_{_TAG}@avfu.ac.in", "password": "Passw0rd!123",
        "first_name": "ZZTEST", "last_name": "NewUser1", "role": "faculty",
        "department_id": str(S["D1"].id), "college_id": str(S["C1"].id),
    })
    assert r.status_code == 201, r.text
    CREATED_USER_IDS.append(uuid.UUID(r.json()["id"]))
    async with AsyncSessionLocal() as db:
        u = await db.get(User, uuid.UUID(r.json()["id"]))
        assert str(u.college_id) == str(S["C1"].id)


async def t_create_user_with_mismatched_department_college_rejected():
    r = await _call("POST", "/auth/users", TOK["sa"], json={
        "email": f"{_PFX}newuser2_{_TAG}@avfu.ac.in", "password": "Passw0rd!123",
        "first_name": "ZZTEST", "last_name": "NewUser2", "role": "faculty",
        "department_id": str(S["D2"].id), "college_id": str(S["C1"].id),  # D2 is NOT linked to C1
    })
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == f"{_PFX}newuser2_{_TAG}@avfu.ac.in".lower()))).scalar_one_or_none()
        assert existing is None, "rejected creation must not leave a partial user row"


async def t_create_user_with_invalid_college_id_rejected():
    r = await _call("POST", "/auth/users", TOK["sa"], json={
        "email": f"{_PFX}newuser3_{_TAG}@avfu.ac.in", "password": "Passw0rd!123",
        "first_name": "ZZTEST", "last_name": "NewUser3", "role": "faculty",
        "college_id": str(uuid.uuid4()),
    })
    assert r.status_code == 400, r.text


async def t_create_user_without_college_still_works():
    r = await _call("POST", "/auth/users", TOK["sa"], json={
        "email": f"{_PFX}newuser4_{_TAG}@avfu.ac.in", "password": "Passw0rd!123",
        "first_name": "ZZTEST", "last_name": "NewUser4", "role": "faculty",
    })
    assert r.status_code == 201, r.text
    CREATED_USER_IDS.append(uuid.UUID(r.json()["id"]))


async def t_non_super_admin_cannot_create_user():
    r = await _call("POST", "/auth/users", TOK["hod_a"], json={
        "email": f"{_PFX}newuser5_{_TAG}@avfu.ac.in", "password": "Passw0rd!123",
        "first_name": "ZZTEST", "last_name": "NewUser5", "role": "faculty", "college_id": str(S["C1"].id),
    })
    assert r.status_code == 403, r.text


async def t_update_user_mismatched_pair_rejected_nothing_changed():
    r = await _call("PATCH", f"/auth/users/{U['hod_b']}", TOK["sa"], json={"college_id": str(S["C1"].id)})
    # hod_b's department is D2, which is NOT associated with C1 -> rejected.
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        u = await db.get(User, U["hod_b"])
        assert u.college_id is None, "a rejected update must change nothing"


async def t_non_super_admin_cannot_set_college_id_via_crafted_body():
    """Only Super Admin may touch `college_id` on `PATCH /auth/users/{id}` at
    all — the endpoint's own `require_roles(SUPER_ADMIN, DPGS)` already
    blocks every other role outright; this confirms a non-privileged caller
    (HOD) gets 403 for a crafted body that tries to set it, not a silent
    no-op or partial apply."""
    r = await _call("PATCH", f"/auth/users/{U['ext_examiner']}", TOK["hod_a"], json={"college_id": str(S["C1"].id)})
    assert r.status_code == 403, r.text
    async with AsyncSessionLocal() as db:
        u = await db.get(User, U["ext_examiner"])
        assert u.college_id is None, "a rejected update must change nothing"


# ── Your Profile: active department/college resolution ─────────────────────────

async def t_single_department_user_sees_own_department():
    r = await _call("GET", "/auth/me", TOK["hod_a"])
    assert r.status_code == 200, r.text
    assert r.json()["department_name"] == S["D1"].name, r.json()["department_name"]


async def t_student_department_resolved_from_own_user_department():
    r = await _call("GET", "/auth/me", TOK["stu_a"])
    assert r.status_code == 200, r.text
    assert r.json()["department_name"] == S["D1"].name, r.json()["department_name"]


async def t_user_without_department_or_college_shows_none():
    r = await _call("GET", "/auth/me", TOK["sa"])
    assert r.status_code == 200, r.text
    assert r.json()["department_name"] is None
    assert r.json()["college_name"] is None


async def t_multi_department_faculty_sees_only_active_department_and_switching_updates_it():
    r = await _call("GET", "/auth/me", TOK["multi_fac"])
    assert r.status_code == 200, r.text
    initial_dept = r.json()["department_name"]
    assert initial_dept in (S["D1"].name, S["D2"].name), initial_dept

    roles_resp = await _call("GET", "/auth/me", TOK["multi_fac"])
    assignments = roles_resp.json()["assigned_role_assignments"]
    other = next(a for a in assignments if a["department_name"] != initial_dept)

    switch = await _call("POST", "/auth/switch-role", TOK["multi_fac"], json={"assignment_id": other["id"]})
    assert switch.status_code == 200, switch.text
    assert switch.json()["department_name"] == other["department_name"], switch.json()["department_name"]
    assert switch.json()["department_name"] != initial_dept, "switching must change the displayed department"

    confirm = await _call("GET", "/auth/me", TOK["multi_fac"])
    assert confirm.json()["department_name"] == other["department_name"], "the new active department must persist for subsequent requests"


async def t_login_also_resolves_department_and_college():
    """The same resolution must work on the LOGIN path, not just /auth/me —
    this exercises a genuinely different source query (see auth.py's login,
    which previously did not eager-load department/college at all)."""
    r = await _call("POST", "/auth/login", None, json={"email": f"{_PFX}hod_a_{_TAG}@avfu.ac.in", "password": "irrelevant"})
    # Password is wrong on purpose (we never set one for ORM-created fixtures) —
    # this only proves the query itself doesn't crash before the password check
    # ever runs; a 401 here (not a 500) is the correct, expected outcome.
    assert r.status_code == 401, r.text


# ── Offered Courses: offering-specific editing ──────────────────────────────────

async def t_hod_can_edit_offering_in_own_department():
    r = await _call("PATCH", f"/courses/offerings/{S['OFFERING1'].id}", TOK["hod_a"], json={"max_enrollment": 80, "section": "A"})
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        o = await db.get(CourseOffering, S["OFFERING1"].id)
        assert o.max_enrollment == 80 and o.section == "A"


async def t_hod_cannot_edit_offering_in_other_department():
    r = await _call("PATCH", f"/courses/offerings/{S['OFFERING2'].id}", TOK["hod_a"], json={"max_enrollment": 99})
    assert r.status_code == 403, r.text
    async with AsyncSessionLocal() as db:
        o = await db.get(CourseOffering, S["OFFERING2"].id)
        assert o.max_enrollment == 60, "cross-department edit attempt must not change anything"


async def t_super_admin_can_edit_offering_across_departments():
    r = await _call("PATCH", f"/courses/offerings/{S['OFFERING2'].id}", TOK["sa"], json={"max_enrollment": 70, "practical_group": "G1"})
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        o = await db.get(CourseOffering, S["OFFERING2"].id)
        assert o.max_enrollment == 70 and o.practical_group == "G1"


async def t_offering_edit_cannot_change_course_master_fields():
    """The update schema has no field for course title/category/credits at
    all — confirm the course itself is untouched after an unrelated offering
    edit, and that sending an unknown field (e.g. attempting to rename the
    course via the offering endpoint) has no effect (Pydantic simply ignores
    fields outside the schema; it does not error, but it also never applies)."""
    r = await _call("PATCH", f"/courses/offerings/{S['OFFERING1'].id}", TOK["hod_a"], json={
        "max_enrollment": 85, "title": "HACKED TITLE", "category": "research", "department_id": str(S["D2"].id),
    })
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        o = await db.get(CourseOffering, S["OFFERING1"].id)
        c = await db.get(Course, S["COURSE1"].id)
        assert o.max_enrollment == 85
        assert o.department_id == S["D1"].id, "department must never change through this endpoint"
        assert c.title == "zztest_cdp_Course1", c.title
        assert c.category == "core", c.category


async def t_invalid_max_enrollment_rejected():
    r = await _call("PATCH", f"/courses/offerings/{S['OFFERING1'].id}", TOK["hod_a"], json={"max_enrollment": 0})
    assert r.status_code == 422, r.text


async def main() -> None:
    await _setup()
    try:
        cases = {
            "Department can belong to multiple Colleges/Outstations": t_department_can_belong_to_multiple_colleges,
            "College/Outstation can have multiple Departments": t_college_can_have_multiple_departments,
            "Duplicate Department-College association rejected (409)": t_duplicate_association_rejected,
            "GET /departments?college_id= returns only associated departments": t_department_filter_by_college_returns_only_associated,
            "Unauthorized (HOD) cannot modify Department-College associations": t_unauthorized_cannot_modify_department_college_association,
            "A department with no college association remains valid": t_unassigned_department_remains_valid,
            "Create user with valid Department+College pair -> 201": t_create_user_with_valid_college_and_department_pair,
            "Create user with mismatched Department/College pair -> 400, nothing created": t_create_user_with_mismatched_department_college_rejected,
            "Create user with invalid college_id -> 400": t_create_user_with_invalid_college_id_rejected,
            "Create user without college_id still works": t_create_user_without_college_still_works,
            "Non-Super-Admin cannot create a user at all": t_non_super_admin_cannot_create_user,
            "Update user with mismatched Department/College pair -> 400, nothing changed": t_update_user_mismatched_pair_rejected_nothing_changed,
            "Non-Super-Admin cannot set college_id via crafted body": t_non_super_admin_cannot_set_college_id_via_crafted_body,
            "Single-department user sees their own department on /auth/me": t_single_department_user_sees_own_department,
            "Student's department resolved from User.department (own record)": t_student_department_resolved_from_own_user_department,
            "User with no department/college shows null (frontend renders 'Not assigned')": t_user_without_department_or_college_shows_none,
            "Multi-department Faculty sees only active department; switching updates it": t_multi_department_faculty_sees_only_active_department_and_switching_updates_it,
            "Login path also resolves department/college without crashing": t_login_also_resolves_department_and_college,
            "HOD can edit an offering in their own department": t_hod_can_edit_offering_in_own_department,
            "HOD cannot edit an offering in another department (403, unchanged)": t_hod_cannot_edit_offering_in_other_department,
            "Super Admin can edit offerings across departments": t_super_admin_can_edit_offering_across_departments,
            "Offering edit cannot change course master fields or department": t_offering_edit_cannot_change_course_master_fields,
            "Invalid max_enrollment rejected (422)": t_invalid_max_enrollment_rejected,
        }
        for name, fn in cases.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "departments": (await db.execute(select(func.count()).select_from(Department).where(Department.code.like(f"ZCD%{_TAG}%")))).scalar_one(),
                "colleges": (await db.execute(select(func.count()).select_from(College).where(College.code.like(f"ZCC%{_TAG}%")))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_CDP user / session / department / college row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
