"""Standalone HTTP-level tests for the Student Progress Report module
(BUSINESS_LOGIC.md section AF).

Modelled directly on `tests/test_synopsis.py` / `tests/test_thesis.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions via `create_access_token`/`RefreshToken` rows, a
`record(name, ok, detail)` pass/fail tracker, a `_run(name, fn)` wrapper, and a
`_setup()`/`_teardown()` pair using a unique `_TAG = uuid.uuid4().hex[:6]` and
`zztest_progress_report_...` prefix on every created row so cleanup is unambiguous. If a real
Incharge Academic Cell or DPGS holder already exists (single-holder roles), the run refuses to
start, exactly like the other approval-workflow test suites.

Also includes a small, separate regression section for the Initial Thesis title-fallback fix
(`create_thesis` in `app/api/v1/endpoints/thesis.py`).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_progress_report
"""
import asyncio
import hashlib
import io
import shutil
import sys
import uuid
from pathlib import Path

import httpx
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import delete, func, select, text

from app.api.v1.endpoints import progress_report as pr_module
from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import AcademicCalendar, Semester
from app.models.audit import AuditLog
from app.models.progress_report import (
    ProgressReport, ProgressReportApprovalCycle, ProgressReportApprovalStage,
    ProgressReportProceedings, ProgressReportSignature,
)
from app.models.ppw import Ppw
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.thesis import Thesis
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_progress_report_"
_LABEL = "ZZTEST_PROGRESS_REPORT"
_DEVICE = "ZZTEST-progress-report"
_UPLOAD_ROOT = Path(settings.UPLOAD_DIR) / "progress-report"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
PR: dict[str, str] = {}                # student key -> progress report id
PR_IDS: list[uuid.UUID] = []           # every report id created, for upload-dir cleanup
COMMITTEE: dict[str, uuid.UUID] = {}
MEMBER: dict[str, uuid.UUID] = {}      # "student:facultykey" -> CommitteeMember id

# ── Thesis regression scratch (Part B) ──────────────────────────────────────
TH: dict[str, str] = {}


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


# ── plumbing ─────────────────────────────────────────────────────────────────

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


def _pdf(pages: int = 1, text_: str = "ZZTEST PROCEEDINGS", size=A4) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for i in range(pages):
        c.drawString(72, size[1] - 100, f"{text_} page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None, title=None, designation=None, active=True) -> None:
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"PR{key.upper()}", title=title,
                  designation=designation, role=role, department_id=dept.id if dept else None,
                  program_id=program.id if program else None, college_id=college.id if college else None,
                  student_roll=roll, mobile="9876543210", gender="Male", is_active=active, is_verified=True)
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_committee(student_key: str, members: list[tuple[str, str]]) -> None:
    """members: [(user_key, role)] — the first entry MUST be the major_advisor. All accepted."""
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title=f"{_LABEL} committee", status="hod_approved")
        db.add(c)
        await db.flush()
        COMMITTEE[student_key] = c.id
        for ukey, role in members:
            m = CommitteeMember(committee_id=c.id, faculty_id=U[ukey], role=role, accepted=True)
            db.add(m)
            await db.flush()
            MEMBER[f"{student_key}:{ukey}"] = m.id
        await db.commit()


# ── HTTP action helpers ──────────────────────────────────────────────────────

async def _create(student_key: str, cal_id, sem_id, expect=201, **extra) -> httpx.Response:
    body = {"academic_year_id": str(cal_id), "semester_id": str(sem_id), **extra}
    r = await _call("POST", "/progress-reports", TOK[student_key], json=body)
    assert r.status_code == expect, (r.status_code, r.text)
    if expect == 201:
        PR[student_key] = r.json()["id"]
        PR_IDS.append(uuid.UUID(PR[student_key]))
    return r


async def _detail(rid, token) -> dict:
    r = await _call("GET", f"/progress-reports/{rid}", token)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _submit(student_key: str, expect=200) -> httpx.Response:
    r = await _call("POST", f"/progress-reports/{PR[student_key]}/submit", TOK[student_key])
    assert r.status_code == expect, (r.status_code, r.text)
    return r


async def _sign(rid, token) -> httpx.Response:
    r = await _call("GET", f"/progress-reports/{rid}/approval/otp", token)
    if r.status_code != 200:
        return r
    return await _call("POST", f"/progress-reports/{rid}/approval/approve", token, json={"otp": r.json()["dev_otp"]})


async def _approve_ok(rid, token, note="") -> dict:
    r = await _sign(rid, token)
    assert r.status_code == 200, f"{note} approve -> {r.status_code} {r.text}"
    return r.json()


async def _incharge_approve(rid, token) -> dict:
    r = await _call("POST", f"/progress-reports/{rid}/approval/approve", token, json={})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _revert(rid, token, remark, expect=200) -> httpx.Response:
    r = await _call("POST", f"/progress-reports/{rid}/approval/revert", token, json={"remark": remark})
    assert r.status_code == expect, (r.status_code, r.text)
    return r


# ── DB read helpers ──────────────────────────────────────────────────────────

async def _row(rid) -> ProgressReport:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ProgressReport).where(ProgressReport.id == uuid.UUID(rid)))).scalar_one()


async def _cycles(rid) -> list[ProgressReportApprovalCycle]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ProgressReportApprovalCycle).where(ProgressReportApprovalCycle.report_id == uuid.UUID(rid))
            .order_by(ProgressReportApprovalCycle.cycle_number)
        )).scalars().all())


async def _stages(cycle_id) -> list[ProgressReportApprovalStage]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ProgressReportApprovalStage).where(ProgressReportApprovalStage.cycle_id == cycle_id)
            .order_by(ProgressReportApprovalStage.sequence)
        )).scalars().all())


