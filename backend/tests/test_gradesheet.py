"""Standalone HTTP-level tests for the Gradesheet -> Approval -> CoE compilation ->
Student Result -> Marksheet -> Grade Card workflow.

Same conventions as tests/test_research_course.py / test_comprehensive_exam.py: the REAL
FastAPI app via `httpx.ASGITransport`, real JWT sessions, a `record()` pass/fail
tracker, every created row tagged `zztest_gs_<TAG>` (users) / `ZZTEST_GS` (other rows)
and a final cleanup assertion. Nothing pre-existing is modified.

The 24-hour rule is tested by moving the workflow's single clock
(`app.core.gradesheet_flow.now`) — no real waiting.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_gradesheet
"""
import asyncio
import hashlib
import io
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import delete, func, select

from app.core import gradesheet_flow as flow
from app.core.config import settings
from app.core.grading_calc import (
    attendance_band, compute_cgpa, compute_entry_outcome, compute_gpa, format_gpa,
)
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import AcademicCalendar, Semester
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.enrollment import StudentEnrollment
from app.models.grading import (
    GradeSheet, GradesheetCycle, GradesheetStage, StudentSemesterResult, StudentSemesterResultCourse, compute_grade,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_gs_"
_LABEL = "ZZTEST_GS"
_DEVICE = "ZZTEST-gs"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
CRS: dict[str, uuid.UUID] = {}
OFF: dict[str, uuid.UUID] = {}
SHEET: dict[str, str] = {}
_ORIG_NOW = flow.now


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=180) as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _session(user_id) -> str:
    async with AsyncSessionLocal() as db:
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=user_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    return create_access_token(str(user_id), {"sid": str(rt)})


async def _mk_user(key, role, dept=None, *, program=None, college=None, roll=None, gender="Male", assign=True):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"GS{key.upper()}",
            role=role, department_id=dept.id if dept else None, program_id=program.id if program else None,
            college_id=college.id if college else None, student_roll=roll, mobile="9876543210", gender=gender,
            is_active=True, is_verified=True,
            date_of_birth=date(2000, 1, 1) if role == UserRole.STUDENT else None,
            father_name="ZZTEST Father" if role == UserRole.STUDENT else None,
            address="ZZTEST Address" if role == UserRole.STUDENT else None,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if assign and role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept and role in (UserRole.HOD, UserRole.FACULTY) else None))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = [l.strip() for l in traceback.format_exc().splitlines() if "test_gradesheet.py" in l or "app" + "\\" in l or "app/" in l]
        record(name, False, f"{type(e).__name__}: {e} @ {' | '.join(tb[-3:])}")


def _set_now(dt):
    flow.now = lambda: dt


def _restore_now():
    flow.now = _ORIG_NOW


def _structure(**over):
    body = {
        "total_theory_marks": 100, "theory_pass_marks": 40,
        "theory_components": [{"code": "first_test", "max_marks": 10}, {"code": "mid_term", "max_marks": 30}, {"code": "end_term", "max_marks": 60}],
        "total_practical_marks": 100, "practical_pass_marks": 40,
    }
    body.update(over)
    return body


def _theory_only(first=20, end=80, **over):
    body = {"total_theory_marks": first + end, "theory_pass_marks": 40,
            "theory_components": [{"code": "first_test", "max_marks": first}, {"code": "end_term", "max_marks": end}]}
    body.update(over)
    return body


async def _create(tok, offering_key, gtype="new", **structure):
    body = {"offering_id": str(OFF[offering_key]), "gradesheet_type": gtype, **(structure or _structure())}
    return await _call("POST", "/grading/sheets", tok, json=body)


def _entry(student_key, marks, attendance=None, remark=None, absent=None):
    e = {"student_id": str(U[student_key]), "component_marks": marks}
    if attendance is not None:
        e["attendance_percent"] = attendance
    if remark is not None:
        e["remark"] = remark
    if absent is not None:
        e["is_absent"] = absent
    return e


async def _detail(tok, sheet_id):
    r = await _call("GET", f"/grading/sheets/{sheet_id}", tok)
    assert r.status_code == 200, r.text
    return r.json()


async def _stage_rows(sheet_id):
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(GradesheetStage).join(GradesheetCycle, GradesheetCycle.id == GradesheetStage.cycle_id)
            .where(GradesheetCycle.sheet_id == uuid.UUID(sheet_id)).order_by(GradesheetCycle.cycle_number, GradesheetStage.sequence)
        )).scalars().all())


async def _finalize_after_submit(sheet_id, *, instructor_keys=()):
    """Drive every approval stage after submission, asserting each succeeds."""
    for k in instructor_keys:
        r = await _call("POST", f"/grading/sheets/{sheet_id}/approve", TOK[k]); assert r.status_code == 200, (k, r.text)
    for k in ("hod1", "incharge", "dpgs", "coe"):
        r = await _call("POST", f"/grading/sheets/{sheet_id}/approve", TOK[k]); assert r.status_code == 200, (k, r.text)
    assert (await _detail(TOK["coe"], sheet_id))["status"] == "approved"


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_gs_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        holders = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(
            UserRoleAssignment.role.in_([UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.CONTROLLER_OF_EXAMINATION])))).scalar_one()
        if holders:
            print("STOP: a real Incharge/DPGS/CoE holder already exists (single-holder roles); refusing to run against it.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        col = College(name=f"{_LABEL} College", code=f"ZZGS{_TAG.upper()}"[:20])
        db.add(col)
        cal = AcademicCalendar(name=f"{_LABEL} {_TAG}", academic_year="2097-98", start_date=date(2097, 7, 1), end_date=date(2098, 6, 30), status="active")
        db.add(cal)
        await db.flush()
        s1 = Semester(calendar_id=cal.id, name="Semester I", sem_type="odd", start_date=date(2097, 7, 1), end_date=date(2097, 12, 31), exam_end=date(2097, 12, 20))
        s2 = Semester(calendar_id=cal.id, name="Semester II", sem_type="even", start_date=date(2098, 1, 1), end_date=date(2098, 6, 30), exam_end=date(2098, 5, 15))
        s3 = Semester(calendar_id=cal.id, name="Semester III", sem_type="odd", start_date=date(2098, 7, 1), end_date=date(2098, 12, 31))
        db.add_all([s1, s2, s3])
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
        S.update(COLLEGE=col, CAL=cal, SEM1=s1, SEM2=s2, SEM3=s3, ADMIN=admin_id)
    d1, d2, pg, college = S["D1"], S["D2"], S["PG"], S["COLLEGE"]

    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("hod2", UserRole.HOD, d2)
    await _mk_user("fac_a", UserRole.FACULTY, d1)      # creating instructor (o1, o2, o3)
    await _mk_user("fac_b", UserRole.FACULTY, d1)      # second instructor of o1/o2
    await _mk_user("fac_c", UserRole.FACULTY, d1)      # unrelated faculty, same department
    await _mk_user("fac_d2", UserRole.FACULTY, d2)     # instructor in the other department
    await _mk_user("fac_ma", UserRole.FACULTY, d1)     # Major Advisor of s1
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL)
    await _mk_user("dpgs", UserRole.DPGS)
    await _mk_user("coe", UserRole.CONTROLLER_OF_EXAMINATION, assign=False)   # assigned through the real API in t_coe_role_*
    await _mk_user("coe2", UserRole.CONTROLLER_OF_EXAMINATION, assign=False)  # second candidate (must be refused)
    await _mk_user("s1", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZGS-1-{_TAG}", gender="Male")
    await _mk_user("s2", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZGS-2-{_TAG}", gender="Female")
    await _mk_user("s3", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZGS-3-{_TAG}", gender="Female")
    await _mk_user("s_other", UserRole.STUDENT, d2, program=pg, college=college, roll=f"ZZGS-o-{_TAG}", gender="Male")

    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U["s1"], research_title="ZZTEST GS committee", status="members_pending")
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U["fac_ma"], role="major_advisor", accepted=True))
        await db.commit()

    S["SA"] = await _session(S["ADMIN"])
    for k in list(U):
        if k not in ("coe", "coe2"):
            TOK[k] = await _session(U[k])

    async with AsyncSessionLocal() as db:
        specs = {
            "c1": ("Molecular Diagnostics", 2, 1, d1), "c2": ("Veterinary Genetics", 2, 0, d1),
            "c3": ("Advanced Immunology", 2, 0, d1), "c_d2": ("Fisheries Nutrition", 2, 0, d2),
            "c_res": ("Research Work", 4, 0, d1),
        }
        for key, (title, th, pr, dept) in specs.items():
            c = Course(course_number=f"ZZ{key.upper()}-{_TAG}", title=f"ZZTEST {title}", department_id=dept.id, credit_theory=th, credit_practical=pr,
                       course_type="both" if pr else "theory", program_level="PG", category="research" if key == "c_res" else "core", credit_type="credit", created_by=S["ADMIN"])
            db.add(c)
            await db.flush()
            CRS[key] = c.id
        await db.commit()
        plan = {  # offering key -> (course, semester, department, faculty keys)
            "o1": ("c1", S["SEM1"], d1, ["fac_a", "fac_b"]), "o2": ("c2", S["SEM1"], d1, ["fac_a", "fac_b"]),
            "o3": ("c3", S["SEM2"], d1, ["fac_a"]), "o_d2": ("c_d2", S["SEM1"], d2, ["fac_d2"]),
            "o_res": ("c_res", S["SEM3"], d1, []),        # Research Course: no OfferingFaculty, per-student instructors
        }
        for key, (ck, sem, dept, facs) in plan.items():
            o = CourseOffering(calendar_id=S["CAL"].id, semester_id=sem.id, course_id=CRS[ck], department_id=dept.id, status="published", created_by=S["ADMIN"])
            db.add(o)
            await db.flush()
            OFF[key] = o.id
            for i, fk in enumerate(facs):
                db.add(OfferingFaculty(offering_id=o.id, faculty_id=U[fk], role="primary" if i == 0 else "secondary"))
        enrol = {"o1": ["s1", "s2", "s3"], "o2": ["s1", "s2"], "o3": ["s1"], "o_d2": ["s_other"], "o_res": ["s2", "s3"]}
        research_instructor = {"s2": "fac_a", "s3": "fac_b"}
        for ok, students in enrol.items():
            for sk in students:
                db.add(StudentEnrollment(student_id=U[sk], offering_id=OFF[ok], status="approved", classification="major",
                                         instructor_id=U[research_instructor[sk]] if ok == "o_res" else None))
        await db.commit()


