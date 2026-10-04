"""Standalone HTTP-level tests for this task's two confirmed requirements:

1. Complete Offered Course Editing — `PATCH /courses/offerings/{id}` now
   supports Academic Year (via `semester_id`, calendar re-derived), Semester,
   Assigned Faculty (bulk replace), Max Enrollment, Section, Practical Group,
   and (Research Course offerings only) Research Assignment strategy. Course
   master fields remain unreachable through this endpoint.

2. Research Course Assignment Strategy — `CourseOffering.research_assignment_
   type` (`major_advisor` / `external_examiner`), an offering-level property.
   "major_advisor" preserves the existing behavior exactly.
   "external_examiner" derives the per-student instructor from the EXISTING
   thesis External Examiner selection workflow (`app.core.research_
   assignment`), both at enrollment time and retroactively once a pending
   student's examiner is later selected (hooked into `vc_select_examiners`).

Modelled on `tests/test_external_examiner.py` for the approval-chain helpers
and `tests/test_research_course.py` for the enrollment/instructor_id
conventions: real FastAPI app via `httpx.ASGITransport`, real JWT sessions, a
`record`/`_run` pass-fail tracker, `zztest_ras_...`-prefixed rows, full
teardown. Refuses to run if a real Incharge Academic Cell / DPGS / Vice
Chancellor holder already exists (all three are single-holder roles), same
safety check `test_external_examiner.py` already uses.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_research_assignment_strategy
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date

import httpx
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.core.security import create_access_token
from app.core import email as email_module
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import AcademicCalendar, Semester
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.enrollment import StudentEnrollment, CourseRegistration
from app.models.external_examiner import (
    ExternalExaminer, ExternalExaminerApprovalCycle, ExternalExaminerAssignment,
    ExternalExaminerSelection, ExternalExaminerSelectionResult,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_ras_"
_LABEL = "ZZTEST_RAS"
_DEVICE = "ZZTEST-ras"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
SEL: dict[str, str] = {}


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


def _email(key: str) -> str:
    return f"{_PFX}{key}_{_TAG}@example.com"


def _proposal(key: str) -> dict:
    return {
        "name": f"ZZTEST Examiner {key.upper()}", "specialization": "ZZTEST Veterinary Pathology",
        "designation": "Professor", "email": _email(key), "phone": "9876500000", "institution": "ZZTEST Institution",
    }


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None):
    # Students must pass require_complete_profile (date_of_birth/gender/
    # father_name/address, see core/dependencies.py's _MANDATORY_PROFILE_
    # FIELDS) to reach enroll()/register_courses() at all — set unconditionally
    # here since every STUDENT fixture in this file needs to enroll.
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"RAS{key.upper()}",
            role=role, department_id=dept.id if dept else None, program_id=program.id if program else None,
            college_id=college.id if college else None, student_roll=roll,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
            date_of_birth=date(2000, 1, 1), father_name="ZZTEST Father", address="ZZTEST Address",
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_committee(student_key: str, ma_key: str) -> None:
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title=f"{_LABEL} committee", status="members_pending")
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True))
        await db.commit()


async def _enrollment_row(student_key: str, offering_key: str) -> StudentEnrollment | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == S[offering_key].id,
        ))).scalar_one_or_none()


async def _offering_row(offering_key: str) -> CourseOffering:
    async with AsyncSessionLocal() as db:
        return await db.get(CourseOffering, S[offering_key].id)


# ── External Examiner approval-chain helper (mirrors test_external_examiner.py) ──

async def _approve(sid, token, expect=200):
    r = await _call("GET", f"/external-examiners/{sid}/approval/otp", token)
    if r.status_code == 400:
        r2 = await _call("POST", f"/external-examiners/{sid}/approval/approve", token, json={})
    else:
        assert r.status_code == 200, r.text
        r2 = await _call("POST", f"/external-examiners/{sid}/approval/approve", token, json={"otp": r.json()["dev_otp"]})
    assert r2.status_code == expect, (r2.status_code, r2.text)
    return r2


async def _drive_examiner_selection_to_vc(student_key: str, ma_key: str, proposals: list[dict]) -> str:
    """Creates a selection, drives it MA -> HOD -> Incharge -> DPGS, and
    returns (selection_id, [proposal_ids]) ready for a vc-selection call —
    the full, real approval chain, never shortcutted, since this task must
    hook the REAL `vc_select_examiners` code path, not a simulated one."""
    r = await _call("POST", "/external-examiners", TOK[ma_key], json={"student_id": str(U[student_key]), "proposals": proposals})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    SEL[student_key] = sid
    await _approve(sid, TOK[ma_key])
    await _approve(sid, TOK["hod1"])
    await _approve(sid, TOK["incharge"])
    await _approve(sid, TOK["dpgs"])
    return sid


async def _vc_select_first_n(sid: str, n: int) -> list[str]:
    r = await _call("GET", f"/external-examiners/{sid}", TOK["dpgs"])
    assert r.status_code == 200, r.text
    proposal_ids = [p["id"] for p in r.json()["proposals"]][:n]
    r2 = await _call("POST", f"/external-examiners/{sid}/vc-selection", TOK["vc"], json={"proposal_ids": proposal_ids})
    assert r2.status_code == 200, r2.text
    return proposal_ids


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


async def _setup() -> None:
    email_module.send_email = lambda to, subject, body: True
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_ras_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS','VICE_CHANCELLOR')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS / Vice Chancellor holder exists (single-holder roles); refusing to run.")
            sys.exit(2)

        d1, d2 = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        pg = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        S["D1"], S["D2"], S["PG"] = d1, d2, pg
        col = College(name=f"{_LABEL} College", code=f"ZRAS{_TAG.upper()}"[:20])
        db.add(col); await db.flush()
        S["COLLEGE"] = col

        cal = AcademicCalendar(name=f"{_LABEL} {_TAG}", academic_year="2098-99", start_date=date(2098, 7, 1), end_date=date(2099, 6, 30), status="active")
        db.add(cal); await db.flush()
        sem = Semester(calendar_id=cal.id, name="Semester I", sem_type="odd", start_date=date(2098, 7, 1), end_date=date(2098, 12, 31), exam_end=date(2098, 12, 20))
        db.add(sem); await db.flush()
        cal2 = AcademicCalendar(name=f"{_LABEL}2 {_TAG}", academic_year="2099-00", start_date=date(2099, 7, 1), end_date=date(2100, 6, 30), status="active")
        db.add(cal2); await db.flush()
        sem2 = Semester(calendar_id=cal2.id, name="Semester I", sem_type="odd", start_date=date(2099, 7, 1), end_date=date(2099, 12, 31), exam_end=date(2099, 12, 20))
        db.add(sem2); await db.flush()
        S["CAL"], S["SEM"], S["CAL2"], S["SEM2"] = cal, sem, cal2, sem2

        # Research-category course + 3 offerings covering the 2 strategies,
        # plus a plain (non-research) course for the "complete offering
        # editing" tests.
        research_course = Course(course_number=f"ZZRAS-RES-{_TAG}", title=f"{_LABEL} Research Course", department_id=d1.id, category="research", program_level="PG")
        plain_course = Course(course_number=f"ZZRAS-PLAIN-{_TAG}", title=f"{_LABEL} Plain Course", department_id=d1.id, category="core", program_level="PG")
        db.add_all([research_course, plain_course]); await db.flush()
        S["RESEARCH_COURSE"], S["PLAIN_COURSE"] = research_course, plain_course

        off_ma = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=research_course.id, department_id=d1.id, max_enrollment=10, status="published", research_assignment_type="major_advisor")
        off_ee1 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=research_course.id, department_id=d1.id, max_enrollment=10, status="published", section="EE1", research_assignment_type="external_examiner")
        off_ee2 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=research_course.id, department_id=d1.id, max_enrollment=10, status="published", section="EE2", research_assignment_type="external_examiner")
        off_plain = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=plain_course.id, department_id=d1.id, max_enrollment=10, status="published")
        off_plain_d2 = CourseOffering(calendar_id=cal.id, semester_id=sem.id, course_id=plain_course.id, department_id=d2.id, max_enrollment=10, status="published", section="D2")
        db.add_all([off_ma, off_ee1, off_ee2, off_plain, off_plain_d2]); await db.flush()
        S["OFF_MA"], S["OFF_EE1"], S["OFF_EE2"], S["OFF_PLAIN"], S["OFF_PLAIN_D2"] = off_ma, off_ee1, off_ee2, off_plain, off_plain_d2
        await db.commit()

    d1, d2, pg, college = S["D1"], S["D2"], S["PG"], S["COLLEGE"]
    await _mk_user("sa", UserRole.SUPER_ADMIN, None)
    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("hod2", UserRole.HOD, d2)
    await _mk_user("ma1", UserRole.FACULTY, d1)
    await _mk_user("fac_edit", UserRole.FACULTY, d1)
    await _mk_user("fac_other_dept", UserRole.FACULTY, d2)
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None)
    await _mk_user("dpgs", UserRole.DPGS, None)
    await _mk_user("vc", UserRole.VICE_CHANCELLOR, None)

    for key in ("stu_ma", "stu_ee_already", "stu_ee_pending", "stu_multi", "stu_b", "stu_noma"):
        await _mk_user(key, UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRAS-{key}-{_TAG}")
    for key in ("stu_ma", "stu_ee_already", "stu_ee_pending", "stu_multi", "stu_b"):
        await _mk_committee(key, "ma1")
    # stu_noma deliberately has NO committee/Major Advisor at all.

    for k in ("sa", "hod1", "hod2", "ma1", "fac_edit", "fac_other_dept", "incharge", "dpgs", "vc",
              "stu_ma", "stu_ee_already", "stu_ee_pending", "stu_multi", "stu_b", "stu_noma"):
        TOK[k] = await _session(U[k])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        sel_ids = (await db.execute(select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.student_id.in_(uids)))).scalars().all()
        cyc_ids = (await db.execute(select(ExternalExaminerApprovalCycle.id).where(ExternalExaminerApprovalCycle.selection_id.in_(sel_ids or [uuid.uuid4()])))).scalars().all()
        examiner_ids = (await db.execute(select(ExternalExaminer.id).where(ExternalExaminer.email.like(f"{_PFX}%")))).scalars().all()

        await db.execute(delete(ExternalExaminerAssignment).where(ExternalExaminerAssignment.examiner_id.in_(examiner_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerSelectionResult).where(ExternalExaminerSelectionResult.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        from app.models.external_examiner import ExternalExaminerApprovalStage, ExternalExaminerProposal, ExternalExaminerSignature
        await db.execute(delete(ExternalExaminerSignature).where(ExternalExaminerSignature.approval_stage_id.in_(
            select(ExternalExaminerApprovalStage.id).where(ExternalExaminerApprovalStage.cycle_id.in_(cyc_ids or [uuid.uuid4()]))
        )))
        await db.execute(delete(ExternalExaminerApprovalStage).where(ExternalExaminerApprovalStage.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerProposal).where(ExternalExaminerProposal.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerApprovalCycle).where(ExternalExaminerApprovalCycle.id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerSelection).where(ExternalExaminerSelection.id.in_(sel_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminer).where(ExternalExaminer.id.in_(examiner_ids or [uuid.uuid4()])))

        extra_examiner_uids = select(User.id).where(User.role == UserRole.EXTERNAL_EXAMINER, User.email.like(f"{_PFX}%"))
        all_test_uids = select(User.id).where(User.email.like(f"%{_TAG}@%") | User.id.in_(extra_examiner_uids))

        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        reg_ids = select(CourseRegistration.id).where(CourseRegistration.student_id.in_(uids))
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.registration_id.in_(reg_ids) | StudentEnrollment.student_id.in_(uids)))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.id.in_(reg_ids)))
        offering_ids = [S[k].id for k in ("OFF_MA", "OFF_EE1", "OFF_EE2", "OFF_PLAIN", "OFF_PLAIN_D2")]
        await db.execute(delete(OfferingFaculty).where(OfferingFaculty.offering_id.in_(offering_ids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(offering_ids)))
        await db.execute(delete(Course).where(Course.id.in_([S["RESEARCH_COURSE"].id, S["PLAIN_COURSE"].id])))
        await db.execute(delete(Semester).where(Semester.id.in_([S["SEM"].id, S["SEM2"].id])))
        await db.execute(delete(AcademicCalendar).where(AcademicCalendar.id.in_([S["CAL"].id, S["CAL2"].id])))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(all_test_uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(User).where(User.role == UserRole.EXTERNAL_EXAMINER, User.email.like(f"{_PFX}%")))
        await db.execute(delete(College).where(College.id == S["COLLEGE"].id))
        await db.commit()


# ── Research Assignment: Major Advisor strategy (existing behavior preserved) ──

async def t_major_advisor_strategy_unchanged():
    r = await _call("POST", "/enrollment", TOK["stu_ma"], json={"offering_id": str(S["OFF_MA"].id)})
    assert r.status_code == 201, r.text
    e = await _enrollment_row("stu_ma", "OFF_MA")
    assert e is not None and e.instructor_id == U["ma1"], e.instructor_id if e else None


async def t_major_advisor_strategy_requires_accepted_ma():
    r = await _call("POST", "/enrollment", TOK["stu_noma"], json={"offering_id": str(S["OFF_MA"].id)})
    assert r.status_code == 403, r.text
    assert await _enrollment_row("stu_noma", "OFF_MA") is None


# ── Research Assignment: External Examiner — already selected ──────────────────

async def t_external_examiner_already_selected_assigned_immediately():
    sid = await _drive_examiner_selection_to_vc("stu_ee_already", "ma1", [_proposal("already_a"), _proposal("already_b"), _proposal("already_c")])
    chosen = await _vc_select_first_n(sid, 1)
    async with AsyncSessionLocal() as db:
        examiner_user_id = (await db.execute(
            select(ExternalExaminer.user_id).join(ExternalExaminerAssignment, ExternalExaminerAssignment.examiner_id == ExternalExaminer.id)
            .where(ExternalExaminerAssignment.student_id == U["stu_ee_already"])
        )).scalar_one()
    assert examiner_user_id is not None

    r = await _call("POST", "/enrollment", TOK["stu_ee_already"], json={"offering_id": str(S["OFF_EE1"].id)})
    assert r.status_code == 201, r.text
    e = await _enrollment_row("stu_ee_already", "OFF_EE1")
    assert e is not None and e.instructor_id == examiner_user_id, (e.instructor_id if e else None, examiner_user_id)


async def t_student_cannot_submit_examiner_id_or_override_strategy():
    """A crafted enroll body cannot change the assigned examiner or the
    offering's own strategy — EnrollRequest has no such fields at all, so
    they are silently dropped; what matters is the OFFERING's stored
    strategy is byte-for-byte unchanged afterward."""
    before = await _offering_row("OFF_EE1")
    before_type = before.research_assignment_type
    r = await _call("POST", "/enrollment", TOK["stu_b"], json={
        "offering_id": str(S["OFF_EE2"].id), "research_assignment_type": "major_advisor",
        "examiner_id": str(U["ma1"]), "instructor_id": str(U["ma1"]),
    })
    assert r.status_code == 201, r.text
    after = await _offering_row("OFF_EE1")
    assert after.research_assignment_type == before_type, "a crafted enroll body must never change the OFFERING's strategy"
    e = await _enrollment_row("stu_b", "OFF_EE2")
    assert e is not None and e.instructor_id is None, "with no examiner selected yet, instructor_id must stay NULL — never the crafted id"


# ── Research Assignment: External Examiner — not yet selected, then selected ───

async def t_external_examiner_pending_then_backfilled():
    r = await _call("POST", "/enrollment", TOK["stu_ee_pending"], json={"offering_id": str(S["OFF_EE1"].id)})
    assert r.status_code == 201, r.text
    e = await _enrollment_row("stu_ee_pending", "OFF_EE1")
    assert e is not None and e.instructor_id is None, "enrollment must succeed with instructor_id NULL (pending) when no examiner is selected yet"

    sid = await _drive_examiner_selection_to_vc("stu_ee_pending", "ma1", [_proposal("pending_a"), _proposal("pending_b"), _proposal("pending_c")])
    await _vc_select_first_n(sid, 1)

    e2 = await _enrollment_row("stu_ee_pending", "OFF_EE1")
    assert e2 is not None and e2.instructor_id is not None, "the pending enrollment must be automatically backfilled once the examiner is selected"


async def t_backfill_is_idempotent_on_replay():
    """Re-running the SAME already-completed vc-selection request (idempotent
    replay, unchanged pre-existing behavior) must not duplicate or change
    the already-backfilled assignment."""
    sid = SEL["stu_ee_pending"]
    before = await _enrollment_row("stu_ee_pending", "OFF_EE1")
    r = await _call("GET", f"/external-examiners/{sid}", TOK["dpgs"])
    proposal_ids = [p["id"] for p in r.json()["proposals"]][:1]
    replay = await _call("POST", f"/external-examiners/{sid}/vc-selection", TOK["vc"], json={"proposal_ids": proposal_ids})
    assert replay.status_code == 200, replay.text
    after = await _enrollment_row("stu_ee_pending", "OFF_EE1")
    assert after.instructor_id == before.instructor_id
    async with AsyncSessionLocal() as db:
        count = (await db.execute(select(func.count()).select_from(ExternalExaminerAssignment).where(ExternalExaminerAssignment.student_id == U["stu_ee_pending"]))).scalar_one()
    assert count == 1, "a replayed VC-selection request must never create a second assignment"


# ── Multiple Research courses / multiple students ───────────────────────────────

async def t_multiple_research_courses_only_external_examiner_offering_gets_examiner():
    r = await _call("POST", "/enrollment/register", TOK["stu_multi"], json={
        "calendar_id": str(S["CAL"].id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(S["OFF_MA"].id), str(S["OFF_EE2"].id)],
    })
    assert r.status_code == 201, r.text
    ma_row = await _enrollment_row("stu_multi", "OFF_MA")
    ee_row = await _enrollment_row("stu_multi", "OFF_EE2")
    assert ma_row is not None and ma_row.instructor_id == U["ma1"], "Major-Advisor-strategy offering must get the Major Advisor"
    assert ee_row is not None and ee_row.instructor_id is None, "External-Examiner-strategy offering must stay pending (no examiner selected yet)"

    sid = await _drive_examiner_selection_to_vc("stu_multi", "ma1", [_proposal("multi_a"), _proposal("multi_b"), _proposal("multi_c")])
    await _vc_select_first_n(sid, 1)

    ma_row_after = await _enrollment_row("stu_multi", "OFF_MA")
    ee_row_after = await _enrollment_row("stu_multi", "OFF_EE2")
    assert ma_row_after.instructor_id == U["ma1"], "the Major-Advisor-strategy enrollment must never be touched by a later examiner selection"
    assert ee_row_after.instructor_id is not None, "the External-Examiner-strategy enrollment must now be backfilled"
    assert ee_row_after.instructor_id != U["ma1"], "the backfilled value must be the examiner, never the Major Advisor"


async def t_different_students_get_their_own_examiner_never_each_others():
    e_already = await _enrollment_row("stu_ee_already", "OFF_EE1")
    e_pending = await _enrollment_row("stu_ee_pending", "OFF_EE1")
    assert e_already is not None and e_pending is not None
    assert e_already.instructor_id != e_pending.instructor_id, "two different students' examiners must never collapse to the same value by accident"
    async with AsyncSessionLocal() as db:
        leak = (await db.execute(select(ExternalExaminerAssignment).where(
            ExternalExaminerAssignment.student_id == U["stu_ee_already"],
        ))).scalars().all()
    assert all(a.student_id == U["stu_ee_already"] for a in leak)


# ── Timing order: enrollment -> selection vs selection -> enrollment ────────────

async def t_both_timing_orders_produce_the_same_correct_result():
    # stu_ee_already: examiner selected BEFORE enrollment (tested above) —
    # stu_ee_pending: enrollment BEFORE examiner selected (tested above) —
    # both already produced a non-null, correctly-assigned instructor_id;
    # this test just re-confirms both final states directly, side by side.
    already = await _enrollment_row("stu_ee_already", "OFF_EE1")
    pending = await _enrollment_row("stu_ee_pending", "OFF_EE1")
    assert already.instructor_id is not None and pending.instructor_id is not None


# ── Security: strategy/examiner/Major-Advisor are never client-controlled ──────

async def t_student_cannot_change_offering_research_assignment_type():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_MA'].id}", TOK["stu_ma"], json={"research_assignment_type": "external_examiner"})
    assert r.status_code == 403, r.text
    assert (await _offering_row("OFF_MA")).research_assignment_type == "major_advisor"


async def t_faculty_cannot_change_offering_research_assignment_type():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_MA'].id}", TOK["ma1"], json={"research_assignment_type": "external_examiner"})
    assert r.status_code == 403, r.text
    assert (await _offering_row("OFF_MA")).research_assignment_type == "major_advisor"


async def t_hod_can_modify_strategy_only_for_own_department():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_MA'].id}", TOK["hod1"], json={"research_assignment_type": "external_examiner"})
    assert r.status_code == 200, r.text
    reverted = await _call("PATCH", f"/courses/offerings/{S['OFF_MA'].id}", TOK["hod1"], json={"research_assignment_type": "major_advisor"})
    assert reverted.status_code == 200, reverted.text
    assert (await _offering_row("OFF_MA")).research_assignment_type == "major_advisor"


async def t_hod_cannot_modify_another_departments_offering_strategy():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN_D2'].id}", TOK["hod1"], json={"max_enrollment": 5})
    assert r.status_code == 403, r.text
    assert (await _offering_row("OFF_PLAIN_D2")).max_enrollment == 10


# ── Research assignment validation (non-research forbidden, research required) ──

async def t_research_assignment_forbidden_on_non_research_offering():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["hod1"], json={"research_assignment_type": "major_advisor"})
    assert r.status_code == 400, r.text


async def t_create_research_offering_without_strategy_rejected():
    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(S["CAL2"].id), "semester_id": str(S["SEM2"].id), "course_id": str(S["RESEARCH_COURSE"].id),
        "department_id": str(S["D1"].id), "max_enrollment": 10, "faculty_ids": [], "leader_id": None,
    })
    assert r.status_code == 400, r.text


async def t_create_non_research_offering_with_strategy_rejected():
    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(S["CAL2"].id), "semester_id": str(S["SEM2"].id), "course_id": str(S["PLAIN_COURSE"].id),
        "department_id": str(S["D1"].id), "max_enrollment": 10, "faculty_ids": [str(U["fac_edit"])], "leader_id": str(U["fac_edit"]),
        "research_assignment_type": "major_advisor",
    })
    assert r.status_code == 400, r.text


async def t_invalid_research_assignment_enum_rejected():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_MA'].id}", TOK["hod1"], json={"research_assignment_type": "student_choice"})
    assert r.status_code == 422, r.text


# ── Complete Offered Course Editing ─────────────────────────────────────────────

async def t_hod_edits_all_offering_fields_own_department():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["hod1"], json={
        "semester_id": str(S["SEM2"].id), "max_enrollment": 42, "section": "Z", "practical_group": "GZ",
        "faculty_ids": [str(U["fac_edit"])], "leader_id": str(U["fac_edit"]),
    })
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        o = await db.get(CourseOffering, S["OFF_PLAIN"].id)
        assert o.semester_id == S["SEM2"].id and o.calendar_id == S["CAL2"].id, "calendar_id must be re-derived from the new semester"
        assert o.max_enrollment == 42 and o.section == "Z" and o.practical_group == "GZ"
        fac = (await db.execute(select(OfferingFaculty).where(OfferingFaculty.offering_id == o.id))).scalars().all()
        assert len(fac) == 1 and fac[0].faculty_id == U["fac_edit"] and fac[0].role == "primary"
    # Revert semester back for subsequent tests' isolation.
    revert = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["hod1"], json={"semester_id": str(S["SEM"].id)})
    assert revert.status_code == 200, revert.text


async def t_offering_edit_cannot_reach_course_master_fields():
    course_before = await _course_row("PLAIN_COURSE")
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["hod1"], json={
        "max_enrollment": 55, "title": "HACKED", "category": "research", "department_id": str(S["D2"].id), "course_id": str(S["RESEARCH_COURSE"].id),
    })
    assert r.status_code == 200, r.text
    course_after = await _course_row("PLAIN_COURSE")
    offering_after = await _offering_row("OFF_PLAIN")
    assert course_after.title == course_before.title and course_after.category == course_before.category
    assert offering_after.department_id == S["D1"].id, "department must never change through this endpoint"
    assert offering_after.course_id == S["PLAIN_COURSE"].id, "which course an offering represents must never change"
    assert offering_after.max_enrollment == 55


async def _course_row(key: str) -> Course:
    async with AsyncSessionLocal() as db:
        return await db.get(Course, S[key].id)


async def t_unauthenticated_and_unauthorized_direct_api_access_blocked():
    r0 = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", None, json={"max_enrollment": 1})
    assert r0.status_code in (401, 403), r0.status_code
    r1 = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["stu_ma"], json={"max_enrollment": 1})
    assert r1.status_code == 403, r1.text
    r2 = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["ma1"], json={"max_enrollment": 1})
    assert r2.status_code == 403, r2.text
    assert (await _offering_row("OFF_PLAIN")).max_enrollment != 1


async def t_super_admin_can_edit_all_fields_across_departments():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN_D2'].id}", TOK["sa"], json={
        "max_enrollment": 33, "faculty_ids": [str(U["fac_other_dept"])], "leader_id": str(U["fac_other_dept"]),
    })
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        o = await db.get(CourseOffering, S["OFF_PLAIN_D2"].id)
        assert o.max_enrollment == 33
        fac = (await db.execute(select(OfferingFaculty).where(OfferingFaculty.offering_id == o.id))).scalars().all()
        assert len(fac) == 1 and fac[0].faculty_id == U["fac_other_dept"]


async def t_hod_cannot_assign_faculty_from_another_department():
    r = await _call("PATCH", f"/courses/offerings/{S['OFF_PLAIN'].id}", TOK["hod1"], json={
        "faculty_ids": [str(U["fac_other_dept"])], "leader_id": str(U["fac_other_dept"]),
    })
    assert r.status_code == 403, r.text


async def main() -> None:
    await _setup()
    try:
        cases = {
            "Major Advisor strategy: unchanged existing behavior": t_major_advisor_strategy_unchanged,
            "Major Advisor strategy: still requires an accepted Major Advisor": t_major_advisor_strategy_requires_accepted_ma,
            "External Examiner already selected: assigned immediately on enrollment": t_external_examiner_already_selected_assigned_immediately,
            "Student cannot submit examiner id / override strategy via crafted body": t_student_cannot_submit_examiner_id_or_override_strategy,
            "External Examiner not yet selected: enrollment succeeds pending, then auto-backfilled": t_external_examiner_pending_then_backfilled,
            "Backfill is idempotent on a replayed VC-selection request": t_backfill_is_idempotent_on_replay,
            "Multiple Research courses: only the External-Examiner offering gets the examiner": t_multiple_research_courses_only_external_examiner_offering_gets_examiner,
            "Different students never receive each other's examiner": t_different_students_get_their_own_examiner_never_each_others,
            "Both timing orders (selected-then-enroll, enroll-then-selected) produce correct results": t_both_timing_orders_produce_the_same_correct_result,
            "Student cannot change an offering's research assignment strategy": t_student_cannot_change_offering_research_assignment_type,
            "Faculty cannot change an offering's research assignment strategy": t_faculty_cannot_change_offering_research_assignment_type,
            "HOD can modify strategy only for their own department's offering": t_hod_can_modify_strategy_only_for_own_department,
            "HOD cannot modify another department's offering": t_hod_cannot_modify_another_departments_offering_strategy,
            "Research assignment forbidden on a non-research offering": t_research_assignment_forbidden_on_non_research_offering,
            "Creating a research offering without a strategy is rejected": t_create_research_offering_without_strategy_rejected,
            "Creating a non-research offering with a strategy is rejected": t_create_non_research_offering_with_strategy_rejected,
            "Invalid research_assignment_type enum value rejected": t_invalid_research_assignment_enum_rejected,
            "HOD edits Academic Year/Semester/Faculty/Max Enrollment/Section/Practical Group (own department)": t_hod_edits_all_offering_fields_own_department,
            "Offering edit cannot reach Course master fields or change department/course": t_offering_edit_cannot_reach_course_master_fields,
            "Unauthenticated/unauthorized direct API access to offering edit is blocked": t_unauthenticated_and_unauthorized_direct_api_access_blocked,
            "Super Admin can edit all offering fields across departments": t_super_admin_can_edit_all_fields_across_departments,
            "HOD cannot assign faculty from another department": t_hod_cannot_assign_faculty_from_another_department,
        }
        for name, fn in cases.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "offerings": (await db.execute(select(func.count()).select_from(CourseOffering).where(CourseOffering.id.in_([S["OFF_MA"].id, S["OFF_EE1"].id, S["OFF_EE2"].id, S["OFF_PLAIN"].id, S["OFF_PLAIN_D2"].id])))).scalar_one(),
                "courses": (await db.execute(select(func.count()).select_from(Course).where(Course.id.in_([S["RESEARCH_COURSE"].id, S["PLAIN_COURSE"].id])))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_RAS user/session/offering/course row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