async def _proceedings(cycle_id) -> list[ProgressReportProceedings]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ProgressReportProceedings).where(ProgressReportProceedings.cycle_id == cycle_id)
            .order_by(ProgressReportProceedings.version_number)
        )).scalars().all())


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    pr_module.send_email = lambda to, subject, body: True
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
            print("STOP: stray zztest_progress_report_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)

        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        col = College(name=f"{_LABEL} College", code=f"ZZPR{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col

        calendars = (await db.execute(select(AcademicCalendar).order_by(AcademicCalendar.start_date.desc()).limit(2))).scalars().all()
        assert len(calendars) >= 2, "need at least 2 existing AcademicCalendar rows in the dev DB to test uniqueness across years"
        S["CAL1"], S["CAL2"] = calendars[0], calendars[1]
        sems1 = (await db.execute(select(Semester).where(Semester.calendar_id == S["CAL1"].id).order_by(Semester.start_date).limit(2))).scalars().all()
        sems2 = (await db.execute(select(Semester).where(Semester.calendar_id == S["CAL2"].id).order_by(Semester.start_date).limit(2))).scalars().all()
        assert len(sems1) >= 2 and len(sems2) >= 2, "need at least 2 existing Semester rows per calendar in the dev DB"
        S["SEM1A"], S["SEM1B"] = sems1[0], sems1[1]
        S["SEM2A"], S["SEM2B"] = sems2[0], sems2[1]

        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, d2, pg, college = S["D1"], S["D2"], S["PG"], S["COLLEGE"]

    # Faculty
    for k in ("ma1", "cm1", "cm2", "ma_rv", "cm1_rv", "cm2_rv", "ma_mar", "cm_mar", "ma_lock", "cm_lock", "x"):
        await _mk_user(k, UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("ma_d2", UserRole.FACULTY, d2, title="Dr.", designation="Professor")
    await _mk_user("hod1", UserRole.HOD, d1, title="Dr.", designation="Professor")
    await _mk_user("hod2", UserRole.HOD, d2, title="Dr.", designation="Professor")
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None, title="Dr.", designation="Professor")
    await _mk_user("dpgs", UserRole.DPGS, None, title="Dr.", designation="Director of PG Studies")

    # Students
    for k in ("s_a", "s_b", "s_px", "s_rv", "s_mar", "s_lock", "s_deptb"):
        dept = d2 if k == "s_deptb" else d1
        await _mk_user(k, UserRole.STUDENT, dept, program=pg, college=college, roll=f"ZZPR-{k}-{_TAG}")
    for i in range(1, 9):
        await _mk_user(f"sy{i}", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZPR-sy{i}-{_TAG}")

    await _mk_committee("s_px", [("ma1", "major_advisor"), ("cm1", "member_major"), ("cm2", "member_minor")])
    await _mk_committee("s_rv", [("ma_rv", "major_advisor"), ("cm1_rv", "member_major"), ("cm2_rv", "member_minor")])
    await _mk_committee("s_mar", [("ma_mar", "major_advisor"), ("cm_mar", "member_major")])
    await _mk_committee("s_lock", [("ma_lock", "major_advisor"), ("cm_lock", "member_major")])
    await _mk_committee("s_deptb", [("ma_d2", "major_advisor")])

    S["SA"] = await _session(admin_id)
    all_users = (
        "ma1", "cm1", "cm2", "ma_rv", "cm1_rv", "cm2_rv", "ma_mar", "cm_mar", "ma_lock", "cm_lock", "x", "ma_d2",
        "hod1", "hod2", "incharge", "dpgs", "s_a", "s_b", "s_px", "s_rv", "s_mar", "s_lock", "s_deptb",
    ) + tuple(f"sy{i}" for i in range(1, 9))
    for k in all_users:
        TOK[k] = await _session(U[k])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        rids = (await db.execute(select(ProgressReport.id).where(ProgressReport.student_id.in_(uids)))).scalars().all()

        await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(uids)))
        await db.execute(delete(ProgressReport).where(ProgressReport.id.in_(rids or [uuid.uuid4()])))  # cascades cycles/stages/signatures/proceedings

        # Thesis regression leftovers (Part B)
        tids = (await db.execute(select(Thesis.id).where(Thesis.student_id.in_(uids)))).scalars().all()
        await db.execute(delete(Thesis).where(Thesis.id.in_(tids or [uuid.uuid4()])))

        ppw_ids = select(Ppw.id).where(Ppw.student_id.in_(uids))
        await db.execute(delete(Ppw).where(Ppw.id.in_(ppw_ids)))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()

    for rid in PR_IDS:
        shutil.rmtree(_UPLOAD_ROOT / str(rid), ignore_errors=True)
    if _UPLOAD_ROOT.exists() and not any(_UPLOAD_ROOT.iterdir()):
        _UPLOAD_ROOT.rmdir()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── tests ────────────────────────────────────────────────────────────────────

async def t_schema():
    async with AsyncSessionLocal() as db:
        for t in ("ams_progress_reports", "ams_progress_report_approval_cycles", "ams_progress_report_approval_stages",
                  "ams_progress_report_signatures", "ams_progress_report_proceedings"):
            assert (await db.execute(text("select count(*) from information_schema.tables where table_name=:t"), {"t": t})).scalar_one() == 1, t
        idx1 = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_progress_report_student_semester'"))).scalar_one()
        assert "UNIQUE" in idx1 and "student_id" in idx1 and "where" not in idx1.lower(), idx1  # plain, NOT partial
        idx2 = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_progress_report_one_active_cycle'"))).scalar_one()
        assert "UNIQUE" in idx2 and "active" in idx2, idx2  # partial


async def t_create_identity_and_isolation():
    # extra client-controlled identity/status fields rejected via extra=forbid
    for bad in ({"student_id": str(U["s_b"])}, {"role": "dpgs"}, {"status": "approved"},
                {"session_year": "First Year"}, {"session_semester": "First Semester"}):
        r = await _call("POST", "/progress-reports", TOK["s_a"], json={
            "academic_year_id": str(S["CAL1"].id), "semester_id": str(S["SEM1A"].id), **bad,
        })
        assert r.status_code == 422, (bad, r.status_code, r.text)

    await _create("s_a", S["CAL1"].id, S["SEM1A"].id)
    await _create("s_b", S["CAL1"].id, S["SEM1A"].id)
    d = await _detail(PR["s_a"], TOK["s_a"])
    assert d["status"] == "draft" and d["can_edit"] is True and d["is_owner"] is True
    assert d["session_year"] is None and d["session_semester"] is None, "semester_completed not set -> both null"

    # Student B cannot view/edit/submit Student A's report -> 404
    assert (await _call("GET", f"/progress-reports/{PR['s_a']}", TOK["s_b"])).status_code == 404
    assert (await _call("PATCH", f"/progress-reports/{PR['s_a']}", TOK["s_b"], json={"research_progress": "hijack"})).status_code == 404
    assert (await _call("POST", f"/progress-reports/{PR['s_a']}/submit", TOK["s_b"])).status_code == 404
    assert (await _row(PR["s_a"])).research_progress is None

    # Proceedings: role-gated (student can never call this endpoint at all)
    assert (await _call("POST", f"/progress-reports/{PR['s_a']}/proceedings", TOK["s_b"],
                         files={"file": ("x.pdf", _pdf(), "application/pdf")})).status_code == 403
    # No student, not even the owner, can ever download Proceedings
    assert (await _call("GET", f"/progress-reports/{PR['s_a']}/proceedings", TOK["s_a"])).status_code == 404
    assert (await _call("GET", f"/progress-reports/{PR['s_a']}/proceedings", TOK["s_b"])).status_code == 404

    # non-students refused
    assert (await _call("POST", "/progress-reports", TOK["ma1"], json={
        "academic_year_id": str(S["CAL1"].id), "semester_id": str(S["SEM1A"].id),
    })).status_code == 403
    assert (await _call("POST", "/progress-reports", None, json={})).status_code in (401, 403)


async def t_session_labels():
    expected = {
        1: ("First Year", "First Semester"), 2: ("First Year", "Second Semester"),
        3: ("Second Year", "First Semester"), 4: ("Second Year", "Second Semester"),
        5: ("Third Year", "First Semester"), 6: ("Third Year", "Second Semester"),
        7: ("Fourth Year", "First Semester"), 8: ("Fourth Year", "Second Semester"),
    }
    for n, (yr, sem) in expected.items():
        key = f"sy{n}"
        await _create(key, S["CAL1"].id, S["SEM1A"].id, semester_completed=n)
        d = await _detail(PR[key], TOK[key])
        assert (d["session_year"], d["session_semester"]) == (yr, sem), (n, d["session_year"], d["session_semester"])
    # semester_completed=None (not yet set) -> both null — already asserted on s_a in the prior test.


async def t_uniqueness():
    # same student + same (year, semester) -> 409, regardless of first report's status
    r = await _call("POST", "/progress-reports", TOK["s_a"], json={
        "academic_year_id": str(S["CAL1"].id), "semester_id": str(S["SEM1A"].id),
    })
    assert r.status_code == 409, r.text
    # different semester_id, same year -> succeeds
    r = await _call("POST", "/progress-reports", TOK["s_a"], json={
        "academic_year_id": str(S["CAL1"].id), "semester_id": str(S["SEM1B"].id),
    })
    assert r.status_code == 201, r.text
    # different academic_year_id, "same" semester position -> succeeds
    r = await _call("POST", "/progress-reports", TOK["s_a"], json={
        "academic_year_id": str(S["CAL2"].id), "semester_id": str(S["SEM2A"].id),
    })
    assert r.status_code == 201, r.text
    # the DB constraint itself refuses a duplicate even bypassing the friendly pre-check
    from sqlalchemy.exc import IntegrityError
    try:
        async with AsyncSessionLocal() as db:
            db.add(ProgressReport(student_id=U["s_a"], academic_year_id=S["CAL1"].id, semester_id=S["SEM1A"].id,
                                   status="draft", student_name_snapshot="x"))
            await db.commit()
        raise AssertionError("the database accepted a duplicate (student, year, semester) Progress Report")
    except IntegrityError:
        pass
    # the reverted-specific case is covered end-to-end in t_revert_major_advisor_and_resubmit


async def t_student_completion_fields():
    """CORRECTED (Sections 1/2): `expected_completion`/`completion_delay_reason` are STUDENT
    fields, set via the student's own create/update endpoints — never `/advisor-fields`."""
    sid = PR["s_a"]  # s_a's current draft (never submitted, no committee assigned — submit's
    # own `expected_completion is None` guard fires before any committee check, so this is
    # testable without setting one up).

    # item 7: submit rejected while expected_completion was never set (still None)
    r = await _call("POST", f"/progress-reports/{sid}/submit", TOK["s_a"])
    assert r.status_code == 400 and "completed in time" in r.json()["detail"], r.text
    assert (await _row(sid)).expected_completion is None

    # item 5: any value outside "Yes"/"No" -> 422 (Pydantic Literal)
    r = await _call("PATCH", f"/progress-reports/{sid}", TOK["s_a"], json={"expected_completion": "Maybe"})
    assert r.status_code == 422, r.text

    # item 1: "Yes" -> stored True, reason stays None
    r = await _call("PATCH", f"/progress-reports/{sid}", TOK["s_a"], json={"expected_completion": "Yes"})
    assert r.status_code == 200, r.text
    row = await _row(sid)
    assert row.expected_completion is True and row.completion_delay_reason is None

    # item 4: "No" + a blank reason -> rejected; the prior stored state is untouched
    r = await _call("PATCH", f"/progress-reports/{sid}", TOK["s_a"], json={"expected_completion": "No", "completion_delay_reason": "   "})
    assert r.status_code == 400, r.text
    assert (await _row(sid)).expected_completion is True, "a rejected edit must not partially apply"

    # item 3: "No" alone, with no reason field at all in the request -> rejected
    r = await _call("PATCH", f"/progress-reports/{sid}", TOK["s_a"], json={"expected_completion": "No"})
    assert r.status_code == 400, r.text
    assert (await _row(sid)).expected_completion is True

    # item 2: "No" + a real reason -> stored
    r = await _call("PATCH", f"/progress-reports/{sid}", TOK["s_a"], json={
        "expected_completion": "No", "completion_delay_reason": "ZZTEST behind on coursework",
    })
    assert r.status_code == 200, r.text
    row = await _row(sid)
    assert row.expected_completion is False and row.completion_delay_reason == "ZZTEST behind on coursework"

    # item 6: "Yes" with a STALE reason in the SAME request -> the reason must not remain active
    r = await _call("PATCH", f"/progress-reports/{sid}", TOK["s_a"], json={
        "expected_completion": "Yes", "completion_delay_reason": "this stale reason must be dropped",
    })
    assert r.status_code == 200, r.text
    row = await _row(sid)
    assert row.expected_completion is True and row.completion_delay_reason is None, \
        "Yes must always clear any reason, even one sent in the very same request"

    # item 3 (second half): submit rejected when "No" is already STORED with no reason. The
    # PATCH-layer guard makes this state unreachable through the API itself, so force it
    # directly in the DB to confirm the submit endpoint has its OWN independent guard too.
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(ProgressReport).where(ProgressReport.id == uuid.UUID(sid)))).scalar_one()
        row.expected_completion = False
        row.completion_delay_reason = None
        await db.commit()
    r = await _call("POST", f"/progress-reports/{sid}/submit", TOK["s_a"])
    assert r.status_code == 400, r.text

    # create-path coverage (items 1/2/5 at creation time, using s_b's still-unused semester slots)
    await _create("s_b", S["CAL1"].id, S["SEM1B"].id, expected_completion="Yes")
    row = await _row(PR["s_b"])
    assert row.expected_completion is True and row.completion_delay_reason is None

    await _create("s_b", S["CAL2"].id, S["SEM2A"].id, expected_completion="No", completion_delay_reason="ZZTEST created-with-reason")
    row = await _row(PR["s_b"])
    assert row.expected_completion is False and row.completion_delay_reason == "ZZTEST created-with-reason"

    await _create("s_b", S["CAL2"].id, S["SEM2B"].id, expect=422, expected_completion="Maybe")
    await _create("s_b", S["CAL2"].id, S["SEM2B"].id, expect=400, expected_completion="No")