async def _teardown() -> None:
    _restore_now()
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"{_PFX}%@%"))
        oids = select(CourseOffering.id).where(CourseOffering.course_id.in_(list(CRS.values()) or [uuid.uuid4()]))
        rids = select(StudentSemesterResult.id).where(StudentSemesterResult.student_id.in_(uids))
        await db.execute(delete(StudentSemesterResultCourse).where(StudentSemesterResultCourse.result_id.in_(rids)))
        await db.execute(delete(StudentSemesterResult).where(StudentSemesterResult.student_id.in_(uids)))
        await db.execute(delete(GradeSheet).where(GradeSheet.offering_id.in_(oids)))   # entries/components/marks/cycles/stages cascade in the DB
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(oids)))
        await db.execute(delete(OfferingFaculty).where(OfferingFaculty.offering_id.in_(oids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(oids)))
        await db.execute(delete(Course).where(Course.id.in_(list(CRS.values()) or [uuid.uuid4()])))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%@%")))
        await db.execute(delete(Semester).where(Semester.calendar_id == S["CAL"].id))
        await db.execute(delete(AcademicCalendar).where(AcademicCalendar.id == S["CAL"].id))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


# ── pure calculation tests ───────────────────────────────────────────────────

async def t_gpa_confirmed_example():
    gpa = compute_gpa(Decimal("166.550"), Decimal("20"))
    assert gpa == Decimal("8.327500"), gpa                      # 166.550 / 20 = 8.3275 exactly
    assert format_gpa(gpa) == "8.327", format_gpa(gpa)          # confirmed display: truncated to 3 dp (not 8.328)
    assert compute_gpa(Decimal("10"), Decimal("0")) is None


async def t_cgpa_rules():
    assert compute_cgpa([Decimal("8.20")]) is None              # first semester: GPA only, no CGPA
    assert format_gpa(compute_cgpa([Decimal("8.20"), Decimal("8.40")])) == "8.300"   # average of semester GPAs
    assert format_gpa(compute_cgpa([Decimal("8"), Decimal("9"), Decimal("7")])) == "8.000"
    assert format_gpa(compute_cgpa([Decimal("9.5"), Decimal("7")])) == "8.250"        # plain average, not credit-weighted


async def t_grade_calculation_fractional_marks():
    assert compute_grade(89.5) == ("A+", 9.0)     # used to fall through to F (gap between 89 and 90)
    assert compute_grade(Decimal("44.5")) == ("F", 0.0)
    assert compute_grade(45) == ("P", 4.0) and compute_grade(90) == ("O", 10.0) and compute_grade(0) == ("F", 0.0)
    comps = [{"code": "end_term", "component_type": "theory", "max_marks": Decimal("100")}]
    out = compute_entry_outcome(comps, {"end_term": Decimal("59.5")}, theory_pass_marks=Decimal("0"))
    assert out["grade_letter"] == "B" and out["grade_points"] == 6.0 and out["marks_percent"] == Decimal("59.50"), out
    fail = compute_entry_outcome(comps, {"end_term": Decimal("55")}, theory_pass_marks=Decimal("60"))
    assert fail["grade_letter"] == "F", fail                    # below the configured theory pass marks
    inc = compute_entry_outcome(comps, {"end_term": None})
    assert inc["grade_letter"] is None and inc["complete"] is False
    ab = compute_entry_outcome(comps, {}, is_absent=True)
    assert ab["grade_letter"] == "F" and ab["grade_points"] == 0.0
    assert [attendance_band(x) for x in (74.99, 75, 85, 85.01)] == ["red", "yellow", "yellow", "green"]


# ── CoE role ─────────────────────────────────────────────────────────────────

async def t_coe_role_is_single_holder_and_departmentless():
    r = await _call("POST", f"/auth/users/{U['coe']}/roles", S["SA"], json={"role": "controller_of_examination", "department_id": str(S["D1"].id)})
    assert r.status_code == 400, r.text                          # institution-wide: never given a department
    r = await _call("POST", f"/auth/users/{U['coe']}/roles", S["SA"], json={"role": "controller_of_examination"})
    assert r.status_code == 201, r.text
    TOK["coe"] = await _session(U["coe"])
    r = await _call("POST", f"/auth/users/{U['coe2']}/roles", S["SA"], json={"role": "controller_of_examination"})
    assert r.status_code == 409, r.text                          # exactly one holder
    assert "Controller of Examination" in r.text, r.text
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.role == UserRole.CONTROLLER_OF_EXAMINATION))).scalar_one()
    assert n == 1, n
    r = await _call("GET", "/admin/roles", S["SA"])
    assert "CONTROLLER_OF_EXAMINATION" in {x["code"] for x in r.json()}, "role master-data row missing"


# ── authorization / creation ─────────────────────────────────────────────────

async def t_assigned_courses_are_backend_scoped():
    r = await _call("GET", "/grading/assigned-courses", TOK["fac_a"])
    assert r.status_code == 200, r.text
    ids = {x["offering_id"] for x in r.json()}
    assert {str(OFF["o1"]), str(OFF["o2"]), str(OFF["o3"])} <= ids and str(OFF["o_d2"]) not in ids, ids
    row = next(x for x in r.json() if x["offering_id"] == str(OFF["o1"]))
    assert row["course_credit"] == "3(2+1)" and row["total_students"] == 3 and row["course_number"].startswith("ZZC1-"), row
    assert row["department"] == S["D1"].name and "ZZTEST_GS College" in row["college_degree"], row
    r = await _call("GET", "/grading/assigned-courses", TOK["fac_c"])
    assert not ({str(OFF[k]) for k in OFF} & {x["offering_id"] for x in r.json()}), "unrelated faculty must see none of these"
    r = await _call("GET", "/grading/assigned-courses", TOK["fac_d2"])
    assert {x["offering_id"] for x in r.json()} & {str(OFF[k]) for k in OFF} == {str(OFF["o_d2"])}
    r = await _call("GET", f"/grading/assigned-courses?semester_id={S['SEM2'].id}", TOK["fac_a"])
    assert {x["offering_id"] for x in r.json()} == {str(OFF["o3"])}
    for role in ("hod1", "s1", "coe", "incharge"):
        assert (await _call("GET", "/grading/assigned-courses", TOK[role])).status_code == 403, role


async def t_create_requires_real_instructor_of_the_offering():
    for who in ("fac_c", "fac_d2"):
        r = await _create(TOK[who], "o1")
        assert r.status_code == 404, (who, r.text)               # an offering id alone never authorizes
    for who in ("hod1", "s1", "incharge", "coe", "dpgs"):
        assert (await _create(TOK[who], "o1")).status_code == 403, who
    assert (await _create(S["SA"], "o1")).status_code == 403     # Super Admin is not a workflow actor


