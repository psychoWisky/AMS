"""Standalone HTTP-level tests for the Comprehensive Examination module.

Modelled directly on `tests/test_research_course.py` (the freshest convention in this repo):
real FastAPI app via `httpx.ASGITransport`, real JWT sessions via `create_access_token`/
`RefreshToken` rows, a `record(name, ok, detail)` pass/fail tracker, a `_run(name, fn)` wrapper,
and a `_setup()`/`_teardown()` pair using a unique `_TAG = uuid.uuid4().hex[:6]` and
`zztest_compexam_...` prefix on every created row so cleanup is unambiguous.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_comprehensive_exam
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import Semester
from app.models.comprehensive_exam import (
    ComprehensiveExamApplication, ComprehensiveExamApplicationCourse, ComprehensiveExamExternalPanelCycle,
    ComprehensiveExamExternalPanelProposal, ComprehensiveExamExternalPanelResult, ComprehensiveExamExternalPanelSelection,
    ComprehensiveExamExternalReportSignature, ComprehensiveExamExternalVivaReport, ComprehensiveExamViva,
    ComprehensiveExamVivaReport, ComprehensiveExamVivaReportSignature,
)
from app.models.course import Course, CourseOffering
from app.models.enrollment import StudentEnrollment
from app.models.email_outbox import EmailOutbox
from app.models.external_examiner import ExternalExaminer
from app.models.grading import GradeEntry, GradeSheet
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_compexam_"
_LABEL = "ZZTEST_COMPEXAM"
_DEVICE = "ZZTEST-compexam"

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
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"CE{key.upper()}",
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


async def _mk_committee(student_key: str, ma_key: str, extra_keys: list[str] | None = None) -> None:
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST CE committee", status="hod_approved")
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True))
        for k in (extra_keys or []):
            db.add(CommitteeMember(committee_id=c.id, faculty_id=U[k], role="member_major", accepted=True))
        await db.commit()


async def _mk_course(key: str, dept, credits: int) -> None:
    async with AsyncSessionLocal() as db:
        c = Course(course_number=f"ZZCE-{key}-{_TAG}", title=f"ZZTEST {key}", department_id=dept.id,
                   program_level="PG", credit_theory=credits, credit_practical=0, created_by=U["sa"])
        db.add(c)
        await db.commit()
        CRS[key] = c.id


async def _mk_offering(key: str, course_key: str, dept) -> None:
    async with AsyncSessionLocal() as db:
        o = CourseOffering(calendar_id=S["SEM"].calendar_id, semester_id=S["SEM"].id, course_id=CRS[course_key],
                            department_id=dept.id, status="published", created_by=U["sa"])
        db.add(o)
        await db.commit()
        OFF[key] = o.id


async def _mk_enrollment(student_key: str, offering_key: str, classification: str, status: str = "approved", failing: bool = False) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        e = StudentEnrollment(student_id=U[student_key], offering_id=OFF[offering_key], classification=classification, status=status)
        db.add(e)
        await db.flush()
        eid = e.id
        if failing:
            sheet = (await db.execute(select(GradeSheet).where(GradeSheet.offering_id == OFF[offering_key]))).scalar_one_or_none()
            if not sheet:
                sheet = GradeSheet(offering_id=OFF[offering_key], created_by=U["sa"])
                db.add(sheet)
                await db.flush()
            db.add(GradeEntry(sheet_id=sheet.id, student_id=U[student_key], enrollment_id=eid, grade_letter="F"))
        await db.commit()
    return eid


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
            print("STOP: stray zztest_compexam_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.level == "PG").limit(1))).scalars().first()
        S["PHD"] = (await db.execute(select(Program).where(Program.level == "PhD").limit(1))).scalars().first()
        S["SEM"] = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert S["PG"] and S["PHD"] and S["SEM"], "test requires at least one PG Program, one PhD Program, and one Semester in the local dev database"
        col = College(name=f"{_LABEL} College", code=f"ZZCE{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, d2, pg, phd, college = S["D1"], S["D2"], S["PG"], S["PHD"], S["COLLEGE"]

    await _mk_user("sa", UserRole.SUPER_ADMIN, None)
    U["sa"] = admin_id  # use the real admin for FK convenience where needed; session/token below

    await _mk_user("hod_a", UserRole.HOD, d1)
    await _mk_user("hod_b", UserRole.HOD, d2)
    await _mk_user("ma_x", UserRole.FACULTY, d1)   # MA for PhD student
    await _mk_user("ma_y", UserRole.FACULTY, d1)   # MA for PG student
    await _mk_user("mem1", UserRole.FACULTY, d1)   # extra committee member
    await _mk_user("mem1b", UserRole.FACULTY, d1)  # second extra member (AND-gate test)
    await _mk_user("mem2", UserRole.FACULTY, d1)
    await _mk_user("fac_unrelated", UserRole.FACULTY, d1)
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None)
    await _mk_user("dpgs", UserRole.DPGS, None)
    await _mk_user("vc", UserRole.VICE_CHANCELLOR, None)

    await _mk_user("s_pg", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZCE-pg-{_TAG}", student_profile=True)
    await _mk_user("s_phd", UserRole.STUDENT, d1, program=phd, college=college, roll=f"ZZCE-phd-{_TAG}", student_profile=True)
    await _mk_user("s_phd2", UserRole.STUDENT, d1, program=phd, college=college, roll=f"ZZCE-phd2-{_TAG}", student_profile=True)
    await _mk_user("s_low", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZCE-low-{_TAG}", student_profile=True)
    await _mk_user("s_noma", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZCE-noma-{_TAG}", student_profile=True)
    await _mk_user("s_deptb", UserRole.STUDENT, d2, program=pg, college=college, roll=f"ZZCE-deptb-{_TAG}", student_profile=True)

    await _mk_committee("s_pg", "ma_y", ["mem1", "mem1b"])
    await _mk_committee("s_phd", "ma_x", ["mem2"])
    await _mk_committee("s_phd2", "ma_x", ["mem2"])
    await _mk_committee("s_low", "ma_y")
    await _mk_committee("s_deptb", "ma_y")
    # s_noma: intentionally no committee.

    for k in ("hod_a", "hod_b", "ma_x", "ma_y", "mem1", "mem1b", "mem2", "fac_unrelated", "incharge", "dpgs", "vc",
              "s_pg", "s_phd", "s_phd2", "s_low", "s_noma", "s_deptb"):
        TOK[k] = await _session(U[k])
    TOK["sa"] = await _session(admin_id)

    # Courses: major x2 (10+12=22 major), minor x1 (10 credits), research x1 (99 credits, must
    # never count), plus a "failing" major course.
    await _mk_course("maj1", d1, 10)
    await _mk_course("maj2", d1, 12)
    await _mk_course("min1", d1, 10)
    await _mk_course("resx", d1, 99)
    await _mk_course("majfail", d1, 50)
    for key in ("maj1", "maj2", "min1", "resx", "majfail"):
        await _mk_offering(key, key, d1)

    for student_key in ("s_pg", "s_phd", "s_phd2"):
        await _mk_enrollment(student_key, "maj1", "major")
        await _mk_enrollment(student_key, "maj2", "major")
        await _mk_enrollment(student_key, "min1", "minor")
        await _mk_enrollment(student_key, "resx", "research")          # must be excluded
    await _mk_enrollment("s_pg", "majfail", "major", failing=True)      # must be excluded (grade F)

    # s_low: only 5 major credits (below 20), no minor.
    await _mk_course("lowmaj", d1, 5)
    await _mk_offering("lowmaj", "lowmaj", d1)
    await _mk_enrollment("s_low", "lowmaj", "major")


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        app_ids = select(ComprehensiveExamApplication.id).where(ComprehensiveExamApplication.student_id.in_(uids))
        viva_ids = select(ComprehensiveExamViva.id).where(ComprehensiveExamViva.application_id.in_(app_ids))
        report_ids = select(ComprehensiveExamVivaReport.id).where(ComprehensiveExamVivaReport.viva_id.in_(viva_ids))
        panel_sel_ids = select(ComprehensiveExamExternalPanelSelection.id).where(ComprehensiveExamExternalPanelSelection.application_id.in_(app_ids))
        panel_cycle_ids = select(ComprehensiveExamExternalPanelCycle.id).where(ComprehensiveExamExternalPanelCycle.selection_id.in_(panel_sel_ids))
        panel_proposal_ids = select(ComprehensiveExamExternalPanelProposal.id).where(ComprehensiveExamExternalPanelProposal.cycle_id.in_(panel_cycle_ids))
        ext_report_ids = select(ComprehensiveExamExternalVivaReport.id).where(ComprehensiveExamExternalVivaReport.application_id.in_(app_ids))
        offering_ids = select(CourseOffering.id).where(CourseOffering.course_id.in_(list(CRS.values()) or [uuid.uuid4()]))

        await db.execute(delete(GradeEntry).where(GradeEntry.sheet_id.in_(select(GradeSheet.id).where(GradeSheet.offering_id.in_(offering_ids)))))
        await db.execute(delete(GradeSheet).where(GradeSheet.offering_id.in_(offering_ids)))
        await db.execute(delete(ComprehensiveExamExternalReportSignature).where(ComprehensiveExamExternalReportSignature.report_id.in_(ext_report_ids)))
        await db.execute(delete(ComprehensiveExamExternalVivaReport).where(ComprehensiveExamExternalVivaReport.id.in_(ext_report_ids)))
        await db.execute(delete(ComprehensiveExamExternalPanelResult).where(ComprehensiveExamExternalPanelResult.cycle_id.in_(panel_cycle_ids)))
        await db.execute(delete(ComprehensiveExamExternalPanelProposal).where(ComprehensiveExamExternalPanelProposal.id.in_(panel_proposal_ids)))
        await db.execute(delete(ComprehensiveExamExternalPanelCycle).where(ComprehensiveExamExternalPanelCycle.id.in_(panel_cycle_ids)))
        await db.execute(delete(ComprehensiveExamExternalPanelSelection).where(ComprehensiveExamExternalPanelSelection.id.in_(panel_sel_ids)))
        await db.execute(delete(ComprehensiveExamVivaReportSignature).where(ComprehensiveExamVivaReportSignature.report_id.in_(report_ids)))
        await db.execute(delete(ComprehensiveExamVivaReport).where(ComprehensiveExamVivaReport.id.in_(report_ids)))
        await db.execute(delete(ComprehensiveExamViva).where(ComprehensiveExamViva.id.in_(viva_ids)))
        await db.execute(delete(ComprehensiveExamApplicationCourse).where(ComprehensiveExamApplicationCourse.application_id.in_(app_ids)))
        await db.execute(delete(ComprehensiveExamApplication).where(ComprehensiveExamApplication.id.in_(app_ids)))
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(offering_ids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(offering_ids)))
        await db.execute(delete(Course).where(Course.id.in_(list(CRS.values()) or [uuid.uuid4()])))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(EmailOutbox).where(EmailOutbox.recipient.like(f"{_PFX}%")))
        await db.execute(delete(ExternalExaminer).where(ExternalExaminer.email.like(f"{_PFX}%")))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


# ── tests: eligibility / creation ────────────────────────────────────────────

async def t_eligible_pg_student_can_submit():
    r = await _call("POST", "/comprehensive-exam/applications", TOK["s_pg"])
    assert r.status_code == 201, r.text
    S["APP_PG"] = r.json()["id"]


async def t_eligible_phd_student_can_submit():
    r = await _call("POST", "/comprehensive-exam/applications", TOK["s_phd"])
    assert r.status_code == 201, r.text
    S["APP_PHD"] = r.json()["id"]


async def t_credit_snapshot_correct_and_research_excluded():
    r = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PG']}", TOK["s_pg"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["major_credits_completed"] == 22, body  # maj1(10)+maj2(12); majfail(50, F) excluded; resx(99) excluded
    assert body["minor_credits_completed"] == 10, body


async def t_frozen_snapshot_survives_later_enrollment_change():
    """Changing enrollment data AFTER submission must not change the already-created application."""
    async with AsyncSessionLocal() as db:
        e = (await db.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == U["s_pg"], StudentEnrollment.offering_id == OFF["maj1"]))).scalar_one()
        e.status = "withdrawn"
        await db.commit()
    r = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PG']}", TOK["s_pg"])
    assert r.json()["major_credits_completed"] == 22, "an already-submitted application must not silently change"
    async with AsyncSessionLocal() as db:
        e = (await db.execute(select(StudentEnrollment).where(StudentEnrollment.student_id == U["s_pg"], StudentEnrollment.offering_id == OFF["maj1"]))).scalar_one()
        e.status = "approved"
        await db.commit()


async def t_major_below_20_rejected():
    r = await _call("POST", "/comprehensive-exam/applications", TOK["s_low"])
    assert r.status_code == 400, r.text
    async with AsyncSessionLocal() as db:
        leftover = (await db.execute(select(ComprehensiveExamApplication).where(ComprehensiveExamApplication.student_id == U["s_low"]))).scalar_one_or_none()
    assert leftover is None


async def t_student_without_major_advisor_rejected():
    r = await _call("POST", "/comprehensive-exam/applications", TOK["s_noma"])
    assert r.status_code in (400, 403), r.text


async def t_duplicate_application_rejected():
    r = await _call("POST", "/comprehensive-exam/applications", TOK["s_pg"])
    assert r.status_code == 409, r.text


async def t_student_cannot_view_another_students_application():
    r = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PG']}", TOK["s_phd"])
    assert r.status_code == 404, r.text


async def t_application_document_downloads_and_contains_dynamic_data():
    r = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PG']}/document", TOK["s_pg"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    from pypdf import PdfReader
    from io import BytesIO
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(r.content)).pages)
    assert "ZZTESTCEPG" in text.replace(" ", "").upper() or "ZZTEST" in text
    assert f"ZZCE-pg-{_TAG}" in text
    assert "( Internal Oral Comprehensive )" in text
    assert "AAU" not in text.upper().replace("ASSAM VETERINARY", "")  # no AAU branding leak
    assert "110" in text or "22" in text  # major percent (22/20*100=110) or raw completed credits present


# ── tests: approval chain ────────────────────────────────────────────────────

async def t_ma_approval():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/approve", TOK["ma_y"])
    assert r.status_code == 200 and r.json()["status"] == "hod_pending", r.text


async def t_ma_cannot_approve_others_application():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD']}/approve", TOK["ma_y"])
    assert r.status_code == 404, r.text


async def t_hod_dept_b_cannot_approve_dept_a_application():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/approve", TOK["hod_b"])
    assert r.status_code == 404, r.text


async def t_hod_approval():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/approve", TOK["hod_a"])
    assert r.status_code == 200 and r.json()["status"] == "incharge_pending", r.text


async def t_incharge_approval():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/approve", TOK["incharge"])
    assert r.status_code == 200 and r.json()["status"] == "dpgs_pending", r.text


async def t_blank_revert_rejected():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD']}/revert", TOK["ma_x"], json={"remark": "   "})
    assert r.status_code == 422, r.text


async def t_dpgs_approval_completes_application():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/approve", TOK["dpgs"])
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text


async def t_stale_status_reapproval_rejected():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/approve", TOK["dpgs"])
    assert r.status_code == 400, r.text


async def t_revert_full_chain_for_phd():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD']}/approve", TOK["ma_x"])
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD']}/revert", TOK["hod_a"], json={"remark": "needs correction"})
    assert r.status_code == 200 and r.json()["status"] == "reverted", r.text
    # A reverted application must not silently become approved.
    r = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PHD']}", TOK["s_phd"])
    assert r.json()["status"] == "reverted"


# ── tests: viva scheduling / result (drive PG through to terminal state) ─────

async def t_hod_schedules_viva_for_pg():
    viva_date = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/viva", TOK["hod_a"], json={"viva_date": viva_date})
    assert r.status_code == 201, r.text
    S["VIVA_PG"] = r.json()["id"]
    S["VIVA_PG_DATE"] = viva_date


async def t_hod_dept_b_cannot_schedule_viva_for_dept_a():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/viva", TOK["hod_b"], json={"viva_date": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 404, r.text


async def t_only_actual_ma_can_record_result():
    r = await _call("POST", f"/comprehensive-exam/vivas/{S['VIVA_PG']}/result", TOK["fac_unrelated"], json={"result": "satisfactory"})
    assert r.status_code == 404, r.text


async def t_ma_marks_unsatisfactory_allows_new_attempt():
    r = await _call("POST", f"/comprehensive-exam/vivas/{S['VIVA_PG']}/result", TOK["ma_y"], json={"result": "unsatisfactory"})
    assert r.status_code == 200, r.text
    new_date = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PG']}/viva", TOK["hod_a"], json={"viva_date": new_date})
    assert r.status_code == 201 and r.json()["attempt_number"] == 2, r.text
    S["VIVA_PG"] = r.json()["id"]
    S["VIVA_PG_DATE"] = new_date


async def t_ma_marks_satisfactory_generates_report():
    r = await _call("POST", f"/comprehensive-exam/vivas/{S['VIVA_PG']}/result", TOK["ma_y"], json={"result": "satisfactory"})
    assert r.status_code == 200, r.text
    S["REPORT_PG"] = r.json()["report_id"]


async def t_report_uses_hod_fixed_date_not_result_date():
    r = await _call("GET", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/document", TOK["ma_y"])
    assert r.status_code == 200, r.text
    from pypdf import PdfReader
    from io import BytesIO
    text = "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(r.content)).pages)
    expected_date = datetime.fromisoformat(S["VIVA_PG_DATE"]).strftime("%d/%m/%Y")
    assert expected_date in text, f"expected HOD-fixed date {expected_date} in report text"
    assert "ZZTESTCEMA_Y" in text.replace(" ", "").upper() or "ZZTEST" in text


async def t_committee_and_gate_single_signature_insufficient():
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/submit", TOK["ma_y"])
    assert r.status_code == 200 and r.json()["status"] == "committee_pending", r.text
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/committee-sign", TOK["mem1"])
    assert r.status_code == 200 and r.json()["status"] == "committee_pending", "one signature must not complete the AND-gate alone"


async def t_committee_and_gate_completes_after_all_sign():
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/committee-sign", TOK["mem1b"])
    assert r.status_code == 200, r.text
    detail = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PG']}", TOK["hod_a"])
    assert detail.json()["latest_viva_report"]["status"] == "hod_pending", detail.text


async def t_viva_report_hod_incharge_dpgs_chain():
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/approve", TOK["hod_a"])
    assert r.status_code == 200 and r.json()["status"] == "incharge_pending", r.text
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/approve", TOK["incharge"])
    assert r.status_code == 200 and r.json()["status"] == "dpgs_pending", r.text
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PG']}/approve", TOK["dpgs"])
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text


async def t_pg_terminal_state_no_external_panel():
    r = await _call("GET", "/comprehensive-exam/external-panel/eligible", TOK["ma_y"])
    assert r.status_code == 200, r.text
    assert all(row["application_id"] != S["APP_PG"] for row in r.json()), "PG must never appear as External Panel eligible"
    r = await _call("POST", "/comprehensive-exam/external-panel", TOK["ma_y"], json={
        "application_id": S["APP_PG"],
        "proposals": [{"name": "X", "specialization": "X", "designation": "X", "email": "x@example.com", "phone": "1", "institution": "X"}] * 5,
    })
    assert r.status_code == 400, r.text


# ── tests: PhD external panel ─────────────────────────────────────────────────

async def _drive_phd_to_first_report_approved():
    """Uses a SEPARATE PhD student (s_phd2) from the one already driven to a `reverted`
    application in `t_revert_full_chain_for_phd` — that row is intentionally left reverted and
    must not be resurrected/reused here."""
    r = await _call("POST", "/comprehensive-exam/applications", TOK["s_phd2"])
    assert r.status_code == 201, r.text
    S["APP_PHD2"] = r.json()["id"]
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/approve", TOK["ma_x"])
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/approve", TOK["hod_a"])
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/approve", TOK["incharge"])
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/approve", TOK["dpgs"])
    assert r.status_code == 200, r.text
    viva_date = datetime.now(timezone.utc).isoformat()
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/viva", TOK["hod_a"], json={"viva_date": viva_date})
    S["VIVA_PHD"] = r.json()["id"]
    r = await _call("POST", f"/comprehensive-exam/vivas/{S['VIVA_PHD']}/result", TOK["ma_x"], json={"result": "satisfactory"})
    S["REPORT_PHD"] = r.json()["report_id"]
    await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PHD']}/submit", TOK["ma_x"])
    await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PHD']}/committee-sign", TOK["mem2"])
    await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PHD']}/approve", TOK["hod_a"])
    await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PHD']}/approve", TOK["incharge"])
    r = await _call("POST", f"/comprehensive-exam/viva-reports/{S['REPORT_PHD']}/approve", TOK["dpgs"])
    assert r.json()["status"] == "approved", r.text


def _five_proposals(prefix="ex"):
    return [{"name": f"{prefix}{i}", "specialization": "Vet Med", "designation": "Professor",
             "email": f"{_PFX}{prefix}{i}_{_TAG}@example.com", "phone": "9800000000", "institution": "ZZTEST Institute"} for i in range(5)]


async def t_only_phd_eligible_for_panel():
    await _run_setup_phd_report()
    r = await _call("GET", "/comprehensive-exam/external-panel/eligible", TOK["ma_x"])
    assert r.status_code == 200, r.text
    assert any(row["application_id"] == S["APP_PHD2"] for row in r.json()), r.text


async def _run_setup_phd_report():
    if "REPORT_PHD" not in S:
        await _drive_phd_to_first_report_approved()


async def t_panel_requires_exactly_5_rejects_4_and_6():
    await _run_setup_phd_report()
    r = await _call("POST", "/comprehensive-exam/external-panel", TOK["ma_x"], json={"application_id": S["APP_PHD2"], "proposals": _five_proposals()[:4]})
    assert r.status_code == 422, r.text
    r = await _call("POST", "/comprehensive-exam/external-panel", TOK["ma_x"], json={"application_id": S["APP_PHD2"], "proposals": _five_proposals() + [_five_proposals("extra")[0]]})
    assert r.status_code == 422, r.text


async def t_panel_proposed_with_exactly_5():
    r = await _call("POST", "/comprehensive-exam/external-panel", TOK["ma_x"], json={"application_id": S["APP_PHD2"], "proposals": _five_proposals()})
    assert r.status_code == 201, r.text
    S["PANEL"] = r.json()["id"]


async def t_application_detail_exposes_panel_cycle_id():
    """Integration-mismatch fix: `GET /applications/{id}`'s `external_panel` field previously
    exposed the SELECTION id (which `/external-panel/{cycle_id}/*` endpoints do not accept at
    all) instead of the actual actionable cycle id — this would have made the panel unreachable
    from the application detail view for every role except the one who just proposed it."""
    r = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PHD2']}", TOK["ma_x"])
    assert r.status_code == 200, r.text
    panel = r.json()["external_panel"]
    assert panel is not None and panel["cycle_id"] == S["PANEL"], panel
    assert panel["current_stage"] == "hod", panel


async def t_pending_approvals_shows_hod_only_at_hod_stage():
    r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK["hod_a"])
    assert r.status_code == 200, r.text
    assert any(row["cycle_id"] == S["PANEL"] for row in r.json()), r.text
    # Wrong department HOD must never see it.
    r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK["hod_b"])
    assert all(row["cycle_id"] != S["PANEL"] for row in r.json()), r.text
    # Not yet this role's turn.
    for tok_key in ("incharge", "dpgs", "vc"):
        r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK[tok_key])
        assert all(row["cycle_id"] != S["PANEL"] for row in r.json()), (tok_key, r.text)


async def t_panel_full_chain_to_vc():
    r = await _call("POST", f"/comprehensive-exam/external-panel/{S['PANEL']}/approve", TOK["hod_a"])
    assert r.status_code == 200 and r.json()["stage"] == "hod", r.text
    detail = await _call("GET", f"/comprehensive-exam/external-panel/{S['PANEL']}", TOK["incharge"])
    assert detail.json()["current_stage"] == "incharge_academic_cell" and detail.json()["hod_approved"], detail.text
    r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK["incharge"])
    assert any(row["cycle_id"] == S["PANEL"] for row in r.json()), r.text

    r = await _call("POST", f"/comprehensive-exam/external-panel/{S['PANEL']}/approve", TOK["incharge"])
    assert r.status_code == 200 and r.json()["stage"] == "incharge_academic_cell", r.text
    r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK["dpgs"])
    assert any(row["cycle_id"] == S["PANEL"] for row in r.json()), r.text

    r = await _call("POST", f"/comprehensive-exam/external-panel/{S['PANEL']}/approve", TOK["dpgs"])
    assert r.status_code == 200 and r.json()["stage"] == "dpgs", r.text
    r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK["vc"])
    assert any(row["cycle_id"] == S["PANEL"] for row in r.json()), r.text
    detail = await _call("GET", f"/comprehensive-exam/external-panel/{S['PANEL']}", TOK["vc"])
    assert detail.json()["current_stage"] == "vc" and detail.json()["dpgs_approved"], detail.text


async def t_vc_select_zero_or_two_rejected():
    detail = await _call("GET", f"/comprehensive-exam/external-panel/{S['PANEL']}", TOK["vc"])
    proposals = detail.json()["proposals"]
    S["PROPOSAL_IDS"] = [p["id"] for p in proposals]
    # zero: omit proposal_id entirely -> 422 (required field)
    r = await _call("POST", f"/comprehensive-exam/external-panel/{S['PANEL']}/vc-selection", TOK["vc"], json={})
    assert r.status_code == 422, r.text


async def t_vc_selects_exactly_one():
    r = await _call("POST", f"/comprehensive-exam/external-panel/{S['PANEL']}/vc-selection", TOK["vc"], json={"proposal_id": S["PROPOSAL_IDS"][0]})
    assert r.status_code == 200, r.text
    S["SELECTED_PROPOSAL"] = S["PROPOSAL_IDS"][0]


async def t_second_selection_is_idempotent_not_a_new_selection():
    r = await _call("POST", f"/comprehensive-exam/external-panel/{S['PANEL']}/vc-selection", TOK["vc"], json={"proposal_id": S["PROPOSAL_IDS"][1]})
    assert r.status_code == 200, r.text
    assert r.json()["selected_proposal_id"] == S["SELECTED_PROPOSAL"], "a repeated selection call must not change the already-selected examiner"


async def t_panel_cleared_from_pending_approvals_after_selection():
    for tok_key in ("hod_a", "incharge", "dpgs", "vc"):
        r = await _call("GET", "/comprehensive-exam/external-panel/pending-approvals", TOK[tok_key])
        assert all(row["cycle_id"] != S["PANEL"] for row in r.json()), (tok_key, r.text)
    detail = await _call("GET", f"/comprehensive-exam/external-panel/{S['PANEL']}", TOK["vc"])
    assert detail.json()["status"] == "approved" and detail.json()["current_stage"] is None, detail.text


async def t_no_user_account_created_for_selected_examiner():
    async with AsyncSessionLocal() as db:
        examiner = (await db.execute(select(ExternalExaminer).where(ExternalExaminer.email.like(f"{_PFX}ex0_%")))).scalar_one_or_none()
        assert examiner is not None, "an ExternalExaminer identity row must exist for the selected proposal"
        assert examiner.user_id is None, "CRITICAL: no AMS User account may ever be created for a Comprehensive Exam external examiner"
        stray_login = (await db.execute(select(func.count()).select_from(User).where(User.email == examiner.email))).scalar_one()
        assert stray_login == 0, "no User row may exist for the external examiner's email"


async def t_email_queued_for_selected_examiner():
    async with AsyncSessionLocal() as db:
        mail = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient == f"{_PFX}ex0_{_TAG}@example.com"))).scalar_one_or_none()
    assert mail is not None, "a notification email must be queued for the selected examiner"
    assert "password" not in mail.body.lower() and "username" not in mail.body.lower(), "the email must never contain login credentials"


# ── tests: external viva report ──────────────────────────────────────────────

def _dummy_pdf(name: str) -> dict:
    from pypdf import PdfWriter
    from io import BytesIO
    buf = BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.write(buf)
    return {"file": (name, buf.getvalue(), "application/pdf")}


async def t_unauthorized_ma_cannot_upload_external_report():
    """A real Major Advisor — just not OF THIS student — must not be able to upload against
    another student's application by supplying its application_id."""
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/external-viva-report/upload", TOK["ma_y"], files=_dummy_pdf("signed.pdf"))
    assert r.status_code == 404, r.text