async def t_editability_and_tampering_shapes():
    # a Faculty/HOD attempting the student-only update/submit endpoints is simply refused by role
    assert (await _call("PATCH", f"/progress-reports/{PR['s_a']}", TOK["ma1"], json={"research_progress": "x"})).status_code == 403
    assert (await _call("POST", f"/progress-reports/{PR['s_a']}/submit", TOK["hod1"])).status_code == 403
    # extra fields rejected (422) on every mutating endpoint, using a real report id belonging
    # to a real approver-eligible flow (validation happens before any state check)
    rid = PR["s_a"]
    assert (await _call("PATCH", f"/progress-reports/{rid}", TOK["s_a"], json={"research_progress": "x", "status": "approved"})).status_code == 422
    assert (await _call("PATCH", f"/progress-reports/{rid}/advisor-fields", TOK["ma1"], json={
        "advisory_remark": "x", "approver_id": str(U["ma1"]),
    })).status_code == 422
    assert (await _call("PATCH", f"/progress-reports/{rid}", TOK["s_a"], json={
        "research_progress": "x", "expected_completion": "Maybe",
    })).status_code == 422
    assert (await _call("POST", f"/progress-reports/{rid}/approval/approve", TOK["ma1"], json={"otp": "1", "department_id": str(S["D1"].id)})).status_code == 422
    assert (await _call("POST", f"/progress-reports/{rid}/approval/revert", TOK["ma1"], json={"remark": "x", "major_advisor_id": str(U["ma1"])})).status_code == 422


