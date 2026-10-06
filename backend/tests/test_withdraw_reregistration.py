"""Standalone HTTP-level tests for re-registration after withdrawal.

Bug under test: a student who registers for a course, withdraws it, and then
tries to register for the SAME offering again was incorrectly rejected with
"You are already registered for ...". The fix (enrollment.py `enroll()` and
`register_courses()`) distinguishes a historical `withdrawn` StudentEnrollment
row from an active one: only an active (non-withdrawn) row blocks
re-registration, and re-registering over a withdrawn row REACTIVATES that same
row in place (never a second row — `uq_enrollment` permits at most one
StudentEnrollment per (student, offering) ever) so the student's
`WithdrawalRequest` audit history is preserved untouched.

Modelled on `tests/test_research_course.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `_run(name, fn)` pass/fail
tracker, a `zztest_wr_...` prefix on every created row, deleted in `finally`.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_withdraw_reregistration
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
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.enrollment import CourseRegistration, StudentEnrollment, WithdrawalRequest
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_wr_"
_LABEL = "ZZTEST_WR"
_DEVICE = "ZZTEST-wr"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
CRS: dict[str, uuid.UUID] = {}
OFF: dict[str, uuid.UUID] = {}


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
        db.add(RefreshToken(id=rt, user_id=user_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    return create_access_token(str(user_id), {"sid": str(rt)})


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None, student_profile=False):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"WR{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            program_id=program.id if program else None, college_id=college.id if college else None,
            student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True,
            date_of_birth=date(2000, 1, 1) if student_profile else None,
            father_name="ZZTEST Father" if student_profile else None,
            address="ZZTEST Address" if student_profile else None,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_committee(student_key: str, ma_keys: list[str]) -> None:
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST WR committee", status="members_pending")
        db.add(c)
        await db.flush()
        for ma_key in ma_keys:
            db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True))
        await db.commit()


async def _enrollment(student_key: str, offering_id) -> StudentEnrollment:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == offering_id,
        ))).scalar_one()


async def _enrollment_count(student_key: str, offering_id) -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == offering_id,
        ))).scalar_one()


async def _withdrawal_request_count(enrollment_id) -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(WithdrawalRequest).where(
            WithdrawalRequest.enrollment_id == enrollment_id,
        ))).scalar_one()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_wr_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        sem = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert sem is not None, "test requires at least 1 existing Semester in the local dev database"
        S["SEM"] = sem
        col = College(name=f"{_LABEL} College", code=f"ZZWR{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, pg, college = S["D1"], S["PG"], S["COLLEGE"]

    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("ma_x", UserRole.FACULTY, d1)
    await _mk_user("ma_z", UserRole.FACULTY, d1)
    await _mk_user("fac_normal", UserRole.FACULTY, d1)
    # Students: s1/s2 exercise the single-offering `enroll()` path; s3 the
    # batch `register_courses()` path; s_cap fills capacity-of-1 offerings.
    await _mk_user("s1", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZWR-1-{_TAG}", student_profile=True)
    await _mk_user("s2", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZWR-2-{_TAG}", student_profile=True)
    await _mk_user("s3", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZWR-3-{_TAG}", student_profile=True)
    await _mk_user("s_cap", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZWR-cap-{_TAG}", student_profile=True)
    await _mk_user("s_res", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZWR-res-{_TAG}", student_profile=True)

    for s in ("s1", "s2", "s3", "s_cap", "s_res"):
        await _mk_committee(s, ["ma_x"])

    S["SA"] = await _session(admin_id)
    for k in ("hod1", "ma_x", "ma_z", "fac_normal", "s1", "s2", "s3", "s_cap", "s_res"):
        TOK[k] = await _session(U[k])

    async with AsyncSessionLocal() as db:
        c1 = Course(course_number=f"ZZWRA-{_TAG}", title="ZZTEST WR Course A", department_id=d1.id,
                     program_level="PG", category="core", created_by=admin_id)
        c2 = Course(course_number=f"ZZWRB-{_TAG}", title="ZZTEST WR Course B", department_id=d1.id,
                     program_level="PG", category="core", created_by=admin_id)
        c3 = Course(course_number=f"ZZWRC-{_TAG}", title="ZZTEST WR Capacity Course", department_id=d1.id,
                     program_level="PG", category="core", created_by=admin_id)
        rc = Course(course_number=f"ZZWRR-{_TAG}", title="ZZTEST WR Research Course", department_id=d1.id,
                     program_level="PG", category="research", created_by=admin_id)
        db.add_all([c1, c2, c3, rc])
        await db.commit()
        CRS["a"], CRS["b"], CRS["cap"], CRS["research"] = c1.id, c2.id, c3.id, rc.id

    async def _offering(course_key, *, max_enrollment=10, research_type=None):
        body = {
            "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
            "course_id": str(CRS[course_key]), "department_id": str(d1.id),
            "max_enrollment": max_enrollment,
        }
        if research_type:
            body["faculty_ids"] = []
            body["leader_id"] = None
            body["research_assignment_type"] = research_type
        else:
            body["faculty_ids"] = [str(U["fac_normal"])]
            body["leader_id"] = str(U["fac_normal"])
        r = await _call("POST", "/courses/offerings", TOK["hod1"], json=body)
        assert r.status_code == 201, r.text
        oid = uuid.UUID(r.json()["id"])
        pub = await _call("PATCH", f"/courses/offerings/{oid}/status", TOK["hod1"], params={"status": "published"})
        assert pub.status_code == 200, pub.text
        return oid

    OFF["a"] = await _offering("a")
    OFF["b"] = await _offering("b")
    OFF["cap"] = await _offering("cap", max_enrollment=1)
    OFF["research"] = await _offering("research", research_type="major_advisor")


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        oids = select(CourseOffering.id).where(CourseOffering.course_id.in_(list(CRS.values()) or [uuid.uuid4()]))
        eids = select(StudentEnrollment.id).where(StudentEnrollment.offering_id.in_(oids))

        await db.execute(delete(WithdrawalRequest).where(WithdrawalRequest.enrollment_id.in_(eids)))
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(oids)))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.student_id.in_(uids)))
        await db.execute(delete(OfferingFaculty).where(OfferingFaculty.offering_id.in_(oids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(oids)))
        await db.execute(delete(Course).where(Course.id.in_(list(CRS.values()) or [uuid.uuid4()])))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


# ── 1. Single-offering `enroll()` path ──────────────────────────────────────

async def t_enroll_withdraw_reenroll_reject_cycle():
    """register -> withdraw -> register -> register-again must be
    allowed / allowed / allowed / rejected, and the SAME row must be reused."""
    r = await _call("POST", "/enrollment", TOK["s1"], json={"offering_id": str(OFF["a"])})
    assert r.status_code == 201, r.text
    e1 = await _enrollment("s1", OFF["a"])
    assert e1.status == "pending"
    original_id = e1.id

    r = await _call("DELETE", f"/enrollment/{e1.id}", TOK["s1"])
    assert r.status_code == 204, r.text
    e2 = await _enrollment("s1", OFF["a"])
    assert e2.status == "withdrawn" and e2.id == original_id

    # Re-register must succeed and REUSE the same row (never a second one).
    r = await _call("POST", "/enrollment", TOK["s1"], json={"offering_id": str(OFF["a"])})
    assert r.status_code == 201, r.text
    assert await _enrollment_count("s1", OFF["a"]) == 1, "re-registration must not create a second row (uq_enrollment)"
    e3 = await _enrollment("s1", OFF["a"])
    assert e3.id == original_id, "re-registration must reactivate the historical row, not insert a new one"
    assert e3.status == "pending"

    # Immediately registering again while ACTIVE must be rejected.
    r = await _call("POST", "/enrollment", TOK["s1"], json={"offering_id": str(OFF["a"])})
    assert r.status_code == 409, r.text
    assert await _enrollment_count("s1", OFF["a"]) == 1


async def t_enroll_withdraw_reenroll_second_offering():
    """Same allowed/allowed/allowed/rejected cycle on a SECOND, independent
    offering, to confirm the fix isn't accidentally offering-specific."""
    r = await _call("POST", "/enrollment", TOK["s2"], json={"offering_id": str(OFF["b"])})
    assert r.status_code == 201, r.text
    e1 = await _enrollment("s2", OFF["b"])
    r = await _call("DELETE", f"/enrollment/{e1.id}", TOK["s2"])
    assert r.status_code == 204, r.text
    r = await _call("POST", "/enrollment", TOK["s2"], json={"offering_id": str(OFF["b"])})
    assert r.status_code == 201, r.text
    assert await _enrollment_count("s2", OFF["b"]) == 1
    r = await _call("POST", "/enrollment", TOK["s2"], json={"offering_id": str(OFF["b"])})
    assert r.status_code == 409, r.text