async def t_invalid_component_configurations_rejected():
    bad = {
        "totals mismatch": _structure(theory_components=[{"code": "first_test", "max_marks": 10}, {"code": "end_term", "max_marks": 60}]),
        "unknown component": _structure(theory_components=[{"code": "quiz", "max_marks": 100}]),
        "practical as theory": _structure(theory_components=[{"code": "practical", "max_marks": 100}]),
        "duplicate component": _structure(theory_components=[{"code": "end_term", "max_marks": 50}, {"code": "end_term", "max_marks": 50}]),
        "no components for theory total": _structure(theory_components=[]),
        "components without theory total": _structure(total_theory_marks=0, theory_pass_marks=0),
        "theory pass above total": _structure(theory_pass_marks=101),
        "practical pass above total": _structure(practical_pass_marks=101),
        "practical pass without practical": _structure(total_practical_marks=0, practical_pass_marks=10),
        "nothing configured": {"total_theory_marks": 0, "total_practical_marks": 0},
        "zero-mark component": _structure(total_theory_marks=90, theory_components=[{"code": "first_test", "max_marks": 0}, {"code": "end_term", "max_marks": 90}]),
        "negative marks": _structure(total_practical_marks=-5),
    }
    for label, body in bad.items():
        r = await _create(TOK["fac_a"], "o2", **body)
        assert r.status_code == 400, (label, r.status_code, r.text)
    r = await _create(TOK["fac_a"], "o2", gtype="bogus", **_theory_only())
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(GradeSheet).where(GradeSheet.offering_id == OFF["o2"]))).scalar_one()
    assert n == 0, "a rejected configuration must not create a gradesheet"


async def t_create_new_gradesheet_with_configured_components():
    r = await _create(TOK["fac_a"], "o1")
    assert r.status_code == 201, r.text
    SHEET["o1_new"] = r.json()["id"]
    d = await _detail(TOK["fac_a"], SHEET["o1_new"])
    assert d["gradesheet_type"] == "new" and d["status"] == "draft" and d["teacher"].endswith("GSFAC_A"), d
    comps = {c["code"]: c for c in d["structure"]["components"]}
    assert list(comps) == ["first_test", "mid_term", "end_term", "practical"], list(comps)
    assert comps["mid_term"]["max_marks"] == 30 and comps["practical"]["component_type"] == "practical"
    assert d["structure"]["total_theory_marks"] == 100 and d["structure"]["theory_pass_marks"] == 40
    assert d["structure"]["total_practical_marks"] == 100 and d["structure"]["practical_pass_marks"] == 40
    assert d["students"] == {"total": 3, "male": 1, "female": 2}, d["students"]
    c = d["course"]
    assert c["course_number"].startswith("ZZC1-") and c["credit"] == "3(2+1)" and c["credit_type"] == "Credit"
    assert c["department"] == S["D1"].name and c["semester"] == "Semester I" and c["session"] == "2097-98"
    assert len(d["entries"]) == 3 and all(e["grade_letter"] is None and e["complete"] is False for e in d["entries"])
    assert d["permissions"]["can_submit"] and d["permissions"]["can_edit_data"] and not d["permissions"]["can_approve"]


async def t_all_gradesheet_types_stored_explicitly_and_related():
    r = await _create(TOK["fac_a"], "o1")
    assert r.status_code == 409, r.text                                       # exactly one NEW sheet per course
    r = await _create(TOK["fac_a"], "o1", gtype="new", **_structure(), related_sheet_id=SHEET["o1_new"])
    assert r.status_code in (400, 409), r.text
    for gtype in ("repeat", "revised", "make_up"):
        r = await _call("POST", "/grading/sheets", TOK["fac_a"], json={
            "offering_id": str(OFF["o1"]), "gradesheet_type": gtype, "related_sheet_id": SHEET["o1_new"], **_theory_only(),
            "student_ids": [str(U["s3"])],
        })
        assert r.status_code == 201, (gtype, r.text)
        SHEET[f"o1_{gtype}"] = r.json()["id"]
        d = await _detail(TOK["fac_a"], r.json()["id"])
        assert d["gradesheet_type"] == gtype and d["related_sheet_id"] == SHEET["o1_new"], d
        assert [e["student_id"] for e in d["entries"]] == [str(U["s3"])], "student subset honoured"
    r = await _call("POST", "/grading/sheets", TOK["fac_a"], json={"offering_id": str(OFF["o1"]), "gradesheet_type": "revised", "related_sheet_id": str(uuid.uuid4()), **_theory_only()})
    assert r.status_code == 400, r.text                                       # related sheet must exist for this offering
    r = await _call("POST", "/grading/sheets", TOK["fac_a"], json={"offering_id": str(OFF["o1"]), "gradesheet_type": "repeat", **_theory_only(), "student_ids": [str(U["s_other"])]})
    assert r.status_code == 400, r.text                                       # student not registered for this course
    lst = (await _call("GET", f"/grading/offering/{OFF['o1']}/sheets", TOK["fac_a"])).json()
    assert {s["gradesheet_type"] for s in lst["sheets"]} == {"new", "repeat", "revised", "make_up"}, lst
    assert lst["offering"]["total_students"] == 3 and lst["can_create"] is True
    assert all(s["teacher"].endswith("GSFAC_A") and s["status"] == "draft" and s["created_at"] for s in lst["sheets"])


async def t_gradesheet_read_isolation_before_submission():
    sid = SHEET["o1_new"]
    for who in ("fac_c", "fac_d2", "hod1", "hod2", "s1", "incharge", "dpgs", "coe"):
        assert (await _call("GET", f"/grading/sheets/{sid}", TOK[who])).status_code == 404, who   # draft = instructors only
    assert (await _call("GET", f"/grading/sheets/{sid}", TOK["fac_b"])).status_code == 200        # co-instructor may view
    assert (await _call("GET", f"/grading/sheets/{sid}", S["SA"])).status_code == 200             # Super Admin: read-only audit
    assert (await _call("GET", f"/grading/offering/{OFF['o1']}/sheets", TOK["fac_c"])).status_code == 404
    lst = (await _call("GET", f"/grading/offering/{OFF['o1']}/sheets", TOK["hod1"])).json()["sheets"]
    assert lst == [], "a department HOD sees no unsubmitted sheets"
    assert (await _call("GET", f"/grading/sheets/{sid}/document", TOK["fac_c"])).status_code == 404


# ── entering data ────────────────────────────────────────────────────────────

async def t_enter_marks_attendance_and_grade_calculation():
    sid = SHEET["o1_new"]
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [
        _entry("s1", {"first_test": 10, "mid_term": 30, "end_term": 60, "practical": 100}, attendance=90, remark="Excellent"),
        _entry("s2", {"first_test": 8, "mid_term": 25, "end_term": 50, "practical": 80}, attendance=80),
        _entry("s3", {"first_test": 9, "mid_term": 28.5, "end_term": 55.5, "practical": 86}, attendance=70),
    ]})
    assert r.status_code == 200, r.text
    e = {x["student_id"]: x for x in (await _detail(TOK["fac_a"], sid))["entries"]}
    a, b, c = e[str(U["s1"])], e[str(U["s2"])], e[str(U["s3"])]
    assert (a["theory_total"], a["practical_total"], a["grand_total"], a["marks_percent"], a["grade_letter"], a["grade_points"]) == (100, 100, 200, 100.0, "O", 10.0), a
    assert (b["theory_total"], b["grand_total"], b["marks_percent"], b["grade_letter"]) == (83, 163, 81.5, "A+"), b
    assert (c["theory_total"], c["grand_total"], c["marks_percent"], c["grade_letter"], c["grade_points"]) == (93, 179, 89.5, "A+", 9.0), c   # fractional marks, no F fall-through
    assert (a["attendance_percent"], a["attendance_band"]) == (90, "green") and b["attendance_band"] == "yellow" and c["attendance_band"] == "red"
    assert a["remark"] == "Excellent" and a["complete"] and c["component_marks"]["mid_term"] == 28.5


async def t_entry_validation():
    sid = SHEET["o1_new"]
    for label, body in {
        "marks above max": _entry("s1", {"first_test": 11}),
        "negative marks": _entry("s1", {"mid_term": -1}),
        "unknown component": _entry("s1", {"quiz": 5}),
        "attendance above 100": _entry("s1", {}, attendance=100.5),
        "attendance below 0": _entry("s1", {}, attendance=-1),
        "student outside gradesheet": _entry("s_other", {"first_test": 5}),
    }.items():
        r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [body]})
        assert r.status_code == 400, (label, r.status_code, r.text)
    # a rejected batch saves nothing: s2 must be unchanged
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [_entry("s2", {"first_test": 1}), _entry("s1", {"first_test": 99})]})
    assert r.status_code == 400
    e = {x["student_id"]: x for x in (await _detail(TOK["fac_a"], sid))["entries"]}
    assert e[str(U["s2"])]["component_marks"]["first_test"] == 8, "rejected batch must not partially save"


async def t_only_the_creator_enters_data():
    sid = SHEET["o1_new"]
    body = {"entries": [_entry("s1", {"first_test": 1})]}
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_b"], json=body)
    assert r.status_code == 403, r.text                                       # a co-instructor approves; they do not enter data
    for who in ("fac_c", "fac_d2", "hod1", "s1"):
        r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK[who], json=body)
        assert r.status_code in (403, 404), (who, r.status_code)
    assert (await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_b"], json=_structure())).status_code == 404
    assert (await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_b"])).status_code == 404
    e = {x["student_id"]: x for x in (await _detail(TOK["fac_a"], sid))["entries"]}
    assert e[str(U["s1"])]["component_marks"]["first_test"] == 10


async def t_absent_student_gets_grade_f():
    sid = SHEET["o1_repeat"]
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [_entry("s3", {}, attendance=40, absent=True)]})
    assert r.status_code == 200, r.text
    e = (await _detail(TOK["fac_a"], sid))["entries"][0]
    assert e["is_absent"] and e["grade_letter"] == "F" and e["grade_points"] == 0.0 and e["attendance_band"] == "red", e


