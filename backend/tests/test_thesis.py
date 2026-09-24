"""Standalone HTTP-level tests for Initial Thesis Management (BUSINESS_LOGIC.md
section AD).

Modelled directly on `tests/test_synopsis.py` / `tests/test_external_examiner.py`:
real FastAPI app via `httpx.ASGITransport`, real JWT sessions via
`create_access_token`/`RefreshToken` rows, a `record(name, ok, detail)` pass/fail
tracker, a `_run(name, fn)` wrapper so one failing test doesn't stop the run, and a
`_setup()`/`_teardown()` pair using a unique `_TAG = uuid.uuid4().hex[:6]` and
`zztest_thesis_...` prefix on every created row so cleanup is unambiguous. Real
students, committees, and single-holder role holders (INCHARGE_ACADEMIC_CELL,
DPGS, VICE_CHANCELLOR) are never touched; if a real holder of any of those already
exists, the run refuses to start. LIBRARIAN is NOT single-holder so no such check
is needed for it, but we do check no stray zztest_thesis_ users already exist.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_thesis
"""
import asyncio
import hashlib
import io
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints import thesis as thesis_module
from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.audit import AuditLog
from app.models.external_examiner import (
    ExternalExaminer, ExternalExaminerApprovalCycle, ExternalExaminerAssignment,
    ExternalExaminerProposal, ExternalExaminerSelection, ExternalExaminerSelectionResult,
)
from app.models.ppw import Ppw
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.thesis import (
    Thesis, ThesisApprovalCycle, ThesisApprovalStage, ThesisDocument,
    ThesisExternalEvaluation, ThesisSeminarCertificate, ThesisSeminarCertificateSignature, ThesisSignature,
)
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_thesis_"
_LABEL = "ZZTEST_THESIS"
_DEVICE = "ZZTEST-thesis"
_UPLOAD_ROOT = Path(settings.UPLOAD_DIR) / "thesis"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}           # user key -> id
TOK: dict[str, str] = {}               # token key -> access token
S: dict = {}                           # scratch
TH: dict[str, str] = {}                # student key -> thesis id
COMMITTEE: dict[str, uuid.UUID] = {}   # student key -> AdvisoryCommittee id
MEMBER: dict[str, uuid.UUID] = {}      # "student:facultykey" -> CommitteeMember id
EX: dict[str, uuid.UUID] = {}          # examiner key -> ExternalExaminer id
ASSIGN: dict[str, uuid.UUID] = {}      # "examinerkey:studentkey" -> ExternalExaminerAssignment id
THESIS_IDS: list[uuid.UUID] = []       # every thesis id created, for upload-dir cleanup


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


def _docx(body: str = "ZZTEST BODY") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0' encoding='UTF-8'?><Types/>")
        zf.writestr("word/document.xml",
                     f"<?xml version='1.0' encoding='UTF-8'?><w:document><w:body><w:p><w:r><w:t>{body}</w:t>"
                     "</w:r></w:p></w:body></w:document>")
    return buf.getvalue()


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None, title=None, designation=None, active=True):
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"TH{key.upper()}", title=title,
                  designation=designation, role=role, department_id=dept.id if dept else None,
                  program_id=program.id if program else None, college_id=college.id if college else None,
                  student_roll=roll, mobile="9876543210", gender="Male", is_active=active, is_verified=True)
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_committee(student_key: str, ma_key: str, extra=(), ma_accepted=True, ma_active=True) -> None:
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST thesis committee", status="members_pending")
        db.add(c)
        await db.flush()
        COMMITTEE[student_key] = c.id
        m = CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=ma_accepted)
        db.add(m)
        await db.flush()
        MEMBER[f"{student_key}:{ma_key}"] = m.id
        for fac_key, role in extra:
            em = CommitteeMember(committee_id=c.id, faculty_id=U[fac_key], role=role, accepted=True)
            db.add(em)
            await db.flush()
            MEMBER[f"{student_key}:{fac_key}"] = em.id
        await db.commit()
    if not ma_active:
        async with AsyncSessionLocal() as db:
            u = await db.get(User, U[ma_key])
            u.is_active = False
            await db.commit()


async def _mk_ppw(student_key: str, research_title) -> None:
    async with AsyncSessionLocal() as db:
        db.add(Ppw(student_id=U[student_key], status="draft", research_title=research_title))
        await db.commit()


async def _mk_examiner(key: str) -> None:
    """A reusable examiner identity + its AMS EXTERNAL_EXAMINER account, created
    directly (bypassing the VC-selection HTTP flow — same shortcut the task brief
    explicitly allows)."""
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}ex_{key}_{_TAG}@example.com", first_name="ZZTEST", last_name=f"EX{key.upper()}",
                  role=UserRole.EXTERNAL_EXAMINER, is_active=True, is_verified=True)
        db.add(u)
        await db.flush()
        U[f"ex_{key}"] = u.id
        ex = ExternalExaminer(email=f"{_PFX}ex_{key}_{_TAG}@example.com", user_id=u.id)
        db.add(ex)
        await db.flush()
        EX[key] = ex.id
        await db.commit()


_SEL_CACHE: dict[str, tuple] = {}     # student_key -> (selection_id, cycle_id) — one selection per student
_SLOT_COUNTER: dict[str, int] = {}    # student_key -> next proposal slot_number


async def _mk_assignment(examiner_key: str, student_key: str, active: bool = True) -> uuid.UUID:
    """Directly builds the External Examiner Selection chain (selection -> cycle
    -> proposal -> result -> assignment) needed to give `student_key` an ACTIVE
    assignment to the reusable `examiner_key` identity, without running the full
    HTTP flow of that other module. `ExternalExaminerSelection.student_id` is
    UNIQUE, so a student who needs MULTIPLE examiners (PhD) reuses the same
    selection/cycle across calls, one new proposal slot per examiner."""
    async with AsyncSessionLocal() as db:
        if student_key in _SEL_CACHE:
            sel_id, cyc_id = _SEL_CACHE[student_key]
        else:
            sel = ExternalExaminerSelection(student_id=U[student_key], degree_level="PG", status="approved")
            db.add(sel)
            await db.flush()
            cyc = ExternalExaminerApprovalCycle(selection_id=sel.id, cycle_number=1, status="approved")
            db.add(cyc)
            await db.flush()
            sel_id, cyc_id = sel.id, cyc.id
            _SEL_CACHE[student_key] = (sel_id, cyc_id)
        slot = _SLOT_COUNTER.get(student_key, 0) + 1
        _SLOT_COUNTER[student_key] = slot
        prop = ExternalExaminerProposal(
            cycle_id=cyc_id, slot_number=slot, examiner_id=EX[examiner_key],
            name_snapshot=f"ZZTEST Examiner {examiner_key}", specialization_snapshot="ZZTEST Spec",
            designation_snapshot="Professor", email_snapshot=f"{_PFX}ex_{examiner_key}_{_TAG}@example.com",
            phone_snapshot="9876500000", institution_snapshot="ZZTEST Institution",
        )
        db.add(prop)
        await db.flush()
        res = ExternalExaminerSelectionResult(cycle_id=cyc_id, proposal_id=prop.id)
        db.add(res)
        await db.flush()
        asg = ExternalExaminerAssignment(examiner_id=EX[examiner_key], student_id=U[student_key],
                                          selection_result_id=res.id, status="active" if active else "completed")
        db.add(asg)
        await db.commit()
        ASSIGN[f"{examiner_key}:{student_key}"] = asg.id
        return asg.id


# ── DB read helpers ──────────────────────────────────────────────────────────

async def _row(sid) -> Thesis:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Thesis).where(Thesis.id == uuid.UUID(sid)))).scalar_one()


async def _cycles(sid) -> list[ThesisApprovalCycle]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ThesisApprovalCycle).where(ThesisApprovalCycle.thesis_id == uuid.UUID(sid)).order_by(ThesisApprovalCycle.cycle_number)
        )).scalars().all())


async def _stages(cycle_id) -> list[ThesisApprovalStage]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ThesisApprovalStage).where(ThesisApprovalStage.cycle_id == cycle_id).order_by(ThesisApprovalStage.sequence)
        )).scalars().all())


async def _evaluations(sid) -> list[ThesisExternalEvaluation]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ThesisExternalEvaluation).where(ThesisExternalEvaluation.thesis_id == uuid.UUID(sid))
        )).scalars().all())


# ── HTTP action helpers ──────────────────────────────────────────────────────

async def _create_thesis(student_key: str, expect=201) -> httpx.Response:
    r = await _call("POST", "/thesis", TOK[student_key], json={})
    assert r.status_code == expect, (r.status_code, r.text)
    if expect == 201:
        TH[student_key] = r.json()["id"]
        THESIS_IDS.append(uuid.UUID(TH[student_key]))
    return r


async def _detail(sid, token) -> dict:
    r = await _call("GET", f"/thesis/{sid}", token)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _upload_doc(sid, doc_type, token, data=None, filename="doc.docx", ct=None) -> httpx.Response:
    data = data if data is not None else _docx()
    ct = ct if ct is not None else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return await _call("POST", f"/thesis/{sid}/documents/{doc_type}", token, files={"file": (filename, data, ct)})