async def t_idor_foreign_and_query_tampering():
    random_id = uuid.uuid4()
    assert (await _call("GET", f"/progress-reports/{random_id}", TOK["dpgs"])).status_code == 404
    assert (await _call("POST", f"/progress-reports/{random_id}/approval/approve", TOK["dpgs"], json={})).status_code == 404
    assert (await _call("GET", f"/progress-reports/{random_id}/proceedings", TOK["dpgs"])).status_code == 404
    # query-parameter tampering is inert
    rid = PR["s_a"]
    base = await _detail(rid, TOK["s_a"])
    r = await _call("GET", f"/progress-reports/{rid}?debug=1&student_id={U['s_b']}&role=super_admin", TOK["s_a"])
    assert r.status_code == 200 and r.json() == base


async def t_department_isolation():
    await _create("s_deptb", S["CAL1"].id, S["SEM1A"].id, expected_completion="Yes")
    await _submit("s_deptb")
    sid = PR["s_deptb"]
    assert (await _row(sid)).status == "major_advisor_pending"
    await _approve_ok(sid, TOK["ma_d2"], "ma_d2")  # no other committee members -> approval skips straight past committee
    assert (await _row(sid)).status == "hod_pending"
    # HOD-A (D1) cannot see or act on a D2 student's report
    assert (await _call("GET", f"/progress-reports/{sid}/approval/otp", TOK["hod1"])).status_code == 403
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["hod1"], json={"otp": "1"})).status_code == 403
    inbox1 = (await _call("GET", "/progress-reports/pending-approvals", TOK["hod1"])).json()
    assert sid not in [row["report_id"] for row in inbox1]
    inbox2 = (await _call("GET", "/progress-reports/pending-approvals", TOK["hod2"])).json()
    assert sid in [row["report_id"] for row in inbox2]
    await _call("GET", f"/progress-reports/{sid}", TOK["hod1"])
    assert (await _call("GET", f"/progress-reports/{sid}", TOK["hod1"])).status_code == 404, "wrong-department HOD cannot even view"
    await _approve_ok(sid, TOK["hod2"], "hod2")
    assert (await _row(sid)).status == "incharge_academic_cell_pending"


