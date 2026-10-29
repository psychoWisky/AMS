"""Standalone HTTP-level tests for the Advisory Committee document task:
`GET /research/committees/{id}/document` — the official PDF generated once a
committee reaches `dpgs_approved`, downloadable only by its own student and
the roles already authorized by `_authorize_committee_view` (reused
unchanged).

Modelled on `tests/test_ppw_research_title_sync.py` (real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `record`/`_run` pass-fail
tracker, `zztest_acd_...`-prefixed rows, full teardown) and
`tests/test_progress_report.py` (direct-DB `AdvisoryCommittee`/
`CommitteeMember` fixtures at a chosen status, the established convention
for pre-built committee test fixtures in this codebase, plus its
single-holder-role STOP guard for INCHARGE_ACADEMIC_CELL/DPGS).

Committee `doc1` is driven through the REAL HTTP approval chain end-to-end
(propose -> accept -> members -> HOD -> Incharge -> DPGS) so the happy-path
PDF-content assertions exercise the actual production code path, not just a
hand-built DB row. `doc2`/`doc3` are direct-DB fixtures at non-final statuses,
used only for the negative/authorization cases, mirroring the existing
`_mk_committee`-style convention.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_advisory_committee_document
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
_PFX = "zztest_acd_"
_DEVICE = "ZZTEST-acd"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
C: dict[str, uuid.UUID] = {}
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


async def _mk_user(key, role, dept, *, designation=None, roll=None) -> None:
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"ACD{key.upper()}",
            designation=designation, role=role, department_id=dept.id if dept else None,
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
    """A generated PDF's text lives in compressed content streams — a raw byte/latin-1 substring
    search over the file will almost never find it. Use pypdf's real text extraction instead."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


async def _committee_status(committee_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as db:
        c = await db.get(AdvisoryCommittee, committee_id)
        return c.status


async def _mk_fixture_committee(student_key: str, status: str) -> uuid.UUID:
    """Direct-DB fixture committee at a chosen non-final status — the
    established convention for pre-built committee fixtures in this test
    suite (see `test_progress_report.py::_mk_committee`); only the real
    `doc1` chain below needs to exercise the live endpoints."""
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST research title", status=status)
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U["ma"], role="major_advisor", accepted=True))
        await db.commit()
        return c.id


async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        # Incharge Academic Cell / DPGS are single-holder roles (unique partial
        # index) — refuse to run if a real office holder already exists, same
        # guard as test_progress_report.py.
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS holder exists (single-holder roles); refusing to run.")
            sys.exit(2)
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_acd_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(2))).scalars().all()
        assert len(depts) >= 2, "need at least 2 existing Department rows in the dev DB"
        S["D1"], S["D2"] = depts
        await db.commit()

    d1, d2 = S["D1"], S["D2"]
    await _mk_user("hod", UserRole.HOD, d1, designation="Professor")
    await _mk_user("ma", UserRole.FACULTY, d1, designation="Professor")
    # member_minor deliberately has NO designation set — exercises the
    # template's "missing optional data" handling (renders "—", never crashes).
    await _mk_user("member_minor", UserRole.FACULTY, d2, designation=None)
    await _mk_user("outsider_faculty", UserRole.FACULTY, d1, designation="Assistant Professor")  # not on any committee
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None, designation="Professor")
    await _mk_user("dpgs", UserRole.DPGS, None, designation="Director of PG Studies")
    await _mk_user("super_admin_probe", UserRole.SUPER_ADMIN, None)  # a FRESH super admin, independent of any seeded one

    await _mk_user("doc1", UserRole.STUDENT, d1, roll=f"ZZACD-doc1-{_TAG}")
    await _mk_user("doc2", UserRole.STUDENT, d1, roll=f"ZZACD-doc2-{_TAG}")
    await _mk_user("doc3", UserRole.STUDENT, d1, roll=f"ZZACD-doc3-{_TAG}")


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


# ── Build doc1's committee through the REAL approval chain ──────────────────