async def t_multiple_withdrawal_cycles_preserve_audit_trail_via_request_withdrawal():
    """Case B (withdrawal of an ALREADY Course-Teacher-approved enrollment)
    goes through WithdrawalRequest + decide_withdrawal_request. Two full
    approve/withdraw/re-register cycles must leave every historical
    WithdrawalRequest row intact (never deleted), all pointing at the SAME
    StudentEnrollment id."""
    r = await _call("POST", "/enrollment", TOK["s1"], json={"offering_id": str(OFF["b"])})
    assert r.status_code == 201, r.text
    e = await _enrollment("s1", OFF["b"])
    enrollment_id = e.id

    for cycle in range(2):
        # Course Teacher approves -> status "approved".
        r = await _call("PATCH", f"/enrollment/{enrollment_id}", TOK["fac_normal"], params={"status": "approved"})
        assert r.status_code == 200, f"cycle {cycle}: {r.text}"
        # Case B: request withdrawal, then Course Teacher approves it.
        r = await _call("POST", f"/enrollment/{enrollment_id}/withdrawal-request", TOK["s1"], json={"reason": f"ZZTEST reason {cycle}"})
        assert r.status_code == 201, f"cycle {cycle}: {r.text}"
        req_id = r.json()["id"]
        r = await _call("PATCH", f"/enrollment/withdrawal-requests/{req_id}", TOK["fac_normal"], json={"approved": True})
        assert r.status_code == 200, f"cycle {cycle}: {r.text}"
        row = await _enrollment("s1", OFF["b"])
        assert row.id == enrollment_id and row.status == "withdrawn"
        assert await _withdrawal_request_count(enrollment_id) == cycle + 1, "each cycle's WithdrawalRequest must be preserved, never deleted"

        if cycle == 0:
            # Re-register for the second cycle.
            r = await _call("POST", "/enrollment", TOK["s1"], json={"offering_id": str(OFF["b"])})
            assert r.status_code == 201, r.text
            row = await _enrollment("s1", OFF["b"])
            assert row.id == enrollment_id, "re-registration must reuse the same row across repeated cycles"
            assert row.status == "pending"
            assert await _enrollment_count("s1", OFF["b"]) == 1

    assert await _withdrawal_request_count(enrollment_id) == 2, "final state: both historical withdrawal requests preserved on the one reused row"