async def t_full_approval_chain_and_related():
    """s_px: MA + member_major + member_minor. Exercises the full happy path (items 5/6),
    committee resolution (item 8), advisor-fields confidentiality/authorization (items 8-14),
    and Proceedings mechanics (item 9) in one continuous flow."""
    await _create("s_px", S["CAL1"].id, S["SEM1A"].id, expected_completion="Yes")
    sid = PR["s_px"]
    await _submit("s_px")
    d = await _detail(sid, TOK["s_px"])
    assert d["status"] == "major_advisor_pending" and d["current_cycle_number"] == 1
    assert d["expected_completion"] == "Yes", "not confidential — every authorized viewer, including the student, sees this"
    cyc = (await _cycles(sid))[-1]
    stages = await _stages(cyc.id)
    assert [s.stage_type for s in stages] == ["major_advisor", "committee_member", "committee_member", "hod", "incharge_academic_cell", "dpgs"]
    ma_member_id = MEMBER["s_px:ma1"]
    assert not any(s.stage_type == "committee_member" and s.committee_member_id == ma_member_id for s in stages), "MA must never be duplicated as a committee-member stage"

    # committee resolution: an unrelated faculty member is blocked at the MA stage
    assert (await _call("GET", f"/progress-reports/{sid}/approval/otp", TOK["x"])).status_code == 403
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["x"], json={"otp": "1"})).status_code == 403
    # out-of-order: HOD/Incharge/DPGS cannot act this early
    for who in ("hod1", "dpgs", "incharge"):
        assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who

    # Major Advisor fields (CORRECTED): confidentiality-by-omission + authorization (items 8-10)
    d_before = await _detail(sid, TOK["ma1"])
    assert "advisory_remark" in d_before and d_before["advisory_remark"] is None
    assert "overall_progress" in d_before and d_before["overall_progress"] is None
    assert "student_conduct" in d_before and d_before["student_conduct"] is None
    assert d_before["can_edit_advisor_fields"] is True, "the live Major Advisor, at their own stage"
    d_student = await _detail(sid, TOK["s_px"])
    assert "advisory_remark" not in d_student and "overall_progress" not in d_student and "student_conduct" not in d_student, \
        "the 3 Major Advisor fields must be entirely ABSENT (not null) from the student's own view"

    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["s_px"], json={"advisory_remark": "x"})).status_code == 403, "student role rejected outright"
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["x"], json={"advisory_remark": "x"})).status_code == 403, "unrelated faculty"
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["cm1"], json={"advisory_remark": "x"})).status_code == 403, "not the MA"
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["ma_rv"], json={"advisory_remark": "x"})).status_code == 403, "another student's Major Advisor"
    r = await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["ma1"], json={
        "advisory_remark": "ZZTEST Good steady progress.", "overall_progress": "ZZTEST On track.", "student_conduct": "ZZTEST Excellent.",
    })
    assert r.status_code == 200, r.text
    d_after = await _detail(sid, TOK["ma1"])
    assert (d_after["advisory_remark"], d_after["overall_progress"], d_after["student_conduct"]) == (
        "ZZTEST Good steady progress.", "ZZTEST On track.", "ZZTEST Excellent.")
    row = await _row(sid)
    assert (row.advisory_remark, row.overall_progress, row.student_conduct) == (
        "ZZTEST Good steady progress.", "ZZTEST On track.", "ZZTEST Excellent.")
    # still invisible to the student even once populated (never null/empty — literally absent)
    d_student2 = await _detail(sid, TOK["s_px"])
    assert "advisory_remark" not in d_student2 and "overall_progress" not in d_student2 and "student_conduct" not in d_student2

    # Proceedings validation (only the live MA, only while major_advisor_pending)
    up = lambda tok, data, name="x.pdf", ct="application/pdf": _call(
        "POST", f"/progress-reports/{sid}/proceedings", tok, files={"file": (name, data, ct)})
    assert (await up(TOK["ma1"], b"PK\x03\x04" + b"0" * 200, "x.docx",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")).status_code == 400
    assert (await up(TOK["ma1"], b"\xff\xd8\xff\xe0" + b"0" * 200)).status_code == 400, "jpeg bytes renamed .pdf"
    assert (await up(TOK["ma1"], b"%PDF-1.4\nnot really a pdf\n%%EOF")).status_code == 400, "corrupt/truncated pdf"
    assert (await up(TOK["ma1"], b"")).status_code == 400, "empty file"
    big = b"%PDF-1.4\n" + b"0" * (settings.MAX_FILE_SIZE_MB * 1024 * 1024)
    assert (await up(TOK["ma1"], big, "big.pdf")).status_code == 413, "oversized"
    # wrong actors blocked even with a valid PDF
    good = _pdf(1, "ZZTEST PROCEEDINGS V1")
    assert (await up(TOK["hod1"], good)).status_code == 403
    assert (await up(TOK["cm1"], good)).status_code == 403, "a committee member, not the MA, cannot upload"
    assert (await up(TOK["x"], good)).status_code == 403

    r = await up(TOK["ma1"], good, "Proceedings v1.pdf")
    assert r.status_code == 201 and r.json()["version"] == 1, r.text
    assert (await _row(sid)).status == "major_advisor_pending", "uploading Proceedings does not auto-approve"
    r = await up(TOK["ma1"], _pdf(1, "ZZTEST PROCEEDINGS V2"), "v2.pdf")
    assert r.status_code == 201 and r.json()["version"] == 2, r.text
    versions = await _proceedings(cyc.id)
    assert [p.version_number for p in versions] == [1, 2], "old version is never deleted"

    # nobody views/downloads Proceedings who shouldn't; the student never can (owner included)
    assert (await _call("GET", f"/progress-reports/{sid}/proceedings", TOK["s_px"])).status_code == 404
    assert (await _call("GET", f"/progress-reports/{sid}/proceedings", TOK["x"])).status_code == 404
    assert (await _call("GET", f"/progress-reports/{sid}", TOK["x"])).status_code == 404, "unrelated faculty cannot even view"
    for who in ("ma1", "cm1", "cm2", "hod1", "incharge", "dpgs"):
        r = await _call("GET", f"/progress-reports/{sid}/proceedings", TOK[who])
        assert r.status_code == 200 and r.content[:5] == b"%PDF-", who
    r = await _call("GET", f"/progress-reports/{sid}/proceedings", S["SA"])
    assert r.status_code == 200

    # Major Advisor approves (OTP)
    await _approve_ok(sid, TOK["ma1"], "ma1")
    assert (await _row(sid)).status == "committee_pending"
    assert (await _sign(sid, TOK["ma1"])).status_code == 403, "MA cannot approve twice"
    # uploading Proceedings is now blocked — the MA stage has moved on
    assert (await up(TOK["ma1"], _pdf(1, "too late"))).status_code == 403
    # item 13: the gate is "only while at THEIR OWN stage" — the actual MA is blocked too, now
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["ma1"], json={"advisory_remark": "too late"})).status_code == 403
    # item 11/12: committee members see the MA's saved fields read-only, and cannot write them
    d_cm1 = await _detail(sid, TOK["cm1"])
    assert d_cm1["advisory_remark"] == "ZZTEST Good steady progress." and d_cm1["can_edit_advisor_fields"] is False
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["cm1"], json={"advisory_remark": "hijack"})).status_code == 403

    # HOD/incharge/dpgs still blocked
    for who in ("hod1", "dpgs", "incharge"):
        assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who
    # a faculty member with no committee role at all cannot approve the committee stage
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["x"], json={"otp": "1"})).status_code == 403

    await _approve_ok(sid, TOK["cm1"], "cm1")
    assert (await _row(sid)).status == "committee_pending", "HOD still blocked with one member outstanding"
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["hod1"], json={"otp": "1"})).status_code == 403
    await _approve_ok(sid, TOK["cm2"], "cm2")
    assert (await _row(sid)).status == "hod_pending"
    d_hod = await _detail(sid, TOK["hod1"])
    assert d_hod["overall_progress"] == "ZZTEST On track." and d_hod["can_edit_advisor_fields"] is False
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["hod1"], json={"advisory_remark": "hijack"})).status_code == 403

    for who in ("dpgs", "incharge"):
        assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who
    await _approve_ok(sid, TOK["hod1"], "hod1")
    assert (await _row(sid)).status == "incharge_academic_cell_pending"
    d_inc = await _detail(sid, TOK["incharge"])
    assert d_inc["student_conduct"] == "ZZTEST Excellent." and d_inc["can_edit_advisor_fields"] is False
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["incharge"], json={"advisory_remark": "hijack"})).status_code == 403

    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["dpgs"], json={"otp": "1"})).status_code == 403, "DPGS blocked before Incharge"
    # Incharge: no OTP possible/required
    assert (await _call("GET", f"/progress-reports/{sid}/approval/otp", TOK["incharge"])).status_code == 400
    await _incharge_approve(sid, TOK["incharge"])
    assert (await _row(sid)).status == "dpgs_pending"
    d_dpgs = await _detail(sid, TOK["dpgs"])
    assert d_dpgs["advisory_remark"] == "ZZTEST Good steady progress." and d_dpgs["can_edit_advisor_fields"] is False
    assert (await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["dpgs"], json={"advisory_remark": "hijack"})).status_code == 403

    d = await _approve_ok(sid, TOK["dpgs"], "dpgs")
    assert d["status"] == "approved"
    row = await _row(sid)
    assert row.status == "approved" and row.approved_at is not None
    assert (await _cycles(sid))[-1].status == "approved"
    # item 14: Super Admin can view the 3 Major Advisor fields (oversight)
    d_sa = await _detail(sid, S["SA"])
    assert (d_sa["advisory_remark"], d_sa["overall_progress"], d_sa["student_conduct"]) == (
        "ZZTEST Good steady progress.", "ZZTEST On track.", "ZZTEST Excellent.")
    # confidentiality-by-omission holds for the student even after full approval
    d_student_final = await _detail(sid, TOK["s_px"])
    assert "advisory_remark" not in d_student_final and "overall_progress" not in d_student_final and "student_conduct" not in d_student_final