async def t_structure_update_rules():
    sid = SHEET["o1_new"]
    r = await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_a"], json=_structure(total_practical_marks=50, practical_pass_marks=20))
    assert r.status_code == 400, r.text                                       # existing practical marks (100) exceed the new maximum
    r = await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_a"], json=_structure(
        total_theory_marks=90, theory_components=[{"code": "first_test", "max_marks": 10}, {"code": "mid_term", "max_marks": 30}, {"code": "end_term", "max_marks": 50}]))
    assert r.status_code == 400, r.text                                       # end-term marks of 60 exceed the new maximum of 50
    r = await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_a"], json=_structure(total_theory_marks=90, theory_components=[{"code": "first_test", "max_marks": 10}]))
    assert r.status_code == 400, r.text                                       # components do not add up to the total
    r = await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_a"], json=_structure())
    assert r.status_code == 200, r.text                                       # unchanged structure is accepted; marks untouched
    d = await _detail(TOK["fac_a"], sid)
    assert len(d["structure"]["components"]) == 4 and {e["grade_letter"] for e in d["entries"]} == {"O", "A+"}


# ── approval workflow ────────────────────────────────────────────────────────

async def t_submit_requires_complete_marks():
    r = await _call("POST", f"/grading/sheets/{SHEET['o1_revised']}/submit", TOK["fac_a"])
    assert r.status_code == 400 and "missing" in r.text.lower(), r.text
    r = await _call("POST", f"/grading/sheets/{SHEET['o1_new']}/submit", TOK["fac_c"])
    assert r.status_code == 404, r.text


async def t_submit_signs_creator_and_opens_instructor_stage():
    sid = SHEET["o1_new"]
    r = await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])
    assert r.status_code == 200 and r.json()["status"] == "instructor_pending", r.text
    deadline = datetime.fromisoformat(r.json()["instructor_deadline_at"])
    d = await _detail(TOK["fac_a"], sid)
    assert d["submitted_at"] and abs((deadline - datetime.fromisoformat(d["submitted_at"])).total_seconds() - 86400) < 2, "deadline = submission + 24h"
    sig = d["signatories"]
    assert [(s["stage_type"], s["status"]) for s in sig["instructors"]] == [("creator", "approved"), ("instructor", "pending")], sig
    assert sig["instructors"][0]["signed_at"] and sig["instructors"][0]["name"].endswith("GSFAC_A")
    assert sig["instructors"][1]["name"].endswith("GSFAC_B") and sig["instructors"][1]["signed_at"] is None
    assert all(sig[k]["status"] == "pending" for k in ("hod", "incharge", "dpgs", "coe"))
    # submitted sheets are locked for editing
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [_entry("s1", {"first_test": 1})]})
    assert r.status_code == 409, r.text
    assert (await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])).status_code == 409


async def t_approval_order_enforced_and_outsiders_blocked():
    sid = SHEET["o1_new"]
    for who in ("hod1", "incharge", "dpgs", "coe"):
        r = await _call("POST", f"/grading/sheets/{sid}/approve", TOK[who])
        assert r.status_code == 409, (who, r.status_code, r.text)         # not their turn while the instructor stage is open
        assert "not your turn" in r.text.lower(), r.text
    for who in ("fac_c", "fac_d2", "hod2", "s1", "s_other"):
        assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK[who])).status_code == 404, who
        assert (await _call("POST", f"/grading/sheets/{sid}/revert", TOK[who], json={"remark": "x"})).status_code == 404, who
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", S["SA"])).status_code == 403   # Super Admin cannot act
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["fac_a"])).status_code in (403, 409)  # creator already signed
    assert (await _detail(TOK["hod1"], sid))["permissions"]["can_approve"] is False
    assert (await _detail(TOK["fac_b"], sid))["permissions"]["can_approve"] is True


async def t_inboxes_reflect_who_is_next():
    sid = SHEET["o1_new"]
    ids = lambda r: {x["id"] for x in r.json()}   # noqa: E731
    assert sid in ids(await _call("GET", "/grading/inbox", TOK["fac_b"]))
    assert sid not in ids(await _call("GET", "/grading/inbox", TOK["fac_a"]))
    for who in ("hod1", "hod2", "incharge", "dpgs", "coe"):
        assert sid not in ids(await _call("GET", "/grading/inbox", TOK[who])), who
    assert (await _call("GET", "/grading/inbox", TOK["s1"])).status_code == 403
    assert (await _call("GET", "/grading/inbox?scope=finalized", TOK["hod1"])).status_code == 403


async def t_other_instructor_approval_advances_to_hod():
    sid = SHEET["o1_new"]
    r = await _call("POST", f"/grading/sheets/{sid}/approve", TOK["fac_b"])
    assert r.status_code == 200 and r.json()["status"] == "hod_pending", r.text
    st = [s for s in await _stage_rows(sid) if s.stage_type == "instructor"][0]
    assert st.status == "approved" and st.acted_at and st.approver_id == U["fac_b"] and st.acted_role == "faculty" and not st.is_deemed
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["fac_b"])).status_code in (403, 409)   # cannot sign twice


async def t_chain_order_hod_incharge_dpgs_coe():
    sid = SHEET["o1_new"]
    for who in ("incharge", "dpgs", "coe"):
        assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK[who])).status_code == 409, who   # incharge cannot precede HOD etc.
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["hod2"])).status_code == 404       # other department's HOD
    assert sid in {x["id"] for x in (await _call("GET", "/grading/inbox", TOK["hod1"])).json()}
    assert sid not in {x["id"] for x in (await _call("GET", "/grading/inbox", TOK["hod2"])).json()}


async def t_revert_requires_remark_and_returns_to_creator():
    sid = SHEET["o1_new"]
    for bad in ({"remark": ""}, {"remark": "   "}, {}):
        r = await _call("POST", f"/grading/sheets/{sid}/revert", TOK["hod1"], json=bad)
        assert r.status_code == 422, (bad, r.status_code)
    assert (await _detail(TOK["hod1"], sid))["status"] == "hod_pending"
    r = await _call("POST", f"/grading/sheets/{sid}/revert", TOK["hod1"], json={"remark": "Recheck Mid Term marks"})
    assert r.status_code == 200 and r.json()["status"] == "reverted", r.text
    d = await _detail(TOK["fac_a"], sid)
    assert d["is_locked"] is False and d["permissions"]["can_edit_data"] and d["permissions"]["can_submit"]
    rows = await _stage_rows(sid)
    by = {s.stage_type: s for s in rows}
    assert by["hod"].status == "reverted" and by["hod"].remark == "Recheck Mid Term marks" and by["hod"].approver_id == U["hod1"]
    assert by["incharge"].status == by["dpgs"].status == by["coe"].status == "cancelled"
    assert by["creator"].status == "approved" and by["instructor"].status == "approved", "earlier signatures stay as history"
    hist = (await _call("GET", f"/grading/sheets/{sid}/approvals", TOK["hod1"])).json()
    assert [(c["cycle_number"], c["status"]) for c in hist["cycles"]] == [(1, "reverted")], hist
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["incharge"])).status_code == 409   # nothing awaiting approval


async def t_resubmission_opens_a_clean_new_cycle():
    sid = SHEET["o1_new"]
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [_entry("s2", {"mid_term": 26})]})
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])
    assert r.status_code == 200 and r.json()["cycle_number"] == 2 and r.json()["status"] == "instructor_pending", r.text
    async with AsyncSessionLocal() as db:
        cycles = (await db.execute(select(GradesheetCycle).where(GradesheetCycle.sheet_id == uuid.UUID(sid)).order_by(GradesheetCycle.cycle_number))).scalars().all()
        assert [(c.cycle_number, c.status) for c in cycles] == [(1, "reverted"), (2, "open")]
        new_stages = list((await db.execute(select(GradesheetStage).where(GradesheetStage.cycle_id == cycles[1].id))).scalars().all())
    assert {(s.stage_type, s.status) for s in new_stages} == {("creator", "approved"), ("instructor", "pending"), ("hod", "pending"), ("incharge", "pending"), ("dpgs", "pending"), ("coe", "pending")}, \
        "fac_b's cycle-1 signature must NOT carry into cycle 2"
    d = await _detail(TOK["fac_a"], sid)
    assert d["signatories"]["cycle_number"] == 2 and d["signatories"]["instructors"][1]["status"] == "pending"
    hist = (await _call("GET", f"/grading/sheets/{sid}/approvals", TOK["fac_a"])).json()["cycles"]
    assert hist[0]["status"] == "reverted" and any(s["status"] == "approved" and s["stage_type"] == "instructor" for s in hist[0]["stages"])