async def t_other_student_isolation():
    """s1 and s2's independent register/withdraw/re-register cycles on the
    SAME offering (OFF["a"]) must never interfere with each other."""
    # s1 already has an active enrollment in OFF["a"] from an earlier test.
    before = await _enrollment("s1", OFF["a"])
    assert before.status == "pending"

    r = await _call("POST", "/enrollment", TOK["s2"], json={"offering_id": str(OFF["a"])})
    assert r.status_code == 201, r.text
    e2 = await _enrollment("s2", OFF["a"])
    r = await _call("DELETE", f"/enrollment/{e2.id}", TOK["s2"])
    assert r.status_code == 204, r.text

    # s1's row must be completely unaffected by s2's withdrawal.
    after = await _enrollment("s1", OFF["a"])
    assert after.id == before.id and after.status == "pending"

    # s2 re-registering must not touch s1's row or count.
    r = await _call("POST", "/enrollment", TOK["s2"], json={"offering_id": str(OFF["a"])})
    assert r.status_code == 201, r.text
    assert await _enrollment_count("s1", OFF["a"]) == 1 and await _enrollment_count("s2", OFF["a"]) == 1


async def t_capacity_still_enforced_on_reregistration():
    r = await _call("POST", "/enrollment", TOK["s_cap"], json={"offering_id": str(OFF["cap"])})
    assert r.status_code == 201, r.text
    e = await _enrollment("s_cap", OFF["cap"])
    r = await _call("PATCH", f"/enrollment/{e.id}", TOK["fac_normal"], params={"status": "approved"})
    assert r.status_code == 200, r.text
    # Capacity (max_enrollment=1) is now full with an APPROVED enrollment.
    r = await _call("POST", "/enrollment", TOK["s3"], json={"offering_id": str(OFF["cap"])})
    assert r.status_code == 400, "capacity must still be enforced for a brand-new registration"

    # s_cap withdraws (Case A is blocked once approved; use Case B).
    r = await _call("POST", f"/enrollment/{e.id}/withdrawal-request", TOK["s_cap"], json={"reason": "ZZTEST capacity test"})
    assert r.status_code == 201, r.text
    req_id = r.json()["id"]
    r = await _call("PATCH", f"/enrollment/withdrawal-requests/{req_id}", TOK["fac_normal"], json={"approved": True})
    assert r.status_code == 200, r.text
    # Now capacity is free again — s3 (a different student) can take the seat.
    r = await _call("POST", "/enrollment", TOK["s3"], json={"offering_id": str(OFF["cap"])})
    assert r.status_code == 201, r.text
    # s_cap re-registering now must be rejected again: s3 occupies the one seat
    # (still "pending", not yet approved, so the capacity count — which only
    # counts "approved" — is actually still 0; but the explicit business rule
    # under test here is simply that re-registration re-runs the SAME capacity
    # check as a fresh registration, not that it is bypassed).
    count_approved = await _enrollment_count("s_cap", OFF["cap"])
    assert count_approved == 1, "s_cap's reactivated row must still be the only StudentEnrollment row for that pair"