async def t_revert_partial_chain():
    """s_rv: MA + member_major + member_minor. Every non-MA revert type (committee/HOD/
    incharge/DPGS) goes back exactly one step WITHOUT ending the cycle, opening a fresh round
    with new stage rows. Full history must survive every revert."""
    await _create("s_rv", S["CAL1"].id, S["SEM1B"].id, expected_completion="No", completion_delay_reason="ZZTEST behind schedule, will finish next term")
    sid = PR["s_rv"]
    await _submit("s_rv")
    assert (await _row(sid)).status == "major_advisor_pending"
    cyc = (await _cycles(sid))[-1]

    # a revert needs a non-blank remark
    for bad in ({"remark": ""}, {"remark": "   "}, {}):
        r = await _call("POST", f"/progress-reports/{sid}/approval/revert", TOK["ma_rv"], json=bad)
        assert r.status_code in (400, 422), bad

    # item 16 setup: Major Advisor saves the 3 assessment fields BEFORE approving — a later
    # Committee-Member-triggered revert back to this stage must not clear them.
    r = await _call("PATCH", f"/progress-reports/{sid}/advisor-fields", TOK["ma_rv"], json={
        "advisory_remark": "ZZTEST round-1 remark", "overall_progress": "ZZTEST round-1 progress", "student_conduct": "ZZTEST round-1 conduct",
    })
    assert r.status_code == 200, r.text

    await _approve_ok(sid, TOK["ma_rv"], "ma_rv")
    assert (await _row(sid)).status == "committee_pending"
    await _approve_ok(sid, TOK["cm1_rv"], "cm1_rv")
    ma_stage_1 = next(s for s in await _stages(cyc.id) if s.stage_type == "major_advisor")
    assert ma_stage_1.status == "approved"

    # Committee Member (cm2, still pending) reverts mid-committee-stage -> major_advisor_pending,
    # cycle stays active (NOT "reverted")
    await _revert(sid, TOK["cm2_rv"], "ZZTEST committee revert — please clarify progress")
    assert (await _row(sid)).status == "major_advisor_pending"
    assert (await _cycles(sid))[-1].status == "active"
    # item 16: the revert-back-to-MA-stage must NOT reset/clear the MA's already-saved fields
    row = await _row(sid)
    assert (row.advisory_remark, row.overall_progress, row.student_conduct) == (
        "ZZTEST round-1 remark", "ZZTEST round-1 progress", "ZZTEST round-1 conduct")
    stages = await _stages(cyc.id)
    # the original MA row is untouched history; cm1's approval is untouched history
    ma_stage_1_after = next(s for s in stages if s.id == ma_stage_1.id)
    assert ma_stage_1_after.status == "approved"
    cm1_stage_1 = next(s for s in stages if s.stage_type == "committee_member" and s.committee_member_id == MEMBER["s_rv:cm1_rv"])
    assert cm1_stage_1.status == "approved"
    cm2_stage_1 = next(s for s in stages if s.stage_type == "committee_member" and s.committee_member_id == MEMBER["s_rv:cm2_rv"])
    assert cm2_stage_1.status == "reverted" and cm2_stage_1.remark == "ZZTEST committee revert — please clarify progress"
    # a fresh, live, pending MA stage now exists (higher sequence than the original)
    live_ma = [s for s in stages if s.stage_type == "major_advisor"]
    fresh_ma = max(live_ma, key=lambda s: s.sequence)
    assert fresh_ma.id != ma_stage_1.id and fresh_ma.status == "pending" and fresh_ma.sequence > ma_stage_1.sequence
    assert (await _call("POST", f"/progress-reports/{sid}/approval/revert", TOK["cm2_rv"], json={"remark": "again"})).status_code == 403, "cannot revert twice"
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["cm1_rv"], json={"otp": "1"})).status_code == 403, "committee stage is not live anymore"

    # Major Advisor re-approves -> committee_pending with FRESH rows for ALL non-MA members —
    # cm1, who had already approved before the revert, must approve AGAIN.
    await _approve_ok(sid, TOK["ma_rv"], "ma_rv round 2")
    assert (await _row(sid)).status == "committee_pending"
    stages = await _stages(cyc.id)
    cm1_live = max((s for s in stages if s.stage_type == "committee_member" and s.committee_member_id == MEMBER["s_rv:cm1_rv"]), key=lambda s: s.sequence)
    cm2_live = max((s for s in stages if s.stage_type == "committee_member" and s.committee_member_id == MEMBER["s_rv:cm2_rv"]), key=lambda s: s.sequence)
    assert cm1_live.status == "pending" and cm1_live.sequence > cm1_stage_1.sequence, "cm1's OLD approval does not carry over into the new round"
    assert cm2_live.status == "pending" and cm2_live.sequence > cm2_stage_1.sequence
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["hod1"], json={"otp": "1"})).status_code == 403
    await _approve_ok(sid, TOK["cm1_rv"], "cm1_rv round 2")
    await _approve_ok(sid, TOK["cm2_rv"], "cm2_rv round 2")
    assert (await _row(sid)).status == "hod_pending"

    # HOD reverts -> committee_pending with fresh committee rows; MA is NOT reopened
    await _revert(sid, TOK["hod1"], "ZZTEST HOD revert — recheck committee sign-off")
    assert (await _row(sid)).status == "committee_pending"
    assert (await _cycles(sid))[-1].status == "active"
    assert (await _call("POST", f"/progress-reports/{sid}/approval/approve", TOK["ma_rv"], json={"otp": "1"})).status_code == 403, "no live pending MA stage"
    stages = await _stages(cyc.id)
    cm1_r2 = max((s for s in stages if s.stage_type == "committee_member" and s.committee_member_id == MEMBER["s_rv:cm1_rv"]), key=lambda s: s.sequence)
    cm2_r2 = max((s for s in stages if s.stage_type == "committee_member" and s.committee_member_id == MEMBER["s_rv:cm2_rv"]), key=lambda s: s.sequence)
    assert cm1_r2.status == "pending" and cm2_r2.status == "pending"
    assert cm1_r2.sequence > cm1_live.sequence and cm2_r2.sequence > cm2_live.sequence
    await _approve_ok(sid, TOK["cm1_rv"], "cm1_rv round 3")
    await _approve_ok(sid, TOK["cm2_rv"], "cm2_rv round 3")
    assert (await _row(sid)).status == "hod_pending"
    await _approve_ok(sid, TOK["hod1"], "hod1 round 2")
    assert (await _row(sid)).status == "incharge_academic_cell_pending"

    # Incharge reverts -> hod_pending with a fresh HOD row
    await _revert(sid, TOK["incharge"], "ZZTEST Incharge revert — HOD to recheck")
    assert (await _row(sid)).status == "hod_pending"
    assert (await _cycles(sid))[-1].status == "active"
    await _approve_ok(sid, TOK["hod1"], "hod1 round 3")
    assert (await _row(sid)).status == "incharge_academic_cell_pending"
    await _incharge_approve(sid, TOK["incharge"])
    assert (await _row(sid)).status == "dpgs_pending"

    # DPGS reverts -> incharge_academic_cell_pending with a fresh Incharge row
    await _revert(sid, TOK["dpgs"], "ZZTEST DPGS revert — Incharge to recheck")
    assert (await _row(sid)).status == "incharge_academic_cell_pending"
    assert (await _cycles(sid))[-1].status == "active"
    await _incharge_approve(sid, TOK["incharge"])
    assert (await _row(sid)).status == "dpgs_pending"
    d = await _approve_ok(sid, TOK["dpgs"], "dpgs final")
    assert d["status"] == "approved"

    # Full history preserved and queryable throughout
    hist = (await _detail(sid, TOK["s_rv"]))["history"]
    assert sum(1 for h in hist if h["status"] == "reverted") == 4, [h["status"] for h in hist]
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(ProgressReportApprovalStage).where(
            ProgressReportApprovalStage.cycle_id == cyc.id))).scalar_one()
    assert n == len(hist) and n > 6, "every stage row across every round is still present"