async def t_instructor_can_revert_and_full_chain_finalizes():
    sid = SHEET["o1_new"]
    r = await _call("POST", f"/grading/sheets/{sid}/revert", TOK["fac_b"], json={"remark": "Attendance figure looks wrong"})
    assert r.status_code == 200, r.text                                       # any acting approver may revert
    assert (await _detail(TOK["fac_a"], sid))["status"] == "reverted"
    assert (await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])).json()["cycle_number"] == 3
    await _finalize_after_submit(sid, instructor_keys=["fac_b"])
    d = await _detail(TOK["coe"], sid)
    assert d["status"] == "approved" and d["is_locked"] is True and d["finalized_at"], d
    sig = d["signatories"]
    assert sig["cycle_number"] == 3 and sig["cycle_status"] == "completed"
    for s in [*sig["instructors"], sig["hod"], sig["incharge"], sig["dpgs"], sig["coe"]]:
        assert s["status"] == "approved" and s["signed_at"] and s["name"], s   # every signatory named + timestamped
    assert sig["hod"]["name"].endswith("GSHOD1") and sig["coe"]["name"].endswith("GSCOE")
    stamps = [s["signed_at"] for s in [sig["instructors"][0], sig["instructors"][1], sig["hod"], sig["incharge"], sig["dpgs"], sig["coe"]]]
    assert stamps == sorted(stamps), "signatures recorded in workflow order"


async def t_finalized_gradesheet_is_locked():
    sid = SHEET["o1_new"]
    r = await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [_entry("s1", {"first_test": 1})]})
    assert r.status_code == 409, r.text
    assert (await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_a"], json=_structure())).status_code == 409
    assert (await _call("POST", f"/grading/sheets/{sid}/revert", TOK["coe"], json={"remark": "late"})).status_code == 409
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["coe"])).status_code == 409


async def t_coe_reviews_and_lists_gradesheets():
    sid = SHEET["o1_new"]
    assert (await _call("GET", f"/grading/sheets/{sid}", TOK["coe"])).status_code == 200
    fin = (await _call("GET", "/grading/inbox?scope=finalized", TOK["coe"])).json()
    assert sid in {x["id"] for x in fin}, fin
    hist = (await _call("GET", f"/grading/sheets/{sid}/approvals", TOK["coe"])).json()["cycles"]
    assert [c["cycle_number"] for c in hist] == [1, 2, 3]
    assert (await _call("GET", "/grading/inbox?scope=nonsense", TOK["coe"])).status_code == 400


async def t_cross_department_isolation():
    r = await _call("POST", "/grading/sheets", TOK["fac_d2"], json={"offering_id": str(OFF["o_d2"]), "gradesheet_type": "new", **_theory_only()})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    SHEET["o_d2_new"] = sid
    assert (await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_d2"], json={"entries": [_entry("s_other", {"first_test": 15, "end_term": 60}, attendance=88)]})).status_code == 200
    for who in ("fac_a", "fac_b", "fac_c", "hod1"):
        assert (await _call("GET", f"/grading/sheets/{sid}", TOK[who])).status_code == 404, who
    assert (await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_d2"])).status_code == 200
    assert (await _detail(TOK["fac_d2"], sid))["status"] == "hod_pending"     # sole instructor: straight to HOD
    for who in ("fac_a", "hod1"):                                             # HOD of ANOTHER department: no read, no approve, no revert
        assert (await _call("GET", f"/grading/sheets/{sid}", TOK[who])).status_code == 404, who
        assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK[who])).status_code == 404, who
        assert (await _call("POST", f"/grading/sheets/{sid}/revert", TOK[who], json={"remark": "x"})).status_code == 404, who
    assert (await _call("GET", f"/grading/sheets/{sid}", TOK["hod2"])).status_code == 200
    assert (await _call("GET", f"/grading/sheets/{sid}", TOK["incharge"])).status_code == 200          # institution-wide once submitted
    assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["hod2"])).status_code == 200
    assert (await _call("GET", f"/grading/sheets/{sid}/document", TOK["hod1"])).status_code == 404


async def t_research_course_per_student_instructors():
    """Research Course: no OfferingFaculty — each student's own instructor grades THEIR row on the shared
    sheet (unchanged rule); the creator submits; the other instructor is derived from the entries'
    enrollments and approves; the chain then finalizes normally."""
    assert (await _call("POST", "/grading/sheets", TOK["fac_c"], json={"offering_id": str(OFF["o_res"]), "gradesheet_type": "new", **_theory_only()})).status_code == 404
    r = await _call("POST", "/grading/sheets", TOK["fac_a"], json={"offering_id": str(OFF["o_res"]), "gradesheet_type": "new", **_theory_only()})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    body = lambda who, marks: {"entries": [_entry(who, marks, attendance=90)]}   # noqa: E731
    assert (await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json=body("s3", {"first_test": 10, "end_term": 60}))).status_code == 403   # s3 is fac_b's student
    assert (await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_b"], json=body("s2", {"first_test": 10, "end_term": 60}))).status_code == 403   # s2 is fac_a's student
    assert (await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_c"], json=body("s2", {"first_test": 10, "end_term": 60}))).status_code == 404   # not an instructor at all
    assert (await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json=body("s2", {"first_test": 15, "end_term": 60}))).status_code == 200
    assert (await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_b"], json=body("s3", {"first_test": 18, "end_term": 70}))).status_code == 200
    assert (await _call("PUT", f"/grading/sheets/{sid}/structure", TOK["fac_b"], json=_theory_only())).status_code == 404       # structure/submit: creator only
    assert (await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_b"])).status_code == 404
    r = await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])
    assert r.status_code == 200 and r.json()["status"] == "instructor_pending" and r.json()["instructor_deadline_at"], r.text
    d = await _detail(TOK["fac_b"], sid)
    assert [(s["stage_type"], s["status"]) for s in d["signatories"]["instructors"]] == [("creator", "approved"), ("instructor", "pending")], d["signatories"]
    assert d["signatories"]["instructors"][1]["name"].endswith("GSFAC_B") and d["permissions"]["can_approve"] is True
    await _finalize_after_submit(sid, instructor_keys=["fac_b"])
    final = await _detail(TOK["coe"], sid)
    assert final["status"] == "approved" and {e["grade_letter"] for e in final["entries"]} == {"A", "A+"}, final["entries"]


# ── 24-hour auto-forward (controlled clock) ──────────────────────────────────

async def t_no_other_instructor_means_no_deadline():
    r = await _call("POST", "/grading/sheets", TOK["fac_a"], json={"offering_id": str(OFF["o3"]), "gradesheet_type": "new", **_theory_only(first=0, end=100, theory_components=[{"code": "end_term", "max_marks": 100}])})
    assert r.status_code == 201, r.text
    SHEET["o3_new"] = r.json()["id"]
    await _call("PUT", f"/grading/sheets/{SHEET['o3_new']}/entries", TOK["fac_a"], json={"entries": [_entry("s1", {"end_term": 65}, attendance=95)]})
    r = await _call("POST", f"/grading/sheets/{SHEET['o3_new']}/submit", TOK["fac_a"])
    assert r.status_code == 200 and r.json()["status"] == "hod_pending" and r.json()["instructor_deadline_at"] is None, r.text


async def t_deadline_lazy_auto_forward_to_hod():
    r = await _create(TOK["fac_a"], "o2", **_theory_only())
    assert r.status_code == 201, r.text
    sid = SHEET["o2_new"] = r.json()["id"]
    await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [
        _entry("s1", {"first_test": 15, "end_term": 60}, attendance=92), _entry("s2", {"first_test": 10, "end_term": 40}, attendance=76)]})
    t0 = datetime(2097, 3, 1, 10, 0, tzinfo=timezone.utc)
    _set_now(t0)
    try:
        r = await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])
        assert r.status_code == 200 and datetime.fromisoformat(r.json()["instructor_deadline_at"]) == t0 + timedelta(hours=24), r.text
        _set_now(t0 + timedelta(hours=23, minutes=59))
        r = await _call("POST", f"/grading/sheets/{sid}/approve", TOK["hod1"])
        assert r.status_code == 409, r.text                                   # one minute short of the window
        assert (await _detail(TOK["hod1"], sid))["status"] == "instructor_pending"
        _set_now(t0 + timedelta(hours=24, minutes=1))
        d = await _detail(TOK["hod1"], sid)                                   # lazy, server-side transition on read
        assert d["status"] == "hod_pending", d["status"]
        inst = d["signatories"]["instructors"][1]
        assert inst["status"] == "deemed_approved" and inst["is_deemed"] is True and inst["signed_at"] is None and inst["name"].endswith("GSFAC_B"), inst
        assert "24 hours" in inst["remark"]
        r = await _call("POST", f"/grading/sheets/{sid}/approve", TOK["hod1"])
        assert r.status_code == 200, r.text
        assert (await _call("POST", f"/grading/sheets/{sid}/approve", TOK["fac_b"])).status_code in (403, 409)   # too late
    finally:
        _restore_now()
    await _finalize_rest(sid)


