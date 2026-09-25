"""Standalone HTTP-level tests for the Research Course per-student instructor
feature (BUSINESS_LOGIC.md Research Course section).

Modelled directly on `tests/test_thesis.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions via `create_access_token`/
`RefreshToken` rows, a `record(name, ok, detail)` pass/fail tracker, a
`_run(name, fn)` wrapper so one failing test doesn't stop the run, and a
`_setup()`/`_teardown()` pair using a unique `_TAG = uuid.uuid4().hex[:6]`
and `zztest_rc_...` prefix on every created row so cleanup is unambiguous.

Business rules under test (confirmed):
  - A Research Course (`Course.category == "research"`) offering may be
    created by the HOD with ZERO OfferingFaculty — and MUST be created with
    zero (a non-empty faculty_ids is rejected for a Research Course, since
    the per-student Major Advisor assignment is the sole instructor
    authority — no competing offering-level instructor is allowed).
  - A non-Research-Course offering keeps the pre-existing "1-3 faculty,
    exactly one Leader" requirement unchanged.
  - At Course Registration time, a Research Course enrollment's
    `StudentEnrollment.instructor_id` is resolved server-side from the
    AUTHENTICATED student's own accepted Major Advisor
    (`CommitteeMember.role == "major_advisor" AND accepted is True`) —
    never client-supplied, never arbitrarily chosen among several, never
    silently null.
  - `CourseOffering` itself is never mutated by any student's registration.
  - Faculty authorization for Research-Course-specific, per-student
    operations (roster actions, grading) is scoped to
    `StudentEnrollment.instructor_id`, never widened to the whole offering.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_research_course
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
from app.models.enrollment import CourseRegistration, StudentEnrollment
from app.models.grading import ApprovalStage, GradeEntry, GradeSheet
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_rc_"
_LABEL = "ZZTEST_RC"
_DEVICE = "ZZTEST-rc"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
CRS: dict[str, uuid.UUID] = {}   # course key -> id
OFF: dict[str, uuid.UUID] = {}   # offering key -> id


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


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None, student_profile=False):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"RC{key.upper()}",
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


async def _mk_committee(student_key: str, ma_keys: list[str], *, accepted: list[bool] | None = None) -> None:
    """Builds a real AdvisoryCommittee with one CommitteeMember(role="major_advisor")
    per entry in `ma_keys` — `accepted` defaults to True for every entry; pass an
    explicit list (same length) to build the zero/multiple-accepted-MA edge cases."""
    if accepted is None:
        accepted = [True] * len(ma_keys)
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST RC committee", status="members_pending")
        db.add(c)
        await db.flush()
        for ma_key, acc in zip(ma_keys, accepted):
            db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=acc))
        await db.commit()


async def _row(offering_id) -> CourseOffering:
    async with AsyncSessionLocal() as db:
        return await db.get(CourseOffering, offering_id)


async def _enrollment(student_key: str, offering_id) -> StudentEnrollment:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == offering_id,
        ))).scalar_one()


async def _offering_faculty_count(offering_id) -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(OfferingFaculty).where(OfferingFaculty.offering_id == offering_id))).scalar_one()


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
            print("STOP: stray zztest_rc_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        sem = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert sem is not None, "test requires at least 1 existing Semester in the local dev database"
        S["SEM"] = sem
        col = College(name=f"{_LABEL} College", code=f"ZZRC{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, d2, pg, college = S["D1"], S["D2"], S["PG"], S["COLLEGE"]

    # HODs
    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("hod2", UserRole.HOD, d2)
    # Faculty: two Major Advisors, one "normal course" instructor, one totally unrelated faculty
    await _mk_user("ma_x", UserRole.FACULTY, d1)
    await _mk_user("ma_y", UserRole.FACULTY, d1)
    await _mk_user("ma_dup1", UserRole.FACULTY, d1)
    await _mk_user("ma_dup2", UserRole.FACULTY, d1)
    await _mk_user("fac_normal", UserRole.FACULTY, d1)
    await _mk_user("fac_unrelated", UserRole.FACULTY, d1)
    # Students
    await _mk_user("sx", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRC-x-{_TAG}", student_profile=True)
    await _mk_user("sy", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRC-y-{_TAG}", student_profile=True)
    await _mk_user("s_noma", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRC-noma-{_TAG}", student_profile=True)
    await _mk_user("s_multi", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRC-multi-{_TAG}", student_profile=True)
    await _mk_user("s_normal", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRC-normal-{_TAG}", student_profile=True)

    await _mk_committee("sx", ["ma_x"])
    await _mk_committee("sy", ["ma_y"])
    await _mk_committee("s_multi", ["ma_dup1", "ma_dup2"])
    # s_noma: intentionally NO committee at all.
    await _mk_committee("s_normal", ["ma_x"])

    S["SA"] = await _session(admin_id)
    for k in ("hod1", "hod2", "ma_x", "ma_y", "ma_dup1", "ma_dup2", "fac_normal", "fac_unrelated",
              "sx", "sy", "s_noma", "s_multi", "s_normal"):
        TOK[k] = await _session(U[k])

    # Courses: one Research Course, one normal ("core") course, same department/level.
    async with AsyncSessionLocal() as db:
        rc = Course(course_number=f"ZZRC-{_TAG}", title="ZZTEST Research Course", department_id=d1.id,
                    program_level="PG", category="research", created_by=admin_id)
        nc = Course(course_number=f"ZZNC-{_TAG}", title="ZZTEST Normal Course", department_id=d1.id,
                    program_level="PG", category="core", created_by=admin_id)
        db.add(rc); db.add(nc)
        await db.commit()
        CRS["research"] = rc.id
        CRS["normal"] = nc.id


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        oids = select(CourseOffering.id).where(CourseOffering.course_id.in_(list(CRS.values()) or [uuid.uuid4()]))
        sids = select(GradeSheet.id).where(GradeSheet.offering_id.in_(oids))

        await db.execute(delete(GradeEntry).where(GradeEntry.sheet_id.in_(sids)))
        await db.execute(delete(ApprovalStage).where(ApprovalStage.sheet_id.in_(sids)))
        await db.execute(delete(GradeSheet).where(GradeSheet.offering_id.in_(oids)))
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


# ── tests: HOD offering creation ─────────────────────────────────────────────

async def t_hod_creates_research_offering_without_instructor():
    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "course_id": str(CRS["research"]), "department_id": str(S["D1"].id),
        "faculty_ids": [], "leader_id": None,
    })
    assert r.status_code == 201, r.text
    OFF["research"] = uuid.UUID(r.json()["id"])
    assert await _offering_faculty_count(OFF["research"]) == 0
    pub = await _call("PATCH", f"/courses/offerings/{OFF['research']}/status", TOK["hod1"], params={"status": "published"})
    assert pub.status_code == 200, pub.text


async def t_hod_cannot_preassign_faculty_to_research_offering():
    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "course_id": str(CRS["research"]), "department_id": str(S["D1"].id),
        "faculty_ids": [str(U["fac_unrelated"])], "leader_id": str(U["fac_unrelated"]),
    })
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(CourseOffering).where(
            CourseOffering.course_id == CRS["research"], CourseOffering.section == None,  # noqa: E711
        ))).scalar_one()
    # the ALLOWED offering from the previous test is the only one that should exist
    assert n == 1, n


async def t_assign_faculty_endpoint_blocked_for_research_offering():
    r = await _call("POST", f"/courses/offerings/{OFF['research']}/faculty", TOK["hod1"],
                     params={"faculty_id": str(U["fac_unrelated"]), "role": "secondary"})
    assert r.status_code == 400, r.text
    assert await _offering_faculty_count(OFF["research"]) == 0


async def t_normal_course_offering_still_requires_instructor():
    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "course_id": str(CRS["normal"]), "department_id": str(S["D1"].id),
        "faculty_ids": [], "leader_id": None,
    })
    assert r.status_code == 400, r.text


async def t_normal_course_offering_with_instructor_succeeds():
    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "course_id": str(CRS["normal"]), "department_id": str(S["D1"].id),
        "faculty_ids": [str(U["fac_normal"])], "leader_id": str(U["fac_normal"]),
    })
    assert r.status_code == 201, r.text
    OFF["normal"] = uuid.UUID(r.json()["id"])
    assert await _offering_faculty_count(OFF["normal"]) == 1
    pub = await _call("PATCH", f"/courses/offerings/{OFF['normal']}/status", TOK["hod1"], params={"status": "published"})
    assert pub.status_code == 200, pub.text


async def t_cross_department_hod_blocked():
    r = await _call("POST", "/courses/offerings", TOK["hod2"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "course_id": str(CRS["research"]), "department_id": str(S["D1"].id),
        "faculty_ids": [], "leader_id": None,
    })
    assert r.status_code == 403, r.text


# ── tests: student registration ──────────────────────────────────────────────

async def t_student_x_registers_research_course():
    r = await _call("POST", "/enrollment/register", TOK["sx"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(OFF["research"])],
    })
    assert r.status_code == 201, r.text
    e = await _enrollment("sx", OFF["research"])
    assert e.instructor_id == U["ma_x"], e.instructor_id


async def t_student_y_registers_same_offering_different_instructor():
    r = await _call("POST", "/enrollment/register", TOK["sy"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(OFF["research"])],
    })
    assert r.status_code == 201, r.text
    e = await _enrollment("sy", OFF["research"])
    assert e.instructor_id == U["ma_y"], e.instructor_id


async def t_offering_never_mutated_by_registration():
    off = await _row(OFF["research"])
    assert await _offering_faculty_count(OFF["research"]) == 0
    assert off.department_id == S["D1"].id  # untouched


async def t_client_supplied_instructor_id_ignored():
    """The registration schema has no `instructor_id` field at all — an
    extra, unexpected key in the JSON body must be silently ignored by
    Pydantic (default behavior), never used as the instructor."""
    r = await _call("POST", "/enrollment/register", TOK["s_normal"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(OFF["normal"])],
        "instructor_id": str(U["fac_unrelated"]),
    })
    assert r.status_code == 201, r.text
    e = await _enrollment("s_normal", OFF["normal"])
    assert e.instructor_id is None, "a normal-course enrollment must never get a research instructor_id"


async def t_student_without_major_advisor_rejected():
    r = await _call("POST", "/enrollment/register", TOK["s_noma"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(OFF["research"])],
    })
    assert r.status_code == 403, r.text  # require_advisory_committee_established gate fires first (no committee at all)


async def t_student_with_multiple_accepted_major_advisors_rejected():
    r = await _call("POST", "/enrollment/register", TOK["s_multi"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(OFF["research"])],
    })
    assert r.status_code == 400, r.text
    assert "more than one" in r.json()["detail"].lower(), r.text
    async with AsyncSessionLocal() as db:
        leftover = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s_multi"], StudentEnrollment.offering_id == OFF["research"],
        ))).scalar_one_or_none()
    assert leftover is None, "the rejected registration must not have created a partial enrollment row"


# ── tests: Teacher Courses / roster / grading isolation ──────────────────────

async def t_teacher_courses_shows_research_offering_for_instructors():
    r = await _call("GET", "/courses/offerings/all", TOK["ma_x"], params={"mine": "true"})
    assert r.status_code == 200, r.text
    assert str(OFF["research"]) in [o["id"] for o in r.json()]
    r = await _call("GET", "/courses/offerings/all", TOK["ma_y"], params={"mine": "true"})
    assert str(OFF["research"]) in [o["id"] for o in r.json()]


async def t_teacher_courses_hides_research_offering_for_unrelated_faculty():
    r = await _call("GET", "/courses/offerings/all", TOK["fac_unrelated"], params={"mine": "true"})
    assert r.status_code == 200, r.text
    assert str(OFF["research"]) not in [o["id"] for o in r.json()]


async def t_roster_restricted_to_own_students_for_research_instructor():
    r = await _call("GET", f"/enrollment/offering/{OFF['research']}", TOK["ma_x"])
    assert r.status_code == 200, r.text
    student_ids = {row["student_id"] for row in r.json()}
    assert student_ids == {str(U["sx"])}, student_ids  # never sees sy's row


async def t_roster_full_for_hod():
    r = await _call("GET", f"/enrollment/offering/{OFF['research']}", TOK["hod1"])
    assert r.status_code == 200, r.text
    student_ids = {row["student_id"] for row in r.json()}
    assert student_ids == {str(U["sx"]), str(U["sy"])}, student_ids


async def t_roster_blocked_for_unrelated_faculty():
    r = await _call("GET", f"/enrollment/offering/{OFF['research']}", TOK["fac_unrelated"])
    assert r.status_code == 403, r.text


async def t_process_enrollment_own_student_allowed():
    e = await _enrollment("sx", OFF["research"])
    r = await _call("PATCH", f"/enrollment/{e.id}", TOK["ma_x"], params={"status": "approved"})
    assert r.status_code == 200, r.text


async def t_process_enrollment_other_instructors_student_blocked():
    e = await _enrollment("sy", OFF["research"])
    r = await _call("PATCH", f"/enrollment/{e.id}", TOK["ma_x"], params={"status": "approved"})
    assert r.status_code == 403, r.text
    # the real instructor can still approve it
    r = await _call("PATCH", f"/enrollment/{e.id}", TOK["ma_y"], params={"status": "approved"})
    assert r.status_code == 200, r.text


async def t_bulk_approve_skips_students_not_instructed():
    # Re-open both to pending so bulk-approve has something to do.
    async with AsyncSessionLocal() as db:
        for key in ("sx", "sy"):
            e = (await db.execute(select(StudentEnrollment).where(
                StudentEnrollment.student_id == U[key], StudentEnrollment.offering_id == OFF["research"],
            ))).scalar_one()
            e.status = "pending"; e.processed_by = None; e.processed_at = None
        await db.commit()
    ex = await _enrollment("sx", OFF["research"])
    ey = await _enrollment("sy", OFF["research"])
    r = await _call("POST", f"/enrollment/offering/{OFF['research']}/bulk-approve", TOK["ma_x"], json={
        "enrollment_ids": [str(ex.id), str(ey.id)], "status": "approved",
    })
    assert r.status_code == 200, r.text
    assert "1 enrollments approved" in r.json()["message"] and "1 skipped" in r.json()["message"], r.json()
    fresh_x = await _enrollment("sx", OFF["research"])
    fresh_y = await _enrollment("sy", OFF["research"])
    assert fresh_x.status == "approved"
    assert fresh_y.status == "pending", "ma_x must not have been able to bulk-approve sy's enrollment"
    # ma_y can approve their own.
    r = await _call("POST", f"/enrollment/offering/{OFF['research']}/bulk-approve", TOK["ma_y"], json={
        "enrollment_ids": [str(ey.id)], "status": "approved",
    })
    assert r.status_code == 200, r.text
    assert (await _enrollment("sy", OFF["research"])).status == "approved"


async def t_grading_entry_isolation():
    r = await _call("POST", "/grading/sheets", TOK["ma_x"], params={"offering_id": str(OFF["research"])})
    assert r.status_code == 201, r.text
    sheet_id = r.json()["id"]

    # ma_x cannot save Student Y's grade entry merely by sharing the offering.
    r = await _call("PUT", f"/grading/sheets/{sheet_id}/entries", TOK["ma_x"], json={
        "entries": [{"student_id": str(U["sx"]), "internal_marks": 20, "external_marks": 60},
                    {"student_id": str(U["sy"]), "internal_marks": 20, "external_marks": 60}],
    })
    assert r.status_code == 403, r.text

    # The atomic pre-check means Student X's entry must NOT have been saved either.
    async with AsyncSessionLocal() as db:
        entry_x = (await db.execute(select(GradeEntry).where(GradeEntry.sheet_id == uuid.UUID(sheet_id), GradeEntry.student_id == U["sx"]))).scalar_one()
    assert entry_x.internal_marks is None, "a rejected batch must not partially save any entry"

    # ma_x saving only their own student succeeds.
    r = await _call("PUT", f"/grading/sheets/{sheet_id}/entries", TOK["ma_x"], json={
        "entries": [{"student_id": str(U["sx"]), "internal_marks": 20, "external_marks": 60}],
    })
    assert r.status_code == 200, r.text

    # ma_y saving their own student succeeds too.
    r = await _call("PUT", f"/grading/sheets/{sheet_id}/entries", TOK["ma_y"], json={
        "entries": [{"student_id": str(U["sy"]), "internal_marks": 18, "external_marks": 55}],
    })
    assert r.status_code == 200, r.text


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "HOD creates Research Course offering with NO instructor -> allowed, zero OfferingFaculty": t_hod_creates_research_offering_without_instructor,
            "HOD cannot pre-assign faculty to a Research Course offering at creation": t_hod_cannot_preassign_faculty_to_research_offering,
            "POST .../offerings/{id}/faculty is blocked for a Research Course offering": t_assign_faculty_endpoint_blocked_for_research_offering,
            "Normal course offering with NO instructor -> still rejected (unchanged rule)": t_normal_course_offering_still_requires_instructor,
            "Normal course offering WITH instructor -> still succeeds (unchanged rule)": t_normal_course_offering_with_instructor_succeeds,
            "Cross-department HOD cannot create an offering in another department": t_cross_department_hod_blocked,
            "Student X registers Research Course -> instructor = their own accepted Major Advisor": t_student_x_registers_research_course,
            "Student Y registers SAME offering -> DIFFERENT instructor (their own Major Advisor)": t_student_y_registers_same_offering_different_instructor,
            "CourseOffering itself is never mutated by any student's registration": t_offering_never_mutated_by_registration,
            "A client-supplied instructor_id in the request body is silently ignored": t_client_supplied_instructor_id_ignored,
            "Student with NO Major Advisor is rejected before registration": t_student_without_major_advisor_rejected,
            "Student with MULTIPLE accepted Major Advisors is rejected, no partial row created": t_student_with_multiple_accepted_major_advisors_rejected,
            "Teacher Courses lists the Research offering for each student's own instructor": t_teacher_courses_shows_research_offering_for_instructors,
            "Teacher Courses hides the Research offering from an unrelated faculty member": t_teacher_courses_hides_research_offering_for_unrelated_faculty,
            "Roster (offering_enrollments) is restricted to only the instructor's own student": t_roster_restricted_to_own_students_for_research_instructor,
            "Roster is full or HOD (department-wide, unchanged)": t_roster_full_for_hod,
            "Roster is blocked entirely for an unrelated faculty member": t_roster_blocked_for_unrelated_faculty,
            "process_enrollment: instructor can approve their own student's enrollment": t_process_enrollment_own_student_allowed,
            "process_enrollment: instructor CANNOT approve another instructor's student": t_process_enrollment_other_instructors_student_blocked,
            "bulk-approve: rows for a non-instructed student are skipped, not processed": t_bulk_approve_skips_students_not_instructed,
            "Grading: an instructor cannot save another instructor's student's entry (atomic, no partial save)": t_grading_entry_isolation,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "courses": (await db.execute(select(func.count()).select_from(Course).where(Course.id.in_(list(CRS.values()) or [uuid.uuid4()])))).scalar_one(),
                "offerings": (await db.execute(select(func.count()).select_from(CourseOffering).where(CourseOffering.course_id.in_(list(CRS.values()) or [uuid.uuid4()])))).scalar_one(),
                "committees": (await db.execute(select(func.count()).select_from(AdvisoryCommittee).where(AdvisoryCommittee.research_title == "ZZTEST RC committee"))).scalar_one(),
                "college": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "course_registrations": (await db.execute(select(func.count()).select_from(CourseRegistration).where(CourseRegistration.student_id.in_(uids)))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_RC user / course / offering / committee / college / session / registration row remains",
               not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