async def t_external_report_upload_version1():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/external-viva-report/upload", TOK["ma_x"], files=_dummy_pdf("signed.pdf"))
    assert r.status_code == 201 and r.json()["version_number"] == 1, r.text
    S["EXT_REPORT"] = r.json()["id"]

    # Integration check: GET /applications/{id} exposes the enriched signatures/timestamps
    # this frontend now depends on for the committee AND-gate and approval-history display.
    detail = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PHD2']}", TOK["ma_x"])
    er = detail.json()["external_viva_report"]
    assert er is not None and er["id"] == S["EXT_REPORT"] and er["status"] == "uploaded", er
    assert er["uploaded_at"] is not None and isinstance(er["signatures"], list), er


async def t_unauthorized_ma_cannot_submit_external_report():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/submit", TOK["ma_y"])
    assert r.status_code == 404, r.text


async def t_external_report_submit_moves_to_committee_pending():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/submit", TOK["ma_x"])
    assert r.status_code == 200 and r.json()["status"] == "committee_pending", r.text


async def t_unauthorized_faculty_cannot_approve_committee_stage():
    """fac_unrelated is a real FACULTY user but not a member of this student's committee at
    all — must not be able to approve (or discover, via a non-404 response, that it exists)."""
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/committee-sign", TOK["fac_unrelated"])
    assert r.status_code == 404, r.text