async def _finalize_rest(sid):
    for k in ("incharge", "dpgs", "coe"):
        r = await _call("POST", f"/grading/sheets/{sid}/approve", TOK[k]); assert r.status_code == 200, (k, r.text)
    assert (await _detail(TOK["coe"], sid))["status"] == "approved"


async def t_deadline_sweeper_is_idempotent_and_server_authoritative():
    """Sweeper path (worker) — and proof the client cannot influence the deadline."""
    r = await _call("POST", "/grading/sheets", TOK["fac_a"], json={"offering_id": str(OFF["o2"]), "gradesheet_type": "repeat", "related_sheet_id": SHEET["o2_new"], **_theory_only(), "student_ids": [str(U["s2"])],
                                                                    "instructor_deadline_at": "2000-01-01T00:00:00Z", "now": "2999-01-01T00:00:00Z"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    await _call("PUT", f"/grading/sheets/{sid}/entries", TOK["fac_a"], json={"entries": [_entry("s2", {"first_test": 12, "end_term": 60}, attendance=81)]})
    t1 = datetime(2097, 4, 1, 9, 0, tzinfo=timezone.utc)
    _set_now(t1)
    try:
        assert (await _call("POST", f"/grading/sheets/{sid}/submit", TOK["fac_a"])).status_code == 200
        async with AsyncSessionLocal() as db:
            assert await flow.sweep_overdue(db, [uuid.UUID(sid)]) == 0, "nothing overdue yet"
        _set_now(t1 + timedelta(hours=25))
        async with AsyncSessionLocal() as db:
            assert await flow.sweep_overdue(db, [uuid.UUID(sid)]) == 1
        async with AsyncSessionLocal() as db:
            assert await flow.sweep_overdue(db, [uuid.UUID(sid)]) == 0, "idempotent: nothing left to forward"
        assert (await _detail(TOK["hod1"], sid))["status"] == "hod_pending"
        rows = await _stage_rows(sid)
        assert [s.status for s in rows if s.stage_type == "instructor"] == ["deemed_approved"]
    finally:
        _restore_now()
    SHEET["o2_repeat"] = sid


# ── CoE compilation / results ────────────────────────────────────────────────

async def t_coe_lists_semester_students_and_readiness():
    r = await _call("GET", "/results/coe/semesters", TOK["coe"])
    assert str(S["SEM1"].id) in {x["semester_id"] for x in r.json()}
    r = await _call("GET", f"/results/coe/semesters/{S['SEM1'].id}/students", TOK["coe"])
    assert r.status_code == 200, r.text
    rows = {x["student_id"]: x for x in r.json() if x["student_id"] in {str(U[k]) for k in ("s1", "s2", "s3", "s_other")}}
    assert rows[str(U["s1"])]["ready"] is True and rows[str(U["s1"])]["courses_total"] == 2 and rows[str(U["s1"])]["result"] is None
    assert rows[str(U["s3"])]["ready"] is True
    assert rows[str(U["s_other"])]["ready"] is False and rows[str(U["s_other"])]["courses_finalized"] == 0, "o_d2 sheet is only at HOD stage... not finalized"
    for who in ("fac_a", "hod1", "incharge", "s1", "dpgs"):
        assert (await _call("GET", f"/results/coe/semesters/{S['SEM1'].id}/students", TOK[who])).status_code == 403, who


async def t_only_coe_can_compile_and_publish():
    body = {"semester_id": str(S["SEM1"].id), "items": [{"student_id": str(U["s1"]), "result_status": "pass"}]}
    for who in ("fac_a", "fac_ma", "hod1", "incharge", "dpgs", "s1"):
        assert (await _call("POST", "/results/coe/compile", TOK[who], json=body)).status_code == 403, who
    assert (await _call("POST", "/results/coe/compile", S["SA"], json=body)).status_code == 403    # no Super Admin stand-in for the CoE
    assert (await _call("POST", "/results/coe/publish", TOK["hod1"], json={"result_ids": [str(uuid.uuid4())]})).status_code == 403
    assert (await _call("POST", "/results/coe/publish", S["SA"], json={"result_ids": [str(uuid.uuid4())]})).status_code == 403


async def t_coe_compiles_pass_and_pass_with_backlogs_manually():
    bad = {"semester_id": str(S["SEM1"].id), "items": [{"student_id": str(U["s1"]), "result_status": "promoted"}]}
    assert (await _call("POST", "/results/coe/compile", TOK["coe"], json=bad)).status_code == 422   # only the two confirmed outcomes
    body = {"semester_id": str(S["SEM1"].id), "items": [
        {"student_id": str(U["s_other"]), "result_status": "pass"},            # not finalized -> reported FIRST; the rest must still compile
        {"student_id": str(U["s1"]), "result_status": "pass"},
        {"student_id": str(U["s2"]), "result_status": "pass_with_backlogs"},   # CoE's manual choice — stored as chosen, not inferred
    ]}
    r = await _call("POST", "/results/coe/compile", TOK["coe"], json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["compiled"]) == 2 and len(out["errors"]) == 1 and "pending" in out["errors"][0]["detail"].lower() and out["errors"][0]["student_id"] == str(U["s_other"]), out
    by = {c["student_id"]: c for c in out["compiled"]}
    S["R1"], S["R2"] = by[str(U["s1"])]["result_id"], by[str(U["s2"])]["result_id"]
    # s1: O(10)x3 + course c2 grade for (15+60)=75 -> A(8)x2 => (30+16)/5 = 9.2
    assert by[str(U["s1"])]["gpa"] == "9.200" and by[str(U["s2"])]["gpa"] == "7.400", by      # s2: A+(9)x3 + C(5)x2 => 37/5
    async with AsyncSessionLocal() as db:
        rs = {r.student_id: r for r in (await db.execute(select(StudentSemesterResult).where(StudentSemesterResult.id.in_([uuid.UUID(S["R1"]), uuid.UUID(S["R2"])])))).scalars().all()}
    assert rs[U["s1"]].result_status == "pass" and rs[U["s2"]].result_status == "pass_with_backlogs"
    assert rs[U["s1"]].status == "compiled" and rs[U["s1"]].compiled_by == U["coe"] and rs[U["s1"]].total_credits == Decimal("5")
    assert rs[U["s1"]].total_credit_points == Decimal("46") and rs[U["s1"]].gpa == Decimal("9.2")
    assert rs[U["s1"]].exam_label == "December 2097 Examination" and rs[U["s1"]].semester_name == "Semester I" and rs[U["s1"]].academic_year == "2097-98"
    assert rs[U["s1"]].degree_name == S["PG"].name and rs[U["s1"]].college_name == f"{_LABEL} College" and rs[U["s1"]].department_name == S["D1"].name


async def t_unpublished_result_is_invisible():
    for who in ("s1", "s2", "hod1", "incharge", "dpgs", "fac_ma", "fac_a"):
        assert (await _call("GET", f"/results/{S['R1']}", TOK[who])).status_code == 404, who
        assert (await _call("GET", f"/results/{S['R1']}/grade-card", TOK[who])).status_code == 404, who
    assert (await _call("GET", "/results/mine", TOK["s1"])).json() == []
    assert (await _call("GET", f"/results/student/{U['s1']}", TOK["s1"])).json() == []
    tr = (await _call("GET", "/results/tracking", TOK["s1"])).json()
    assert {r["status"] for r in tr["rows"]} == {"pending"}, tr
    d = (await _call("GET", f"/results/{S['R1']}", TOK["coe"])).json()                    # CoE previews the compiled result
    assert d["status"] == "compiled" and d["gpa"] == "9.200" and d["cgpa"] is None
    assert (await _call("POST", "/results/coe/publish", TOK["coe"], json={"result_ids": [str(uuid.uuid4())]})).json()["errors"][0]["detail"] == "Result not found."


async def t_publish_and_student_result_management():
    r = await _call("POST", "/results/coe/publish", TOK["coe"], json={"result_ids": [S["R1"], S["R2"]]})
    assert r.status_code == 200 and sorted(r.json()["published"]) == sorted([S["R1"], S["R2"]]) and not r.json()["errors"], r.text
    again = (await _call("POST", "/results/coe/publish", TOK["coe"], json={"result_ids": [S["R1"]]})).json()
    assert again["errors"][0]["detail"] == "Already published."
    mine = (await _call("GET", "/results/mine", TOK["s1"])).json()
    assert len(mine) == 1 and mine[0]["id"] == S["R1"] and mine[0]["semester"] == "Semester I" and mine[0]["academic_year"] == "2097-98"
    assert mine[0]["degree"] == S["PG"].name and mine[0]["result_status"] == "pass" and mine[0]["result_status_label"] == "Pass"
    d = (await _call("GET", f"/results/{S['R1']}", TOK["s1"])).json()
    st = d["student"]
    assert (st["name"].endswith("GSS1"), st["roll_no"], st["college"], st["department"]) == (True, f"ZZGS-1-{_TAG}", f"{_LABEL} College", S["D1"].name)
    assert d["result_status_label"] == "Pass" and d["semester"] == "Semester I" and d["academic_year"] == "2097-98"
    assert d["promoted_class"] is None and d["admission_status"] is None and d["class_label"] is None, "progression fields are never fabricated"
    assert [c["grade_letter"] for c in d["courses"]] == ["O", "A"] and d["courses"][0]["course_number"].startswith("ZZC1-")
    assert d["totals"] == {"credits": 5.0, "grade_points": 18.0, "credit_points": 46.0}
    assert d["gpa"] == "9.200" and d["cgpa"] is None and d["cgpa_applicable"] is False     # first semester: GPA only
    assert (await _call("GET", f"/results/{S['R2']}", TOK["s2"])).json()["result_status_label"] == "Pass with Backlogs"


async def t_recompile_of_published_result_refused():
    body = {"semester_id": str(S["SEM1"].id), "items": [{"student_id": str(U["s1"]), "result_status": "pass_with_backlogs"}]}
    out = (await _call("POST", "/results/coe/compile", TOK["coe"], json=body)).json()
    assert out["compiled"] == [] and "already published" in out["errors"][0]["detail"].lower(), out


async def t_result_tracking_reflects_compilation():
    tr = (await _call("GET", "/results/tracking", TOK["s1"])).json()
    st = {r["course_code"][:4]: r["status"] for r in tr["rows"]}
    assert st == {"ZZC1": "compiled", "ZZC2": "compiled", "ZZC3": "pending"}, st       # semester II course not yet compiled
    row = next(r for r in tr["rows"] if r["course_code"].startswith("ZZC1"))
    assert row["course_title"] == "ZZTEST Molecular Diagnostics" and row["course_department"] == S["D1"].name
    only = (await _call("GET", f"/results/tracking?semester_id={S['SEM2'].id}", TOK["s1"])).json()["rows"]
    assert [r["course_code"][:4] for r in only] == ["ZZC3"]
    yr = (await _call("GET", f"/results/tracking?calendar_id={S['CAL'].id}", TOK["s1"])).json()
    assert len(yr["rows"]) == 3 and yr["filters"]["academic_years"][0]["academic_year"] == "2097-98" and len(yr["filters"]["semesters"]) == 2
    assert (await _call("GET", "/results/tracking", TOK["s_other"])).json()["rows"][0]["status"] == "pending"
    assert (await _call("GET", "/results/tracking", TOK["hod1"])).status_code == 403


async def t_student_cannot_reach_another_students_result():
    for path in (f"/results/{S['R2']}", f"/results/{S['R2']}/grade-card", f"/results/student/{U['s2']}"):
        assert (await _call("GET", path, TOK["s1"])).status_code == 404, path            # changing the id exposes nothing
    for path in (f"/results/{S['R1']}", f"/results/{S['R1']}/grade-card"):
        assert (await _call("GET", path, TOK["s3"])).status_code == 404, path
    assert (await _call("GET", f"/results/{S['R1']}", TOK["s_other"])).status_code == 404
    assert (await _call("GET", f"/grading/student/{U['s2']}/gpa", TOK["s1"])).status_code == 403   # existing GPA-endpoint rule preserved


async def t_staff_visibility_of_published_results():
    ok = {"hod1": 200, "incharge": 200, "dpgs": 200, "fac_ma": 200, "coe": 200}
    for who, code in ok.items():
        assert (await _call("GET", f"/results/{S['R1']}", TOK[who])).status_code == code, who
    for who in ("hod2", "fac_c", "fac_a", "fac_d2"):
        assert (await _call("GET", f"/results/{S['R1']}", TOK[who])).status_code == 404, who        # other department / not the advisor
    assert (await _call("GET", f"/results/{S['R2']}", TOK["fac_ma"])).status_code == 404          # advisor of s1 only, not s2
    assert len((await _call("GET", f"/results/student/{U['s1']}", TOK["hod1"])).json()) == 1


async def t_second_semester_cgpa_is_average_of_semester_gpas():
    sid = SHEET["o3_new"]
    await _finalize_after_submit_from_hod(sid)
    r = await _call("POST", "/results/coe/compile", TOK["coe"], json={"semester_id": str(S["SEM2"].id), "items": [{"student_id": str(U["s1"]), "result_status": "pass"}]})
    out = r.json()
    assert len(out["compiled"]) == 1 and out["compiled"][0]["gpa"] == "7.000", out          # B+ (7) x 2 credits
    rid2 = out["compiled"][0]["result_id"]
    S["R1_S2"] = rid2
    assert (await _call("GET", f"/results/{rid2}", TOK["s1"])).status_code == 404               # unpublished
    assert (await _call("GET", f"/results/{rid2}", TOK["coe"])).json()["cgpa"] == "8.100"      # CoE preview already applies the rule
    await _call("POST", "/results/coe/publish", TOK["coe"], json={"result_ids": [rid2]})
    d = (await _call("GET", f"/results/{rid2}", TOK["s1"])).json()
    assert d["gpa"] == "7.000" and d["cgpa"] == "8.100" and d["cgpa_applicable"] is True, d    # (9.2 + 7.0) / 2 — plain average, not credit-weighted
    first = (await _call("GET", f"/results/{S['R1']}", TOK["s1"])).json()
    assert first["cgpa"] is None, "the first semester still shows GPA only"
    g = (await _call("GET", f"/grading/student/{U['s1']}/gpa", TOK["s1"])).json()
    assert [x["sgpa"] for x in g["semesters"]] == ["9.200", "7.000"] and g["cgpa"] == "8.100" and g["cgpa_applicable"] is True, g
    assert (await _call("GET", f"/grading/student/{U['s1']}/gpa", TOK["fac_ma"])).status_code == 200
    mine = (await _call("GET", "/results/mine", TOK["s1"])).json()
    assert [m["semester"] for m in mine] == ["Semester II", "Semester I"]
    assert len((await _call("GET", f"/results/mine?semester_id={S['SEM1'].id}", TOK["s1"])).json()) == 1


async def _finalize_after_submit_from_hod(sid):
    for k in ("hod1", "incharge", "dpgs", "coe"):
        r = await _call("POST", f"/grading/sheets/{sid}/approve", TOK[k]); assert r.status_code == 200, (k, r.text)


async def t_no_academic_progression_side_effects():
    async with AsyncSessionLocal() as db:
        s1 = await db.get(User, U["s1"])
        assert s1.academic_year_id is None and s1.admission_year is None, "compiling/publishing never promotes or re-classifies a student"
        assert (await db.execute(select(func.count()).select_from(StudentEnrollment).where(StudentEnrollment.student_id == U["s1"]))).scalar_one() == 3


# ── PDFs ─────────────────────────────────────────────────────────────────────

def _pdf_text(resp: httpx.Response) -> str:
    from pypdf import PdfReader
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    assert resp.headers["content-type"] == "application/pdf" and resp.content[:5] == b"%PDF-"
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(resp.content)).pages)


