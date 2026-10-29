"""Standalone HTTP-level tests for the post-DPGS-approval Advisory Committee
revision task: a committee remains editable after `dpgs_approved` (member
add/remove/replace by the Major Advisor, Major Advisor replacement by the
HOD), every authorized change restarts the HOD -> Incharge Academic Cell ->
DPGS chain, and the student loses document-download access the instant a
revision is initiated, regaining it only after the REVISED committee reaches
`dpgs_approved` again.

Modelled on `tests/test_advisory_committee_document.py` (same harness: real
FastAPI app via `httpx.ASGITransport`, real JWT sessions, a `record`/`_run`
pass-fail tracker, `zztest_acr_...`-prefixed rows, full teardown, the same
single-holder-role STOP guard for INCHARGE_ACADEMIC_CELL/DPGS). Three
independent committees (stu1/stu2/stu3) are each driven through the REAL
HTTP approval chain to `dpgs_approved` first, then each exercises ONE of the
three confirmed revision triggers (member add, member removal without
replacement, Major Advisor replacement) so the scenarios don't interfere
with each other.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_advisory_committee_revision
"""
import asyncio
import hashlib
import io
import sys
import uuid

import httpx
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import Department, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_acr_"
_DEVICE = "ZZTEST-acr"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
C: dict[str, uuid.UUID] = {}
M: dict[str, uuid.UUID] = {}  # committee_id:role -> member_id, for targeted removal
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
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"ACR{key.upper()}",
            designation="Professor" if role != UserRole.STUDENT else None,
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


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