async def t_revert_major_advisor_and_resubmit():
    """s_mar: MA + member_major. A Major Advisor revert ends the cycle and returns to the
    student — the student edits and resubmits the SAME report id, starting cycle 2. Also
    exercises the reverted-specific uniqueness case (a second creation attempt is still 409
    while the existing report sits at status='reverted')."""
    await _create("s_mar", S["CAL2"].id, S["SEM2B"].id, research_title="ZZTEST v1", expected_completion="Yes")
    sid = PR["s_mar"]
    await _submit("s_mar")
    assert (await _row(sid)).status == "major_advisor_pending"
    await _revert(sid, TOK["ma_mar"], "ZZTEST Major Advisor revert — redo the research progress section")
    row = await _row(sid)
    assert row.status == "reverted"
    cyc1 = (await _cycles(sid))[-1]
    assert cyc1.status == "reverted" and cyc1.reverted_at is not None
    d = await _detail(sid, TOK["s_mar"])
    assert d["can_edit"] is True
    ri = d["revert_info"]
    assert ri["role"] == "Major Advisor" and "redo the research progress" in ri["remark"]

    # a second creation attempt for the SAME (year, semester) is still refused while reverted
    r = await _call("POST", "/progress-reports", TOK["s_mar"], json={
        "academic_year_id": str(S["CAL2"].id), "semester_id": str(S["SEM2B"].id),
    })
    assert r.status_code == 409, r.text

    # the student edits and resubmits the SAME report id
    assert (await _call("PATCH", f"/progress-reports/{sid}", TOK["s_mar"], json={"research_title": "ZZTEST v2"})).status_code == 200
    await _submit("s_mar")
    row = await _row(sid)
    assert str(row.id) == sid and row.status == "major_advisor_pending"
    cyc2 = (await _cycles(sid))[-1]
    assert cyc2.cycle_number == 2 and cyc2.id != cyc1.id
    stages2 = await _stages(cyc2.id)
    assert all(s.status == "pending" for s in stages2), "cycle 2 starts with a completely fresh stage set"
    assert len(await _cycles(sid)) == 2


async def t_committee_lock():
    await _create("s_lock", S["CAL2"].id, S["SEM2A"].id, expected_completion="Yes")
    await _submit("s_lock")
    sid = PR["s_lock"]
    cid = COMMITTEE["s_lock"]
    assert (await _row(sid)).status == "major_advisor_pending"

    r = await _call("POST", f"/research/committees/{cid}/reassign-major-advisor", S["SA"], json={"major_advisor_id": str(U["x"])})
    assert r.status_code == 409 and "Progress Report" in r.json()["detail"], r.text
    r = await _call("POST", f"/research/committees/{cid}/members", S["SA"], json={"faculty_id": str(U["x"]), "role": "member_minor"})
    assert r.status_code == 409 and "Progress Report" in r.json()["detail"], r.text
    r = await _call("DELETE", f"/research/committees/{cid}/members/{MEMBER['s_lock:cm_lock']}", S["SA"])
    assert r.status_code == 409 and "Progress Report" in r.json()["detail"], r.text

    # release via a Major-Advisor-level revert
    await _revert(sid, TOK["ma_lock"], "ZZTEST release the lock")
    assert (await _cycles(sid))[-1].status == "reverted"
    r = await _call("DELETE", f"/research/committees/{cid}/members/{MEMBER['s_lock:cm_lock']}", S["SA"])
    assert r.status_code == 204, r.text

    # release also happens once a (separate) report reaches full DPGS approval
    approved_sid = PR["s_px"]
    assert (await _row(approved_sid)).status == "approved"
    r = await _call("POST", f"/research/committees/{COMMITTEE['s_px']}/members", S["SA"], json={"faculty_id": str(U["x"]), "role": "member_minor"})
    assert r.status_code != 409, "the lock must have released after full DPGS approval"