async def t_gradesheet_pdf_contains_course_marks_components_and_signatories():
    txt = _pdf_text(await _call("GET", f"/grading/sheets/{SHEET['o1_new']}/document", TOK["fac_a"]))
    flat = " ".join(txt.split())
    for needle in ("COURSE GRADESHEET", f"ZZC1-{_TAG}", "ZZTEST Molecular Diagnostics", "3(2+1)", "Credit", S["D1"].name, "Semester I", "2097-98",
                   f"ZZTEST_GS College", "First Test (10)", "Mid Term (30)", "End Term (60)", "Practical (100)", "Total Theory (100)", "Grand Total (200)",
                   "Marks %", "Attendance %", "Remark", f"ZZGS-1-{_TAG}", f"ZZGS-3-{_TAG}", "GSS2", "Excellent", "100.00", "82.00", "89.50",
                   "90.00", "80.00", "70.00", "A+", "Total Students", "Male 1, Female 2"):
        assert needle in flat, f"missing in Gradesheet PDF: {needle!r}"
    for needle in ("Course Instructor (Prepared by)", "GSFAC_A", "GSFAC_B", "HOD", "GSHOD1", "Incharge Academic Cell", "GSINCHARGE", "DPGS", "GSDPGS",
                   "Controller of Examination", "GSCOE", "IST"):
        assert needle in flat, f"missing signatory content in PDF: {needle!r}"
    assert flat.count("Signed ") >= 6, "every signatory carries a signed-at timestamp"
    assert "Pending" not in flat.split("Signatories")[1], "a fully approved sheet has no pending signatory"
    assert "Approved" in flat
    # a draft/partially approved sheet shows pending signatories instead
    txt2 = " ".join(_pdf_text(await _call("GET", f"/grading/sheets/{SHEET['o1_revised']}/document", TOK["fac_a"])).split())
    assert "Not yet submitted" in txt2 and "Revised" in txt2
    for who in ("s1", "fac_c", "hod2"):
        assert (await _call("GET", f"/grading/sheets/{SHEET['o1_new']}/document", TOK[who])).status_code == 404, who


