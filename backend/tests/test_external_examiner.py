"""Standalone HTTP-level tests for the External Examiner Selection module
(BUSINESS_LOGIC.md section AC).

Modelled directly on `tests/test_synopsis.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions via `create_access_token`/
`RefreshToken` rows, a `record(name, ok, detail)` pass/fail tracker, a
`_run(name, fn)` wrapper so one failing test doesn't stop the run, and a
`_setup()`/`_teardown()` pair using a unique `_TAG = uuid.uuid4().hex[:6]`
and `zztest_extexam_...` prefix on every created row so cleanup is
unambiguous. Real students, committees, and role holders are never touched;
if a real Incharge Academic Cell / DPGS / Vice Chancellor holder already
exists (all three are single-holder roles) the run refuses to start.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_external_examiner
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import selectinload

from app.api.v1.endpoints import external_examiner as ee_module
from app.core import email as email_module
from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.audit import AuditLog
from app.models.email_outbox import EmailOutbox
from app.models.external_examiner import (
    ExternalExaminer, ExternalExaminerApprovalCycle, ExternalExaminerApprovalStage,
    ExternalExaminerAssignment, ExternalExaminerProposal, ExternalExaminerSelection,
    ExternalExaminerSelectionResult, ExternalExaminerSignature,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_extexam_"
_LABEL = "ZZTEST_EXTEXAM"
_DEVICE = "ZZTEST-extexam"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}          # user key -> id
TOK: dict[str, str] = {}              # token key -> access token
S: dict = {}                          # scratch (departments, programs, selection ids, ...)
SEL: dict[str, str] = {}              # student key -> selection id
CM: dict[str, uuid.UUID] = {}         # "student:facultykey" -> CommitteeMember id
COMMITTEE: dict[str, uuid.UUID] = {}  # student key -> AdvisoryCommittee id

_OUTSIDE_DOMAIN = "example.com"


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


def _email(key: str) -> str:
    return f"{_PFX}{key}_{_TAG}@{_OUTSIDE_DOMAIN}"


def _proposal(key: str, institution: str = "ZZTEST Institution") -> dict:
    return {
        "name": f"ZZTEST Examiner {key.upper()}", "specialization": "ZZTEST Veterinary Pathology",
        "designation": "Professor", "email": _email(key), "phone": "9876500000", "institution": institution,
    }


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None, title=None, designation=None):
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"EE{key.upper()}", title=title,
                  designation=designation, role=role, department_id=dept.id if dept else None,
                  program_id=program.id if program else None, college_id=college.id if college else None,
                  student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True)
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role not in (UserRole.STUDENT,):
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_direct_examiner(key) -> None:
    """A standalone EXTERNAL_EXAMINER account created directly (bypassing the
    VC-selection flow) with NO ExternalExaminer/Assignment row — used only for
    the deactivation-gate tests that don't need a real assignment history."""
    from app.core.security import hash_password
    async with AsyncSessionLocal() as db:
        u = User(email=_email(key), first_name="ZZTEST", last_name=f"EE{key.upper()}", role=UserRole.EXTERNAL_EXAMINER,
                  hashed_password=hash_password("ZZTestTemp123!"), is_active=True, is_verified=True, must_change_password=True)
        db.add(u)
        await db.commit()
        U[key] = u.id


async def _mk_committee(student_key: str, ma_key: str, extra=()) -> None:
    """AdvisoryCommittee + accepted Major Advisor (+ optional extra accepted members)."""
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST EE committee", status="members_pending")
        db.add(c)
        await db.flush()
        COMMITTEE[student_key] = c.id
        m = CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True)
        db.add(m)
        await db.flush()
        CM[f"{student_key}:{ma_key}"] = m.id
        for fac_key, role in extra:
            em = CommitteeMember(committee_id=c.id, faculty_id=U[fac_key], role=role, accepted=True)
            db.add(em)
            await db.flush()
            CM[f"{student_key}:{fac_key}"] = em.id
        await db.commit()


# ── DB read helpers ──────────────────────────────────────────────────────────

async def _selection_row(sid) -> ExternalExaminerSelection:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ExternalExaminerSelection).where(ExternalExaminerSelection.id == uuid.UUID(sid)))).scalar_one()


async def _cycles(sid) -> list[ExternalExaminerApprovalCycle]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ExternalExaminerApprovalCycle).where(ExternalExaminerApprovalCycle.selection_id == uuid.UUID(sid))
            .order_by(ExternalExaminerApprovalCycle.cycle_number)
        )).scalars().all())


async def _stages(cycle_id) -> list[ExternalExaminerApprovalStage]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ExternalExaminerApprovalStage).where(ExternalExaminerApprovalStage.cycle_id == cycle_id)
            .order_by(ExternalExaminerApprovalStage.sequence)
        )).scalars().all())


async def _proposals(cycle_id) -> list[ExternalExaminerProposal]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(ExternalExaminerProposal).where(ExternalExaminerProposal.cycle_id == cycle_id)
            .order_by(ExternalExaminerProposal.slot_number)
        )).scalars().all())


async def _examiner_by_email(key: str) -> ExternalExaminer | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(ExternalExaminer).options(selectinload(ExternalExaminer.user)).where(ExternalExaminer.email == _email(key))
        )).scalar_one_or_none()