async def t_drive_doc1_committee_to_dpgs_approved():
    r = await _call("POST", "/research/committees", TOK["hod"], json={
        "student_id": str(U["doc1"]), "major_advisor_id": str(U["ma"]), "research_title": "ZZTEST Document Task Title",
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    C["doc1"] = uuid.UUID(cid)

    r = await _call("PATCH", f"/research/committees/{cid}/major-advisor-response", TOK["ma"], json={"accepted": True})
    assert r.status_code == 200, r.text

    r = await _call("POST", f"/research/committees/{cid}/members", TOK["ma"], json={
        "role": "member_minor", "faculty_id": str(U["member_minor"]),
    })
    assert r.status_code == 201, r.text

    r = await _call("POST", f"/research/committees/{cid}/members", TOK["ma"], json={
        "role": "member_of_others", "external_name": "Dr. ZZTEST External",
        "external_designation": "Professor", "external_institute": "ZZTEST External Institute",
    })
    assert r.status_code == 201, r.text

    member_minor_id = next(m["id"] for m in (await _call("GET", f"/research/committees/student/{U['doc1']}", TOK["hod"])).json()["members"] if m["role"] == "member_minor")
    r = await _call("PATCH", f"/research/committees/{cid}/members/{member_minor_id}/accept", TOK["member_minor"], params={"accepted": True})
    assert r.status_code == 200, r.text

    assert await _committee_status(C["doc1"]) == "hod_pending", await _committee_status(C["doc1"])

    r = await _call("PATCH", f"/research/committees/{cid}/hod-approval", TOK["hod"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{cid}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    r = await _call("PATCH", f"/research/committees/{cid}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _committee_status(C["doc1"]) == "dpgs_approved"


# ── Fixture committees for negative/authorization cases ─────────────────────

async def t_build_fixture_committees():
    C["doc2"] = await _mk_fixture_committee("doc2", "members_pending")  # not yet HOD-approved
    C["doc3"] = await _mk_fixture_committee("doc3", "reverted")          # terminal non-approved state


# ── 1/2/3/4/5/6: a dpgs_approved committee produces a correct, clean PDF ────

async def t_dpgs_approved_committee_downloads_valid_pdf():
    r = await _call("GET", f"/research/committees/{C['doc1']}/document", TOK["doc1"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    S["doc1_pdf_text"] = _extract_pdf_text(r.content)
    assert len(r.content) > 500, "PDF response unexpectedly tiny"


async def t_pdf_contains_correct_institutional_identity_and_address():
    text_ = S["doc1_pdf_text"]
    assert "assam veterinary and fishery university" in text_.lower(), "missing correct institutional identity"
    assert "khanapara" in text_.lower() and "781022" in text_, "missing AVFU's established address"


async def t_pdf_contains_correct_student_and_committee_information():
    # pypdf's text extraction follows the rendered table's line-wrapping, so a
    # long cell value can come back with a newline where the PDF only shows a
    # soft wrap (e.g. "ZZTEST\nACDMEMBER_MINOR") — collapse all whitespace
    # before asserting so this is checked on CONTENT, not on incidental PDF
    # line breaks.
    text_ = " ".join(S["doc1_pdf_text"].split()).upper()
    assert "ZZTEST ACDDOC1" in text_, "missing student's real name"
    assert f"ZZACD-DOC1-{_TAG}".upper() in text_, "missing student's real roll number"
    assert "ZZTEST ACDMA" in text_, "missing Major Advisor's real name"
    assert "ZZTEST ACDMEMBER_MINOR" in text_, "missing internal member's real name"
    assert "ZZTEST EXTERNAL" in text_, "missing external member's real name"
    assert "ZZTEST EXTERNAL INSTITUTE" in text_, "missing external member's institute"


async def t_pdf_does_not_contain_jorhat():
    assert "jorhat" not in S["doc1_pdf_text"].lower()


async def t_pdf_does_not_contain_excluded_form_number_or_clause_text():
    text_ = S["doc1_pdf_text"].lower()
    for excluded in ("pg-07", "pg-11", "vide clause", "2.06.01", "2.06.04", "academic regulation form"):
        assert excluded not in text_, f"excluded form/clause text leaked: {excluded!r}"


async def t_pdf_does_not_contain_aau_branding():
    assert "AAU" not in S["doc1_pdf_text"].upper().replace("ASSAM VETERINARY AND FISHERY UNIVERSITY", "")


# ── 7/8: student download — own document ok, cross-student IDOR blocked ─────

async def t_student_can_download_own_committee_document():
    r = await _call("GET", f"/research/committees/{C['doc1']}/document", TOK["doc1"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text


async def t_student_cannot_download_another_students_committee_document():
    r = await _call("GET", f"/research/committees/{C['doc1']}/document", TOK["doc2"])
    assert r.status_code == 403, (r.status_code, r.text)
    r2 = await _call("GET", f"/research/committees/{C['doc2']}/document", TOK["doc1"])
    assert r2.status_code == 403, (r2.status_code, r2.text)


# ── 9/10: pre-final-approval and reverted committees are rejected ───────────

async def t_student_cannot_download_before_dpgs_final_approval():
    r = await _call("GET", f"/research/committees/{C['doc2']}/document", TOK["doc2"])
    assert r.status_code == 400, (r.status_code, r.text)


async def t_reverted_committee_document_rejected():
    r = await _call("GET", f"/research/committees/{C['doc3']}/document", TOK["doc3"])
    assert r.status_code == 400, (r.status_code, r.text)


# ── 11: existing authorization rules remain effective for other roles ───────

async def t_hod_in_same_department_can_download():
    r = await _call("GET", f"/research/committees/{C['doc1']}/document", TOK["hod"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text


async def t_super_admin_can_download():
    r = await _call("GET", f"/research/committees/{C['doc1']}/document", TOK["super_admin_probe"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text


async def t_faculty_not_on_committee_cannot_download():
    r = await _call("GET", f"/research/committees/{C['doc1']}/document", TOK["outsider_faculty"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_unknown_committee_id_is_404():
    r = await _call("GET", f"/research/committees/{uuid.uuid4()}/document", TOK["doc1"])
    assert r.status_code == 404, (r.status_code, r.text)


# ── 12: missing/incomplete optional data does not break generation ──────────
# (already exercised above — member_minor has no `designation`, and the
# committee has no `research_area`/college at all; t_dpgs_approved_committee_
# downloads_valid_pdf already proves generation succeeds regardless.)

async def t_missing_optional_designation_does_not_break_generation():
    assert "—" in S["doc1_pdf_text"] or "member_minor".upper() not in S["doc1_pdf_text"].upper(), \
        "sanity check only — real assertion is that generation above did not raise/500"


async def main() -> None:
    await _setup()
    try:
        cases = {
            "Drive doc1's committee through the real HOD->Incharge->DPGS chain to dpgs_approved": t_drive_doc1_committee_to_dpgs_approved,
            "Build doc2 (members_pending) / doc3 (reverted) fixture committees": t_build_fixture_committees,
            "1: dpgs_approved committee produces a valid downloadable PDF": t_dpgs_approved_committee_downloads_valid_pdf,
            "2: PDF contains correct institutional identity and address": t_pdf_contains_correct_institutional_identity_and_address,
            "3: PDF contains correct student and committee information": t_pdf_contains_correct_student_and_committee_information,
            "4: PDF does not contain Jorhat": t_pdf_does_not_contain_jorhat,
            "5: PDF does not contain excluded form number / clause text": t_pdf_does_not_contain_excluded_form_number_or_clause_text,
            "6: PDF does not contain AAU branding": t_pdf_does_not_contain_aau_branding,
            "7: student can download their own committee document": t_student_can_download_own_committee_document,
            "8: student cannot download another student's committee document": t_student_cannot_download_another_students_committee_document,
            "9: cannot download before DPGS final approval": t_student_cannot_download_before_dpgs_final_approval,
            "10: reverted committee document is rejected": t_reverted_committee_document_rejected,
            "11a: HOD in the same department can download": t_hod_in_same_department_can_download,
            "11b: Super Admin can download": t_super_admin_can_download,
            "11c: Faculty not on the committee cannot download": t_faculty_not_on_committee_cannot_download,
            "11d: unknown committee id is 404": t_unknown_committee_id_is_404,
            "12: missing optional data does not break generation": t_missing_optional_designation_does_not_break_generation,
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
        record("cleanup: no ZZTEST_ACD user / session row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