async def t_deemed_approval_shown_in_pdf_signatories():
    txt = " ".join(_pdf_text(await _call("GET", f"/grading/sheets/{SHEET['o2_new']}/document", TOK["coe"])).split())
    assert "Not signed" in txt and "auto-forwarded after 24 hours" in txt, "deemed approval must not be presented as a signature"


async def t_grade_card_pdf_content_and_security():
    txt = " ".join(_pdf_text(await _call("GET", f"/results/{S['R1_S2']}/grade-card", TOK["s1"])).split())
    for needle in ("Assam Veterinary and Fishery University", "Statement of Marks/Grades", f"{S['PG'].name} ({_LABEL} College)", f"Semester II ({_LABEL} College)",
                   "May 2098 Examination", "2097-98", f"ZZGS-1-{_TAG}", "GSS1", "Sub Code/Course ID", "Subject/Papers", "Grade Points", "Credit Points",
                   f"ZZC3-{_TAG}", "ZZTEST Advanced Immunology", "B+", "7.000", "14.000", "Total", "GPA: 7.000", "Result: Pass", "CGPA: 8.100", "Dated:"):
        assert needle in txt, f"missing in Grade Card PDF: {needle!r}"
    first = " ".join(_pdf_text(await _call("GET", f"/results/{S['R1']}/grade-card", TOK["s1"])).split())
    assert "GPA: 9.200" in first and "CGPA: —" in first and "December 2097 Examination" in first and "Semester I (" in first
    assert f"ZZC1-{_TAG}" in first and f"ZZC2-{_TAG}" in first and "46.000" in first
    assert (await _call("GET", f"/results/{S['R2']}/grade-card", TOK["s1"])).status_code == 404
    assert (await _call("GET", f"/results/{S['R1']}/grade-card", TOK["hod2"])).status_code == 404
    pb = " ".join(_pdf_text(await _call("GET", f"/results/{S['R2']}/grade-card", TOK["s2"])).split())
    assert "Result: Pass with Backlogs" in pb and "GPA: 7.400" in pb


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "GPA = credit points / credits; 166.550/20 = 8.3275 displays as 8.327": t_gpa_confirmed_example,
            "CGPA: none in first semester, average of semester GPAs afterwards": t_cgpa_rules,
            "Grade calculation: fractional marks resolve correctly; pass marks; absent; attendance bands": t_grade_calculation_fractional_marks,
            "CoE role: single holder, departmentless, assignable by Super Admin, master-data row present": t_coe_role_is_single_holder_and_departmentless,
            "Assigned courses are scoped in the backend to the caller's own offerings": t_assigned_courses_are_backend_scoped,
            "Create: only a real instructor of the offering (others 404, wrong roles 403)": t_create_requires_real_instructor_of_the_offering,
            "Create: invalid component/pass-mark configurations rejected, nothing persisted": t_invalid_component_configurations_rejected,
            "Create New gradesheet with configured theory + practical components": t_create_new_gradesheet_with_configured_components,
            "Create Repeat / Revised / Make up (explicit type, related sheet, student subset)": t_all_gradesheet_types_stored_explicitly_and_related,
            "Gradesheet read isolation before submission (draft = instructors only)": t_gradesheet_read_isolation_before_submission,
            "Enter marks + attendance; totals, %, grade, attendance band (incl. fractional marks)": t_enter_marks_attendance_and_grade_calculation,
            "Entry validation: max marks, negatives, unknown component, attendance range, atomic batch": t_entry_validation,
            "Only the creating instructor enters data (co-instructors/outsiders cannot)": t_only_the_creator_enters_data,
            "Absent student gets F": t_absent_student_gets_grade_f,
            "Structure update rules (existing marks guard)": t_structure_update_rules,
            "Submit requires complete marks and the creator": t_submit_requires_complete_marks,
            "Submit: creator signs, other instructor stage + 24h deadline opened, sheet locked": t_submit_signs_creator_and_opens_instructor_stage,
            "Approval order enforced; outsiders/Super Admin blocked": t_approval_order_enforced_and_outsiders_blocked,
            "Inboxes show only the actor whose turn it is": t_inboxes_reflect_who_is_next,
            "Other instructor approval (signature + timestamp) advances to HOD": t_other_instructor_approval_advances_to_hod,
            "HOD/Incharge/DPGS/CoE cannot skip ahead; other-department HOD blocked": t_chain_order_hod_incharge_dpgs_coe,
            "Revert needs a remark, cancels later stages, returns to creator, history kept": t_revert_requires_remark_and_returns_to_creator,
            "Resubmission opens a clean new cycle (no stale signatures)": t_resubmission_opens_a_clean_new_cycle,
            "Instructor revert; full chain HOD->Incharge->DPGS->CoE finalizes with timestamped signatories": t_instructor_can_revert_and_full_chain_finalizes,
            "Finalized gradesheet is locked": t_finalized_gradesheet_is_locked,
            "CoE reviews finalized gradesheets and approval history": t_coe_reviews_and_lists_gradesheets,
            "Cross-department isolation (faculty, HOD) for gradesheets": t_cross_department_isolation,
            "Research Course: per-student instructors grade their own rows, co-instructor approves, chain finalizes": t_research_course_per_student_instructors,
            "Sole instructor: no deadline, straight to HOD": t_no_other_instructor_means_no_deadline,
            "24h rule (controlled clock): deemed approval, lazy server-side forward to HOD": t_deadline_lazy_auto_forward_to_hod,
            "24h rule: sweeper is idempotent; client-supplied deadline/time ignored": t_deadline_sweeper_is_idempotent_and_server_authoritative,
            "CoE lists semester students and readiness (non-CoE blocked)": t_coe_lists_semester_students_and_readiness,
            "Only the CoE can compile/publish (Super Admin and all others 403)": t_only_coe_can_compile_and_publish,
            "CoE compiles Pass / Pass with Backlogs manually; GPA stored; per-student errors": t_coe_compiles_pass_and_pass_with_backlogs_manually,
            "Unpublished results are invisible to students and other staff": t_unpublished_result_is_invisible,
            "Publish -> student Result Management + details/marksheet (no fabricated progression)": t_publish_and_student_result_management,
            "Published results cannot be recompiled": t_recompile_of_published_result_refused,
            "Result Tracking statuses and filters": t_result_tracking_reflects_compilation,
            "A student cannot reach another student's result / marksheet / grade card": t_student_cannot_reach_another_students_result,
            "Staff visibility of published results (HOD dept, Major Advisor, Incharge/DPGS)": t_staff_visibility_of_published_results,
            "Second semester CGPA = average of semester GPAs (first semester GPA only)": t_second_semester_cgpa_is_average_of_semester_gpas,
            "Compile/publish has no academic-progression side effects": t_no_academic_progression_side_effects,
            "Gradesheet PDF: course info, components, marks, attendance, grades, signatories + timestamps": t_gradesheet_pdf_contains_course_marks_components_and_signatories,
            "Gradesheet PDF: deemed approval is not shown as a signature": t_deemed_approval_shown_in_pdf_signatories,
            "Grade Card PDF: content, GPA/CGPA, Pass / Pass with Backlogs, authorization": t_grade_card_pdf_content_and_security,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()

    async with AsyncSessionLocal() as db:
        left = {
            "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one(),
            "courses": (await db.execute(select(func.count()).select_from(Course).where(Course.title.like("ZZTEST%")))).scalar_one(),
            "offerings": (await db.execute(select(func.count()).select_from(CourseOffering).where(CourseOffering.course_id.in_(list(CRS.values()))))).scalar_one(),
            "enrollments": (await db.execute(select(func.count()).select_from(StudentEnrollment).where(StudentEnrollment.offering_id.in_(list(OFF.values()))))).scalar_one(),
            "sheets": (await db.execute(select(func.count()).select_from(GradeSheet).where(GradeSheet.offering_id.in_(list(OFF.values()))))).scalar_one(),
            "results": (await db.execute(select(func.count()).select_from(StudentSemesterResult).where(StudentSemesterResult.student_name.like("ZZTEST%")))).scalar_one(),
            "calendars": (await db.execute(select(func.count()).select_from(AcademicCalendar).where(AcademicCalendar.name.like(f"{_LABEL}%")))).scalar_one(),
            "coe_assignments": (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.role == UserRole.CONTROLLER_OF_EXAMINATION))).scalar_one(),
            "colleges": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
            "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            "orphan_assignments": (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one(),
        }
    record("cleanup: no ZZTEST_GS user / course / offering / enrollment / gradesheet / cycle / result / calendar / CoE assignment / session row remains",
           not any(left.values()), str(left))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for n, d in FAILED:
            print(f"  FAILED: {n} — {d}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