async def _make_submittable(student_key: str, percent: float = 5.0, software: str = "ZZTEST Turnitin") -> None:
    sid = TH[student_key]
    tok = TOK[student_key]
    assert (await _upload_doc(sid, "thesis_file", tok)).status_code == 201
    assert (await _call("PATCH", f"/thesis/{sid}", tok, json={"plagiarism_student_percent": percent, "plagiarism_software_name": software})).status_code == 200
    assert (await _upload_doc(sid, "plagiarism_student_report", tok)).status_code == 201
    assert (await _call("PATCH", f"/thesis/{sid}", tok, json={"abstract": "ZZTEST abstract text."})).status_code == 200


async def _submit(student_key: str, expect=200) -> httpx.Response:
    r = await _call("POST", f"/thesis/{TH[student_key]}/submit", TOK[student_key])
    assert r.status_code == expect, (r.status_code, r.text)
    return r


# PG25 / Certificate I plumbing (CORRECTED this revision — PG25 is Major-Advisor-driven, not
# HOD-driven; see BUSINESS_LOGIC.md AD.15). `_PG25_COMMITTEE_SIGNERS` lists the REQUIRED
# non-Major-Advisor committee signers per student key (derived from `_mk_committee(...)` in
# `_setup()` below) — every other student's committee has ONLY the Major Advisor, so after
# MA Submit there is nothing left for the Advisory Committee to do and PG25 goes straight to
# `hod_pending`. `_MA_KEY`/`_PG25_HOD` cover the students whose Major Advisor/department HOD
# are NOT "ma1"/"hod1".
_PG25_COMMITTEE_SIGNERS = {"s_lock": ["mem_lock"]}
_PG25_HOD = {"s_deptb": "hod2"}
_MA_KEY = {"s_deptb": "ma_d2", "s_lock": "ma_lock"}


def _ma_key_for(student_key: str) -> str:
    return _MA_KEY.get(student_key, "ma1")


async def _complete_pg25(student_key: str) -> str:
    """Drives PG25 to fully "approved" for `student_key`'s Thesis: Major Advisor Satisfactory ->
    Submit (MA signs) -> every required (non-MA) Advisory Committee member signs -> the
    student's own-department HOD gives final approval. Idempotent: if PG25 is already approved
    (e.g. a more detailed dedicated test already drove it through), returns the existing
    certificate id without re-acting, so this helper is always safe to call unconditionally."""
    sid = TH[student_key]
    existing = await _detail(sid, TOK[student_key])
    if existing.get("pg25") and existing["pg25"]["status"] == "approved":
        return existing["pg25"]["id"]
    ma_key = _ma_key_for(student_key)
    hod_key = _PG25_HOD.get(student_key, "hod1")
    r = await _call("POST", f"/thesis/{sid}/pg25/satisfactory", TOK[ma_key])
    assert r.status_code == 201, r.text
    cert_id = r.json()["certificate_id"]
    r2 = await _call("POST", f"/thesis/{sid}/pg25/submit", TOK[ma_key])
    assert r2.status_code == 200, r2.text
    for fac_key in _PG25_COMMITTEE_SIGNERS.get(student_key, []):
        r3 = await _call("POST", f"/thesis/pg25/{cert_id}/sign", TOK[fac_key])
        assert r3.status_code == 200, r3.text
    r4 = await _call("POST", f"/thesis/pg25/{cert_id}/hod-approve", TOK[hod_key])
    assert r4.status_code == 200, r4.text
    return cert_id


async def _generate_cert_i(student_key: str, ma_key: str | None = None) -> httpx.Response:
    sid = TH[student_key]
    r = await _call("POST", f"/thesis/{sid}/certificate-i/generate", TOK[ma_key or _ma_key_for(student_key)])
    assert r.status_code == 201, r.text
    return r


async def _approve_ma(student_key: str) -> None:
    """Generates Certificate I (now mandatory, Section 20) then approves the Major Advisor
    stage — the drop-in replacement for a bare `_approve(sid, TOK[ma_key])` everywhere the
    Major Advisor stage is approved in this file."""
    ma_key = _ma_key_for(student_key)
    await _generate_cert_i(student_key, ma_key)
    await _approve(TH[student_key], TOK[ma_key])


async def _approve(sid, token, expect=200) -> httpx.Response:
    """Handles both signatory stages (OTP required) and the Incharge stage
    (workflow-only, GET otp returns 400) uniformly."""
    r = await _call("GET", f"/thesis/{sid}/approval/otp", token)
    if r.status_code == 400:
        r2 = await _call("POST", f"/thesis/{sid}/approval/approve", token, json={})
    else:
        assert r.status_code == 200, r.text
        r2 = await _call("POST", f"/thesis/{sid}/approval/approve", token, json={"otp": r.json()["dev_otp"]})
    assert r2.status_code == expect, (r2.status_code, r2.text)
    return r2


async def _revert(sid, token, remark, expect=200) -> httpx.Response:
    r = await _call("POST", f"/thesis/{sid}/approval/revert", token, json={"remark": remark})
    assert r.status_code == expect, (r.status_code, r.text)
    return r


async def _set_library_data(sid, token, percent=12.5) -> None:
    r = await _call("PATCH", f"/thesis/{sid}/library-plagiarism", token, json={"plagiarism_library_percent": percent})
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/thesis/{sid}/library-plagiarism/report", token, files={"file": ("lib.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 201, r.text


