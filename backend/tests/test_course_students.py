"""Standalone HTTP-level tests for the Course Students task: DPGS and Incharge
Academic Cell get global (cross-department), READ-ONLY access to course
offerings and their enrolled-student rosters, via the existing
`GET /courses/offerings/all` and `GET /enrollment/offering/{id}` endpoints —
reused unchanged except for `offering_enrollments`'s new dedicated read-only
branch (`backend/app/api/v1/endpoints/enrollment.py`). HOD keeps its
pre-existing department-scoped access; neither new role gains any write
capability on `PATCH /enrollment/{id}` or `POST /offering/{id}/bulk-approve`.

Modelled on `tests/test_advisory_committee_document.py` (real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `record`/`_run` pass-fail
tracker, `zztest_cst_...`-prefixed rows, full teardown, the same
single-holder-role STOP guard for INCHARGE_ACADEMIC_CELL/DPGS).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_course_students
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.course import Course, CourseOffering
from app.models.enrollment import StudentEnrollment
from app.models.academic import AcademicCalendar, Semester
from app.models.user import Department, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_cst_"
_DEVICE = "ZZTEST-cst"

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


async def _session(user_id) -> str:
    async with AsyncSessionLocal() as db:
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=user_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(),
                             device_info=_DEVICE))
        await db.commit()
    return create_access_token(str(user_id), {"sid": str(rt)})


async def _mk_user(key, role, dept, *, roll=None) -> None:
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"CST{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()
    TOK[key] = await _session(U[key])


async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS holder exists (single-holder roles); refusing to run.")
            sys.exit(2)
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_cst_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        assert len(depts) >= 2, "need at least 2 existing Department rows in the dev DB"
        S["D1"], S["D2"] = depts
        cal = (await db.execute(select(AcademicCalendar).order_by(AcademicCalendar.start_date.desc()).limit(1))).scalar_one_or_none()
        assert cal is not None, "need at least 1 existing AcademicCalendar row in the dev DB"
        S["CAL"] = cal
        sem = (await db.execute(select(Semester).where(Semester.calendar_id == cal.id).limit(1))).scalar_one_or_none()
        assert sem is not None, "need at least 1 existing Semester row for that calendar in the dev DB"
        S["SEM"] = sem

        # Two offerings of two DIFFERENT courses, in two DIFFERENT departments
        # — the whole point of "global, cross-department" visibility.
        c1 = Course(course_number=f"ZZCST1-{_TAG}", title="ZZTEST Course Students A", department_id=S["D1"].id,
                    credit_theory=3, credit_practical=0, program_level="PG")
        c2 = Course(course_number=f"ZZCST2-{_TAG}", title="ZZTEST Course Students B", department_id=S["D2"].id,
                    credit_theory=2, credit_practical=1, program_level="PG")
        db.add_all([c1, c2])
        await db.flush()
        o1 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=c1.id, department_id=S["D1"].id, status="published")
        o2 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=c2.id, department_id=S["D2"].id, status="published")
        o_empty = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=c1.id, department_id=S["D1"].id, section="Z", status="published")
        db.add_all([o1, o2, o_empty])
        await db.flush()
        S["OFF1"], S["OFF2"], S["OFF_EMPTY"] = o1.id, o2.id, o_empty.id
        await db.commit()

    d1, d2 = S["D1"], S["D2"]
    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("hod2", UserRole.HOD, d2)
    await _mk_user("outsider_faculty", UserRole.FACULTY, d1)
    await _mk_user("super_admin_probe", UserRole.SUPER_ADMIN, None)
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None)
    await _mk_user("dpgs", UserRole.DPGS, None)

    await _mk_user("s1_approved", UserRole.STUDENT, d1, roll=f"ZZCST-s1-{_TAG}")
    await _mk_user("s2_approved", UserRole.STUDENT, d2, roll=f"ZZCST-s2-{_TAG}")
    await _mk_user("s3_pending", UserRole.STUDENT, d1, roll=f"ZZCST-s3-{_TAG}")
    await _mk_user("s4_withdrawn", UserRole.STUDENT, d1, roll=f"ZZCST-s4-{_TAG}")

    async with AsyncSessionLocal() as db:
        db.add_all([
            StudentEnrollment(student_id=U["s1_approved"], offering_id=S["OFF1"], status="approved"),
            StudentEnrollment(student_id=U["s2_approved"], offering_id=S["OFF2"], status="approved"),
            StudentEnrollment(student_id=U["s3_pending"], offering_id=S["OFF1"], status="pending"),
            StudentEnrollment(student_id=U["s4_withdrawn"], offering_id=S["OFF1"], status="withdrawn"),
        ])
        await db.commit()


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(Course.id).where(Course.course_number.like(f"ZZCST%-{_TAG}"))
        oids = select(CourseOffering.id).where(CourseOffering.course_id.in_(cids))
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(oids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(oids)))
        await db.execute(delete(Course).where(Course.id.in_(cids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── 1/2: DPGS / Incharge can browse offerings across multiple departments ───

async def t_dpgs_browses_offerings_across_departments():
    r = await _call("GET", "/courses/offerings/all", TOK["dpgs"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(S["OFF1"]) in ids and str(S["OFF2"]) in ids, "DPGS must see offerings from BOTH departments, unrestricted"


async def t_incharge_browses_offerings_across_departments():
    r = await _call("GET", "/courses/offerings/all", TOK["incharge"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(S["OFF1"]) in ids and str(S["OFF2"]) in ids, "Incharge Academic Cell must see offerings from BOTH departments, unrestricted"


# ── 3/4: DPGS / Incharge can retrieve enrolled-student rosters ──────────────

async def t_dpgs_can_retrieve_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["dpgs"])
    assert r.status_code == 200, r.text
    names = {row["student_name"] for row in r.json()}
    assert "ZZTEST CSTS1_APPROVED" in {n.upper() for n in names if n}, r.text


async def t_incharge_can_retrieve_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF2']}", TOK["incharge"])
    assert r.status_code == 200, r.text
    names = {row["student_name"] for row in r.json()}
    assert "ZZTEST CSTS2_APPROVED" in {n.upper() for n in names if n}, r.text


# ── 5/6: only approved enrollments appear; pending/withdrawn excluded ───────

async def t_only_approved_enrollments_in_dpgs_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["dpgs"])
    assert r.status_code == 200, r.text
    rows = r.json()
    statuses = {row["status"] for row in rows}
    assert statuses <= {"approved"}, f"DPGS roster must contain ONLY approved rows, got statuses: {statuses}"
    names = {(row["student_name"] or "").upper() for row in rows}
    assert "ZZTEST CSTS1_APPROVED" in names
    assert "ZZTEST CSTS3_PENDING" not in names, "pending enrollment must not appear in the roster"
    assert "ZZTEST CSTS4_WITHDRAWN" not in names, "withdrawn enrollment must not appear in the roster"


async def t_dpgs_cannot_widen_roster_via_status_param():
    # Even if DPGS explicitly asks for every status, the backend must still
    # hard-pin to "approved" — this is the roster, not a general enrollment list.
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["dpgs"], params={"status": "pending"})
    assert r.status_code == 200, r.text
    names = {(row["student_name"] or "").upper() for row in r.json()}
    assert "ZZTEST CSTS3_PENDING" not in names, "a crafted status= param must not widen DPGS's roster beyond approved"


# ── 7: offering with no approved enrollments returns a valid empty roster ───

async def t_empty_offering_returns_empty_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF_EMPTY']}", TOK["dpgs"])
    assert r.status_code == 200 and r.json() == [], r.text
    r2 = await _call("GET", f"/enrollment/offering/{S['OFF_EMPTY']}", TOK["incharge"])
    assert r2.status_code == 200 and r2.json() == [], r2.text


# ── 8: nonexistent offering -> 404 ───────────────────────────────────────────

async def t_nonexistent_offering_is_404():
    r = await _call("GET", f"/enrollment/offering/{uuid.uuid4()}", TOK["dpgs"])
    assert r.status_code == 404, (r.status_code, r.text)
    r2 = await _call("GET", f"/enrollment/offering/{uuid.uuid4()}", TOK["incharge"])
    assert r2.status_code == 404, (r2.status_code, r2.text)


# ── 9: HOD access remains restricted to their own department ────────────────

async def t_hod_sees_only_own_department_offerings():
    r = await _call("GET", "/courses/offerings/all", TOK["hod1"])
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(S["OFF1"]) in ids, "HOD1 must see their own department's offering"
    assert str(S["OFF2"]) not in ids, "HOD1 must NOT see a different department's offering"


async def t_hod_cannot_access_foreign_department_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF2']}", TOK["hod1"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_hod_can_access_own_department_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["hod1"])
    assert r.status_code == 200, r.text


# ── 10: unauthorized roles cannot retrieve rosters ───────────────────────────

async def t_outsider_faculty_cannot_access_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["outsider_faculty"])
    assert r.status_code in (403, 404), (r.status_code, r.text)


async def t_student_cannot_access_roster():
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["s1_approved"])
    assert r.status_code == 403, (r.status_code, r.text)


# ── 11/12/13: DPGS/Incharge get NO write capability ──────────────────────────

async def t_dpgs_cannot_patch_individual_enrollment():
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s3_pending"], StudentEnrollment.offering_id == S["OFF1"],
        ))).scalar_one()
    r = await _call("PATCH", f"/enrollment/{row.id}", TOK["dpgs"], json={"status": "approved"})
    assert r.status_code in (401, 403, 404, 405), f"DPGS must not be able to PATCH an enrollment — got {r.status_code} {r.text}"


async def t_incharge_cannot_patch_individual_enrollment():
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s3_pending"], StudentEnrollment.offering_id == S["OFF1"],
        ))).scalar_one()
    r = await _call("PATCH", f"/enrollment/{row.id}", TOK["incharge"], json={"status": "approved"})
    assert r.status_code in (401, 403, 404, 405), f"Incharge must not be able to PATCH an enrollment — got {r.status_code} {r.text}"


async def t_neither_role_gains_bulk_approve():
    r = await _call("POST", f"/enrollment/offering/{S['OFF1']}/bulk-approve", TOK["dpgs"], json={"enrollment_ids": []})
    assert r.status_code in (401, 403, 404, 405), f"DPGS must not be able to bulk-approve — got {r.status_code} {r.text}"
    r2 = await _call("POST", f"/enrollment/offering/{S['OFF1']}/bulk-approve", TOK["incharge"], json={"enrollment_ids": []})
    assert r2.status_code in (401, 403, 404, 405), f"Incharge must not be able to bulk-approve — got {r2.status_code} {r2.text}"
    # confirm the pending enrollment is genuinely untouched by either attempt
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s3_pending"], StudentEnrollment.offering_id == S["OFF1"],
        ))).scalar_one()
        assert row.status == "pending", f"pending enrollment must remain untouched, got status={row.status}"


# ── Sanity: Super Admin unaffected (pre-existing access, unchanged) ─────────

async def t_super_admin_unaffected():
    r = await _call("GET", f"/enrollment/offering/{S['OFF1']}", TOK["super_admin_probe"])
    assert r.status_code == 200, r.text


async def main() -> None:
    await _setup()
    try:
        cases = {
            "1: DPGS browses offerings across multiple departments": t_dpgs_browses_offerings_across_departments,
            "2: Incharge Academic Cell browses offerings across multiple departments": t_incharge_browses_offerings_across_departments,
            "3: DPGS can retrieve an enrolled-student roster": t_dpgs_can_retrieve_roster,
            "4: Incharge Academic Cell can retrieve an enrolled-student roster": t_incharge_can_retrieve_roster,
            "5/6: only approved enrollments appear; pending/withdrawn excluded": t_only_approved_enrollments_in_dpgs_roster,
            "DPGS cannot widen the roster via a crafted status= param": t_dpgs_cannot_widen_roster_via_status_param,
            "7: offering with no approved enrollments returns an empty roster": t_empty_offering_returns_empty_roster,
            "8: nonexistent offering returns 404": t_nonexistent_offering_is_404,
            "9a: HOD sees only their own department's offerings": t_hod_sees_only_own_department_offerings,
            "9b: HOD cannot access a foreign department's roster": t_hod_cannot_access_foreign_department_roster,
            "9c: HOD can still access their own department's roster": t_hod_can_access_own_department_roster,
            "10a: an outsider Faculty cannot access the roster": t_outsider_faculty_cannot_access_roster,
            "10b: a Student cannot access the roster": t_student_cannot_access_roster,
            "11: DPGS cannot PATCH an individual enrollment": t_dpgs_cannot_patch_individual_enrollment,
            "12: Incharge Academic Cell cannot PATCH an individual enrollment": t_incharge_cannot_patch_individual_enrollment,
            "13: neither role gains bulk-approve, enrollment stays untouched": t_neither_role_gains_bulk_approve,
            "Super Admin's pre-existing roster access is unaffected": t_super_admin_unaffected,
        }
        for name, fn in cases.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "courses": (await db.execute(select(func.count()).select_from(Course).where(Course.course_number.like(f"ZZCST%-{_TAG}")))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_CST row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