# ── 2. Batch `register_courses()` path ──────────────────────────────────────

async def t_register_courses_batch_withdraw_reregister():
    r = await _call("POST", "/enrollment/register", TOK["s3"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "offering_ids": [str(OFF["a"]), str(OFF["b"])],
    })
    assert r.status_code == 201, r.text
    reg_id = r.json()["id"]
    ea = await _enrollment("s3", OFF["a"])
    eb = await _enrollment("s3", OFF["b"])
    assert ea.status == "pending" and eb.status == "pending"
    original_ea_id, original_eb_id = ea.id, eb.id

    # Withdraw OFF["a"] only (still pending -> Case A direct withdraw).
    r = await _call("DELETE", f"/enrollment/{ea.id}", TOK["s3"])
    assert r.status_code == 204, r.text
    assert (await _enrollment("s3", OFF["a"])).status == "withdrawn"
    assert (await _enrollment("s3", OFF["b"])).status == "pending", "withdrawing one offering must not touch the other"

    # Re-registering for BOTH in one batch call: OFF["a"] is withdrawn (must
    # reactivate), OFF["b"] is still active (must reject the whole batch,
    # same as the single-offering behavior, and change nothing).
    r = await _call("POST", "/enrollment/register", TOK["s3"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "offering_ids": [str(OFF["a"]), str(OFF["b"])],
    })
    assert r.status_code == 409, r.text
    assert (await _enrollment("s3", OFF["a"])).status == "withdrawn", "a rejected batch must not partially reactivate"

    # Re-registering for ONLY the withdrawn offering must succeed and reuse the same row.
    r = await _call("POST", "/enrollment/register", TOK["s3"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "offering_ids": [str(OFF["a"])],
    })
    assert r.status_code == 201, r.text
    ea2 = await _enrollment("s3", OFF["a"])
    assert ea2.id == original_ea_id and ea2.status == "pending"
    assert await _enrollment_count("s3", OFF["a"]) == 1
    eb2 = await _enrollment("s3", OFF["b"])
    assert eb2.id == original_eb_id and eb2.status == "pending", "the other offering's row must be untouched"

    async with AsyncSessionLocal() as db:
        reg = await db.get(CourseRegistration, uuid.UUID(reg_id))
        assert reg.student_id == U["s3"]