async def _advance_to(student_key: str, target_status: str, *, librarian_key="librarian1") -> None:
    """Drives a freshly-submitted Thesis through the chain up to (and including)
    reaching `target_status`, using the standard approvers."""
    sid = TH[student_key]
    order = ["major_advisor_pending", "hod_pending", "librarian_pending", "incharge_pending", "dpgs_pending",
              "external_examiner_pending", "dpgs_final_pending", "approved"]
    stage_tok = {
        "major_advisor_pending": TOK["ma1"], "hod_pending": TOK["hod1"], "librarian_pending": TOK[librarian_key],
        "incharge_pending": TOK["incharge"], "dpgs_pending": TOK["dpgs"], "dpgs_final_pending": TOK["dpgs"],
    }
    idx_target = order.index(target_status)
    for phase in order[:idx_target]:
        cur = (await _row(sid)).status
        if cur != phase:
            continue
        if phase == "librarian_pending":
            await _set_library_data(sid, stage_tok[phase])
        if phase == "major_advisor_pending":
            await _generate_cert_i(student_key)
        await _approve(sid, stage_tok[phase])


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    thesis_module.send_email = lambda to, subject, body: True
    settings.ENVIRONMENT = "development"

    async with AsyncSessionLocal() as db:
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS','VICE_CHANCELLOR')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS / Vice Chancellor holder exists (single-holder roles); refusing to run.")
            sys.exit(2)
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_thesis_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        S["PHD"] = (await db.execute(select(Program).where(Program.code == "PhD(F)"))).scalar_one()
        col = College(name=f"{_LABEL} College", code=f"ZZTH{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, d2, pg, phd, college = S["D1"], S["D2"], S["PG"], S["PHD"], S["COLLEGE"]

    # Faculty / approvers
    await _mk_user("ma1", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("ma2", UserRole.FACULTY, d1, title="Dr.", designation="Professor")   # not on any committee
    await _mk_user("ma_d2", UserRole.FACULTY, d2, title="Dr.", designation="Professor")
    await _mk_user("ma_lock", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("mem_lock", UserRole.FACULTY, d1, title="Dr.", designation="Assistant Professor")
    await _mk_user("hod1", UserRole.HOD, d1, title="Dr.", designation="Professor")
    await _mk_user("hod2", UserRole.HOD, d2, title="Dr.", designation="Professor")
    await _mk_user("librarian1", UserRole.LIBRARIAN, None, title="Mr.", designation="Librarian")
    await _mk_user("librarian2", UserRole.LIBRARIAN, None, title="Ms.", designation="Librarian")
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None, title="Dr.", designation="Professor")
    await _mk_user("dpgs", UserRole.DPGS, None, title="Dr.", designation="Director of PG Studies")

    # Students
    for k in ("s1", "s2", "s_val", "s_noma", "s_lib1", "s_lib2", "s_revert", "s_lock", "s_zero", "s_examx"):
        await _mk_user(k, UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZTH-{k}-{_TAG}")
    await _mk_user("s_phd", UserRole.STUDENT, d1, program=phd, college=college, roll=f"ZZTH-phd-{_TAG}")
    await _mk_user("s_deptb", UserRole.STUDENT, d2, program=pg, college=college, roll=f"ZZTH-deptb-{_TAG}")
    await _mk_user("s_nopp", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZTH-nopp-{_TAG}")
    await _mk_user("s_blank", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZTH-blank-{_TAG}")

    for k in ("s1", "s2", "s_val", "s_lib1", "s_lib2", "s_revert", "s_zero", "s_examx"):
        await _mk_committee(k, "ma1")
    await _mk_committee("s_phd", "ma1")
    await _mk_committee("s_deptb", "ma_d2")
    await _mk_committee("s_lock", "ma_lock", extra=[("mem_lock", "member_major")])
    await _mk_committee("s_noma", "ma1", ma_accepted=None)
    await _mk_committee("s_blank", "ma1")
    # s_nopp: no PPW, no committee needed for the creation test

    for k in ("s1", "s2", "s_val", "s_noma", "s_lib1", "s_lib2", "s_revert", "s_lock", "s_zero", "s_examx", "s_phd", "s_deptb"):
        await _mk_ppw(k, f"ZZTEST Research Title for {k}")
    await _mk_ppw("s_blank", "   ")   # blank/whitespace-only research_title -> creation must be rejected

    S["SA"] = await _session(admin_id)
    faculty_and_students = (
        "ma1", "ma2", "ma_d2", "ma_lock", "mem_lock", "hod1", "hod2", "librarian1", "librarian2", "incharge", "dpgs",
        "s1", "s2", "s_val", "s_noma", "s_lib1", "s_lib2", "s_revert", "s_lock", "s_zero", "s_examx", "s_phd",
        "s_deptb", "s_nopp", "s_blank",
    )
    for k in faculty_and_students:
        TOK[k] = await _session(U[k])

    # Examiners
    await _mk_examiner("shared")   # will be assigned to BOTH s1 AND s_examx (cross-student authorization test)
    await _mk_examiner("phd_a")
    await _mk_examiner("phd_b")
    TOK["ex_shared"] = await _session(U["ex_shared"])
    TOK["ex_phd_a"] = await _session(U["ex_phd_a"])
    TOK["ex_phd_b"] = await _session(U["ex_phd_b"])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        tids = (await db.execute(select(Thesis.id).where(Thesis.student_id.in_(uids)))).scalars().all()

        await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(uids)))
        await db.execute(delete(Thesis).where(Thesis.id.in_(tids or [uuid.uuid4()])))  # cascades documents/cycles/stages/signatures/evaluations

        examiner_ids = (await db.execute(select(ExternalExaminer.id).where(ExternalExaminer.email.like(f"{_PFX}%")))).scalars().all()
        await db.execute(delete(ExternalExaminerAssignment).where(ExternalExaminerAssignment.examiner_id.in_(examiner_ids or [uuid.uuid4()])))
        sel_ids = (await db.execute(select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.student_id.in_(uids)))).scalars().all()
        cyc_ids = (await db.execute(select(ExternalExaminerApprovalCycle.id).where(ExternalExaminerApprovalCycle.selection_id.in_(sel_ids or [uuid.uuid4()])))).scalars().all()
        await db.execute(delete(ExternalExaminerSelectionResult).where(ExternalExaminerSelectionResult.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerProposal).where(ExternalExaminerProposal.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerApprovalCycle).where(ExternalExaminerApprovalCycle.id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerSelection).where(ExternalExaminerSelection.id.in_(sel_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminer).where(ExternalExaminer.id.in_(examiner_ids or [uuid.uuid4()])))

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

    for tid in THESIS_IDS:
        shutil.rmtree(_UPLOAD_ROOT / str(tid), ignore_errors=True)
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
        for t in ("ams_theses", "ams_thesis_documents", "ams_thesis_approval_cycles",
                  "ams_thesis_approval_stages", "ams_thesis_signatures", "ams_thesis_external_evaluations"):
            assert (await db.execute(text("select count(*) from information_schema.tables where table_name=:t"), {"t": t})).scalar_one() == 1, t
        idx1 = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_thesis_initial_per_student'"))).scalar_one()
        assert "UNIQUE" in idx1 and "thesis_type" in idx1 and "'initial'" in idx1, idx1
        idx2 = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_thesis_one_active_cycle'"))).scalar_one()
        assert "UNIQUE" in idx2 and "active" in idx2, idx2
        enum_vals = (await db.execute(text(
            "select enumlabel from pg_enum e join pg_type t on t.oid=e.enumtypid where t.typname='ams_user_role'"
        ))).scalars().all()
        assert "LIBRARIAN" in enum_vals, enum_vals
        single_idx = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_user_role_assignment_single_holder'"))).scalar_one()
        assert "LIBRARIAN" not in single_idx, "LIBRARIAN must NOT be covered by the single-holder index: " + single_idx


async def t_librarian_multi_holder():
    # A second Librarian assignment must succeed (unlike DPGS/Incharge/VC).
    r = await _call("POST", f"/auth/users/{U['ma2']}/roles", S["SA"], json={"role": "librarian"})
    assert r.status_code in (200, 201), r.text
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.role == UserRole.LIBRARIAN))).scalar_one()
    assert n >= 2, n
    # a second DPGS assignment is still refused (existing single-holder rule, unaffected by this migration)
    r = await _call("POST", f"/auth/users/{U['ma2']}/roles", S["SA"], json={"role": "dpgs"})
    assert r.status_code == 409, r.text


async def t_create_thesis():
    # no PPW at all
    r = await _call("POST", "/thesis", TOK["s_nopp"], json={})
    assert r.status_code == 400 and "PPW" in r.json()["detail"], r.text
    # PPW exists but blank research_title
    r = await _call("POST", "/thesis", TOK["s_blank"], json={})
    assert r.status_code == 400 and "PPW" in r.json()["detail"], r.text
    # happy creation: title sourced exactly from PPW.research_title
    r = await _create_thesis("s1")
    assert r.json()["title"] == f"ZZTEST Research Title for s1"
    d = await _detail(TH["s1"], TOK["s1"])
    assert d["title"] == f"ZZTEST Research Title for s1" and d["status"] == "draft" and d["thesis_type"] == "initial"
    # a second creation attempt is rejected
    r = await _call("POST", "/thesis", TOK["s1"], json={})
    assert r.status_code == 409, r.text
    # no student_id (or any id) field is client-controlled: the request schema (now that
    # `title` is an accepted body field, for the PPW-title-fallback fix) declares extra="forbid",
    # so a tampered student_id is rejected outright by validation, never silently accepted.
    r = await _call("POST", "/thesis", TOK["s2"], json={"student_id": str(U["s1"])})
    assert r.status_code == 422, r.text
    r = await _call("POST", "/thesis", TOK["s2"], json={})
    assert r.status_code == 201, r.text
    TH["s2"] = r.json()["id"]
    THESIS_IDS.append(uuid.UUID(TH["s2"]))
    assert r.json()["title"] == "ZZTEST Research Title for s2", "the thesis created is always the CALLER's own, sourced from the caller's own PPW"
    # non-students refused
    assert (await _call("POST", "/thesis", TOK["ma1"], json={})).status_code == 403


async def t_student_isolation():
    # s2's thesis was already created (as a side effect of the id-tampering probe) in t_create_thesis
    sid_a, tok_b = TH["s1"], TOK["s2"]
    assert (await _call("GET", f"/thesis/{sid_a}", tok_b)).status_code == 404
    assert (await _call("PATCH", f"/thesis/{sid_a}", tok_b, json={"abstract": "hijack"})).status_code == 404
    assert (await _call("POST", f"/thesis/{sid_a}/submit", tok_b)).status_code == 404
    r = await _upload_doc(sid_a, "thesis_file", tok_b)
    assert r.status_code == 404
    # B did not actually mutate A's thesis
    assert (await _row(sid_a)).abstract is None
    # download: use a real document id belonging to A once uploaded by A
    assert (await _upload_doc(sid_a, "thesis_file", TOK["s1"])).status_code == 201
    doc_id = (await _detail(sid_a, TOK["s1"]))["documents"]["thesis_file"]["id"]
    assert (await _call("GET", f"/thesis/{sid_a}/documents/{doc_id}/download", tok_b)).status_code == 404