async def _user_row(user_id) -> User:
    async with AsyncSessionLocal() as db:
        return await db.get(User, user_id)


# ── HTTP action helpers ──────────────────────────────────────────────────────

async def _create(student_key, ma_token, proposals, expect=201):
    r = await _call("POST", "/external-examiners", ma_token, json={"student_id": str(U[student_key]), "proposals": proposals})
    assert r.status_code == expect, (r.status_code, r.text)
    if expect == 201:
        SEL[student_key] = r.json()["id"]
    return r


async def _detail(sid, token) -> dict:
    r = await _call("GET", f"/external-examiners/{sid}", token)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _approve(sid, token, expect=200):
    """Handles both signatory stages (OTP required) and the Incharge stage
    (workflow-only, GET otp returns 400) uniformly, exactly like the caller
    would: request the OTP, and if the stage says none is needed, approve
    with an empty body instead."""
    r = await _call("GET", f"/external-examiners/{sid}/approval/otp", token)
    if r.status_code == 400:
        r2 = await _call("POST", f"/external-examiners/{sid}/approval/approve", token, json={})
    else:
        assert r.status_code == 200, r.text
        r2 = await _call("POST", f"/external-examiners/{sid}/approval/approve", token, json={"otp": r.json()["dev_otp"]})
    assert r2.status_code == expect, (r2.status_code, r2.text)
    return r2


async def _revert(sid, token, remark, expect=200):
    r = await _call("POST", f"/external-examiners/{sid}/approval/revert", token, json={"remark": remark})
    assert r.status_code == expect, (r.status_code, r.text)
    return r