# ── Part B: Thesis title-fallback regression ────────────────────────────────

async def _th_mk_committee(student_key: str, ma_key: str) -> None:
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title=f"{_LABEL} thesis committee", status="hod_approved")
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True))
        await db.commit()


async def t_thesis_title_fallback():
    pg = S["PG"]
    d1 = S["D1"]
    college = S["COLLEGE"]
    await _mk_user("th_full", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZPR-th-full-{_TAG}")
    await _mk_user("th_blank", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZPR-th-blank-{_TAG}")
    await _mk_user("th_noppw", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZPR-th-noppw-{_TAG}")
    TOK["th_full"] = await _session(U["th_full"])
    TOK["th_blank"] = await _session(U["th_blank"])
    TOK["th_noppw"] = await _session(U["th_noppw"])

    async with AsyncSessionLocal() as db:
        db.add(Ppw(student_id=U["th_full"], status="draft", research_title="ZZTEST PPW Title Original"))
        db.add(Ppw(student_id=U["th_blank"], status="draft", research_title="   "))
        await db.commit()

    # 1) PPW with a non-blank research_title -> empty body uses the PPW's title
    r = await _call("POST", "/thesis", TOK["th_full"], json={})
    assert r.status_code == 201, r.text
    TH["th_full"] = r.json()["id"]
    assert r.json()["title"] == "ZZTEST PPW Title Original"

    # 2) PPW exists but research_title is blank/whitespace-only -> {} rejected, explicit title works
    r = await _call("POST", "/thesis", TOK["th_blank"], json={})
    assert r.status_code == 400, r.text
    r = await _call("POST", "/thesis", TOK["th_blank"], json={"title": "ZZTEST My Manual Title"})
    assert r.status_code == 201, r.text
    TH["th_blank"] = r.json()["id"]
    assert r.json()["title"] == "ZZTEST My Manual Title"

    # 3) No PPW at all -> {} rejected; explicit title works, ppw_id is null
    r = await _call("POST", "/thesis", TOK["th_noppw"], json={})
    assert r.status_code == 400, r.text
    r = await _call("POST", "/thesis", TOK["th_noppw"], json={"title": "ZZTEST No PPW Title"})
    assert r.status_code == 201, r.text
    TH["th_noppw"] = r.json()["id"]
    assert r.json()["title"] == "ZZTEST No PPW Title"
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Thesis).where(Thesis.id == uuid.UUID(TH["th_noppw"])))).scalar_one()
        assert t.ppw_id is None

    # 4) Regression: changing the PPW's research_title afterward never alters title_snapshot
    async with AsyncSessionLocal() as db:
        ppw = (await db.execute(select(Ppw).where(Ppw.student_id == U["th_full"]))).scalar_one()
        ppw.research_title = "ZZTEST PPW Title CHANGED LATER"
        await db.commit()
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Thesis).where(Thesis.id == uuid.UUID(TH["th_full"])))).scalar_one()
        assert t.title_snapshot == "ZZTEST PPW Title Original", "title_snapshot must not resync with the PPW"


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "SCHEMA: 5 tables; plain unique (student,year,semester); partial unique (one active cycle)": t_schema,
            "CREATE/IDENTITY/ISOLATION: extra=forbid on identity fields; Student B blocked (404) on A's report; Proceedings never downloadable by any student": t_create_identity_and_isolation,
            "SESSION LABELS: all 8 semester_completed -> Year/Semester mappings; None -> null/null": t_session_labels,
            "UNIQUENESS: same (year,semester) -> 409; different semester/year -> 201; DB constraint itself refuses": t_uniqueness,
            "STUDENT COMPLETION FIELDS: expected_completion Yes/No + reason rules (create and update paths), submit guards": t_student_completion_fields,
            "EDITABILITY/TAMPERING: non-owner role-gated 403; extra=forbid on PATCH/advisor-fields/approve/revert": t_editability_and_tampering_shapes,
            "IDOR: foreign report id -> 404 everywhere; query-param tampering inert": t_idor_foreign_and_query_tampering,
            "DEPARTMENT ISOLATION: wrong-department HOD cannot see/act/view; right department can": t_department_isolation,
            "FULL APPROVAL CHAIN + committee resolution + advisor-fields + Proceedings mechanics": t_full_approval_chain_and_related,
            "REVERT MATRIX (partial, non-MA): committee/HOD/Incharge/DPGS each go back one step, cycle stays active, fresh rounds, full history": t_revert_partial_chain,
            "REVERT (Major Advisor) + RESUBMIT: ends the cycle; same report id; reverted-specific uniqueness; cycle 2 fresh stages": t_revert_major_advisor_and_resubmit,
            "COMMITTEE LOCK: reassign/add/remove blocked (409, 'Progress Report') while active; released after MA-revert and after full approval": t_committee_lock,
            "THESIS REGRESSION: title fallback from PPW / explicit title required when blank or absent / snapshot never resyncs": t_thesis_title_fallback,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "reports": (await db.execute(select(func.count()).select_from(ProgressReport).where(ProgressReport.student_id.in_(uids)))).scalar_one(),
                "cycles": (await db.execute(select(func.count()).select_from(ProgressReportApprovalCycle))).scalar_one(),
                "stages": (await db.execute(select(func.count()).select_from(ProgressReportApprovalStage))).scalar_one(),
                "signatures": (await db.execute(select(func.count()).select_from(ProgressReportSignature))).scalar_one(),
                "proceedings": (await db.execute(select(func.count()).select_from(ProgressReportProceedings))).scalar_one(),
                "committees": (await db.execute(select(func.count()).select_from(AdvisoryCommittee).where(AdvisoryCommittee.research_title.like(f"{_LABEL}%")))).scalar_one(),
                "theses": (await db.execute(select(func.count()).select_from(Thesis).where(Thesis.student_id.in_(uids)))).scalar_one(),
                "ppw": (await db.execute(select(func.count()).select_from(Ppw).where(Ppw.student_id.in_(uids)))).scalar_one(),
                "college": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        upload_leftover = _UPLOAD_ROOT.exists()
        record("cleanup: zero ZZTEST_PROGRESS_REPORT rows remain in any touched table, no uploaded Proceedings files remain",
               not any(left.values()) and orphans == 0 and not upload_leftover, str(left) + f" upload_dir_exists={upload_leftover}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