async def t_committee_revert_blank_remark_rejected():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/revert", TOK["mem2"], json={"remark": "   "})
    assert r.status_code == 422, r.text


async def t_committee_member_approves_advances_to_hod_pending():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/committee-sign", TOK["mem2"])
    assert r.status_code == 200 and r.json()["status"] == "hod_pending", r.text


async def t_hod_dept_b_cannot_approve_or_revert_external_report():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/approve", TOK["hod_b"])
    assert r.status_code == 404, r.text
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/revert", TOK["hod_b"], json={"remark": "x"})
    assert r.status_code == 404, r.text


async def t_hod_reverts_external_report_with_remark():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/revert", TOK["hod_a"], json={"remark": "signature missing"})
    assert r.status_code == 200 and r.json()["status"] == "reverted", r.text
    detail = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PHD2']}", TOK["ma_x"])
    er = detail.json()["external_viva_report"]
    assert er["status"] == "reverted" and er["revert_remark"] == "signature missing", er


async def t_external_report_upload_version2_preserves_v1():
    r = await _call("POST", f"/comprehensive-exam/applications/{S['APP_PHD2']}/external-viva-report/upload", TOK["ma_x"], files=_dummy_pdf("signed2.pdf"))
    assert r.status_code == 201 and r.json()["version_number"] == 2, r.text
    S["EXT_REPORT_V2"] = r.json()["id"]

    async with AsyncSessionLocal() as db:
        v1 = await db.get(ComprehensiveExamExternalVivaReport, uuid.UUID(S["EXT_REPORT"]))
        assert v1 is not None and v1.status == "reverted", "version 1 must remain intact, never deleted"
        assert v1.stored_filename is not None
    # The old version's PDF must still be readable — never deleted from storage either.
    r = await _call("GET", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT']}/document", TOK["ma_x"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text


async def t_external_report_full_approval_chain():
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/submit", TOK["ma_x"])
    assert r.status_code == 200, r.text

    # Physical-signature rule: approval never requires (or even inspects) any electronic
    # signature/OTP payload — an extraneous, signature-shaped body is silently accepted/ignored,
    # never rejected AND never treated as a real signature (these endpoints have no such field
    # in their request schema at all).
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/committee-sign", TOK["mem2"], json={"otp": "000000", "signature": "fake-signature-payload"})
    assert r.status_code == 200 and r.json()["status"] == "hod_pending", r.text

    # Wrong role/stage at each step must be blocked, never silently accepted.
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/approve", TOK["dpgs"])
    assert r.status_code == 404, r.text  # DPGS is not HOD -> _hod_dept_matches fails -> not_found
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/approve", TOK["hod_a"])
    assert r.status_code == 200 and r.json()["status"] == "incharge_pending", r.text

    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/approve", TOK["hod_a"])
    assert r.status_code == 403, r.text  # HOD is not Incharge Academic Cell
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/approve", TOK["incharge"])
    assert r.status_code == 200 and r.json()["status"] == "dpgs_pending", r.text

    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/approve", TOK["incharge"])
    assert r.status_code == 403, r.text  # Incharge is not DPGS
    r = await _call("POST", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/approve", TOK["dpgs"])
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text

    detail = await _call("GET", f"/comprehensive-exam/applications/{S['APP_PHD2']}", TOK["hod_a"])
    er = detail.json()["external_viva_report"]
    assert er["status"] == "approved" and er["hod_approved"] and er["incharge_approved"] and er["dpgs_approved"] and er["approved_at"], er


async def t_unrelated_faculty_cannot_download_external_report_document():
    r = await _call("GET", f"/comprehensive-exam/external-viva-reports/{S['EXT_REPORT_V2']}/document", TOK["fac_unrelated"])
    assert r.status_code == 404, r.text


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "Eligible PG student can submit application": t_eligible_pg_student_can_submit,
            "Eligible PhD student can submit application": t_eligible_phd_student_can_submit,
            "Credit snapshot correct: research and failing grade excluded": t_credit_snapshot_correct_and_research_excluded,
            "Frozen snapshot survives a later enrollment change": t_frozen_snapshot_survives_later_enrollment_change,
            "Major < 20 rejected, no row created": t_major_below_20_rejected,
            "Student without accepted Major Advisor rejected": t_student_without_major_advisor_rejected,
            "Duplicate application rejected": t_duplicate_application_rejected,
            "Student cannot view another student's application (IDOR)": t_student_cannot_view_another_students_application,
            "Application document downloads and contains dynamic data, no AAU branding": t_application_document_downloads_and_contains_dynamic_data,
            "MA approval advances to hod_pending": t_ma_approval,
            "MA cannot approve another student's application (IDOR)": t_ma_cannot_approve_others_application,
            "HOD Dept B cannot approve Dept A application (IDOR)": t_hod_dept_b_cannot_approve_dept_a_application,
            "HOD approval advances to incharge_pending": t_hod_approval,
            "Incharge approval advances to dpgs_pending": t_incharge_approval,
            "Blank revert remark rejected": t_blank_revert_rejected,
            "DPGS approval completes the application": t_dpgs_approval_completes_application,
            "Stale/already-approved application re-approval rejected": t_stale_status_reapproval_rejected,
            "Full revert-to-reverted for PhD application (never silently approved)": t_revert_full_chain_for_phd,
            "HOD schedules Viva for PG": t_hod_schedules_viva_for_pg,
            "HOD Dept B cannot schedule Viva for Dept A student (IDOR)": t_hod_dept_b_cannot_schedule_viva_for_dept_a,
            "Only the actual Major Advisor can record a Viva result": t_only_actual_ma_can_record_result,
            "Unsatisfactory allows a new (unlimited) Viva attempt": t_ma_marks_unsatisfactory_allows_new_attempt,
            "Satisfactory generates the Viva Report": t_ma_marks_satisfactory_generates_report,
            "Viva Report uses the HOD-fixed date, not the result-recording date": t_report_uses_hod_fixed_date_not_result_date,
            "Committee AND-gate: one signature is not enough": t_committee_and_gate_single_signature_insufficient,
            "Committee AND-gate completes once all required members sign": t_committee_and_gate_completes_after_all_sign,
            "Viva Report HOD -> Incharge -> DPGS chain": t_viva_report_hod_incharge_dpgs_chain,
            "PG terminal state: no External Panel eligibility or access": t_pg_terminal_state_no_external_panel,
            "Only PhD applications are External Panel eligible": t_only_phd_eligible_for_panel,
            "External Panel requires exactly 5 proposals (4 and 6 rejected)": t_panel_requires_exactly_5_rejects_4_and_6,
            "External Panel proposed with exactly 5": t_panel_proposed_with_exactly_5,
            "Application detail exposes the actionable panel cycle_id (not the selection id)": t_application_detail_exposes_panel_cycle_id,
            "Pending-approvals shows HOD only while stage is hod (dept-isolated)": t_pending_approvals_shows_hod_only_at_hod_stage,
            "External Panel MA -> HOD -> Incharge -> DPGS chain": t_panel_full_chain_to_vc,
            "VC selecting zero is rejected": t_vc_select_zero_or_two_rejected,
            "VC selects exactly 1 of 5": t_vc_selects_exactly_one,
            "Second VC selection is idempotent, never changes the examiner": t_second_selection_is_idempotent_not_a_new_selection,
            "Panel cleared from every role's pending-approvals after selection": t_panel_cleared_from_pending_approvals_after_selection,
            "CRITICAL: no User account created for the selected external examiner": t_no_user_account_created_for_selected_examiner,
            "Notification email queued, no credentials included": t_email_queued_for_selected_examiner,
            "Unauthorized Major Advisor cannot upload another student's External Viva Report": t_unauthorized_ma_cannot_upload_external_report,
            "External Viva Report uploaded as version 1, application detail exposes signatures/timestamps": t_external_report_upload_version1,
            "Unauthorized Major Advisor cannot submit another student's External Viva Report": t_unauthorized_ma_cannot_submit_external_report,
            "External Viva Report submit moves to committee_pending": t_external_report_submit_moves_to_committee_pending,
            "Unrelated faculty cannot approve the committee stage (IDOR)": t_unauthorized_faculty_cannot_approve_committee_stage,
            "Committee revert with a blank remark is rejected": t_committee_revert_blank_remark_rejected,
            "Committee member approves, advances to hod_pending": t_committee_member_approves_advances_to_hod_pending,
            "HOD Dept B cannot approve or revert the External Viva Report (IDOR)": t_hod_dept_b_cannot_approve_or_revert_external_report,
            "HOD reverts the External Viva Report with a remark": t_hod_reverts_external_report_with_remark,
            "External Viva Report upload version 2 preserves version 1 (row and file)": t_external_report_upload_version2_preserves_v1,
            "External Viva Report full approval chain (wrong-role/stage blocked at each step, no signature payload required)": t_external_report_full_approval_chain,
            "Unrelated faculty cannot download the External Viva Report document (IDOR)": t_unrelated_faculty_cannot_download_external_report_document,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
            app_ids = select(ComprehensiveExamApplication.id).where(ComprehensiveExamApplication.student_id.in_(uids))
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "applications": (await db.execute(select(func.count()).select_from(ComprehensiveExamApplication).where(ComprehensiveExamApplication.student_id.in_(uids)))).scalar_one(),
                "courses": (await db.execute(select(func.count()).select_from(Course).where(Course.id.in_(list(CRS.values()) or [uuid.uuid4()])))).scalar_one(),
                "committees": (await db.execute(select(func.count()).select_from(AdvisoryCommittee).where(AdvisoryCommittee.research_title == "ZZTEST CE committee"))).scalar_one(),
                "examiners": (await db.execute(select(func.count()).select_from(ExternalExaminer).where(ExternalExaminer.email.like(f"{_PFX}%")))).scalar_one(),
                "email_outbox": (await db.execute(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.recipient.like(f"{_PFX}%")))).scalar_one(),
                "college": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        import shutil
        from pathlib import Path
        upload_dir = Path(settings.UPLOAD_DIR) / "comprehensive-exam"
        # Clean up any stray uploaded/generated files created during this run.
        for app_id_str in (S.get("APP_PG"), S.get("APP_PHD"), S.get("APP_PHD2")):
            if not app_id_str:
                continue
            for subdir in ("applications", "viva-reports", "external-viva-reports"):
                shutil.rmtree(upload_dir / subdir / app_id_str, ignore_errors=True)
        for key in ("REPORT_PG", "REPORT_PHD"):
            if S.get(key):
                shutil.rmtree(upload_dir / "viva-reports" / S[key], ignore_errors=True)
        for key in ("EXT_REPORT", "EXT_REPORT_V2"):
            if S.get(key):
                shutil.rmtree(upload_dir / "external-viva-reports" / S[key], ignore_errors=True)
        record("cleanup: no ZZTEST_COMPEXAM user / application / course / committee / examiner / college / session row remains",
               not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