def _extract_pdf_text(data: bytes) -> str:
    """A generated PDF's text lives in compressed content streams — a raw byte/latin-1 substring
    search over the file will almost never find it. Use pypdf's real text extraction instead."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


def _pdf(body: bytes = b"dummy") -> bytes:
    # A minimal, genuinely-parseable one-page PDF (pypdf's `inspect_pdf` requires a real,
    # structurally-valid document — a bare "%PDF-" prefix is deliberately rejected elsewhere in
    # this test, so the "good" fixture here must actually parse).
    from pypdf import PdfWriter
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.write(buf)
    return buf.getvalue()


async def t_upload_validation():
    sid = TH["s1"]
    tok = TOK["s1"]
    good_docx = _docx()
    for doc_type in ("thesis_file", "plagiarism_student_report"):
        assert (await _upload_doc(sid, doc_type, tok, data=b"%PDF-1.4\n" + b"0" * 100, filename="x.pdf", ct="application/pdf")).status_code == 400
        assert (await _upload_doc(sid, doc_type, tok, data=b"plain text", filename="x.txt", ct="text/plain")).status_code == 400
        assert (await _upload_doc(sid, doc_type, tok, data=b"MZ" + b"\x00" * 200, filename="x.docx", ct="application/vnd.openxmlformats-officedocument.wordprocessingml.document")).status_code == 400, "renamed .exe"
        assert (await _upload_doc(sid, doc_type, tok, data=b"", filename="x.docx")).status_code == 400, "empty file"
        assert (await _upload_doc(sid, doc_type, tok, data=b"PK\x03\x04" + b"\x00" * 100, filename="x.docx")).status_code == 400, "corrupt zip"
        assert (await _upload_doc(sid, doc_type, tok, data=good_docx, filename="x.docx", ct="text/plain")).status_code == 400, "wrong declared content-type"
        r = await _upload_doc(sid, doc_type, tok, data=good_docx)
        assert r.status_code == 201, (doc_type, r.text)
    # PDF-format documents (Section 5, this revision): Payment Receipt, Proceedings of the
    # Thesis Seminar, Clearance, and the Student Declaration (Annexure-I upload path).
    good_pdf = _pdf()
    for doc_type in ("payment_receipt", "seminar_proceedings", "clearance", "declaration_annexure1"):
        assert (await _upload_doc(sid, doc_type, tok, data=good_docx, filename="x.docx", ct="application/vnd.openxmlformats-officedocument.wordprocessingml.document")).status_code == 400, "docx rejected for a PDF-only type"
        assert (await _upload_doc(sid, doc_type, tok, data=b"plain text", filename="x.pdf", ct="application/pdf")).status_code == 400, "renamed .txt"
        assert (await _upload_doc(sid, doc_type, tok, data=b"", filename="x.pdf", ct="application/pdf")).status_code == 400, "empty file"
        assert (await _upload_doc(sid, doc_type, tok, data=b"%PDF-1.4\n" + b"not a real pdf structure" * 5, filename="x.pdf", ct="application/pdf")).status_code == 400, "corrupt/unparseable pdf"
        assert (await _upload_doc(sid, doc_type, tok, data=good_pdf, filename="x.pdf", ct="text/plain")).status_code == 400, "wrong declared content-type"
        r = await _upload_doc(sid, doc_type, tok, data=good_pdf, filename="x.pdf", ct="application/pdf")
        assert r.status_code == 201, (doc_type, r.text)
    # Annexure-IV no longer exists as a document type at all — the generic upload endpoint
    # rejects it exactly like any other unknown type (Section 4/29: "attempts to use it are
    # rejected"), and PG25/Certificate I are system-generated only, never student-uploadable.
    for removed_or_generated in ("annexure_iv", "seminar_certificate_pg25", "certificate_i_pg27"):
        assert (await _upload_doc(sid, removed_or_generated, tok, data=good_pdf, filename="x.pdf", ct="application/pdf")).status_code == 404
    # library plagiarism report (as Librarian) — same validation. s_val was already
    # fully submitted (major_advisor_pending) by t_submission_validation; advance it
    # two stages so a Librarian is actually able to act.
    await _advance_ma_hod("s_val")
    lib_tok = TOK["librarian1"]
    lsid = TH["s_val"]
    assert (await _call("POST", f"/thesis/{lsid}/library-plagiarism/report", lib_tok, files={"file": ("x.pdf", b"%PDF-1.4\n" + b"0" * 100, "application/pdf")})).status_code == 400
    assert (await _call("POST", f"/thesis/{lsid}/library-plagiarism/report", lib_tok, files={"file": ("x.docx", b"", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})).status_code == 400
    assert (await _call("POST", f"/thesis/{lsid}/library-plagiarism/report", lib_tok, files={"file": ("x.docx", good_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})).status_code == 201


async def _advance_ma_hod(student_key: str) -> None:
    """Assumes the thesis is already submitted (major_advisor_pending) and drives
    it through the Major Advisor + HOD approvals to reach librarian_pending."""
    sid = TH[student_key]
    await _approve_ma(student_key)
    await _approve(sid, TOK["hod1"])
    assert (await _row(sid)).status == "librarian_pending"


async def t_submission_validation():
    await _create_thesis("s_val")
    sid = TH["s_val"]
    tok = TOK["s_val"]
    # PG25 must be fully approved before ANY other submission validation is even reachable
    # (Section 17 — the first-checked gate); completed once, up front, so the rest of this
    # test's per-field assertions below remain meaningful.
    r = await _submit("s_val", expect=400)
    assert "PG 25" in r.json()["detail"], r.text
    await _complete_pg25("s_val")
    # no thesis_file
    r = await _submit("s_val", expect=400)
    assert "Thesis File" in r.json()["detail"], r.text
    assert (await _upload_doc(sid, "thesis_file", tok)).status_code == 201
    # no plagiarism percent
    r = await _submit("s_val", expect=400)
    assert "plagiarism percentage" in r.json()["detail"], r.text
    assert (await _call("PATCH", f"/thesis/{sid}", tok, json={"plagiarism_student_percent": 3.5})).status_code == 200
    # no plagiarism software name
    r = await _submit("s_val", expect=400)
    assert "software name" in r.json()["detail"], r.text
    assert (await _call("PATCH", f"/thesis/{sid}", tok, json={"plagiarism_software_name": "ZZTEST Software"})).status_code == 200
    # no plagiarism report
    r = await _submit("s_val", expect=400)
    assert "plagiarism report" in r.json()["detail"], r.text
    assert (await _upload_doc(sid, "plagiarism_student_report", tok)).status_code == 201
    # no abstract
    r = await _submit("s_val", expect=400)
    assert "Abstract" in r.json()["detail"], r.text
    assert (await _call("PATCH", f"/thesis/{sid}", tok, json={"abstract": "ZZTEST abstract."})).status_code == 200
    # no accepted Major Advisor -> `ma1`'s committee membership for s_noma has `accepted=None`,
    # so `_my_ma_membership` (accepted == True only) never resolves it -> ma1 cannot even START
    # PG25 for this student (404, indistinguishable from "not your advisee") -> PG25 can never
    # be completed -> the submission gate (Section 11/17) blocks submission. The underlying
    # business rule (an unaccepted Major Advisor blocks progress) is still exercised, just
    # surfaced at the PG25 step instead of the old direct submission-time message.
    await _create_thesis("s_noma")
    await _make_submittable("s_noma")
    r = await _call("POST", f"/thesis/{TH['s_noma']}/pg25/satisfactory", TOK["ma1"])
    assert r.status_code == 404, r.text
    r = await _submit("s_noma", expect=400)
    assert "PG 25" in r.json()["detail"], r.text
    # a fully complete submission succeeds and seeds exactly 6 stages, in order
    r = await _submit("s_val", expect=200)
    assert r.json()["status"] == "major_advisor_pending"
    cyc = (await _cycles(sid))[-1]
    stages = await _stages(cyc.id)
    assert [s.stage_type for s in stages] == ["major_advisor", "hod", "librarian", "incharge_academic_cell", "dpgs", "dpgs_final"]
    assert [s.sequence for s in stages] == [1, 2, 3, 4, 5, 6]
    assert all(s.status == "pending" for s in stages)
    # cannot re-submit while under approval
    assert (await _submit("s_val", expect=400))


async def t_full_approval_chain():
    """s1: thesis_file+docs already uploaded by t_upload_validation; drive it
    through the full chain, PG (single examiner)."""
    sid = TH["s1"]
    await _mk_assignment("shared", "s1")
    if (await _row(sid)).status == "draft":
        await _make_submittable("s1")
        await _complete_pg25("s1")
        await _submit("s1")
    # major advisor
    assert (await _row(sid)).status == "major_advisor_pending"
    await _approve_ma("s1")
    assert (await _row(sid)).status == "hod_pending"
    # hod (same department)
    await _approve(sid, TOK["hod1"])
    assert (await _row(sid)).status == "librarian_pending"
    # librarian: approving BEFORE entering plagiarism data is rejected
    r = await _approve(sid, TOK["librarian1"], expect=400)
    assert "plagiarism" in r.json()["detail"].lower(), r.text
    # entering the data does NOT itself advance the stage
    await _call("PATCH", f"/thesis/{sid}/library-plagiarism", TOK["librarian1"], json={"plagiarism_library_percent": 8.0})
    assert (await _row(sid)).status == "librarian_pending"
    await _call("POST", f"/thesis/{sid}/library-plagiarism/report", TOK["librarian1"], files={"file": ("lib.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert (await _row(sid)).status == "librarian_pending"
    # now approve librarian stage
    await _approve(sid, TOK["librarian1"])
    assert (await _row(sid)).status == "incharge_pending"
    # incharge: NO OTP required — requesting one is rejected
    assert (await _call("GET", f"/thesis/{sid}/approval/otp", TOK["incharge"])).status_code == 400
    await _approve(sid, TOK["incharge"])
    assert (await _row(sid)).status == "dpgs_pending"
    # dpgs approves -> external_examiner_pending, creates evaluation rows for every ACTIVE assignment
    await _approve(sid, TOK["dpgs"])
    assert (await _row(sid)).status == "external_examiner_pending"
    evals = await _evaluations(sid)
    assert len(evals) == 1 and evals[0].status == "pending"
    S["s1_eval_id"] = str(evals[0].id)
    # examiner uploads report -> ALL submitted -> dpgs_final_pending
    r = await _call("POST", f"/thesis/{sid}/evaluations/{evals[0].id}/report", TOK["ex_shared"],
                     files={"file": ("report.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 201 and r.json()["status"] == "submitted", r.text
    assert (await _row(sid)).status == "dpgs_final_pending"
    # dpgs final approval (OTP) -> approved, evaluation approved
    d = await _approve(sid, TOK["dpgs"])
    assert d.json()["status"] == "approved"
    final = (await _evaluations(sid))[0]
    assert final.status == "approved" and final.dpgs_approved_at is not None


async def t_dpgs_zero_assignments_rejected():
    await _create_thesis("s_zero")
    await _make_submittable("s_zero")
    await _complete_pg25("s_zero")
    await _submit("s_zero")
    await _advance_to("s_zero", "dpgs_pending")
    r = await _approve(TH["s_zero"], TOK["dpgs"], expect=400)
    assert "External Examiner" in r.json()["detail"], r.text
    assert (await _row(TH["s_zero"])).status == "dpgs_pending"


async def t_multi_examiner_phd():
    await _create_thesis("s_phd")
    await _mk_assignment("phd_a", "s_phd")
    await _mk_assignment("phd_b", "s_phd")
    await _make_submittable("s_phd")
    await _complete_pg25("s_phd")
    await _submit("s_phd")
    await _advance_to("s_phd", "dpgs_pending")
    await _approve(TH["s_phd"], TOK["dpgs"])
    sid = TH["s_phd"]
    assert (await _row(sid)).status == "external_examiner_pending"
    evals = await _evaluations(sid)
    assert len(evals) == 2
    ev_a = next(e for e in evals if e.assignment_id == ASSIGN["phd_a:s_phd"])
    ev_b = next(e for e in evals if e.assignment_id == ASSIGN["phd_b:s_phd"])
    # only one examiner submits: status stays external_examiner_pending
    r = await _call("POST", f"/thesis/{sid}/evaluations/{ev_a.id}/report", TOK["ex_phd_a"],
                     files={"file": ("r.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 201, r.text
    assert (await _row(sid)).status == "external_examiner_pending", "must wait for BOTH examiners"
    # cannot submit twice
    r = await _call("POST", f"/thesis/{sid}/evaluations/{ev_a.id}/report", TOK["ex_phd_a"],
                     files={"file": ("r2.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 400, r.text
    # second examiner submits -> now dpgs_final_pending
    r = await _call("POST", f"/thesis/{sid}/evaluations/{ev_b.id}/report", TOK["ex_phd_b"],
                     files={"file": ("r.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 201, r.text
    assert (await _row(sid)).status == "dpgs_final_pending"
    await _approve(sid, TOK["dpgs"])
    assert (await _row(sid)).status == "approved"
    evals = await _evaluations(sid)
    assert all(e.status == "approved" for e in evals)


_STAGE_ORDER = ["major_advisor", "hod", "librarian", "incharge_academic_cell", "dpgs", "dpgs_final"]


async def t_revert_matrix():
    plan = [
        ("major_advisor_pending", "ma1", "major_advisor", []),
        ("hod_pending", "hod1", "hod", ["ma1"]),
        ("librarian_pending", "librarian1", "librarian", ["ma1", "hod1"]),
        ("incharge_pending", "incharge", "incharge_academic_cell", ["ma1", "hod1", "librarian1"]),
        ("dpgs_pending", "dpgs", "dpgs", ["ma1", "hod1", "librarian1", "incharge"]),
    ]
    for n, (phase, reverter_key, stage_type, before) in enumerate(plan, start=1):
        if n == 1:
            await _create_thesis("s_revert")
            await _complete_pg25("s_revert")   # PG25 happens ONCE, independent of resubmission cycles
        await _make_submittable("s_revert")
        await _submit("s_revert")
        sid = TH["s_revert"]
        d = await _detail(sid, TOK["s_revert"])
        assert d["status"] == "major_advisor_pending"
        for who in before:
            if who == "librarian1":
                await _set_library_data(sid, TOK["librarian1"])
            if who == "ma1":
                await _generate_cert_i("s_revert", "ma1")
            await _approve(sid, TOK[who])
        assert (await _row(sid)).status == phase
        # a remark is required
        assert (await _call("POST", f"/thesis/{sid}/approval/revert", TOK[reverter_key], json={"remark": ""})).status_code in (400, 422)
        remark = f"ZZTEST revert remark {n} by {stage_type}"
        await _revert(sid, TOK[reverter_key], remark)
        d = await _detail(sid, TOK["s_revert"])
        assert d["status"] == "reverted" and d["can_edit"] is True
        ri = d["revert_info"]
        assert ri["remark"] == remark and ri["role"] == thesis_module._STAGE_ROLE_LABELS[stage_type]
        cyc = (await _cycles(sid))[-1]
        stages = await _stages(cyc.id)
        reverted_stage = next(s for s in stages if s.status == "reverted")
        assert reverted_stage.stage_type == stage_type
        others_after = [s for s in stages if s.id != reverted_stage.id and _STAGE_ORDER.index(s.stage_type) > _STAGE_ORDER.index(stage_type)]
        assert all(s.status == "cancelled" for s in others_after), [(s.stage_type, s.status) for s in others_after]
        # nobody can approve a reverted thesis; the reverter cannot revert twice
        assert (await _call("POST", f"/thesis/{sid}/approval/approve", TOK[reverter_key], json={})).status_code == 403


async def t_dpgs_final_revert_rejected():
    """No revert path exists for the dpgs_final stage."""
    key = "s_zero"  # already exists and is stuck at dpgs_pending (zero assignments) from an earlier test
    sid = TH[key]
    assert (await _row(sid)).status == "dpgs_pending"
    await _mk_assignment("shared", key)
    await _approve(sid, TOK["dpgs"])
    assert (await _row(sid)).status == "external_examiner_pending"
    ev = (await _evaluations(sid))[0]
    r = await _call("POST", f"/thesis/{sid}/evaluations/{ev.id}/report", TOK["ex_shared"],
                     files={"file": ("r.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 201, r.text
    assert (await _row(sid)).status == "dpgs_final_pending"
    r = await _call("POST", f"/thesis/{sid}/approval/revert", TOK["dpgs"], json={"remark": "ZZTEST try to revert final"})
    assert r.status_code == 400, r.text
    assert (await _row(sid)).status == "dpgs_final_pending", "revert must not have taken effect"
    await _approve(sid, TOK["dpgs"])
    assert (await _row(sid)).status == "approved"


async def t_librarian_mechanics():
    # multiple different librarians can each act on different pending-librarian theses
    await _create_thesis("s_lib1")
    await _create_thesis("s_lib2")
    for k in ("s_lib1", "s_lib2"):
        await _make_submittable(k)
        await _complete_pg25(k)
        await _submit(k)
        await _approve_ma(k)
        await _approve(TH[k], TOK["hod1"])
        assert (await _row(TH[k])).status == "librarian_pending"
    await _set_library_data(TH["s_lib1"], TOK["librarian1"])
    await _approve(TH["s_lib1"], TOK["librarian1"])
    assert (await _row(TH["s_lib1"])).status == "incharge_pending"
    await _set_library_data(TH["s_lib2"], TOK["librarian2"])
    await _approve(TH["s_lib2"], TOK["librarian2"])
    assert (await _row(TH["s_lib2"])).status == "incharge_pending"

    # a non-Librarian role attempting Librarian-only endpoints is rejected
    probe_sid = TH["s_zero"]
    for tok in (TOK["ma1"], TOK["hod1"], TOK["incharge"], TOK["dpgs"], TOK["s1"]):
        assert (await _call("PATCH", f"/thesis/{probe_sid}/library-plagiarism", tok, json={"plagiarism_library_percent": 1})).status_code == 403
        assert (await _call("POST", f"/thesis/{probe_sid}/library-plagiarism/report", tok, files={"file": ("x.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})).status_code == 403
    # a Librarian acting when the thesis is NOT at the librarian stage is rejected (400)
    assert (await _call("PATCH", f"/thesis/{probe_sid}/library-plagiarism", TOK["librarian1"], json={"plagiarism_library_percent": 1})).status_code == 400


async def t_confidentiality_library_report():
    sid = TH["s_lib1"]
    d = await _detail(sid, TOK["s_lib1"])
    assert d["plagiarism_library_percent"] == 12.5
    doc_id = d["documents"]["plagiarism_library_report"]["id"]
    assert (await _call("GET", f"/thesis/{sid}/documents/{doc_id}/download", TOK["s_lib1"])).status_code == 404
    for tok in (TOK["ma1"], TOK["hod1"], TOK["librarian1"], TOK["incharge"], TOK["dpgs"]):
        r = await _call("GET", f"/thesis/{sid}/documents/{doc_id}/download", tok)
        assert r.status_code == 200, r.text


async def t_confidentiality_external_report():
    sid = TH["s1"]  # already approved with one evaluation
    d = await _detail(sid, TOK["s1"])
    assert "external_report" in d and d["external_report"] == [{
        "id": S["s1_eval_id"], "name": d["external_report"][0]["name"], "status": "approved",
        "report_available": True, "submitted_at": d["external_report"][0]["submitted_at"],
        "dpgs_approved_at": d["external_report"][0]["dpgs_approved_at"],
    }]
    for tok in (TOK["ma1"], TOK["hod1"], TOK["librarian1"], TOK["incharge"]):
        body = await _detail(sid, tok)
        assert "external_report" not in body, (tok, body.keys())
        assert body["external_evaluation_completed"] is True
    for tok in (TOK["dpgs"], S["SA"]):
        body = await _detail(sid, tok)
        assert "external_report" in body and len(body["external_report"]) == 1

    # before approval: key genuinely ABSENT (not null/empty) for student and non-DPGS approvers
    await _create_thesis("s_examx")
    await _mk_assignment("shared", "s_examx")
    await _make_submittable("s_examx")
    assert (await _upload_doc(TH["s_examx"], "seminar_proceedings", TOK["s_examx"], data=_pdf(), filename="p.pdf", ct="application/pdf")).status_code == 201
    await _complete_pg25("s_examx")
    await _submit("s_examx")
    await _advance_to("s_examx", "dpgs_pending")
    await _approve(TH["s_examx"], TOK["dpgs"])
    sid2 = TH["s_examx"]
    d2 = await _detail(sid2, TOK["s_examx"])
    assert "external_report" not in d2 and d2["external_evaluation_completed"] is False
    for tok in (TOK["ma1"], TOK["hod1"], TOK["librarian1"], TOK["incharge"]):
        body = await _detail(sid2, tok)
        assert "external_report" not in body
    # DPGS always sees the full list, even pending
    body = await _detail(sid2, TOK["dpgs"])
    assert "external_report" in body and len(body["external_report"]) == 1 and body["external_report"][0]["status"] == "pending"


async def t_examiner_access_and_cross_student_authorization():
    """`shared` examiner has a real, ACTIVE assignment for BOTH s1 (approved) and
    s_examx (external_examiner_pending). Verify view/download scope, own-report
    upload, and that a mismatched thesis/evaluation pairing is blocked even though
    the examiner genuinely has a legitimate assignment on both sides."""
    sid_x = TH["s_examx"]
    ev_x = (await _evaluations(sid_x))[0]
    # can view
    d = await _call("GET", f"/thesis/{sid_x}", TOK["ex_shared"])
    assert d.status_code == 200, d.text
    body = d.json()
    assert set(body["documents"].keys()) == {"thesis_file", "plagiarism_student_report", "plagiarism_library_report"}
    # download allowed docs; the 4 administrative-only document types are not even
    # listed in an examiner's `documents` dict (asserted above) — confirm attempting
    # to download one directly by its real id (resolved via an authorized viewer) 404s
    tf_id = (await _detail(sid_x, TOK["dpgs"]))["documents"]["thesis_file"]["id"]
    assert (await _call("GET", f"/thesis/{sid_x}/documents/{tf_id}/download", TOK["ex_shared"])).status_code == 200
    seminar_id = (await _detail(sid_x, TOK["dpgs"]))["documents"]["seminar_proceedings"]["id"]
    assert (await _call("GET", f"/thesis/{sid_x}/documents/{seminar_id}/download", TOK["ex_shared"])).status_code == 404
    # own-report upload already exercised in t_full_approval_chain / t_multi_examiner_phd

    # cross-student block: s1's evaluation id against s_examx's thesis id, and vice versa
    sid_a = TH["s1"]
    ev_a_id = S["s1_eval_id"]
    r = await _call("POST", f"/thesis/{sid_x}/evaluations/{ev_a_id}/report", TOK["ex_shared"],
                     files={"file": ("x.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 404, r.text
    r = await _call("POST", f"/thesis/{sid_a}/evaluations/{ev_x.id}/report", TOK["ex_shared"],
                     files={"file": ("x.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 404, r.text
    # an examiner with NO assignment for a thesis is blocked from viewing/uploading entirely
    r = await _call("GET", f"/thesis/{sid_a}", TOK["ex_phd_a"])
    assert r.status_code == 404, r.text
    r = await _call("POST", f"/thesis/{sid_a}/evaluations/{ev_a_id}/report", TOK["ex_phd_a"],
                     files={"file": ("x.docx", _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 404, r.text
    # uploading before external_examiner_pending, and double-submission, are already
    # exercised end-to-end in t_multi_examiner_phd.


async def t_department_isolation():
    await _create_thesis("s_deptb")
    await _make_submittable("s_deptb")
    await _complete_pg25("s_deptb")
    await _submit("s_deptb")
    await _approve_ma("s_deptb")
    sid = TH["s_deptb"]
    assert (await _row(sid)).status == "hod_pending"
    # HOD-A (D1) cannot act on / see a D2 student's thesis
    assert (await _call("GET", f"/thesis/{sid}/approval/otp", TOK["hod1"])).status_code == 403
    assert (await _call("POST", f"/thesis/{sid}/approval/approve", TOK["hod1"], json={"otp": "1"})).status_code == 403
    inbox = (await _call("GET", "/thesis/pending-approvals", TOK["hod1"])).json()
    assert sid not in [r["thesis_id"] for r in inbox]
    # HOD-B (D2) can
    inbox2 = (await _call("GET", "/thesis/pending-approvals", TOK["hod2"])).json()
    assert sid in [r["thesis_id"] for r in inbox2]
    await _approve(sid, TOK["hod2"])
    assert (await _row(sid)).status == "librarian_pending"
    # a Major Advisor not on the committee cannot act
    assert (await _call("POST", f"/thesis/{sid}/approval/approve", TOK["ma2"], json={"otp": "1"})).status_code == 403


async def t_idor_and_tampering():
    sid = TH["s1"]
    other_sid = TH["s_deptb"]
    # foreign document_id
    doc_id_other = (await _detail(other_sid, TOK["dpgs"]))["documents"]["thesis_file"]["id"]
    assert (await _call("GET", f"/thesis/{sid}/documents/{doc_id_other}/download", TOK["dpgs"])).status_code == 404
    # foreign thesis_id
    assert (await _call("GET", f"/thesis/{uuid.uuid4()}", TOK["dpgs"])).status_code == 404
    assert (await _call("POST", f"/thesis/{uuid.uuid4()}/approval/approve", TOK["dpgs"], json={})).status_code in (403, 404)
    # query-parameter tampering has zero effect
    base = await _detail(sid, TOK["dpgs"])
    r = await _call("GET", f"/thesis/{sid}?debug=1&include_report=true&student_id={U['s_deptb']}", TOK["dpgs"])
    assert r.status_code == 200 and r.json() == base
    # body role injection rejected via extra=forbid
    assert (await _call("PATCH", f"/thesis/{sid}", TOK["s1"], json={"abstract": "x", "role": "dpgs"})).status_code == 422
    assert (await _call("PATCH", f"/thesis/{TH['s_lib1']}/library-plagiarism", TOK["librarian1"], json={"plagiarism_library_percent": 1, "role": "dpgs"})).status_code == 422
    assert (await _call("POST", f"/thesis/{sid}/approval/approve", TOK["dpgs"], json={"otp": "1", "role": "dpgs"})).status_code == 422
    assert (await _call("POST", f"/thesis/{sid}/approval/revert", TOK["dpgs"], json={"remark": "x", "role": "dpgs"})).status_code == 422


async def t_major_advisor_committee_lock():
    committee_id = COMMITTEE["s_lock"]
    # s_lock's Thesis was already created by `t_pg25_committee_multi_signer_and_department_isolation`
    # (which needed it to exercise the multi-signer PG25 committee flow); `_complete_pg25` is
    # idempotent and simply confirms the already-approved state here.
    await _make_submittable("s_lock")
    await _complete_pg25("s_lock")
    await _submit("s_lock")
    sid = TH["s_lock"]
    lock_msg_fragment = "Initial Thesis"

    # unauthorized callers still get their normal 403 first — a role-gated dependency
    # (never HOD/Super Admin) rejects before the lock check is ever reached
    r = await _call("POST", f"/research/committees/{committee_id}/reassign-major-advisor", TOK["ma2"], json={"major_advisor_id": str(U["mem_lock"])})
    assert r.status_code == 403 and "Thesis" not in r.text, r.text

    r = await _call("POST", f"/research/committees/{committee_id}/reassign-major-advisor", S["SA"], json={"major_advisor_id": str(U["mem_lock"])})
    assert r.status_code == 409 and lock_msg_fragment in r.json()["detail"], r.text
    r = await _call("POST", f"/research/committees/{committee_id}/members", S["SA"], json={"faculty_id": str(U["ma2"]), "role": "member_minor"})
    assert r.status_code == 409 and lock_msg_fragment in r.json()["detail"], r.text
    r = await _call("DELETE", f"/research/committees/{committee_id}/members/{MEMBER['s_lock:mem_lock']}", S["SA"])
    assert r.status_code == 409 and lock_msg_fragment in r.json()["detail"], r.text

    # release the lock via revert -> lock is gone
    await _approve_ma("s_lock")
    await _revert(sid, TOK["hod1"], "ZZTEST release the MA lock")
    assert (await _cycles(sid))[-1].status == "reverted"
    r = await _call("DELETE", f"/research/committees/{committee_id}/members/{MEMBER['s_lock:mem_lock']}", S["SA"])
    assert r.status_code == 204, r.text

    # the lock also releases once a (separate) thesis reaches full approval
    sid_done = TH["s_zero"]
    assert (await _row(sid_done)).status == "approved"
    assert (await _cycles(sid_done))[-1].status != "active"


async def t_declaration_generate_and_upload():
    """Section 6/7, 29: Generate produces a real PDF with dynamic student/advisor data and no
    AAU branding; Generate and Upload occupy the SAME `declaration_annexure1` slot (versions of
    one document, not two separate concepts)."""
    sid = TH["s2"]
    tok = TOK["s2"]
    r = await _call("POST", f"/thesis/{sid}/declaration/generate", tok)
    assert r.status_code == 201, r.text
    v1 = r.json()["version"]
    doc_id = (await _detail(sid, tok))["documents"]["declaration_annexure1"]["id"]
    dl = await _call("GET", f"/thesis/{sid}/documents/{doc_id}/download", tok)
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"
    body_text = _extract_pdf_text(dl.content)
    assert "ZZTEST" in body_text.upper() and "S2" in body_text.upper(), "generated PDF should embed the real student name, not a hard-coded example"
    assert "assam agricultural university" not in body_text.lower(), "no AAU branding may leak into a generated AVFU document"
    assert "assam veterinary" in body_text.lower(), "AVFU's own established university name must appear"
    # Upload occupies the SAME logical slot as a new version
    r2 = await _upload_doc(sid, "declaration_annexure1", tok, data=_pdf(), filename="mine.pdf", ct="application/pdf")
    assert r2.status_code == 201 and r2.json()["version"] == v1 + 1, "Upload must version the SAME declaration_annexure1 slot Generate uses"
    # non-owner cannot generate on another student's thesis
    assert (await _call("POST", f"/thesis/{TH['s1']}/declaration/generate", TOK["s2"])).status_code == 404


async def t_pg25_ma_driven_lifecycle_and_idor():
    """PG25 CORRECTED: Major-Advisor-driven, using s2 (dept d1, Major Advisor ma1, a
    single-member committee so, after Submit, there is nothing for the Advisory Committee to
    do and the certificate goes straight to `hod_pending`) to exercise the full Unsatisfactory ->
    Satisfactory -> Submit -> HOD-revert -> MA-regenerate -> HOD-approve lifecycle end to end,
    plus MA/HOD authorization and IDOR."""
    sid = TH["s2"]
    # wrong faculty (not this student's Major Advisor) cannot act at all
    assert (await _call("POST", f"/thesis/{sid}/pg25/satisfactory", TOK["ma2"])).status_code == 404
    assert (await _call("POST", f"/thesis/{sid}/pg25/unsatisfactory", TOK["ma2"])).status_code == 404

    # attempt 1: the Major Advisor marks the seminar Unsatisfactory — a REAL state change, no
    # PG25 document, no approval workflow, submission stays blocked
    r = await _call("POST", f"/thesis/{sid}/pg25/unsatisfactory", TOK["ma1"])
    assert r.status_code == 201, r.text
    d = await _detail(sid, TOK["s2"])
    assert d["pg25"]["status"] == "unsatisfactory" and d["pg25"]["attempt_number"] == 1
    assert d["documents"]["seminar_certificate_pg25"] is None, "Unsatisfactory must never generate a PG25 document"
    # cannot Submit / regenerate against an unsatisfactory attempt
    assert (await _call("POST", f"/thesis/{sid}/pg25/submit", TOK["ma1"])).status_code == 400
    assert (await _call("POST", f"/thesis/{sid}/pg25/regenerate", TOK["ma1"])).status_code == 400

    # attempt 2 (a fresh offline seminar): the Major Advisor marks it Satisfactory
    r = await _call("POST", f"/thesis/{sid}/pg25/satisfactory", TOK["ma1"])
    assert r.status_code == 201, r.text
    cert_id = r.json()["certificate_id"]
    d = await _detail(sid, TOK["s2"])
    assert d["pg25"]["attempt_number"] == 2 and d["pg25"]["version_number"] == 1 and d["pg25"]["status"] == "generated"
    # a duplicate Satisfactory click while one is already in flight is rejected (idempotency)
    assert (await _call("POST", f"/thesis/{sid}/pg25/satisfactory", TOK["ma1"])).status_code == 409
    assert (await _call("POST", f"/thesis/{sid}/pg25/unsatisfactory", TOK["ma1"])).status_code == 409
    # not yet MA-signed -> cannot be approved by anyone yet; wrong MA cannot submit
    assert (await _call("POST", f"/thesis/{sid}/pg25/submit", TOK["ma2"])).status_code == 404

    # the real Major Advisor submits: MA signature applied, routed toward the (empty) committee
    # and straight to hod_pending since s2's committee has no OTHER accepted members
    r = await _call("POST", f"/thesis/{sid}/pg25/submit", TOK["ma1"])
    assert r.status_code == 200 and r.json()["status"] == "hod_pending", r.text
    d = await _detail(sid, TOK["s2"])
    assert d["pg25"]["ma_signed_at"] is not None and d["pg25"]["status"] == "hod_pending"
    assert d["documents"]["seminar_certificate_pg25"] is None, "still not visible to the student before HOD approval"

    # wrong-department HOD cannot approve/revert a d1 student's certificate
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/hod-approve", TOK["hod2"])).status_code == 404
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/revert", TOK["hod2"], json={"remark": "x"})).status_code == 404
    # an unrelated faculty member (not a committee signer, not the MA) is blocked everywhere
    assert (await _call("GET", f"/thesis/pg25/{cert_id}", TOK["ma2"])).status_code == 404
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/sign", TOK["ma2"])).status_code == 404
    # a revert remark is mandatory
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/revert", TOK["hod1"], json={"remark": ""})).status_code in (400, 422)

    # own-department HOD reverts (real effect this time) -> terminal for this version
    r = await _call("POST", f"/thesis/pg25/{cert_id}/revert", TOK["hod1"], json={"remark": "ZZTEST please clarify the seminar date"})
    assert r.status_code == 200 and r.json()["status"] == "reverted", r.text
    # a reverted certificate cannot be approved or re-reverted
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/hod-approve", TOK["hod1"])).status_code == 400
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/revert", TOK["hod1"], json={"remark": "again"})).status_code == 404
    # wrong MA cannot regenerate; the real MA can
    assert (await _call("POST", f"/thesis/{sid}/pg25/regenerate", TOK["ma2"])).status_code == 404
    r = await _call("POST", f"/thesis/{sid}/pg25/regenerate", TOK["ma1"])
    assert r.status_code == 201, r.text
    cert_id_v2 = r.json()["certificate_id"]
    assert cert_id_v2 != cert_id, "regeneration must create a NEW row, never resurrect the reverted one"
    d = await _detail(sid, TOK["s2"])
    assert d["pg25"]["attempt_number"] == 2 and d["pg25"]["version_number"] == 2 and d["pg25"]["status"] == "generated"
    # the OLD (reverted) certificate is preserved as history, still independently readable
    old = await _call("GET", f"/thesis/pg25/{cert_id}", TOK["ma1"])
    assert old.status_code == 200 and old.json()["status"] == "reverted" and old.json()["revert_remark"]

    # MA submits the regenerated version -> hod_pending again -> HOD (own dept) approves for real
    assert (await _call("POST", f"/thesis/{sid}/pg25/submit", TOK["ma1"])).status_code == 200
    r = await _call("POST", f"/thesis/pg25/{cert_id_v2}/hod-approve", TOK["hod1"])
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    d = await _detail(sid, TOK["s2"])
    assert d["pg25"]["status"] == "approved" and d["documents"]["seminar_certificate_pg25"] is not None
    doc_id = d["documents"]["seminar_certificate_pg25"]["id"]
    assert (await _call("GET", f"/thesis/{sid}/documents/{doc_id}/download", TOK["s2"])).status_code == 200
    # re-approving / re-reverting an already-approved certificate is refused
    assert (await _call("POST", f"/thesis/pg25/{cert_id_v2}/hod-approve", TOK["hod1"])).status_code == 400
    assert (await _call("POST", f"/thesis/pg25/{cert_id_v2}/revert", TOK["hod1"], json={"remark": "x"})).status_code == 404
    # a fresh attempt can no longer be started once approved
    assert (await _call("POST", f"/thesis/{sid}/pg25/satisfactory", TOK["ma1"])).status_code == 409

    # HOD's inbox no longer lists s2 (nothing left pending their approval)
    hod_rows = (await _call("GET", "/thesis/pg25/hod", TOK["hod1"])).json()
    assert not any(r["thesis_id"] == sid for r in hod_rows)
    # student isolation: another student cannot see/act on s2's certificate by id
    assert (await _call("GET", f"/thesis/pg25/{cert_id_v2}", TOK["s1"])).status_code in (401, 403, 404)
    assert (await _call("POST", f"/thesis/pg25/{cert_id_v2}/sign", TOK["s2"])).status_code in (401, 403, 404)


async def t_pg25_committee_multi_signer_and_department_isolation():
    """PG25 CORRECTED, multi-signer committee (s_lock: Major Advisor ma_lock + committee member
    mem_lock) — exercises real committee sign/IDOR/status-transition/Home-Pending behavior that
    a solo-Major-Advisor committee (s2, above) cannot. s_lock's Thesis is created here (needed
    before `t_major_advisor_committee_lock` runs later and reuses it); `_complete_pg25` is
    idempotent, so that later test's own call simply confirms the already-approved state."""
    await _create_thesis("s_lock")
    sid = TH["s_lock"]
    r = await _call("POST", f"/thesis/{sid}/pg25/satisfactory", TOK["ma_lock"])
    assert r.status_code == 201, r.text
    cert_id = r.json()["certificate_id"]
    assert (await _call("POST", f"/thesis/{sid}/pg25/submit", TOK["ma_lock"])).status_code == 200
    d = await _detail(sid, TOK["s_lock"])
    assert d["pg25"]["status"] == "committee_pending" and d["pg25"]["signatures_required"] == 1

    # an unrelated faculty member cannot sign, view, or revert
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/sign", TOK["ma2"])).status_code == 404
    assert (await _call("GET", f"/thesis/pg25/{cert_id}", TOK["ma2"])).status_code == 404
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/revert", TOK["ma2"], json={"remark": "x"})).status_code == 404
    # the Major Advisor (already signed via Submit) has no separate committee signature row
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/sign", TOK["ma_lock"])).status_code == 404
    # appears in mem_lock's Pending, not yet in Home
    pending = (await _call("GET", "/thesis/pg25/mine/pending", TOK["mem_lock"])).json()
    assert any(r["certificate_id"] == cert_id for r in pending)
    home = (await _call("GET", "/thesis/pg25/mine/home", TOK["mem_lock"])).json()
    assert not any(r["certificate_id"] == cert_id for r in home)

    # the real committee member signs -> status moves to hod_pending (only one required signer)
    r = await _call("POST", f"/thesis/pg25/{cert_id}/sign", TOK["mem_lock"])
    assert r.status_code == 200 and r.json()["status"] == "hod_pending", r.text
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/sign", TOK["mem_lock"])).status_code == 400, "already approved"
    # wrong-department HOD blocked; own-department HOD approves
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/hod-approve", TOK["hod2"])).status_code == 404
    assert (await _call("POST", f"/thesis/pg25/{cert_id}/hod-approve", TOK["hod1"])).status_code == 200
    home = (await _call("GET", "/thesis/pg25/mine/home", TOK["mem_lock"])).json()
    assert any(r["certificate_id"] == cert_id for r in home)


