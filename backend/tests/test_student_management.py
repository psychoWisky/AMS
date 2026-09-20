"""Standalone HTTP-level tests for the Super Admin split between
  * User Management  — GET/PATCH /auth/users  (staff / system users only), and
  * Students         — GET/PATCH /students     (all students; Super Admin only).

Drives the REAL FastAPI app via `httpx.ASGITransport` with real JWT sessions.
There is no Student table: students are `User` rows, and their Academic Year /
Semester come from Course Registrations and Enrollments — so the filter tests
create real (temporary) registration / enrollment rows.

Everything created is `zztest_student_management_...` / `ZZTEST_STUDENT_MANAGEMENT`
(users, colleges, orientation candidates, registrations, enrollments, sessions)
and is deleted in `finally`; enrollments/registrations/candidates go before users.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_student_management
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date

import httpx
from sqlalchemy import select, delete, func

from app.db.base import AsyncSessionLocal
from app.main import app
from app.core.security import create_access_token
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Department, Program, College
from app.models.academic import AcademicCalendar, Semester
from app.models.course import CourseOffering
from app.models.enrollment import CourseRegistration, StudentEnrollment
from app.models.orientation import OrientationCandidate

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_student_management_"
_CODE_PFX = "ZZTESTSM"


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
S: dict = {}
U: dict = {}     # name -> user id
TOK: dict = {}   # name -> access token


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with _client() as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _mk_user(name: str, legacy_role: UserRole, dept, **extra) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{name.lower()}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=name, role=legacy_role,
                 department_id=dept, is_active=True, is_verified=True, **extra)
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


async def _session(name: str) -> str:
    rt = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(RefreshToken(id=rt, user_id=U[name], token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info="ZZTEST-student-management"))
        await db.commit()
    TOK[name] = create_access_token(str(U[name]), {"sid": str(rt)})
    return TOK[name]


def _ours(rows: list[dict]) -> set[str]:
    ids = {str(v): k for k, v in U.items()}
    return {ids[r["id"]] for r in rows if r["id"] in ids}


def _count(rows: list[dict], name: str) -> int:
    return sum(1 for r in rows if r["id"] == str(U[name]))


async def _students(token=None, **params) -> dict:
    r = await _call("GET", "/students", token or S["super"], params={"page_size": 100, **params})
    assert r.status_code == 200, f"GET /students {params}: {r.status_code} {r.text}"
    return r.json()


async def _db_user(name: str) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == U[name]))).scalar_one()


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        d1, d2 = (await db.execute(select(Department).order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = d1, d2
        S["P1"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()          # linked to every department
        S["P2"] = (await db.execute(select(Program).where(Program.code == "Ph.D(V)"))).scalar_one()       # linked to every department
        S["P_BAD"] = (await db.execute(select(Program).where(Program.code == "MFSc"))).scalar_one()       # no department links at all
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info="ZZTEST-student-management"))
        off = (await db.execute(select(CourseOffering).limit(1))).scalar_one()
        S["offering"], S["calO"], S["semO"] = off.id, off.calendar_id, off.semester_id
        # a calendar different from the offering's, with two of its semesters
        sems = (await db.execute(select(Semester).where(Semester.calendar_id != off.calendar_id).order_by(Semester.start_date).limit(2))).scalars().all()
        S["calX"], S["semX"], S["semY"] = sems[0].calendar_id, sems[0].id, sems[1].id
        S["calX_label"], S["calO_label"] = (await db.get(AcademicCalendar, S["calX"])).academic_year, (await db.get(AcademicCalendar, S["calO"])).academic_year
        await db.commit()
    S["super"] = create_access_token(str(admin_id), {"sid": str(rt)})
    for code in ("C1", "C2"):
        r = await _call("POST", "/admin/colleges", S["super"], json={"name": f"ZZTEST SM College {code}", "code": f"{_CODE_PFX}{code}{_TAG.upper()}"})
        assert r.status_code == 201, r.text
        S[code] = uuid.UUID(r.json()["id"])
    # Student edits require the College + Programme to be a mapped pair: map both test colleges to both programmes used below.
    for college in ("C1", "C2"):
        for prog in ("P1", "P2"):
            r = await _call("POST", f"/admin/colleges/{S[college]}/programs", S["super"], json={"program_id": str(S[prog].id)})
            assert r.status_code == 201, r.text

    D1, D2, P1, P2 = S["D1"].id, S["D2"].id, S["P1"].id, S["P2"].id
    prof = dict(date_of_birth=date(2000, 1, 1), gender="Male", blood_group="O+", father_name="ZZTEST Father", abc_id="ABC-SM", address="ZZTEST addr", mobile="9876543210", admission_year=2026)
    # students (legacy role STUDENT). S4 gets NO assignment row (Orientation-style account) and no college.
    await _mk_user("S1", UserRole.STUDENT, D1, program_id=P1, college_id=S["C2"], academic_year_id=S["calX"], student_roll=f"ZZTEST-SM-1-{_TAG}", **prof); await _grant("S1", "student")
    await _mk_user("S2", UserRole.STUDENT, D2, program_id=P1, college_id=S["C2"], student_roll=f"ZZTEST-SM-2-{_TAG}", **prof); await _grant("S2", "student")
    await _mk_user("S3", UserRole.STUDENT, D1, program_id=P2, college_id=S["C1"], academic_year_id=S["calO"], student_roll=f"ZZTEST-SM-3-{_TAG}", **prof); await _grant("S3", "student")
    await _mk_user("S4", UserRole.STUDENT, D2, program_id=P1, student_roll=f"ZZTEST-SM-4-{_TAG}", **prof)              # no assignment row
    await _mk_user("S5", UserRole.STUDENT, D2, program_id=P1, college_id=S["C1"], academic_year_id=S["calX"], student_roll=f"ZZTEST-SM-5-{_TAG}", **prof); await _grant("S5", "student")
    # staff: ST = FACULTY@D1 + HOD@D2; SS = FACULTY@D1 + STUDENT (staff who is also a student)
    await _mk_user("ST", UserRole.FACULTY, D1, designation="Professor", employee_id=f"ZZTEST-EMP-{_TAG}")
    await _grant("ST", "faculty", D1); await _grant("ST", "hod", D2)
    await _mk_user("SS", UserRole.FACULTY, D1, program_id=P1, student_roll=f"ZZTEST-SM-SS-{_TAG}"); await _grant("SS", "faculty", D1); await _grant("SS", "student")
    await _mk_user("HOD", UserRole.HOD, D1); await _grant("HOD", "hod", D1)
    await _mk_user("FAC", UserRole.FACULTY, D1); await _grant("FAC", "faculty", D1)
    for n in ("HOD", "FAC", "S1"):
        await _session(n)

    async with AsyncSessionLocal() as db:
        # academic records: S1 reg in (calX, semX); S3 regs in (calX, semX) AND (calX, semY); S2 enrolled in the offering's (calO, semO)
        db.add(CourseRegistration(student_id=U["S1"], semester_id=S["semX"], calendar_id=S["calX"]))
        db.add(CourseRegistration(student_id=U["S3"], semester_id=S["semX"], calendar_id=S["calX"]))
        db.add(CourseRegistration(student_id=U["S3"], semester_id=S["semY"], calendar_id=S["calX"]))
        db.add(StudentEnrollment(student_id=U["S2"], offering_id=S["offering"], status="pending"))
        # Orientation candidates: each mirrors its student's roll number but names a DIFFERENT college than the account's own
        # (S5 own C1 / candidate C2; S1 own C2 / candidate C1) — the candidate's college must never be used.
        for n, college in (("S5", S["C2"]), ("S1", S["C1"])):
            u = await db.get(User, U[n])
            db.add(OrientationCandidate(personal_email=f"{_PFX}cand_{n.lower()}_{_TAG}@example.com", first_name="ZZTEST", last_name=n, academic_year="ZZTEST",
                                        program_id=P1, department_id=u.department_id, college_id=college, roll_no=u.student_roll, student_user_id=u.id))
        await db.commit()


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        ids = list(U.values())
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.student_id.in_(ids)))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.student_id.in_(ids)))
        await db.execute(delete(OrientationCandidate).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == "ZZTEST-student-management"))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.execute(delete(College).where(College.code.like(f"{_CODE_PFX}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


# ── 1. User Management excludes students ────────────────────────────────────

async def t_user_directory_excludes_students():
    r = await _call("GET", "/auth/users", S["super"]); assert r.status_code == 200
    rows = r.json()
    assert not (_ours(rows) & {"S1", "S2", "S3", "S4", "S5"}), f"students must not be in the staff directory: {_ours(rows)}"
    assert {"ST", "SS", "HOD", "FAC"} <= _ours(rows), "staff (including a staff member who is also a student) stay listed"
    assert _count(rows, "ST") == 1 and _count(rows, "SS") == 1, "multi-role staff appear exactly once"
    st = next(x for x in rows if x["id"] == str(U["ST"]))
    assert sorted((a["role"], a["department_id"]) for a in st["assigned_role_assignments"]) == sorted([("faculty", str(S["D1"].id)), ("hod", str(S["D2"].id))])
    for role in ("faculty", "hod"):
        assert _ours((await _call("GET", "/auth/users", S["super"], params={"role": role})).json()) & {"S1", "S2", "S3", "S4", "S5"} == set()
    assert (await _call("GET", "/auth/users", S["super"], params={"role": "student"})).status_code == 400
    hod_rows = (await _call("GET", "/auth/users", TOK["HOD"])).json()
    assert not (_ours(hod_rows) & {"S1", "S2", "S3", "S4", "S5"}), "a HOD's directory must not list students either"
    assert (await _call("GET", "/auth/users", TOK["HOD"], params={"role": "student"})).status_code == 400


# ── 2. Students API is Super Admin only ─────────────────────────────────────

async def t_students_api_is_super_admin_only():
    sid = str(U["S1"])
    for who in ("HOD", "FAC", "S1", None):
        tok = TOK.get(who) if who else None
        for method, url, kw in (("GET", "/students", {}), ("GET", f"/students/{sid}", {}), ("PATCH", f"/students/{sid}", {"json": {"first_name": "Hacked"}})):
            r = await _call(method, url, tok, **kw)
            assert r.status_code in ((403,) if who else (401, 403)), f"{who} {method} {url} -> {r.status_code}"
    assert (await _db_user("S1")).first_name == "ZZTEST", "a rejected request must not modify the student"
    assert (await _call("GET", "/students", S["super"])).status_code == 200


# ── 3. Listing, filters, pagination, search ─────────────────────────────────

async def t_list_all_students_no_duplicates():
    data = await _students()
    rows = data["items"]
    assert {"S1", "S2", "S3", "S4", "S5", "SS"} <= _ours(rows), _ours(rows)
    assert not (_ours(rows) & {"ST", "HOD", "FAC"}), "staff-only users are not students"
    assert all(_count(rows, n) == 1 for n in ("S1", "S2", "S3", "S4", "S5", "SS")), "each student appears once (S3 has two registrations)"
    assert len({r["id"] for r in rows}) == len(rows) and data["total"] == len(rows)
    s4 = next(r for r in rows if r["id"] == str(U["S4"]))
    assert s4["student_roll"].startswith("ZZTEST-SM-4") and s4["program_code"] == "MVSc" and s4["department_name"] == S["D2"].name
    s3 = next(r for r in rows if r["id"] == str(U["S3"]))
    assert s3["academic_year_id"] == str(S["calO"]) and s3["academic_year"] == S["calO_label"], "Academic Year is the ASSIGNED calendar (S3 is registered in calX but assigned calO)"
    assert s3["latest_semester"], "the latest semester is still derived from registrations"
    assert "latest_academic_year" not in s3, "Academic Year has one meaning: the assigned calendar"
    assert s4["academic_year_id"] is None and s4["academic_year"] is None, "an unassigned student has no Academic Year"


async def t_each_filter_independently():
    D1, D2, P2, C1, C2 = S["D1"].id, S["D2"].id, S["P2"].id, S["C1"], S["C2"]
    assert _ours((await _students(department_id=str(D1)))["items"]) >= {"S1", "S3", "SS"} and not (_ours((await _students(department_id=str(D1)))["items"]) & {"S2", "S4", "S5"})
    assert {"S2", "S4", "S5"} <= _ours((await _students(department_id=str(D2)))["items"]) and not (_ours((await _students(department_id=str(D2)))["items"]) & {"S1", "S3"})
    assert _ours((await _students(program_id=str(P2)))["items"]) == {"S3"}
    got = _ours((await _students(college_id=str(C1)))["items"])
    assert got == {"S3", "S5"}, f"College C1 = User.college_id only: S3, S5 (NOT S1, whose candidate says C1 but whose account says C2): {got}"
    assert _ours((await _students(college_id=str(C2)))["items"]) == {"S1", "S2"}, "College C2: S1, S2 (NOT S5, whose candidate says C2)"
    cal = _ours((await _students(academic_year_id=str(S["calX"])))["items"])
    assert cal == {"S1", "S5"}, f"Academic Year = User.academic_year_id: S1 and S5 (S5 has no registrations; S3 is REGISTERED in calX but assigned calO): {cal}"
    assert _ours((await _students(academic_year_id=str(S["calO"])))["items"]) == {"S3"}
    assert _ours((await _students(semester_id=str(S["semX"])))["items"]) == {"S1", "S3"}
    assert _ours((await _students(semester_id=str(S["semY"])))["items"]) == {"S3"}
    assert _ours((await _students(semester_id=str(S["semO"])))["items"]) & {"S1", "S2", "S3"} == {"S2"}, "S2 has an ENROLLMENT (no registration) in this semester"
    assert _count((await _students(semester_id=str(S["semX"])))["items"], "S3") == 1, "two registrations must not duplicate the student"


async def t_filters_combine_with_and():
    D1, D2, P2, C1 = S["D1"].id, S["D2"].id, S["P2"].id, S["C1"]
    assert _ours((await _students(department_id=str(D1), program_id=str(P2)))["items"]) == {"S3"}
    assert _ours((await _students(department_id=str(D2), college_id=str(C1), academic_year_id=str(S["calX"])))["items"]) == {"S5"}
    assert _ours((await _students(department_id=str(D1), college_id=str(C1), academic_year_id=str(S["calX"])))["items"]) == set(), "S3 is in D1/C1 but assigned calO"
    assert _ours((await _students(department_id=str(D2), college_id=str(C1), semester_id=str(S["semX"])))["items"]) == set()
    assert _ours((await _students(program_id=str(P2), semester_id=str(S["semY"]), college_id=str(C1), department_id=str(D1), academic_year_id=str(S["calO"])))["items"]) == {"S3"}
    assert _ours((await _students(academic_year_id=str(S["calX"]), semester_id=str(S["semX"])))["items"]) == {"S1"}, "assigned calX AND registered in semX"
    assert _ours((await _students(academic_year_id=str(S["calX"]), semester_id=str(S["semY"])))["items"]) == set()


async def t_search_and_pagination():
    assert _ours((await _students(q=f"ZZTEST-SM-2-{_TAG}"))["items"]) == {"S2"}, "roll number search"
    assert _ours((await _students(q=f"{_PFX}s3_{_TAG}"))["items"]) == {"S3"}, "email search"
    assert _ours((await _students(q="zztest s5"))["items"]) == {"S5"}, "full-name search (first + last)"
    assert _ours((await _students(q="ZZTEST-SM-"))["items"]) >= {"S1", "S2", "S3", "S4", "S5", "SS"}
    seen, total = [], None
    for page in (1, 2, 3, 4):
        r = await _call("GET", "/students", S["super"], params={"q": "ZZTEST-SM-", "page": page, "page_size": 2}); assert r.status_code == 200, r.text
        body = r.json(); total = total or body["total"]; assert body["total"] == total and len(body["items"]) <= 2
        seen += [x["id"] for x in body["items"]]
    assert len(seen) == len(set(seen)) == total == 6, f"pages must partition the result (no overlap): {len(seen)} / {total}"
    assert (await _call("GET", "/students", S["super"], params={"page_size": 1000})).status_code == 422, "page size is capped"


async def t_student_detail_and_staff_are_not_students():
    r = await _call("GET", f"/students/{U['S1']}", S["super"]); assert r.status_code == 200
    d = r.json()
    for f in ("email", "first_name", "middle_name", "last_name", "student_roll", "mobile", "date_of_birth", "gender", "blood_group", "father_name", "abc_id", "address",
              "admission_year", "program_id", "department_id", "college_id", "is_active", "academic_year_id", "academic_year", "latest_semester"):
        assert f in d, f"detail is missing {f}"
    assert d["college_id"] == str(S["C2"]) and "college_from_orientation" not in d
    d4 = (await _call("GET", f"/students/{U['S4']}", S["super"])).json()
    assert d4["college_id"] is None and d4["college_name"] is None, "a student without a college has none"
    assert (await _call("GET", f"/students/{U['ST']}", S["super"])).status_code == 404, "a staff-only user is not a student"
    assert (await _call("GET", f"/students/{uuid.uuid4()}", S["super"])).status_code == 404


# ── 4. Editing a student ────────────────────────────────────────────────────

async def t_edit_every_field_and_roll_sync():
    new_roll = f"ZZTEST-SM-1-EDITED-{_TAG}"
    body = {
        "email": f"{_PFX}s1_edited_{_TAG}@avfu.ac.in", "first_name": "Edited", "middle_name": "Mid", "last_name": "Student",
        "student_roll": new_roll, "mobile": "+91 98765-43210", "date_of_birth": "1999-05-17", "gender": "female", "blood_group": "ab-",
        "father_name": "Edited Father", "abc_id": "ABC-EDITED", "address": "Edited address\nline 2", "admission_year": 2025,
        "program_id": str(S["P2"].id), "department_id": str(S["D2"].id), "college_id": str(S["C1"]), "is_active": True,
    }
    r = await _call("PATCH", f"/students/{U['S1']}", S["super"], json=body); assert r.status_code == 200, r.text
    d = (await _call("GET", f"/students/{U['S1']}", S["super"])).json()
    expect = {**body, "gender": "Female", "blood_group": "AB-", "email": body["email"].lower()}
    for k, v in expect.items():
        assert d[k] == v, f"{k}: {d[k]!r} != {v!r}"
    assert d["program_name"] == S["P2"].name and d["department_name"] == S["D2"].name and d["college_name"] == "ZZTEST SM College C1"
    u = await _db_user("S1")
    assert u.role == UserRole.STUDENT, "editing a student never changes their role"
    async with AsyncSessionLocal() as db:
        cand = (await db.execute(select(OrientationCandidate).where(OrientationCandidate.student_user_id == U["S1"]))).scalar_one()
    assert cand.roll_no == new_roll, "the Orientation copy of the roll number must follow the canonical User.student_roll"
    # clearing optional fields, and clearing the college (leaves NO college — S1's candidate names C1 but is never consulted)
    r = await _call("PATCH", f"/students/{U['S1']}", S["super"], json={"abc_id": None, "address": "  ", "middle_name": "", "gender": None, "college_id": None})
    assert r.status_code == 200, r.text
    d = (await _call("GET", f"/students/{U['S1']}", S["super"])).json()
    assert d["abc_id"] is None and d["address"] is None and d["middle_name"] is None and d["gender"] is None
    assert d["college_id"] is None and d["college_name"] is None, "cleared college stays cleared; no Orientation fallback"
    # the student can still authenticate, and is still a student
    me = await _call("GET", "/auth/me", TOK["S1"]); assert me.status_code == 200 and me.json()["active_role"] == "student"
    # status: deactivate -> cannot authenticate; reactivate
    assert (await _call("PATCH", f"/students/{U['S1']}", S["super"], json={"is_active": False})).status_code == 200
    assert (await _call("GET", "/auth/me", TOK["S1"])).status_code == 401
    assert (await _call("PATCH", f"/students/{U['S1']}", S["super"], json={"is_active": True})).status_code == 200
    assert (await _call("GET", "/auth/me", TOK["S1"])).status_code == 200


async def t_edit_validation_rejects_bad_input_and_changes_nothing():
    before = await _db_user("S2")
    snap = lambda u: (u.email, u.first_name, u.student_roll, u.program_id, u.department_id, u.college_id, u.gender, u.date_of_birth, u.mobile, u.admission_year)
    ghost = str(uuid.uuid4())
    cases = [
        ({"program_id": ghost}, 400, "unknown programme"), ({"department_id": ghost}, 400, "unknown department"), ({"college_id": ghost}, 400, "unknown college"),
        ({"program_id": str(S["P_BAD"].id)}, 400, "programme not associated with the student's department"),
        ({"program_id": str(S["P_BAD"].id), "department_id": str(S["D1"].id)}, 400, "invalid programme/department pair"),
        ({"student_roll": f"ZZTEST-SM-3-{_TAG}"}, 409, "duplicate roll number"),
        ({"email": f"{_PFX}s3_{_TAG}@avfu.ac.in"}, 409, "duplicate email"),
        ({"first_name": "  "}, 422, "blank first name"), ({"last_name": None}, 422, "null last name"), ({"email": None}, 422, "null email"),
        ({"email": "not-an-email"}, 422, "bad email"), ({"student_roll": ""}, 422, "blank roll"), ({"student_roll": "x" * 51}, 422, "roll too long"),
        ({"program_id": None}, 422, "clearing programme"), ({"department_id": None}, 422, "clearing department"), ({"is_active": None}, 422, "null status"),
        ({"gender": "robot"}, 422, "bad gender"), ({"blood_group": "Z+"}, 422, "bad blood group"),
        ({"date_of_birth": "2999-01-01"}, 422, "future date of birth"), ({"date_of_birth": "not-a-date"}, 422, "bad date"),
        ({"mobile": "call me"}, 422, "bad mobile"), ({"admission_year": 1800}, 422, "implausible admission year"),
        ({"program_id": "not-a-uuid"}, 422, "malformed uuid"),
    ]
    for body, code, why in cases:
        r = await _call("PATCH", f"/students/{U['S2']}", S["super"], json=body)
        assert r.status_code == code, f"{why}: expected {code}, got {r.status_code} {r.text[:120]}"
    assert snap(await _db_user("S2")) == snap(before), "no rejected request may change the student"
    assert (await _call("PATCH", f"/students/{U['S2']}", S["super"], json={"program_id": str(S["P2"].id), "department_id": str(S["D1"].id)})).status_code == 200, "a VALID pair is accepted"


# ── 5. Editing a staff user (User Management) ───────────────────────────────

async def t_edit_user_full_profile_and_roles_untouched():
    async with AsyncSessionLocal() as db:
        before_roles = sorted((a.role.value, str(a.department_id)) for a in (await db.execute(select(UserRoleAssignment).where(UserRoleAssignment.user_id == U["ST"]))).scalars().all())
    body = {
        "title": "dr.", "first_name": "Staff", "middle_name": "M", "last_name": "Edited", "designation": "Associate Professor", "employee_id": f"ZZTEST-EMP-EDITED-{_TAG}",
        "mobile": "9123456789", "date_of_birth": "1980-02-03", "gender": "Other", "blood_group": "b+", "father_name": "Staff Father", "abc_id": "ABC-STAFF",
        "address": "Staff address", "college_id": str(S["C1"]),
    }
    r = await _call("PATCH", f"/auth/users/{U['ST']}", S["super"], json=body); assert r.status_code == 200, r.text
    row = next(x for x in (await _call("GET", "/auth/users", S["super"])).json() if x["id"] == str(U["ST"]))
    exp = {**body, "title": "Dr.", "blood_group": "B+"}
    for k, v in exp.items():
        assert row[k] == v, f"{k}: {row[k]!r} != {v!r}"
    async with AsyncSessionLocal() as db:
        after_roles = sorted((a.role.value, str(a.department_id)) for a in (await db.execute(select(UserRoleAssignment).where(UserRoleAssignment.user_id == U["ST"]))).scalars().all())
    assert after_roles == before_roles == sorted([("faculty", str(S["D1"].id)), ("hod", str(S["D2"].id))]), "profile edits never touch role assignments"
    assert len([1 for x in (await _call("GET", "/auth/users", S["super"])).json() if x["id"] == str(U["ST"])]) == 1
    # clearing, and fields that may not be cleared
    assert (await _call("PATCH", f"/auth/users/{U['ST']}", S["super"], json={"address": None, "abc_id": "", "role": None, "first_name": None, "email": None})).status_code == 200
    row = next(x for x in (await _call("GET", "/auth/users", S["super"])).json() if x["id"] == str(U["ST"]))
    assert row["address"] is None and row["abc_id"] is None and row["first_name"] == "Staff" and row["role"] == "faculty"
    for bad, code in (({"college_id": str(uuid.uuid4())}, 400), ({"gender": "robot"}, 422), ({"blood_group": "Q"}, 422), ({"mobile": "abc"}, 422),
                      ({"date_of_birth": "2999-12-31"}, 422), ({"title": "King"}, 422)):
        assert (await _call("PATCH", f"/auth/users/{U['ST']}", S["super"], json=bad)).status_code == code, bad


async def t_user_edit_is_protected_and_not_for_students():
    for who in ("HOD", "FAC", "S1", None):
        r = await _call("PATCH", f"/auth/users/{U['ST']}", TOK.get(who) if who else None, json={"first_name": "Hacked"})
        assert r.status_code in ((403,) if who else (401, 403)), f"{who}: {r.status_code}"
    assert (await _db_user("ST")).first_name == "Staff"
    for name in ("S1", "S4"):
        r = await _call("PATCH", f"/auth/users/{U[name]}", S["super"], json={"first_name": "ViaUserMgmt"})
        assert r.status_code == 400, f"student {name} must not be editable through User Management: {r.status_code}"
    assert (await _db_user("S1")).first_name != "ViaUserMgmt"
    r = await _call("PATCH", f"/auth/users/{U['SS']}", S["super"], json={"designation": "Lecturer"})
    assert r.status_code == 200, "a staff member who is also a student stays editable as staff"


async def t_student_authentication_intact():
    # S4 has NO assignment rows (Orientation-style): it is still a student, and first login self-heals it.
    assert _count((await _students())["items"], "S4") == 1
    assert _count((await _call("GET", "/auth/users", S["super"])).json(), "S4") == 0
    await _session("S4")
    me = await _call("GET", "/auth/me", TOK["S4"]); assert me.status_code == 200 and me.json()["active_role"] == "student", me.text
    assert _count((await _students())["items"], "S4") == 1 and _count((await _call("GET", "/auth/users", S["super"])).json(), "S4") == 0


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "USER MGMT: students absent from GET /auth/users for Super Admin and HOD; role=student -> 400; staff (and staff who are also students) listed once with paired assignments": t_user_directory_excludes_students,
            "SECURITY: /students list, detail and edit -> 403 for HOD, Faculty, Student; 401/403 unauthenticated; nothing modified": t_students_api_is_super_admin_only,
            "STUDENTS: Super Admin lists all students (incl. an account with no assignment row), each once, staff-only excluded": t_list_all_students_no_duplicates,
            "STUDENTS: department, programme, college (User.college_id), academic year (User.academic_year_id) and semester filters each work alone": t_each_filter_independently,
            "STUDENTS: filters combine with AND; no duplicates with several matching records": t_filters_combine_with_and,
            "STUDENTS: search (roll, email, full name) and pagination (no overlap, capped page size)": t_search_and_pagination,
            "STUDENTS: detail carries every profile field; a staff-only id is 404": t_student_detail_and_staff_are_not_students,
            "STUDENT EDIT: every field incl. roll number, programme, department, college; Orientation roll copy synced; clearing; role and login unaffected": t_edit_every_field_and_roll_sync,
            "STUDENT EDIT: unknown ids, invalid programme/department pair, duplicates, blanks, bad formats -> rejected; nothing changed; valid pair accepted": t_edit_validation_rejects_bad_input_and_changes_nothing,
            "USER EDIT: every profile field, clearing, validation; role assignments untouched": t_edit_user_full_profile_and_roles_untouched,
            "USER EDIT: protected from HOD/Faculty/Student; students not editable via User Management": t_user_edit_is_protected_and_not_for_students,
            "AUTH: an assignment-less student is still a student and can log in": t_student_authentication_intact,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_PFX}%")))).scalars().all(),
                "colleges": (await db.execute(select(College.id).where(College.code.like(f"{_CODE_PFX}%")))).scalars().all(),
                "candidates": (await db.execute(select(OrientationCandidate.id).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))).scalars().all(),
                "sessions": (await db.execute(select(RefreshToken.id).where(RefreshToken.device_info == "ZZTEST-student-management"))).scalars().all(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        RESULTS.record("cleanup: no ZZTEST_STUDENT_MANAGEMENT user / college / candidate / session / assignment remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