async def _committee_status(committee_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as db:
        c = await db.get(AdvisoryCommittee, committee_id)
        return c.status


async def _download(committee_id: uuid.UUID, token):
    return await _call("GET", f"/research/committees/{committee_id}/document", token)


async def _drive_to_dpgs_approved(student_key: str, ma_key: str, minor_key: str) -> uuid.UUID:
    """Real HTTP chain: propose MA -> MA accepts -> add a Member Minor ->
    Member Minor accepts (auto-advances to hod_pending) -> HOD -> Incharge ->
    DPGS. Returns the committee id."""
    r = await _call("POST", "/research/committees", TOK["hod1"], json={
        "student_id": str(U[student_key]), "major_advisor_id": str(U[ma_key]), "research_title": "ZZTEST Revision Task Title",
    })
    assert r.status_code == 201, r.text
    cid = uuid.UUID(r.json()["id"])

    r = await _call("PATCH", f"/research/committees/{cid}/major-advisor-response", TOK[ma_key], json={"accepted": True})
    assert r.status_code == 200, r.text

    r = await _call("POST", f"/research/committees/{cid}/members", TOK[ma_key], json={
        "role": "member_minor", "faculty_id": str(U[minor_key]),
    })
    assert r.status_code == 201, r.text
    detail = (await _call("GET", f"/research/committees/student/{U[student_key]}", TOK["hod1"])).json()
    minor_member_id = next(m["id"] for m in detail["members"] if m["role"] == "member_minor")
    M[f"{student_key}:member_minor"] = uuid.UUID(minor_member_id)

    r = await _call("PATCH", f"/research/committees/{cid}/members/{minor_member_id}/accept", TOK[minor_key], params={"accepted": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(cid) == "hod_pending", await _committee_status(cid)

    r = await _call("PATCH", f"/research/committees/{cid}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{cid}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{cid}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(cid) == "dpgs_approved"
    return cid


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
            print("STOP: stray zztest_acr_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        assert len(depts) >= 2, "need at least 2 existing Department rows in the dev DB"
        S["D1"], S["D2"] = depts
        await db.commit()

    d1, d2 = S["D1"], S["D2"]
    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("hod2", UserRole.HOD, d2)  # different department — for cross-department authorization checks
    await _mk_user("ma1", UserRole.FACULTY, d1)       # original Major Advisor (d1 — student's own department)
    await _mk_user("ma2", UserRole.FACULTY, d1)       # replacement Major Advisor (also d1)
    await _mk_user("minor1", UserRole.FACULTY, d2)    # Member Minor (must be OTHER department)
    await _mk_user("new_supporting", UserRole.FACULTY, d1)  # newly added member (Supporting — no department restriction)
    await _mk_user("new_supporting2", UserRole.FACULTY, d1)  # second member added to stu2's committee before the removal scenario
    await _mk_user("outsider_faculty", UserRole.FACULTY, d1)  # not on any committee
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None)
    await _mk_user("dpgs", UserRole.DPGS, None)

    await _mk_user("stu1", UserRole.STUDENT, d1, roll=f"ZZACR-stu1-{_TAG}")  # member-add scenario
    await _mk_user("stu2", UserRole.STUDENT, d1, roll=f"ZZACR-stu2-{_TAG}")  # member-removal scenario
    await _mk_user("stu3", UserRole.STUDENT, d1, roll=f"ZZACR-stu3-{_TAG}")  # Major Advisor change scenario


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
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


# ── Build all three baseline committees (DPGS-approved) ──────────────────────

async def t_build_baseline_committees():
    C["stu1"] = await _drive_to_dpgs_approved("stu1", "ma1", "minor1")
    C["stu2"] = await _drive_to_dpgs_approved("stu2", "ma1", "minor1")
    C["stu3"] = await _drive_to_dpgs_approved("stu3", "ma1", "minor1")
    # baseline: student can download immediately after DPGS approval
    for key in ("stu1", "stu2", "stu3"):
        r = await _download(C[key], TOK[key])
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", (key, r.status_code, r.text)


# ── Scenario A: Major Advisor adds a member to a DPGS-approved committee ────

async def t_unauthorized_user_cannot_add_member():
    r = await _call("POST", f"/research/committees/{C['stu1']}/members", TOK["outsider_faculty"], json={
        "role": "supporting", "faculty_id": str(U["new_supporting"]),
    })
    assert r.status_code == 403, (r.status_code, r.text)


async def t_add_member_leaves_approved_state_and_blocks_download():
    r = await _call("POST", f"/research/committees/{C['stu1']}/members", TOK["ma1"], json={
        "role": "supporting", "faculty_id": str(U["new_supporting"]),
    })
    assert r.status_code == 201, r.text
    status = await _committee_status(C["stu1"])
    assert status == "members_pending", status  # new member not yet accepted

    r = await _download(C["stu1"], TOK["stu1"])
    assert r.status_code == 400, (r.status_code, r.text)


async def t_cannot_hod_approve_before_new_member_accepts():
    r = await _call("PATCH", f"/research/committees/{C['stu1']}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 400, (r.status_code, r.text)


async def t_new_member_acceptance_required_then_hod_then_incharge_then_dpgs():
    detail = (await _call("GET", f"/research/committees/student/{U['stu1']}", TOK["hod1"])).json()
    new_member_id = next(m["id"] for m in detail["members"] if m["role"] == "supporting")

    # download still blocked right up until the new member accepts
    r = await _download(C["stu1"], TOK["stu1"])
    assert r.status_code == 400, (r.status_code, r.text)

    r = await _call("PATCH", f"/research/committees/{C['stu1']}/members/{new_member_id}/accept", TOK["new_supporting"], params={"accepted": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(C["stu1"]) == "hod_pending"

    r = await _download(C["stu1"], TOK["stu1"])
    assert r.status_code == 400, (r.status_code, r.text)  # HOD approval still pending

    r = await _call("PATCH", f"/research/committees/{C['stu1']}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _download(C["stu1"], TOK["stu1"])
    assert r.status_code == 400, (r.status_code, r.text)  # Incharge Academic Cell approval still pending

    r = await _call("PATCH", f"/research/committees/{C['stu1']}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _download(C["stu1"], TOK["stu1"])
    assert r.status_code == 400, (r.status_code, r.text)  # DPGS approval still pending

    r = await _call("PATCH", f"/research/committees/{C['stu1']}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(C["stu1"]) == "dpgs_approved"


async def t_student_can_download_revised_document_with_new_member():
    r = await _download(C["stu1"], TOK["stu1"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    text_ = " ".join(_extract_pdf_text(r.content).split()).upper()
    assert "ZZTEST ACRNEW_SUPPORTING" in text_, "revised document must reflect the newly added member"
    assert "ZZTEST ACRMINOR1" in text_, "revised document must still include the original, untouched member"


# ── Scenario B: member removal without replacement ──────────────────────────

async def t_stu2_gets_a_second_member_before_the_removal_scenario():
    """stu2's baseline committee (from t_build_baseline_committees) has only
    ONE non-Major-Advisor member (minor1) — removing it with nothing left
    would correctly land on "member_selection" (an empty committee has
    nothing for HOD to approve yet, the same pre-existing rule that already
    blocks the ORIGINAL formation flow from auto-advancing with zero
    members). To actually exercise "restart at HOD approval" (at least one
    member remains, already accepted), add a second member first and drive
    it through its own full reapproval cycle."""
    r = await _call("POST", f"/research/committees/{C['stu2']}/members", TOK["ma1"], json={
        "role": "supporting", "faculty_id": str(U["new_supporting2"]),
    })
    assert r.status_code == 201, r.text
    detail = (await _call("GET", f"/research/committees/student/{U['stu2']}", TOK["hod1"])).json()
    new_member_id = next(m["id"] for m in detail["members"] if m["role"] == "supporting")
    r = await _call("PATCH", f"/research/committees/{C['stu2']}/members/{new_member_id}/accept", TOK["new_supporting2"], params={"accepted": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(C["stu2"]) == "hod_pending"
    r = await _call("PATCH", f"/research/committees/{C['stu2']}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{C['stu2']}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{C['stu2']}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(C["stu2"]) == "dpgs_approved"


async def t_member_removal_without_replacement_restarts_at_hod_approval():
    r = await _call("DELETE", f"/research/committees/{C['stu2']}/members/{M['stu2:member_minor']}", TOK["ma1"])
    assert r.status_code == 204, r.text
    # new_supporting2 remains and was already accepted -> no acceptance step
    # needed, lands directly on hod_pending.
    assert await _committee_status(C["stu2"]) == "hod_pending", await _committee_status(C["stu2"])

    r = await _download(C["stu2"], TOK["stu2"])
    assert r.status_code == 400, (r.status_code, r.text)

    r = await _call("PATCH", f"/research/committees/{C['stu2']}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{C['stu2']}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _download(C["stu2"], TOK["stu2"])
    assert r.status_code == 400, (r.status_code, r.text)  # still awaiting DPGS

    r = await _call("PATCH", f"/research/committees/{C['stu2']}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _download(C["stu2"], TOK["stu2"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    text_ = " ".join(_extract_pdf_text(r.content).split()).upper()
    assert "ZZTEST ACRMINOR1" not in text_, "removed member must not appear in the revised document"
    assert "ZZTEST ACRNEW_SUPPORTING2" in text_, "remaining member must still appear in the revised document"


# ── Scenario C: HOD replaces the Major Advisor ───────────────────────────────

async def t_unauthorized_hod_cannot_change_major_advisor_across_departments():
    r = await _call("POST", f"/research/committees/{C['stu3']}/reassign-major-advisor", TOK["hod2"], json={"major_advisor_id": str(U["ma2"])})
    assert r.status_code == 403, (r.status_code, r.text)


async def t_hod_initiates_ma_change_does_not_auto_approve_and_blocks_download():
    r = await _call("POST", f"/research/committees/{C['stu3']}/reassign-major-advisor", TOK["hod1"], json={"major_advisor_id": str(U["ma2"])})
    assert r.status_code == 200, r.text
    status = await _committee_status(C["stu3"])
    assert status == "major_advisor_pending", f"HOD's initiation must NOT auto-approve the revised committee — got {status}"

    r = await _download(C["stu3"], TOK["stu3"])
    assert r.status_code == 400, (r.status_code, r.text)


async def t_new_major_advisor_must_accept_then_hod_must_explicitly_approve():
    # before acceptance, HOD approval is not even reachable
    r = await _call("PATCH", f"/research/committees/{C['stu3']}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 400, (r.status_code, r.text)

    r = await _call("PATCH", f"/research/committees/{C['stu3']}/major-advisor-response", TOK["ma2"], json={"accepted": True})
    assert r.status_code == 200, r.text
    # existing Member Minor was already accepted and untouched -> skips
    # straight to hod_pending, no member re-acceptance required.
    assert await _committee_status(C["stu3"]) == "hod_pending"

    r = await _download(C["stu3"], TOK["stu3"])
    assert r.status_code == 400, (r.status_code, r.text)  # HOD has not explicitly approved yet

    r = await _call("PATCH", f"/research/committees/{C['stu3']}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 400, f"Incharge must not be reachable before explicit HOD approval — got {r.status_code} {r.text}"


async def t_ma_change_requires_incharge_and_dpgs_then_download_allowed():
    r = await _call("PATCH", f"/research/committees/{C['stu3']}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _download(C["stu3"], TOK["stu3"])
    assert r.status_code == 400, (r.status_code, r.text)

    r = await _call("PATCH", f"/research/committees/{C['stu3']}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _download(C["stu3"], TOK["stu3"])
    assert r.status_code == 400, (r.status_code, r.text)

    r = await _call("PATCH", f"/research/committees/{C['stu3']}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(C["stu3"]) == "dpgs_approved"

    r = await _download(C["stu3"], TOK["stu3"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    text_ = " ".join(_extract_pdf_text(r.content).split()).upper()
    assert "ZZTEST ACRMA2" in text_, "revised document must show the NEW Major Advisor"
    assert "ZZTEST ACRMA1" not in text_, "revised document must not show the REPLACED Major Advisor"


# ── Cross-cutting: IDOR / authorization on the document endpoint itself ─────

async def t_student_cannot_download_another_students_revised_document():
    r = await _download(C["stu1"], TOK["stu2"])
    assert r.status_code == 403, (r.status_code, r.text)
    r2 = await _download(C["stu3"], TOK["stu1"])
    assert r2.status_code == 403, (r2.status_code, r2.text)


async def t_unauthorized_role_and_unknown_id_rejected():
    r = await _call("GET", f"/research/committees/{C['stu1']}/document", TOK["outsider_faculty"])
    assert r.status_code == 403, (r.status_code, r.text)
    r2 = await _call("GET", f"/research/committees/{uuid.uuid4()}/document", TOK["stu1"])
    assert r2.status_code == 404, (r2.status_code, r2.text)


async def main() -> None:
    await _setup()
    try:
        cases = {
            "Build baseline DPGS-approved committees for stu1/stu2/stu3": t_build_baseline_committees,
            "Unauthorized faculty cannot add a committee member": t_unauthorized_user_cannot_add_member,
            "1-2: Member add leaves dpgs_approved and blocks download": t_add_member_leaves_approved_state_and_blocks_download,
            "4: cannot HOD-approve before the new member accepts": t_cannot_hod_approve_before_new_member_accepts,
            "3,5,6,7: new member accepts -> HOD -> Incharge -> DPGS, download blocked throughout": t_new_member_acceptance_required_then_hod_then_incharge_then_dpgs,
            "9: student downloads the revised document (shows new + original members)": t_student_can_download_revised_document_with_new_member,
            "Give stu2's committee a second member before the removal scenario": t_stu2_gets_a_second_member_before_the_removal_scenario,
            "10: member removal without replacement restarts at HOD approval": t_member_removal_without_replacement_restarts_at_hod_approval,
            "Major Advisor change: unauthorized cross-department HOD rejected": t_unauthorized_hod_cannot_change_major_advisor_across_departments,
            "1: HOD initiates MA change — does not auto-approve, blocks download": t_hod_initiates_ma_change_does_not_auto_approve_and_blocks_download,
            "2,3,4: new MA must accept; HOD must explicitly approve (not skippable)": t_new_major_advisor_must_accept_then_hod_must_explicitly_approve,
            "5,6,7: Incharge + DPGS mandatory, then download allowed with the new MA": t_ma_change_requires_incharge_and_dpgs_then_download_allowed,
            "8: student cannot download another student's (revised) document": t_student_cannot_download_another_students_revised_document,
            "Unauthorized role / unknown committee id rejected on the document endpoint": t_unauthorized_role_and_unknown_id_rejected,
        }
        for name, fn in cases.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_ACR user / session row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