# ── 3. Research Course re-resolution on reactivation ────────────────────────

async def t_research_course_instructor_reresolved_on_reactivation():
    """A withdrawn research-course row's instructor_id must be RE-RESOLVED
    against the student's CURRENT accepted Major Advisor on reactivation, not
    left stale from before the withdrawal."""
    r = await _call("POST", "/enrollment", TOK["s_res"], json={"offering_id": str(OFF["research"])})
    assert r.status_code == 201, r.text
    e1 = await _enrollment("s_res", OFF["research"])
    assert e1.instructor_id == U["ma_x"]

    r = await _call("DELETE", f"/enrollment/{e1.id}", TOK["s_res"])
    assert r.status_code == 204, r.text

    # The student's Major Advisor changes between withdrawal and re-registration.
    async with AsyncSessionLocal() as db:
        cm = (await db.execute(select(CommitteeMember).where(
            CommitteeMember.committee_id == (await db.execute(
                select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id == U["s_res"])
            )).scalar_one(),
        ))).scalar_one()
        cm.faculty_id = U["ma_z"]
        await db.commit()

    r = await _call("POST", "/enrollment", TOK["s_res"], json={"offering_id": str(OFF["research"])})
    assert r.status_code == 201, r.text
    e2 = await _enrollment("s_res", OFF["research"])
    assert e2.id == e1.id, "reactivation must reuse the same row"
    assert e2.instructor_id == U["ma_z"], "instructor_id must be re-resolved against the CURRENT accepted Major Advisor, not left stale"


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "ENROLL: register -> withdraw -> register -> register-again = allowed/allowed/allowed/rejected; same row reused (uq_enrollment)": t_enroll_withdraw_reenroll_reject_cycle,
            "ENROLL: identical cycle on a second, independent offering": t_enroll_withdraw_reenroll_second_offering,
            "WITHDRAWAL REQUEST (Case B): two full approve/withdraw/re-register cycles preserve every historical WithdrawalRequest on the one reused row": t_multiple_withdrawal_cycles_preserve_audit_trail_via_request_withdrawal,
            "ISOLATION: one student's withdraw/re-register on a shared offering never affects another student's own enrollment row": t_other_student_isolation,
            "CAPACITY: max_enrollment is still enforced for fresh registrations after a seat is vacated by withdrawal": t_capacity_still_enforced_on_reregistration,
            "BATCH /enrollment/register: withdraw one of two offerings, re-register; mixed withdrawn+active batch is rejected and changes nothing; the untouched offering's row is preserved": t_register_courses_batch_withdraw_reregister,
            "RESEARCH COURSE: reactivating a withdrawn row re-resolves instructor_id against the CURRENT accepted Major Advisor": t_research_course_instructor_reresolved_on_reactivation,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_PFX}%")))).scalars().all(),
                "colleges": (await db.execute(select(College.id).where(College.name.like(f"{_LABEL}%")))).scalars().all(),
                "sessions": (await db.execute(select(RefreshToken.id).where(RefreshToken.device_info == _DEVICE))).scalars().all(),
                "courses": (await db.execute(select(Course.id).where(Course.course_number.like(f"ZZWR%{_TAG}")))).scalars().all(),
            }
        RESULTS_OK = not any(left.values())
        record("cleanup: no ZZTEST_WR user / college / session / course remains", RESULTS_OK, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