async def _vc_select(sid, token, proposal_ids, expect=200):
    r = await _call("POST", f"/external-examiners/{sid}/vc-selection", token, json={"proposal_ids": [str(p) for p in proposal_ids]})
    assert r.status_code == expect, (r.status_code, r.text)
    return r


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    email_module.send_email = lambda to, subject, body: True    # OTP email: never really sent
    settings.ENVIRONMENT = "development"                        # dev_otp available to the tests

    async with AsyncSessionLocal() as db:
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS','VICE_CHANCELLOR')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS / Vice Chancellor holder exists (single-holder roles); refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        S["D1"], S["D2"] = depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        S["PHD"] = (await db.execute(select(Program).where(Program.code == "PhD(F)"))).scalar_one()
        col = College(name=f"{_LABEL} College", code=f"ZZEE{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, d2, pg, phd, college = S["D1"], S["D2"], S["PG"], S["PHD"], S["COLLEGE"]

    # Faculty / approvers
    await _mk_user("ma1", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("ma_lock", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("ma_lock2", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("mem_lock", UserRole.FACULTY, d1, title="Dr.", designation="Associate Professor")
    await _mk_user("mem_lock2", UserRole.FACULTY, d1, title="Dr.", designation="Assistant Professor")
    await _mk_user("hod1", UserRole.HOD, d1, title="Dr.", designation="Professor")
    await _mk_user("hod2", UserRole.HOD, d2, title="Dr.", designation="Professor")
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None, title="Dr.", designation="Professor")
    await _mk_user("dpgs", UserRole.DPGS, None, title="Dr.", designation="Director of PG Studies")
    await _mk_user("vc", UserRole.VICE_CHANCELLOR, None, title="Dr.", designation="Vice Chancellor")
    await _mk_user("vc_candidate", UserRole.FACULTY, d1, title="Dr.", designation="Professor")   # for single-holder test

    # Students
    await _mk_user("s_iso", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZEE-ISO-{_TAG}")
    await _mk_user("s_pg", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZEE-PG-{_TAG}")
    await _mk_user("s_phd", UserRole.STUDENT, d1, program=phd, college=college, roll=f"ZZEE-PHD-{_TAG}")
    await _mk_user("s_cnt_pg", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZEE-CPG-{_TAG}")
    await _mk_user("s_cnt_phd", UserRole.STUDENT, d1, program=phd, college=college, roll=f"ZZEE-CPHD-{_TAG}")
    await _mk_user("s_edit", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZEE-EDIT-{_TAG}")
    await _mk_user("s_revert", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZEE-REV-{_TAG}")
    await _mk_user("s_lock", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZEE-LOCK-{_TAG}")

    for k in ("s_iso", "s_pg", "s_phd", "s_cnt_pg", "s_cnt_phd", "s_edit", "s_revert"):
        await _mk_committee(k, "ma1")
    await _mk_committee("s_lock", "ma_lock", extra=[("mem_lock", "member_major")])

    # Direct (assignment-free) EXTERNAL_EXAMINER accounts for the deactivation tests
    await _mk_direct_examiner("direct1")
    await _mk_direct_examiner("direct2")

    S["SA"] = await _session(admin_id)
    for k in ("ma1", "ma_lock", "ma_lock2", "mem_lock", "mem_lock2", "hod1", "hod2", "incharge", "dpgs", "vc", "vc_candidate",
              "s_iso", "s_pg", "s_phd", "s_cnt_pg", "s_cnt_phd", "s_edit", "s_revert", "s_lock"):
        TOK[k] = await _session(U[k])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        sel_ids = (await db.execute(select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.student_id.in_(uids)))).scalars().all()
        cyc_ids = (await db.execute(select(ExternalExaminerApprovalCycle.id).where(ExternalExaminerApprovalCycle.selection_id.in_(sel_ids or [uuid.uuid4()])))).scalars().all()
        examiner_ids = (await db.execute(select(ExternalExaminer.id).where(ExternalExaminer.email.like(f"{_PFX}%")))).scalars().all()

        await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(uids)))
        await db.execute(delete(EmailOutbox).where(EmailOutbox.recipient.like(f"{_PFX}%")))
        await db.execute(delete(ExternalExaminerAssignment).where(ExternalExaminerAssignment.examiner_id.in_(examiner_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerSelectionResult).where(ExternalExaminerSelectionResult.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerSignature).where(ExternalExaminerSignature.approval_stage_id.in_(
            select(ExternalExaminerApprovalStage.id).where(ExternalExaminerApprovalStage.cycle_id.in_(cyc_ids or [uuid.uuid4()]))
        )))
        await db.execute(delete(ExternalExaminerApprovalStage).where(ExternalExaminerApprovalStage.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerProposal).where(ExternalExaminerProposal.cycle_id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerApprovalCycle).where(ExternalExaminerApprovalCycle.id.in_(cyc_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminerSelection).where(ExternalExaminerSelection.id.in_(sel_ids or [uuid.uuid4()])))
        await db.execute(delete(ExternalExaminer).where(ExternalExaminer.id.in_(examiner_ids or [uuid.uuid4()])))

        # Any AMS account created automatically by vc-selection (role EXTERNAL_EXAMINER, our test domain)
        extra_examiner_uids = select(User.id).where(User.role == UserRole.EXTERNAL_EXAMINER, User.email.like(f"{_PFX}%"))
        all_test_uids = select(User.id).where(User.email.like(f"%{_TAG}@%") | User.id.in_(extra_examiner_uids))

        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(all_test_uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(User).where(User.role == UserRole.EXTERNAL_EXAMINER, User.email.like(f"{_PFX}%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── tests ────────────────────────────────────────────────────────────────────

async def t_schema():
    async with AsyncSessionLocal() as db:
        for t in ("ams_external_examiners", "ams_external_examiner_selections", "ams_external_examiner_approval_cycles",
                  "ams_external_examiner_proposals", "ams_external_examiner_approval_stages", "ams_external_examiner_signatures",
                  "ams_external_examiner_selection_results", "ams_external_examiner_assignments"):
            assert (await db.execute(text("select count(*) from information_schema.tables where table_name=:t"), {"t": t})).scalar_one() == 1, t
        idx = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_ext_examiner_one_active_cycle'"))).scalar_one()
        assert "UNIQUE" in idx and "active" in idx, idx
        idx2 = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_user_role_assignment_single_holder'"))).scalar_one()
        assert "VICE_CHANCELLOR" in idx2, "the single-holder index must also cover VICE_CHANCELLOR: " + idx2


async def t_student_isolation():
    """A Student session gets 403/404 (never leaked data) on every External
    Examiner endpoint — including on their OWN selection once one exists."""
    await _create("s_iso", TOK["ma1"], [_proposal("iso1"), _proposal("iso2"), _proposal("iso3")])
    stok = TOK["s_iso"]
    sid = SEL["s_iso"]
    assert (await _call("POST", "/external-examiners", stok, json={"student_id": str(U["s_iso"]), "proposals": []})).status_code == 403
    assert (await _call("GET", "/external-examiners/mine", stok)).status_code == 403
    assert (await _call("GET", "/external-examiners/pending-approvals", stok)).status_code == 403
    assert (await _call("GET", f"/external-examiners/{sid}", stok)).status_code == 404
    r = await _call("PATCH", f"/external-examiners/{sid}/proposals/{uuid.uuid4()}", stok, json=_proposal("x"))
    assert r.status_code in (403, 404)
    assert (await _call("GET", f"/external-examiners/{sid}/approval/otp", stok)).status_code == 403
    assert (await _call("POST", f"/external-examiners/{sid}/approval/approve", stok, json={})).status_code == 403
    assert (await _call("POST", f"/external-examiners/{sid}/approval/revert", stok, json={"remark": "r"})).status_code == 403
    assert (await _call("POST", f"/external-examiners/{sid}/vc-selection", stok, json={"proposal_ids": []})).status_code == 403
    assert (await _call("GET", f"/external-examiners/examiners/{uuid.uuid4()}", stok)).status_code == 403


async def t_proposal_count_enforcement():
    tok = TOK["ma1"]
    # PG (s_cnt_pg): only 3 succeeds
    r2 = await _create("s_cnt_pg", tok, [_proposal("cnt_pg_a"), _proposal("cnt_pg_b")], expect=400)
    assert "exactly 3" in r2.json()["detail"], r2.text
    r4 = await _create("s_cnt_pg", tok, [_proposal(f"cnt_pg_{i}") for i in range(4)], expect=400)
    assert "exactly 3" in r4.json()["detail"], r4.text
    await _create("s_cnt_pg", tok, [_proposal("cnt_pg_1"), _proposal("cnt_pg_2"), _proposal("cnt_pg_3")], expect=201)

    # PhD (s_cnt_phd): only 5 succeeds
    r4b = await _create("s_cnt_phd", tok, [_proposal(f"cnt_phd4_{i}") for i in range(4)], expect=400)
    assert "exactly 5" in r4b.json()["detail"], r4b.text
    r6 = await _create("s_cnt_phd", tok, [_proposal(f"cnt_phd6_{i}") for i in range(6)], expect=400)
    assert "exactly 5" in r6.json()["detail"], r6.text
    await _create("s_cnt_phd", tok, [_proposal(f"cnt_phd5_{i}") for i in range(5)], expect=201)


async def t_outside_avfu_rejection():
    tok = TOK["ma1"]
    good = [_proposal("dom_a"), _proposal("dom_b")]
    for bad_email in ("person@avfu.ac.in", "PERSON@AVFU.AC.IN", "person@AvFu.Ac.In"):
        bad_proposal = {**_proposal("dom_bad"), "email": bad_email}
        r = await _call("POST", "/external-examiners", tok, json={"student_id": str(U["s_edit"]), "proposals": good + [bad_proposal]})
        assert r.status_code == 400 and "outside" in r.json()["detail"], (bad_email, r.status_code, r.text)
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(ExternalExaminerSelection.id).where(ExternalExaminerSelection.student_id == U["s_edit"]))).scalar_one_or_none() is None


async def t_happy_path_pg_and_confidentiality():
    """S_PG's full chain: MA -> HOD -> Incharge -> DPGS -> VC selects 1. Also:
    VC-selection count enforcement, idempotency, first-time account creation,
    confidentiality of the result."""
    await _create("s_pg", TOK["ma1"], [_proposal("shared"), _proposal("pg_b"), _proposal("pg_c")])
    sid = SEL["s_pg"]
    d = await _detail(sid, TOK["ma1"])
    assert d["status"] == "major_advisor_pending" and d["degree_level"] == "PG"
    assert d["required_proposal_count"] == 3 and d["required_selection_count"] == 1
    await _approve(sid, TOK["ma1"])
    assert (await _detail(sid, TOK["ma1"]))["status"] == "hod_pending"
    # wrong-department HOD is refused
    assert (await _call("GET", f"/external-examiners/{sid}/approval/otp", TOK["hod2"])).status_code == 403
    await _approve(sid, TOK["hod1"])
    assert (await _detail(sid, TOK["hod1"]))["status"] == "incharge_pending"
    # Incharge stage needs no OTP
    assert (await _call("GET", f"/external-examiners/{sid}/approval/otp", TOK["incharge"])).status_code == 400
    await _approve(sid, TOK["incharge"])
    assert (await _detail(sid, TOK["incharge"]))["status"] == "dpgs_pending"
    await _approve(sid, TOK["dpgs"])
    d = await _detail(sid, TOK["dpgs"])
    assert d["status"] == "vc_pending"
    proposal_ids = [p["id"] for p in d["proposals"]]

    # VC-selection count enforcement: PG requires exactly 1
    assert (await _vc_select(sid, TOK["vc"], [], expect=400))
    assert (await _vc_select(sid, TOK["vc"], proposal_ids[:2], expect=400))
    chosen = proposal_ids[0]     # slot 1 -- this is the "shared" examiner email, reused by S_PHD later
    r = await _vc_select(sid, TOK["vc"], [chosen])
    assert r.json()["status"] == "approved" and r.json()["selection_completed"] is True
    S["s_pg_chosen_proposal"] = chosen

    # idempotency: identical replay creates nothing new and returns the same result
    async with AsyncSessionLocal() as db:
        before = {
            "examiners": (await db.execute(select(func.count()).select_from(ExternalExaminer))).scalar_one(),
            "users": (await db.execute(select(func.count()).select_from(User).where(User.role == UserRole.EXTERNAL_EXAMINER))).scalar_one(),
            "results": (await db.execute(select(func.count()).select_from(ExternalExaminerSelectionResult))).scalar_one(),
            "assignments": (await db.execute(select(func.count()).select_from(ExternalExaminerAssignment))).scalar_one(),
        }
    r2 = await _vc_select(sid, TOK["vc"], [chosen])
    assert r2.json() == r.json(), "identical replay must return the identical result"
    async with AsyncSessionLocal() as db:
        after = {
            "examiners": (await db.execute(select(func.count()).select_from(ExternalExaminer))).scalar_one(),
            "users": (await db.execute(select(func.count()).select_from(User).where(User.role == UserRole.EXTERNAL_EXAMINER))).scalar_one(),
            "results": (await db.execute(select(func.count()).select_from(ExternalExaminerSelectionResult))).scalar_one(),
            "assignments": (await db.execute(select(func.count()).select_from(ExternalExaminerAssignment))).scalar_one(),
        }
    assert before == after, (before, after)

    # first-time account creation
    examiner = await _examiner_by_email("shared")
    assert examiner is not None and examiner.user_id is not None
    S["shared_password_hash"] = examiner.user.hashed_password
    async with AsyncSessionLocal() as db:
        outbox = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient == _email("shared")))).scalars().all()
    assert len(outbox) == 1 and "Assignment for Thesis Evaluation" in outbox[0].subject and "Temporary Password" in outbox[0].body

    # confidentiality after VC selection: MA/HOD/Incharge see completion only, never the result
    for tok in (TOK["ma1"], TOK["hod1"], TOK["incharge"]):
        body = await _detail(sid, tok)
        assert body["selection_completed"] is True
        assert "selected_proposal_ids" not in body, body.keys()
    # DPGS/VC/Super Admin see exactly which proposal(s) were chosen
    for tok in (TOK["dpgs"], TOK["vc"], S["SA"]):
        body = await _detail(sid, tok)
        assert body.get("selected_proposal_ids") == [chosen], body


async def t_happy_path_phd_and_examiner_reuse():
    """S_PHD's full chain, VC selects 2 — one of them (slot 1) is the SAME
    examiner email already selected for S_PG above (reuse rule); the other
    (the "solo" examiner) is brand new."""
    proposals = [_proposal("shared", institution="ZZTEST Institution Y"), _proposal("solo"),
                 _proposal("phd_c"), _proposal("phd_d"), _proposal("phd_e")]
    await _create("s_phd", TOK["ma1"], proposals)
    sid = SEL["s_phd"]
    d = await _detail(sid, TOK["ma1"])
    assert d["degree_level"] == "PhD" and d["required_proposal_count"] == 5 and d["required_selection_count"] == 2
    await _approve(sid, TOK["ma1"])
    await _approve(sid, TOK["hod1"])
    await _approve(sid, TOK["incharge"])
    await _approve(sid, TOK["dpgs"])
    d = await _detail(sid, TOK["dpgs"])
    proposal_ids = [p["id"] for p in d["proposals"]]

    # VC-selection count enforcement: PhD requires exactly 2
    assert (await _vc_select(sid, TOK["vc"], [proposal_ids[0]], expect=400))
    assert (await _vc_select(sid, TOK["vc"], proposal_ids[:3], expect=400))
    chosen = [proposal_ids[0], proposal_ids[1]]      # shared + solo
    r = await _vc_select(sid, TOK["vc"], chosen)
    assert r.json()["status"] == "approved"
    S["s_phd_chosen_proposals"] = chosen
    S["solo_examiner_key"] = "solo"

    # reuse: same ExternalExaminer + same AMS User account across both students
    examiner = await _examiner_by_email("shared")
    assert examiner is not None
    async with AsyncSessionLocal() as db:
        assignments = (await db.execute(select(ExternalExaminerAssignment).where(ExternalExaminerAssignment.examiner_id == examiner.id))).scalars().all()
        user_count = (await db.execute(select(func.count()).select_from(User).where(User.email == _email("shared")))).scalar_one()
    assert len(assignments) == 2 and {a.student_id for a in assignments} == {U["s_pg"], U["s_phd"]}
    assert user_count == 1, "exactly one AMS account exists for the reused examiner"

    # existing-account email (no password), and the password hash is unchanged
    assert examiner.user.hashed_password == S["shared_password_hash"], "reusing an examiner must never reset their password"
    async with AsyncSessionLocal() as db:
        outbox = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient == _email("shared")))).scalars().all()
    assert len(outbox) == 2   # one first-time (S_PG), one existing-account (S_PHD)
    existing_mail = sorted(outbox, key=lambda o: o.created_at)[-1]
    assert "New External Examiner Assignment" in existing_mail.subject
    assert "Temporary Password" not in existing_mail.body and "Password" not in existing_mail.body

    # historical snapshot immutability: S_PG's proposal for the shared examiner still reads Institution X
    pg_cycle = (await _cycles(SEL["s_pg"]))[-1]
    pg_props = await _proposals(pg_cycle.id)
    shared_pg_prop = next(p for p in pg_props if p.email_snapshot == _email("shared"))
    assert shared_pg_prop.institution_snapshot == "ZZTEST Institution", shared_pg_prop.institution_snapshot
    phd_cycle = (await _cycles(SEL["s_phd"]))[-1]
    phd_props = await _proposals(phd_cycle.id)
    shared_phd_prop = next(p for p in phd_props if p.email_snapshot == _email("shared"))
    assert shared_phd_prop.institution_snapshot == "ZZTEST Institution Y", "the later proposal keeps its OWN snapshot"


async def t_incharge_edit_vs_approve():
    proposals = [_proposal("edit_a"), _proposal("edit_b"), _proposal("edit_c")]
    await _create("s_edit", TOK["ma1"], proposals)
    sid = SEL["s_edit"]
    await _approve(sid, TOK["ma1"])
    await _approve(sid, TOK["hod1"])
    d = await _detail(sid, TOK["incharge"])
    assert d["status"] == "incharge_pending"
    proposal_id = d["proposals"][0]["id"]

    # tampering: extra field is rejected outright (extra="forbid")
    bad = {**proposals[0], "role": "dpgs"}
    assert (await _call("PATCH", f"/external-examiners/{sid}/proposals/{proposal_id}", TOK["incharge"], json=bad)).status_code == 422

    edited = {**proposals[0], "specialization": "ZZTEST Corrected Specialization"}
    r = await _call("PATCH", f"/external-examiners/{sid}/proposals/{proposal_id}", TOK["incharge"], json=edited)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "incharge_pending", "editing must never auto-approve the stage"
    edited_proposal = next(p for p in body["proposals"] if p["id"] == proposal_id)
    assert edited_proposal["specialization"] == "ZZTEST Corrected Specialization" and edited_proposal["edited"] is True

    async with AsyncSessionLocal() as db:
        logs = (await db.execute(select(AuditLog).where(AuditLog.entity_id == proposal_id))).scalars().all()
    assert len(logs) == 1 and logs[0].action == "external_examiner.incharge_edit_proposal", [l.action for l in logs]

    # editing the email re-resolves examiner_id against the reusable identity
    reuse_edit = {**proposals[0], "specialization": "ZZTEST Corrected Specialization", "email": _email("shared")}
    r = await _call("PATCH", f"/external-examiners/{sid}/proposals/{proposal_id}", TOK["incharge"], json=reuse_edit)
    assert r.status_code == 200, r.text
    edited_proposal = next(p for p in r.json()["proposals"] if p["id"] == proposal_id)
    assert edited_proposal["is_reused_examiner"] is True, "editing the email must re-resolve examiner_id"

    # IDOR: a proposal id from a DIFFERENT selection is never accepted here, even
    # while the Incharge genuinely has a pending action on THIS selection —
    # never distinguishes "wrong cycle" from "doesn't exist".
    foreign_proposal_id = S["s_pg_chosen_proposal"]
    r = await _call("PATCH", f"/external-examiners/{sid}/proposals/{foreign_proposal_id}", TOK["incharge"], json=proposals[1])
    assert r.status_code == 404, (r.status_code, r.text)

    # approve is a SEPARATE call and still succeeds afterward
    await _approve(sid, TOK["incharge"])
    assert (await _detail(sid, TOK["dpgs"]))["status"] == "dpgs_pending"



async def t_revert_matrix():
    """S_REVERT is reverted by HOD, then Incharge, then DPGS, then VC in turn —
    each always returns to the Major Advisor, with a brand-new cycle each time."""
    plan = [
        ("hod1", "hod", ["ma1"]),
        ("incharge", "incharge_academic_cell", ["ma1", "hod1"]),
        ("dpgs", "dpgs", ["ma1", "hod1", "incharge"]),
        ("vc", "vc", ["ma1", "hod1", "incharge", "dpgs"]),
    ]
    for n, (reverter_key, stage_type, before) in enumerate(plan, start=1):
        await _create("s_revert", TOK["ma1"], [_proposal(f"rev{n}_a"), _proposal(f"rev{n}_b"), _proposal(f"rev{n}_c")])
        sid = SEL["s_revert"]
        d = await _detail(sid, TOK["ma1"])
        assert d["current_cycle_number"] == n
        for who in before:
            await _approve(sid, TOK[who])
        remark = f"ZZTEST revert remark {n} by {stage_type}"
        # a remark is required
        assert (await _call("POST", f"/external-examiners/{sid}/approval/revert", TOK[reverter_key], json={"remark": ""})).status_code in (400, 422)
        await _revert(sid, TOK[reverter_key], remark)
        d = await _detail(sid, TOK["ma1"])
        assert d["status"] == "reverted" and d["can_edit"] is True
        ri = d["revert_info"]
        assert ri["remark"] == remark and ri["cycle_number"] == n
        cyc = (await _cycles(sid))[-1]
        stages = await _stages(cyc.id)
        reverted_stage = next(s for s in stages if s.status == "reverted")
        assert reverted_stage.stage_type == stage_type
        others_pending = [s for s in stages if s.id != reverted_stage.id and s.stage_type != "major_advisor" and _PHASE_AFTER(stage_type, s.stage_type)]
        assert all(s.status == "cancelled" for s in others_pending), [(s.stage_type, s.status) for s in others_pending]
        # nobody can approve a reverted selection; the reverter cannot revert twice
        assert (await _call("POST", f"/external-examiners/{sid}/approval/approve", TOK[reverter_key], json={})).status_code == 403
        # the Major Advisor sees the revert remark on their own token; the student has zero access
        assert (await _call("GET", f"/external-examiners/{sid}", TOK["s_revert"])).status_code == 404


_STAGE_ORDER = ["major_advisor", "hod", "incharge_academic_cell", "dpgs", "vc"]


def _PHASE_AFTER(reverted_type: str, other_type: str) -> bool:
    return _STAGE_ORDER.index(other_type) > _STAGE_ORDER.index(reverted_type)


async def t_major_advisor_lock():
    committee_id = COMMITTEE["s_lock"]
    await _create("s_lock", TOK["ma_lock"], [_proposal("lock_a"), _proposal("lock_b"), _proposal("lock_c")])
    sid = SEL["s_lock"]
    lock_msg = ("Cannot {action} because this student's External Examiner Selection is currently under approval. "
                "The approval workflow must be completed or reverted before {until}.")

    r = await _call("POST", f"/research/committees/{committee_id}/reassign-major-advisor", S["SA"], json={"major_advisor_id": str(U["mem_lock"])})
    assert r.status_code == 409 and r.json()["detail"] == lock_msg.format(action="change the Major Advisor", until="the Major Advisor can be changed"), r.text

    r = await _call("POST", f"/research/committees/{committee_id}/members", S["SA"], json={"faculty_id": str(U["mem_lock2"]), "role": "member_minor"})
    assert r.status_code == 409 and r.json()["detail"] == lock_msg.format(action="add a committee member", until="the committee can be changed"), r.text

    r = await _call("DELETE", f"/research/committees/{committee_id}/members/{CM['s_lock:mem_lock']}", S["SA"])
    assert r.status_code == 409 and r.json()["detail"] == lock_msg.format(action="remove a committee member", until="the committee can be changed"), r.text

    # release the lock: MA approves, then HOD reverts -> cycle is no longer active
    await _approve(sid, TOK["ma_lock"])
    await _revert(sid, TOK["hod1"], "ZZTEST release the MA lock")
    assert (await _cycles(sid))[-1].status == "reverted"

    # remove_member has no other precondition -> now succeeds
    r = await _call("DELETE", f"/research/committees/{committee_id}/members/{CM['s_lock:mem_lock']}", S["SA"])
    assert r.status_code == 204, r.text
    # add_member: committee is still in members_pending -> now succeeds
    r = await _call("POST", f"/research/committees/{committee_id}/members", S["SA"], json={"faculty_id": str(U["mem_lock2"]), "role": "member_minor"})
    assert r.status_code == 201, r.text
    # reassign_major_advisor: simulate the "declined" precondition it requires, independent of the EE lock
    async with AsyncSessionLocal() as db:
        ma_member = await db.get(CommitteeMember, CM["s_lock:ma_lock"])
        ma_member.accepted = False
        committee = await db.get(AdvisoryCommittee, committee_id)
        committee.status = "reverted"
        await db.commit()
    r = await _call("POST", f"/research/committees/{committee_id}/reassign-major-advisor", S["SA"], json={"major_advisor_id": str(U["ma_lock2"])})
    assert r.status_code == 200, r.text


async def t_idor_and_direct_id_tampering():
    # HOD of a DIFFERENT department cannot see a D1 student's selection
    assert (await _call("GET", f"/external-examiners/{SEL['s_phd']}", TOK["hod2"])).status_code == 404
    assert (await _call("GET", f"/external-examiners/{SEL['s_revert']}", TOK["hod2"])).status_code == 404

    # a proposal_id from a totally different selection is never accepted even
    # while the caller genuinely has a pending action (covered in-depth, with a
    # live incharge_pending stage, by t_incharge_edit_vs_approve above) — here,
    # confirm the SAME foreign id is refused as a VC-selection choice too:
    # any caller without a currently-actionable stage on that selection at all
    # gets refused before the id is even considered.
    other_sid = SEL["s_cnt_pg"]
    r = await _call("POST", f"/external-examiners/{other_sid}/vc-selection", TOK["vc"], json={"proposal_ids": [S["s_pg_chosen_proposal"]]})
    assert r.status_code in (400, 403), (r.status_code, r.text)

    # nonexistent selection id -> 404, not a 500 or a leak
    assert (await _call("GET", f"/external-examiners/{uuid.uuid4()}", TOK["dpgs"])).status_code == 404
    assert (await _call("POST", f"/external-examiners/{uuid.uuid4()}/approval/approve", TOK["dpgs"], json={})).status_code in (403, 404)


async def t_query_and_body_tampering():
    sid = SEL["s_pg"]
    base = await _detail(sid, TOK["dpgs"])
    r = await _call("GET", f"/external-examiners/{sid}?debug=1&include_selection=true&department_id={S['D2'].id}", TOK["dpgs"])
    assert r.status_code == 200 and r.json() == base, "unknown query params must change nothing"

    # extra/unexpected fields are rejected (Pydantic extra=\"forbid\") — never silently ignored, never privilege-elevating
    assert (await _call("POST", "/external-examiners", TOK["ma1"], json={
        "student_id": str(U["s_cnt_pg"]), "proposals": [_proposal("t1")], "role": "dpgs",
    })).status_code == 422
    assert (await _call("POST", f"/external-examiners/{sid}/approval/approve", TOK["dpgs"], json={"otp": "1", "force": True})).status_code == 422
    assert (await _call("POST", f"/external-examiners/{sid}/approval/revert", TOK["dpgs"], json={"remark": "x", "role": "dpgs"})).status_code == 422
    assert (await _call("POST", f"/external-examiners/{sid}/vc-selection", TOK["vc"], json={"proposal_ids": [], "admin": True})).status_code == 422


async def t_examiner_account_deactivation():
    # Super Admin can freely deactivate a direct EXTERNAL_EXAMINER account with no assignment
    r = await _call("PATCH", f"/auth/users/{U['direct1']}", S["SA"], json={"is_active": False})
    assert r.status_code == 200, r.text
    assert (await _user_row(U["direct1"])).is_active is False

    # DPGS may only manage EXTERNAL_EXAMINER accounts, never Faculty/Student
    r = await _call("PATCH", f"/auth/users/{U['ma1']}", TOK["dpgs"], json={"is_active": False})
    assert r.status_code == 403, r.text
    r = await _call("PATCH", f"/auth/users/{U['s_pg']}", TOK["dpgs"], json={"is_active": False})
    assert r.status_code == 403, r.text

    # DPGS must send EXACTLY {"is_active": false} — nothing else
    r = await _call("PATCH", f"/auth/users/{U['direct2']}", TOK["dpgs"], json={"is_active": False, "designation": "X"})
    assert r.status_code == 403, r.text
    r = await _call("PATCH", f"/auth/users/{U['direct2']}", TOK["dpgs"], json={"is_active": True})
    assert r.status_code == 403, r.text
    r = await _call("PATCH", f"/auth/users/{U['direct2']}", TOK["dpgs"], json={"is_active": False})
    assert r.status_code == 200, r.text
    assert (await _user_row(U["direct2"])).is_active is False

    # An examiner with an ACTIVE assignment cannot be deactivated by anyone, Super Admin included
    shared = await _examiner_by_email("shared")
    r = await _call("PATCH", f"/auth/users/{shared.user_id}", S["SA"], json={"is_active": False})
    assert r.status_code == 409, r.text
    solo = await _examiner_by_email(S["solo_examiner_key"])
    r = await _call("PATCH", f"/auth/users/{solo.user_id}", TOK["dpgs"], json={"is_active": False})
    assert r.status_code == 409, r.text


async def t_vc_single_holder():
    r = await _call("POST", f"/auth/users/{U['vc_candidate']}/roles", S["SA"], json={"role": "vice_chancellor"})
    assert r.status_code == 409, r.text
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(
            UserRoleAssignment.role == UserRole.VICE_CHANCELLOR
        ))).scalar_one()
    assert n == 1, "still exactly one Vice Chancellor holder"


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "SCHEMA: 8 tables present; one-active-cycle index; VICE_CHANCELLOR covered by the single-holder index": t_schema,
            "STUDENT ISOLATION: 403/404 on every External Examiner endpoint, including the student's own selection": t_student_isolation,
            "PROPOSAL COUNT: PG needs exactly 3 (2/4 rejected), PhD needs exactly 5 (4/6 rejected)": t_proposal_count_enforcement,
            "OUTSIDE-AVFU: @avfu.ac.in rejected case-insensitively as an examiner email": t_outside_avfu_rejection,
            "HAPPY PATH (PG): MA->HOD->Incharge->DPGS->VC selects 1; VC count enforcement; idempotent replay; confidentiality": t_happy_path_pg_and_confidentiality,
            "HAPPY PATH (PhD): VC selects 2; reused examiner = same identity/account across students; snapshot immutability": t_happy_path_phd_and_examiner_reuse,
            "INCHARGE: edit != approve (stage stays pending, audited); email edit re-resolves examiner_id; foreign proposal id -> 404": t_incharge_edit_vs_approve,
            "REVERT MATRIX: HOD/Incharge/DPGS/VC each revert to the Major Advisor; other pending stages cancelled; student sees nothing": t_revert_matrix,
            "MAJOR ADVISOR LOCK: reassign/add/remove member blocked (exact 409 message) while a cycle is active; released after revert": t_major_advisor_lock,
            "IDOR: cross-department HOD, foreign proposal id, nonexistent selection id — never 200, never leaked": t_idor_and_direct_id_tampering,
            "TAMPERING: unknown query params change nothing; unknown body fields rejected (422) everywhere": t_query_and_body_tampering,
            "DEACTIVATION: Super Admin free on assignment-free accounts; DPGS scoped to EXTERNAL_EXAMINER + exact body; active assignment blocks everyone": t_examiner_account_deactivation,
            "VC SINGLE-HOLDER: a second Vice Chancellor assignment is refused": t_vc_single_holder,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "direct_examiners": (await db.execute(select(func.count()).select_from(User).where(User.role == UserRole.EXTERNAL_EXAMINER, User.email.like(f"{_PFX}%")))).scalar_one(),
                "examiners": (await db.execute(select(func.count()).select_from(ExternalExaminer).where(ExternalExaminer.email.like(f"{_PFX}%")))).scalar_one(),
                "selections": (await db.execute(select(func.count()).select_from(ExternalExaminerSelection)
                              .where(ExternalExaminerSelection.student_id.in_(select(User.id).where(User.email.like(f"%{_TAG}@%")))))).scalar_one(),
                "cycles": (await db.execute(select(func.count()).select_from(ExternalExaminerApprovalCycle)
                           .join(ExternalExaminerSelection, ExternalExaminerSelection.id == ExternalExaminerApprovalCycle.selection_id)
                           .where(ExternalExaminerSelection.student_id.in_(select(User.id).where(User.email.like(f"%{_TAG}@%")))))).scalar_one(),
                "assignments": (await db.execute(select(func.count()).select_from(ExternalExaminerAssignment))).scalar_one(),
                "outbox": (await db.execute(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.recipient.like(f"{_PFX}%")))).scalar_one(),
                "committees": (await db.execute(select(func.count()).select_from(AdvisoryCommittee).where(AdvisoryCommittee.research_title == "ZZTEST EE committee"))).scalar_one(),
                "audit": (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action.like("external_examiner.%")).where(
                    AuditLog.user_id.in_(select(User.id).where(User.email.like(f"%{_TAG}@%")))
                ))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "college": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_EXTEXAM user / examiner / selection / cycle / assignment / outbox / committee / audit / session row remains",
               not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