async def t_certificate_i_authorization_and_regeneration():
    """Section 19/20/21/25/29: only the real Major Advisor may generate; wrong stage/wrong
    person rejected; approval blocked until generated; and — using s_revert's already-exercised
    revert/resubmit history from `t_revert_matrix` — an OBSOLETE Certificate I from an earlier,
    reverted cycle does NOT satisfy the newest cycle's requirement."""
    # s2: PG25 was already approved by t_pg25_ma_driven_lifecycle_and_idor; complete the
    # remaining submission preconditions and submit to reach major_advisor_pending.
    sid = TH["s2"]
    await _make_submittable("s2")
    await _submit("s2")
    assert (await _row(sid)).status == "major_advisor_pending"
    # wrong person (not this student's Major Advisor) cannot generate
    assert (await _call("POST", f"/thesis/{sid}/certificate-i/generate", TOK["ma2"])).status_code == 403
    # approval is blocked before Certificate I exists for this cycle
    r = await _approve(sid, TOK["ma1"], expect=400)
    assert "Certificate I" in r.json()["detail"], r.text
    # the real Major Advisor generates -> a real PDF, auto-signed, with dynamic (not hard-coded) data
    r = await _call("POST", f"/thesis/{sid}/certificate-i/generate", TOK["ma1"])
    assert r.status_code == 201, r.text
    doc_id = r.json()["document_id"]
    dl = await _call("GET", f"/thesis/{sid}/documents/{doc_id}/download", TOK["s2"])
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"
    body_text = _extract_pdf_text(dl.content)
    assert "assam agricultural university" not in body_text.lower()
    assert "assam veterinary" in body_text.lower()
    # generating again for the SAME (still-active) cycle is rejected (no duplicate versions
    # without cause)
    assert (await _call("POST", f"/thesis/{sid}/certificate-i/generate", TOK["ma1"])).status_code == 409
    # now approval succeeds
    await _approve(sid, TOK["ma1"])
    assert (await _row(sid)).status == "hod_pending"

    # Regeneration-after-revert regression (Section 21), using s_revert's real history: every
    # `before=["ma1"]` iteration of `t_revert_matrix` called `_generate_cert_i("s_revert", ...)`
    # again on a freshly-active cycle after the previous one was reverted. If an obsolete
    # Certificate I had incorrectly satisfied the new cycle, that call would have hit the
    # generate endpoint's OWN "already generated" 409 rather than succeeding — which it did not
    # (that test already passed) — and independently: there must be MORE than one
    # certificate_i_pg27 version on s_revert's thesis, each tied to a DIFFERENT cycle_id.
    sid_r = TH["s_revert"]
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ThesisDocument.version_number, ThesisDocument.cycle_id)
            .where(ThesisDocument.thesis_id == uuid.UUID(sid_r), ThesisDocument.document_type == "certificate_i_pg27")
            .order_by(ThesisDocument.version_number)
        )).all()
    assert len(rows) >= 2, "s_revert's revert/resubmit history must have produced more than one Certificate I version"
    cycle_ids = {r[1] for r in rows}
    assert len(cycle_ids) == len(rows), "each Certificate I version must be tied to its OWN (different) approval cycle, never reused"


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "SCHEMA: 6 tables; LIBRARIAN in enum and NOT in single-holder index; partial unique indexes present": t_schema,
            "LIBRARIAN MULTI-HOLDER: a second Librarian succeeds; a second DPGS still refused": t_librarian_multi_holder,
            "CREATE: title sourced from PPW exactly; no PPW/blank PPW -> 400; second thesis -> 409; no client id fields": t_create_thesis,
            "STUDENT ISOLATION: Student B blocked (404) on GET/PATCH/submit/upload/download of Student A's thesis": t_student_isolation,
            "SUBMISSION VALIDATION: each missing precondition rejected with the right message; complete submission seeds exactly 6 ordered stages": t_submission_validation,
            "FILE VALIDATION: .docx accepted; pdf/txt/renamed-exe/empty/corrupt-zip/wrong-content-type rejected across doc types + library report": t_upload_validation,
            "FULL APPROVAL CHAIN (PG, 1 examiner): MA->HOD->Librarian(separate data-entry vs approve)->Incharge(no OTP)->DPGS->examiner->DPGS final->approved": t_full_approval_chain,
            "DPGS ZERO ASSIGNMENTS: approving the dpgs stage with no active External Examiner assignment is rejected": t_dpgs_zero_assignments_rejected,
            "MULTI-EXAMINER (PhD, 2 examiners): both must submit before dpgs_final_pending; no double-submit": t_multi_examiner_phd,
            "REVERT MATRIX: MA/HOD/Librarian/Incharge/DPGS each revert with a remark to the STUDENT; later stages cancelled": t_revert_matrix,
            "DPGS_FINAL REVERT BLOCKED: no revert path exists for the final DPGS stage": t_dpgs_final_revert_rejected,
            "LIBRARIAN MECHANICS: two different Librarians act on two different theses; non-Librarian 403; wrong-stage 400": t_librarian_mechanics,
            "CONFIDENTIALITY (library report): student sees the percentage but the document itself 404s; every approver can download": t_confidentiality_library_report,
            "CONFIDENTIALITY (external report): key genuinely absent pre-approval; present post-approval to student/DPGS only, never HOD/Librarian/Incharge/MA": t_confidentiality_external_report,
            "EXAMINER ACCESS: doc scope limited to 3 types; cross-student thesis/evaluation-id pairing blocked despite a real assignment on both sides": t_examiner_access_and_cross_student_authorization,
            "DEPARTMENT ISOLATION: HOD-A cannot act on/see a Dept-B student; unrelated Major Advisor cannot act": t_department_isolation,
            "IDOR/TAMPERING: foreign document/thesis ids -> 404; query params inert; extra body fields -> 422 (extra=forbid)": t_idor_and_tampering,
            "DECLARATION: Generate produces a real, dynamic, AVFU-branded PDF with no AAU text; Upload versions the SAME slot; non-owner blocked": t_declaration_generate_and_upload,
            "PG25 (MA-driven): wrong-MA blocked, Unsatisfactory is real, Satisfactory->Submit->HOD-revert->MA-regenerate->HOD-approve, IDOR, stale/reverted never satisfies anything": t_pg25_ma_driven_lifecycle_and_idor,
            "PG25 (multi-signer committee): real committee sign/IDOR/Home-Pending scoping, department-isolated HOD final approval": t_pg25_committee_multi_signer_and_department_isolation,
            "COMMITTEE LOCK: reassign/add/remove blocked (409, 'Initial Thesis') while active; unauthorized still 403 first; released after revert/approval": t_major_advisor_committee_lock,
            "CERTIFICATE I: only the real Major Advisor generates, MA approval blocked until generated, no AAU branding, regenerated (never reused) across revert/resubmission cycles": t_certificate_i_authorization_and_regeneration,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "theses": (await db.execute(select(func.count()).select_from(Thesis).where(Thesis.student_id.in_(uids)))).scalar_one(),
                "documents": (await db.execute(select(func.count()).select_from(ThesisDocument))).scalar_one(),
                "cycles": (await db.execute(select(func.count()).select_from(ThesisApprovalCycle))).scalar_one(),
                "stages": (await db.execute(select(func.count()).select_from(ThesisApprovalStage))).scalar_one(),
                "signatures": (await db.execute(select(func.count()).select_from(ThesisSignature))).scalar_one(),
                "evaluations": (await db.execute(select(func.count()).select_from(ThesisExternalEvaluation))).scalar_one(),
                "seminar_certificates": (await db.execute(select(func.count()).select_from(ThesisSeminarCertificate))).scalar_one(),
                "seminar_certificate_signatures": (await db.execute(select(func.count()).select_from(ThesisSeminarCertificateSignature))).scalar_one(),
                "examiners": (await db.execute(select(func.count()).select_from(ExternalExaminer).where(ExternalExaminer.email.like(f"{_PFX}%")))).scalar_one(),
                "assignments": (await db.execute(select(func.count()).select_from(ExternalExaminerAssignment))).scalar_one(),
                "committees": (await db.execute(select(func.count()).select_from(AdvisoryCommittee).where(AdvisoryCommittee.research_title == "ZZTEST thesis committee"))).scalar_one(),
                "ppw": (await db.execute(select(func.count()).select_from(Ppw).where(Ppw.student_id.in_(uids)))).scalar_one(),
                "college": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        upload_leftover = _UPLOAD_ROOT.exists()
        record("cleanup: no ZZTEST_THESIS user / thesis / document / cycle / stage / signature / evaluation / examiner / assignment / committee / PPW / college / session row remains, no uploaded files remain",
               not any(left.values()) and orphans == 0 and not upload_leftover, str(left) + f" upload_dir_exists={upload_leftover}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
