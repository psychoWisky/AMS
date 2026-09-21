"""Standalone HTTP-level tests for the First Synopsis module (BUSINESS_LOGIC.md section AB).

Real FastAPI app via `httpx.ASGITransport`, real JWT sessions (including sessions pinned to one specific
role assignment for the multi-role tests), real Chromium PDF generation. Everything created is
`ZZTEST_SYNOPSIS...` / `zztest_synopsis_...` and removed in `finally`, including the uploaded/frozen PDFs
under `uploads/synopsis/`. Real students, committees and roles are never touched. If a real Incharge Academic
Cell or DPGS holder already exists (the roles are single-holder), the run stops instead of interfering.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_synopsis
"""
import asyncio
import hashlib
import io
import shutil
import sys
import uuid
from pathlib import Path

import httpx
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A3, A4, LETTER, landscape
from reportlab.pdfgen import canvas
from sqlalchemy import delete, func, select, text

from app.api.v1.endpoints import synopsis as synopsis_module
from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.audit import AuditLog
from app.models.course import Course
from app.models.ppw import Ppw, PpwCourse
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.synopsis import Synopsis, SynopsisApprovalCycle, SynopsisApprovalStage, SynopsisFile, SynopsisSignature
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment
from app.utils.pdf_merge import is_a4

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_synopsis_"
_LABEL = "ZZTEST_SYNOPSIS"
_DEVICE = "ZZTEST-synopsis"
_UPLOAD_ROOT = Path(settings.UPLOAD_DIR) / "synopsis"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
A: dict[str, uuid.UUID] = {}          # assignment ids, "key:ROLE"
TOK: dict[str, str] = {}
S: dict = {}
SYN: dict[str, str] = {}              # student key -> synopsis id
UPLOADED_TEXT = "ZZTEST UPLOADED BODY"


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


def _pdf(pages: int = 1, text_: str = UPLOADED_TEXT, size=A4) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for i in range(pages):
        c.drawString(72, size[1] - 100, f"{text_} page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _encrypted_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.encrypt("secret")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _pages_text(pdf: bytes) -> list[str]:
    return [" ".join((p.extract_text() or "").split()) for p in PdfReader(io.BytesIO(pdf)).pages]


def _all_text(pdf: bytes) -> str:
    return " ".join(_pages_text(pdf))


async def _mk_user(key, legacy_role, dept, assignments, *, program=None, college=None, roll=None, title=None, designation=None, active=True):
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"SYN{key.upper()}", title=title, designation=designation,
                 role=legacy_role, department_id=dept.id if dept else None, program_id=program.id if program else None,
                 college_id=college, student_roll=roll, mobile="9876543210", gender="Male", is_active=active, is_verified=True)
        db.add(u)
        await db.flush()
        U[key] = u.id
        for role, adept in assignments:
            a = UserRoleAssignment(user_id=u.id, role=role, department_id=adept.id if adept else None)
            db.add(a)
            await db.flush()
            A[f"{key}:{role.value}" + (f":{adept.code}" if adept else "")] = a.id
        await db.commit()


async def _mk_committee(student_key, members):
    """members: [(user_key, committee_role)] — all accepted unless the role string ends with '?'."""
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST committee", status="hod_approved")
        db.add(c)
        await db.flush()
        S[f"committee:{student_key}"] = c.id
        for i, (ukey, role) in enumerate(members):
            pending = role.endswith("?")
            m = CommitteeMember(committee_id=c.id, faculty_id=U[ukey], role=role.rstrip("?"), accepted=None if pending else True)
            db.add(m)
            await db.flush()
            S[f"member:{student_key}:{ukey}"] = m.id
        await db.commit()


async def _reset_committee(student_key, members, status="hod_approved", reverted=False):
    """Rebuild a ZZTEST student's committee members (all accepted) and set its status directly in the database."""
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as db:
        cid = S[f"committee:{student_key}"]
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id == cid))
        for ukey, role in members:
            m = CommitteeMember(committee_id=cid, faculty_id=U[ukey], role=role, accepted=True)
            db.add(m)
            await db.flush()
            S[f"member:{student_key}:{ukey}"] = m.id
        c = await db.get(AdvisoryCommittee, cid)
        c.status, c.reverted_at = status, (datetime.now(timezone.utc) if reverted else None)
        await db.commit()


async def _committee_state(student_key):
    async with AsyncSessionLocal() as db:
        c = await db.get(AdvisoryCommittee, S[f"committee:{student_key}"])
        members = (await db.execute(select(CommitteeMember.faculty_id, CommitteeMember.role, CommitteeMember.accepted).where(CommitteeMember.committee_id == c.id))).all()
        return c.status, sorted((str(f), r, a) for f, r, a in members)


async def _sign(sid, token):
    r = await _call("GET", f"/synopsis/{sid}/approval/otp", token)
    if r.status_code != 200:
        return r          # refused at the OTP step (e.g. 403): the caller inspects it
    return await _call("POST", f"/synopsis/{sid}/approval/approve", token, json={"otp": r.json()["dev_otp"]})


async def _approve_ok(sid, token, note=""):
    r = await _sign(sid, token)
    assert r.status_code == 200, f"{note} approve -> {r.status_code} {r.text}"
    return r.json()


async def _incharge_approve(sid, token):
    r = await _call("POST", f"/synopsis/{sid}/approval/approve", token, json={})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _detail(sid, token) -> dict:
    r = await _call("GET", f"/synopsis/{sid}", token)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _prepare(student_key, title="ZZTEST research problem", pdf=None) -> str:
    """create -> title -> upload; returns the synopsis id (does not submit)."""
    r = await _call("POST", "/synopsis", TOK[student_key], json={})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    SYN[student_key] = sid
    assert (await _call("PATCH", f"/synopsis/{sid}", TOK[student_key], json={"title": title})).status_code == 200
    r = await _call("POST", f"/synopsis/{sid}/file", TOK[student_key], files={"file": ("mysyn.pdf", pdf or _pdf(2), "application/pdf")})
    assert r.status_code == 201, r.text
    return sid


async def _submit(student_key, expect=200):
    r = await _call("PATCH", f"/synopsis/{SYN[student_key]}/submit", TOK[student_key])
    assert r.status_code == expect, (r.status_code, r.text)
    return r


async def _row(sid) -> Synopsis:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Synopsis).where(Synopsis.id == uuid.UUID(sid)))).scalar_one()


async def _cycles(sid) -> list[SynopsisApprovalCycle]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(SynopsisApprovalCycle).where(SynopsisApprovalCycle.synopsis_id == uuid.UUID(sid)).order_by(SynopsisApprovalCycle.cycle_number))).scalars().all())


async def _stages(cycle_id) -> list[SynopsisApprovalStage]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(SynopsisApprovalStage).where(SynopsisApprovalStage.cycle_id == cycle_id).order_by(SynopsisApprovalStage.sequence))).scalars().all())


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    synopsis_module.send_email = lambda to, subject, body: True     # never send real email
    settings.ENVIRONMENT = "development"                            # dev_otp available to the tests
    async with AsyncSessionLocal() as db:
        holders = (await db.execute(text("select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS')"))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS holder exists (single-holder roles); refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(3))).scalars().all()
        S["D1"], S["D2"], S["D3"] = depts
        S["LPM"] = (await db.execute(select(Department).where(Department.code == "LPM"))).scalar_one()
        S["P"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        ug = Program(name=f"{_LABEL} UG Programme", code=f"ZZSYN{_TAG.upper()}"[:20], level="UG")
        col = College(name=f"{_LABEL} College", code=f"ZZSYN{_TAG.upper()}"[:20])
        db.add_all([ug, col])
        await db.flush()
        S["UG"], S["COLLEGE"] = ug, col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, d2 = S["D1"], S["D2"]
    F, H, ST = UserRole.FACULTY, UserRole.HOD, UserRole.STUDENT
    inc, dpgs = UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS
    for k in ("s1", "s2", "s3", "s4", "s5", "s6", "s7"):
        await _mk_user(k, ST, d1, [(ST, None)], program=S["P"], college=S["COLLEGE"].id, roll=f"ZZTEST-SYN-{k}-{_TAG}")
    await _mk_user("sug", ST, d1, [(ST, None)], program=S["UG"], college=S["COLLEGE"].id, roll=f"ZZTEST-SYN-ug-{_TAG}")
    await _mk_user("ma", F, d1, [(F, d1)], title="Dr.", designation="Professor")
    await _mk_user("co", F, d1, [(F, d1)], title="Dr.", designation="Associate Professor")
    await _mk_user("mm", F, d1, [(F, d1)], title="Dr.", designation="Assistant Professor")
    await _mk_user("mn", F, d2, [(F, d2)], title="Dr.", designation="Professor")
    await _mk_user("sp", F, d1, [(F, d1)], title="Dr.", designation="Professor")
    await _mk_user("ot1", F, d2, [(F, d2)], title="Dr.", designation="Professor")
    await _mk_user("ot2", F, d2, [(F, d2)], title="Dr.", designation="Associate Professor")
    await _mk_user("x", F, d1, [(F, d1)], title="Dr.", designation="Professor")                      # on no committee
    await _mk_user("inactive", F, d1, [(F, d1)], title="Dr.", designation="Professor", active=False)
    await _mk_user("hod1", H, d1, [(H, d1)], title="Dr.", designation="Professor")
    await _mk_user("hod2", H, d2, [(H, d2)], title="Dr.", designation="Professor")
    await _mk_user("mh", H, d1, [(H, d1), (H, d2)], title="Dr.", designation="Professor")            # HOD @ D1 and HOD @ D2
    await _mk_user("incfac", F, d1, [(F, d1), (inc, None)], title="Dr.", designation="Professor")   # faculty AND Incharge
    await _mk_user("dpgs", dpgs, None, [(dpgs, None)], title="Dr.", designation="Director of PG Studies")

    # S1: full committee incl. a legacy Co-Major and TWO 'members from others'; S2/S5: MA + INCFAC; S3: none; S4: pending member; S6: inactive member
    await _mk_committee("s1", [("ma", "major_advisor"), ("co", "co_major_advisor"), ("mm", "member_major"), ("mn", "member_minor"),
                               ("sp", "supporting"), ("ot1", "member_of_others"), ("ot2", "member_of_others")])
    await _mk_committee("s2", [("ma", "major_advisor"), ("incfac", "member_major")])
    await _mk_committee("s5", [("ma", "major_advisor"), ("incfac", "member_major")])
    await _mk_committee("s4", [("ma", "major_advisor"), ("mm", "member_major?")])
    await _mk_committee("s6", [("ma", "major_advisor"), ("inactive", "member_major")])
    await _mk_committee("s7", [("ma", "major_advisor"), ("mm", "member_major")])

    # S1's PPW: Minor = D2, Supporting = LPM + D3  (Major = S1's own department D1)
    async with AsyncSessionLocal() as db:
        courses = {}
        for key, dept in (("minor", S["D2"]), ("sup_lpm", S["LPM"]), ("sup_other", S["D3"])):
            c = Course(course_number=f"ZZTEST-SYN-{key}-{_TAG}", title=f"{_LABEL} {key}", department_id=dept.id, credit_theory=2, credit_practical=0, program_level="PG")
            db.add(c)
            await db.flush()
            courses[key] = c
        ppw = Ppw(student_id=U["s1"], status="draft")
        db.add(ppw)
        await db.flush()
        db.add_all([
            PpwCourse(ppw_id=ppw.id, course_id=courses["minor"].id, classification="minor", sl_no=1),
            PpwCourse(ppw_id=ppw.id, course_id=courses["sup_lpm"].id, classification="supporting", sl_no=1),
            PpwCourse(ppw_id=ppw.id, course_id=courses["sup_other"].id, classification="supporting", sl_no=2),
        ])
        await db.commit()

    S["SA"] = await _session(admin_id)
    for k in ("s1", "s2", "s3", "s4", "s5", "s6", "s7", "sug", "ma", "co", "mm", "mn", "sp", "ot1", "ot2", "x", "hod1", "hod2", "dpgs"):
        TOK[k] = await _session(U[k])
    TOK["incfac:faculty"] = await _session(U["incfac"], A[f"incfac:{UserRole.FACULTY.value}:{d1.code}"])
    TOK["incfac:incharge"] = await _session(U["incfac"], A[f"incfac:{UserRole.INCHARGE_ACADEMIC_CELL.value}"])
    TOK["mh:d1"] = await _session(U["mh"], A[f"mh:{UserRole.HOD.value}:{S['D1'].code}"])
    TOK["mh:d2"] = await _session(U["mh"], A[f"mh:{UserRole.HOD.value}:{S['D2'].code}"])
    S["hod1_token"] = TOK["hod1"]


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"{_PFX}%"))
        sids = (await db.execute(select(Synopsis.id).where(Synopsis.student_id.in_(uids)))).scalars().all()
        if sids:
            await db.execute(delete(AuditLog).where(AuditLog.entity_id.in_([str(i) for i in sids])))
        await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(uids)))
        await db.execute(delete(Synopsis).where(Synopsis.student_id.in_(uids)))       # cascades cycles/stages/signatures; files after cycles
        await db.execute(delete(SynopsisFile).where(SynopsisFile.synopsis_id.in_(sids or [uuid.uuid4()])))
        ppw_ids = select(Ppw.id).where(Ppw.student_id.in_(uids))
        await db.execute(delete(PpwCourse).where(PpwCourse.ppw_id.in_(ppw_ids)))
        await db.execute(delete(Ppw).where(Ppw.student_id.in_(uids)))
        await db.execute(delete(Course).where(Course.course_number.like(f"ZZTEST-SYN-%-{_TAG}")))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.execute(delete(Program).where(Program.name.like(f"{_LABEL}%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()
        for sid in sids:
            shutil.rmtree(_UPLOAD_ROOT / str(sid), ignore_errors=True)
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
        idx = (await db.execute(text("select indexdef from pg_indexes where indexname='uq_synopsis_first_per_student'"))).scalar_one()
        assert "UNIQUE" in idx and "synopsis_type" in idx and "'first'" in idx, idx
        assert (await db.execute(text("select indexdef from pg_indexes where indexname='uq_synopsis_one_active_cycle'"))).scalar_one()
        for t in ("ams_synopses", "ams_synopsis_files", "ams_synopsis_approval_cycles", "ams_synopsis_approval_stages", "ams_synopsis_signatures"):
            assert (await db.execute(text("select count(*) from information_schema.tables where table_name=:t"), {"t": t})).scalar_one() == 1, t


async def t_create_and_uniqueness():
    sid = (await _call("POST", "/synopsis", TOK["s1"], json={"title": "  ZZTEST first title  "}))
    assert sid.status_code == 201, sid.text
    SYN["s1"] = sid.json()["id"]
    d = await _detail(SYN["s1"], TOK["s1"])
    assert d["synopsis_type"] == "first" and d["status"] == "draft" and d["title"] == "ZZTEST first title" and d["can_edit"] is True
    # a second First Synopsis is refused — friendly 409, and the DB itself refuses it too
    r = await _call("POST", "/synopsis", TOK["s1"], json={})
    assert r.status_code == 409, r.text
    from sqlalchemy.exc import IntegrityError
    try:
        async with AsyncSessionLocal() as db:
            db.add(Synopsis(student_id=U["s1"], synopsis_type="first", status="draft"))
            await db.commit()
        raise AssertionError("the database accepted a second First Synopsis")
    except IntegrityError:
        pass
    # ... but the design leaves room for a future Revise Synopsis (different type is outside the constraint)
    async with AsyncSessionLocal() as db:
        rev = Synopsis(student_id=U["s1"], synopsis_type="revise", status="draft", parent_synopsis_id=uuid.UUID(SYN["s1"]))
        db.add(rev)
        await db.commit()
        await db.execute(delete(Synopsis).where(Synopsis.id == rev.id))
        await db.commit()
    # tampering: no student/type/status can be supplied
    for bad in ({"student_id": str(U["s2"])}, {"synopsis_type": "revise"}, {"status": "approved"}):
        assert (await _call("POST", "/synopsis", TOK["s2"], json=bad)).status_code == 422, bad
    assert not (await _call("GET", "/synopsis/me", TOK["s2"])).status_code == 200
    # another student cannot see or touch S1's synopsis, whatever id they use
    assert (await _call("GET", f"/synopsis/{SYN['s1']}", TOK["s2"])).status_code == 404
    assert (await _call("PATCH", f"/synopsis/{SYN['s1']}", TOK["s2"], json={"title": "hijack"})).status_code == 404
    assert (await _call("PATCH", f"/synopsis/{SYN['s1']}/submit", TOK["s2"])).status_code == 404
    # non-students and non-postgraduates
    assert (await _call("POST", "/synopsis", TOK["ma"], json={})).status_code == 403
    assert (await _call("POST", "/synopsis", TOK["sug"], json={})).status_code == 403
    assert (await _call("POST", "/synopsis", None, json={})).status_code in (401, 403)
    assert (await _row(SYN["s1"])).title == "ZZTEST first title"


async def t_upload_validation():
    sid = SYN["s1"]
    up = lambda name, data, ct="application/pdf", tok=None: _call("POST", f"/synopsis/{sid}/file", tok or TOK["s1"], files={"file": (name, data, ct)})
    docx = b"PK\x03\x04" + b"0" * 200
    assert (await up("s.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")).status_code == 400
    assert (await up("s.doc", b"\xd0\xcf\x11\xe0" + b"0" * 100, "application/msword")).status_code == 400
    assert (await up("s.txt", _pdf(1), "text/plain")).status_code == 400, "a PDF renamed .txt is not accepted"
    assert (await up("s.pdf", _pdf(1), "image/png")).status_code == 400, "wrong declared content type"
    assert (await up("s.pdf", b"\x89PNG\r\n\x1a\n" + b"0" * 200, "application/pdf")).status_code == 400, "PNG bytes named .pdf"
    assert (await up("s.pdf", b"just some text", "application/pdf")).status_code == 400
    assert (await up("s.pdf", b"%PDF-1.4\nthis is not really a pdf at all\n%%EOF", "application/pdf")).status_code == 400, "magic bytes alone are not enough"
    assert (await up("s.pdf", b"", "application/pdf")).status_code == 400
    assert (await up("s.pdf", _encrypted_pdf(), "application/pdf")).status_code == 400, "encrypted PDFs are rejected"
    big = b"%PDF-1.4\n" + b"0" * (settings.MAX_FILE_SIZE_MB * 1024 * 1024)
    assert (await up("big.pdf", big, "application/pdf")).status_code == 413, "oversized"
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(SynopsisFile).where(SynopsisFile.synopsis_id == uuid.UUID(sid)))).scalar_one() == 0, "nothing stored for rejected uploads"
    # valid upload: Letter-size, 3 pages
    r = await up("My Synopsis (final).pdf", _pdf(3, size=LETTER))
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["file"]["version"] == 1 and d["file"]["page_count"] == 3 and d["file"]["original_filename"] == "My Synopsis (final).pdf"
    assert "stored_filename" not in str(d) and "uploads" not in str(d), "no filesystem path is exposed"
    # replacing creates version 2 and removes the unreferenced version 1 from disk and DB
    files_before = sorted(p.name for p in (_UPLOAD_ROOT / sid).iterdir())
    r = await up("v2.pdf", _pdf(1, "ZZTEST V2 BODY", A4))
    assert r.status_code == 201 and r.json()["file"]["version"] == 2
    files_after = sorted(p.name for p in (_UPLOAD_ROOT / sid).iterdir())
    assert len(files_after) == 1 and files_after != files_before, (files_before, files_after)
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(SynopsisFile).where(SynopsisFile.synopsis_id == uuid.UUID(sid)))).scalar_one() == 1
    # only the owner can upload
    assert (await up("x.pdf", _pdf(1), tok=TOK["s2"])).status_code == 404
    assert (await up("x.pdf", _pdf(1), tok=TOK["ma"])).status_code == 403
    assert (await up("x.pdf", _pdf(1), tok=TOK["hod1"])).status_code == 403
    # put the 3-page Letter file back as the working version
    assert (await up("My Synopsis (final).pdf", _pdf(3, size=LETTER))).status_code == 201


async def t_submit_preconditions():
    sid = SYN["s1"]
    # no title -> blank the title and try
    assert (await _call("PATCH", f"/synopsis/{sid}", TOK["s1"], json={"title": "   "})).status_code == 200
    r = await _call("PATCH", f"/synopsis/{sid}/submit", TOK["s1"])
    assert r.status_code == 400 and "Title" in r.json()["detail"], r.text
    assert (await _call("PATCH", f"/synopsis/{sid}", TOK["s1"], json={"title": "ZZTEST Prevalence and therapeutic management study"})).status_code == 200
    # no file
    await _prepare("s3")
    async with AsyncSessionLocal() as db:   # remove S3's file to test the missing-file rule
        await db.execute(delete(SynopsisFile).where(SynopsisFile.synopsis_id == uuid.UUID(SYN["s3"])))
        await db.commit()
    r = await _call("PATCH", f"/synopsis/{SYN['s3']}/submit", TOK["s3"])
    assert r.status_code == 400 and "PDF" in r.json()["detail"], r.text
    assert (await _call("POST", f"/synopsis/{SYN['s3']}/file", TOK["s3"], files={"file": ("a.pdf", _pdf(1), "application/pdf")})).status_code == 201
    # no advisory committee
    r = await _call("PATCH", f"/synopsis/{SYN['s3']}/submit", TOK["s3"])
    assert r.status_code == 400 and "Advisory Committee" in r.json()["detail"], r.text
    # a member who has not accepted
    await _prepare("s4")
    r = await _call("PATCH", f"/synopsis/{SYN['s4']}/submit", TOK["s4"])
    assert r.status_code == 400 and "accepted" in r.json()["detail"], r.text
    # an inactive member
    await _prepare("s6")
    r = await _call("PATCH", f"/synopsis/{SYN['s6']}/submit", TOK["s6"])
    assert r.status_code == 400 and "inactive" in r.json()["detail"], r.text
    for k in ("s3", "s4", "s6"):
        assert (await _row(SYN[k])).status == "draft"
    # a draft is private: approvers of every kind get 404
    for tok in ("ma", "hod1", "x", "dpgs"):
        assert (await _call("GET", f"/synopsis/{SYN['s1']}", TOK[tok])).status_code == 404, tok
    # a draft document can be previewed by its owner once a file exists
    r = await _call("GET", f"/synopsis/{SYN['s1']}/document", TOK["s1"])
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"


async def t_workflow_stage_order_and_all_members():
    sid = SYN["s1"]
    await _submit("s1")
    d = await _detail(sid, TOK["s1"])
    assert d["status"] == "major_advisor_pending" and d["current_cycle_number"] == 1 and d["can_edit"] is False
    assert [c["role_label"] for c in d["committee"]] == ["Major Advisor", "Co-Major Advisor", "Member Major", "Member Minor", "Supporting", "Members from Others", "Members from Others"]
    assert [a["role_label"] for a in d["approvals"]] == ["Head of the Department", "Incharge Academic Cell", "DPGS"]
    assert (await _call("PATCH", f"/synopsis/{sid}/submit", TOK["s1"])).status_code == 400, "cannot re-submit while under approval"
    # stages cannot be skipped: nobody but the Major Advisor can act now
    for who in ("co", "mm", "mn", "sp", "ot1", "ot2", "x", "hod1", "dpgs", "incfac:incharge", "incfac:faculty"):
        r = await _call("GET", f"/synopsis/{sid}/approval/otp", TOK[who])
        assert r.status_code == 403, (who, r.status_code)
        r = await _call("POST", f"/synopsis/{sid}/approval/approve", TOK[who], json={"otp": "000000"})
        assert r.status_code == 403, (who, r.status_code)
    # MA: OTP is mandatory and must be right
    assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["ma"], json={"otp": "123456"})).status_code == 400   # no OTP requested yet
    r = await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["ma"])
    assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["ma"], json={"otp": "not-it"})).status_code == 400
    assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["ma"], json={"otp": r.json()["dev_otp"]})).status_code == 200
    assert (await _detail(sid, TOK["s1"]))["status"] == "committee_pending"
    assert (await _sign(sid, TOK["ma"])).status_code == 403, "the MA cannot approve twice"
    # committee: every member must approve; HOD/Incharge/DPGS cannot act early
    for who in ("hod1", "dpgs", "incfac:incharge"):
        assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who
    order = ["co", "mm", "mn", "sp", "ot1"]
    for i, who in enumerate(order):
        await _approve_ok(sid, TOK[who], who)
        assert (await _detail(sid, TOK["s1"]))["status"] == "committee_pending", f"still waiting after {who}"
        assert (await _sign(sid, TOK[who])).status_code == 403, f"{who} cannot approve twice"
        if i == 1:
            S["doc_mid"] = await _call("GET", f"/synopsis/{sid}/document", TOK["s1"])       # in-progress document (MA + 2 members signed)
    assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["hod1"], json={"otp": "1"})).status_code == 403, "HOD still blocked with one member outstanding"
    await _approve_ok(sid, TOK["ot2"], "ot2")
    assert (await _detail(sid, TOK["s1"]))["status"] == "hod_pending"
    # HOD stage: wrong department HOD refused, right one accepted
    assert (await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["hod2"])).status_code == 403
    for who in ("ma", "dpgs", "incfac:incharge"):
        assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who
    await _approve_ok(sid, TOK["hod1"], "hod")
    assert (await _detail(sid, TOK["s1"]))["status"] == "incharge_pending"
    # Incharge: no OTP needed/possible, only the Incharge session may act (not DPGS/HOD/faculty session of the same person)
    assert (await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["incfac:incharge"])).status_code == 400
    for who in ("dpgs", "hod1", "incfac:faculty", "ma"):
        assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK[who], json={})).status_code == 403, who
    await _incharge_approve(sid, TOK["incfac:incharge"])
    assert (await _detail(sid, TOK["s1"]))["status"] == "dpgs_pending"
    for who in ("incfac:incharge", "hod1"):
        assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who
    d = await _approve_ok(sid, TOK["dpgs"], "dpgs")
    assert d["status"] == "approved"
    # persisted audit trail: every stage approved by the right person, signatories have a consumed OTP, Incharge has none
    cyc = (await _cycles(sid))[0]
    stages = await _stages(cyc.id)
    assert cyc.status == "approved" and len(stages) == 10 and all(s.status == "approved" for s in stages), [(s.role_label, s.status) for s in stages]
    async with AsyncSessionLocal() as db:
        sig = {sid_: n for sid_, n in (await db.execute(
            select(SynopsisSignature.approval_stage_id, func.count()).where(SynopsisSignature.approval_stage_id.in_([s.id for s in stages]), SynopsisSignature.otp_used == True)  # noqa: E712
            .group_by(SynopsisSignature.approval_stage_id))).all()}
    for st in stages:
        assert (st.id in sig) == (st.stage_type != "incharge_academic_cell"), f"OTP signature record mismatch for {st.role_label}"
        assert st.approver_id and st.acted_at and st.acted_role
    assert next(s for s in stages if s.stage_type == "incharge_academic_cell").approver_id == U["incfac"]


async def t_final_freeze_and_locks():
    sid = SYN["s1"]
    d = await _detail(sid, TOK["s1"])
    assert d["status"] == "approved" and d["frozen_document"] is True and d["can_edit"] is False
    cyc = (await _cycles(sid))[0]
    assert cyc.snapshot and cyc.frozen_pdf_filename and cyc.frozen_pdf_sha256 and cyc.frozen_at
    frozen = (_UPLOAD_ROOT / sid / cyc.frozen_pdf_filename).read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == cyc.frozen_pdf_sha256
    S["frozen_sha"], S["frozen_bytes"] = cyc.frozen_pdf_sha256, frozen
    # nobody but the Super Admin can edit; the student cannot restart or create another
    assert (await _call("PATCH", f"/synopsis/{sid}", TOK["s1"], json={"title": "change"})).status_code == 400
    assert (await _call("POST", f"/synopsis/{sid}/file", TOK["s1"], files={"file": ("n.pdf", _pdf(1), "application/pdf")})).status_code == 400
    assert (await _call("PATCH", f"/synopsis/{sid}/submit", TOK["s1"])).status_code == 400
    assert (await _call("POST", "/synopsis", TOK["s1"], json={})).status_code == 409
    for who in ("ma", "hod1", "dpgs", "incfac:incharge", "mm", "x"):
        assert (await _call("PATCH", f"/synopsis/{sid}", TOK[who], json={"title": "change", "reason": "r"})).status_code == 403, who
        assert (await _call("POST", f"/synopsis/{sid}/file", TOK[who], files={"file": ("n.pdf", _pdf(1), "application/pdf")})).status_code == 403, who
        assert (await _call("POST", f"/synopsis/{sid}/approval/revert", TOK[who], json={"remark": "late revert"})).status_code == 403, who
    assert (await _row(sid)).title == "ZZTEST Prevalence and therapeutic management study"
    # the served document is the frozen file
    r = await _call("GET", f"/synopsis/{sid}/document", TOK["s1"])
    assert r.status_code == 200 and r.content == frozen
    r2 = await _call("GET", f"/synopsis/{sid}/document", TOK["hod1"])
    assert r2.content == frozen, "every authorized viewer gets the same frozen bytes"


async def t_super_admin_override_and_frozen_stability():
    sid = SYN["s1"]
    new_title = "ZZTEST corrected title by administrator"
    # reason is mandatory
    assert (await _call("PATCH", f"/synopsis/{sid}", S["SA"], json={"title": new_title})).status_code == 400
    assert (await _call("PATCH", f"/synopsis/{sid}", S["SA"], json={"title": new_title, "reason": "  "})).status_code == 400
    assert (await _call("PATCH", f"/synopsis/{sid}", S["SA"], json={"title": "", "reason": "blank"})).status_code == 400
    cycles_before = [(c.id, c.status, c.frozen_pdf_sha256, c.title_snapshot) for c in await _cycles(sid)]
    stages_before = [(s.id, s.status, s.approver_id, s.acted_at) for s in await _stages((await _cycles(sid))[0].id)]
    r = await _call("PATCH", f"/synopsis/{sid}", S["SA"], json={"title": new_title, "reason": "Typographical correction requested by the student"})
    assert r.status_code == 200 and r.json()["title"] == new_title
    r = await _call("POST", f"/synopsis/{sid}/file", S["SA"], data={"reason": "Corrected scan supplied"}, files={"file": ("fixed.pdf", _pdf(1, "ZZTEST ADMIN REPLACEMENT"), "application/pdf")})
    assert r.status_code == 201 and r.json()["file"]["version"] >= 2
    assert (await _call("POST", f"/synopsis/{sid}/file", S["SA"], files={"file": ("fixed.pdf", _pdf(1), "application/pdf")})).status_code == 400, "reason required for the file too"
    # audited
    async with AsyncSessionLocal() as db:
        logs = (await db.execute(select(AuditLog).where(AuditLog.entity_id == sid).order_by(AuditLog.created_at))).scalars().all()
    assert [l.action for l in logs] == ["synopsis.admin_edit_title", "synopsis.admin_replace_file"], [l.action for l in logs]
    assert logs[0].old_value["title"] == "ZZTEST Prevalence and therapeutic management study" and logs[0].new_value["title"] == new_title
    assert logs[0].metadata_["reason"].startswith("Typographical") and logs[0].role_context == "super_admin" and logs[0].user_id == S["ADMIN"]
    # history and the frozen document are untouched
    assert cycles_before == [(c.id, c.status, c.frozen_pdf_sha256, c.title_snapshot) for c in await _cycles(sid)]
    assert stages_before == [(s.id, s.status, s.approver_id, s.acted_at) for s in await _stages((await _cycles(sid))[0].id)]
    r = await _call("GET", f"/synopsis/{sid}/document", TOK["s1"])
    assert r.content == S["frozen_bytes"], "the approved document did not change"
    try:
        # live data changes do not alter it either: college renamed, member designation changed, department changed
        async with AsyncSessionLocal() as db:
            await db.execute(text("update ams_colleges set name = :n where id = :i"), {"n": f"{_LABEL} College RENAMED", "i": S["COLLEGE"].id})
            await db.execute(text("update ams_users set designation = 'Retired', department_id = :d where id = :i"), {"d": S["D3"].id, "i": U["ma"]})
            await db.execute(text("update ams_users set department_id = :d where id = :i"), {"d": S["D2"].id, "i": U["s1"]})
            await db.commit()
        r = await _call("GET", f"/synopsis/{sid}/document", TOK["s1"])
        assert r.content == S["frozen_bytes"]
        text_ = _all_text(r.content)
        assert f"{_LABEL} College" in text_ and "RENAMED" not in text_ and "Retired" not in text_
        # reproducible: if the stored copy is lost, it is rebuilt from the approved SNAPSHOT (not from the changed live data)
        cyc = (await _cycles(sid))[0]
        (_UPLOAD_ROOT / sid / cyc.frozen_pdf_filename).unlink()
        r = await _call("GET", f"/synopsis/{sid}/document", TOK["s1"])
        assert r.status_code == 200 and r.content[:5] == b"%PDF-"
        rebuilt = _all_text(r.content)
        assert "RENAMED" not in rebuilt and "Retired" not in rebuilt and f"{_LABEL} College" in rebuilt
        assert UPLOADED_TEXT in rebuilt and "ADMIN REPLACEMENT" not in rebuilt, "the rebuilt package embeds the ORIGINAL approved upload, not the admin's replacement"
        # a tampered stored copy is detected
        (_UPLOAD_ROOT / sid / cyc.frozen_pdf_filename).write_bytes(S["frozen_bytes"] + b"x")
        r = await _call("GET", f"/synopsis/{sid}/document", TOK["s1"])
        assert r.status_code == 500 and "integrity" in r.json()["detail"]

    finally:   # put the ZZTEST live data back so later tests see the original state
        async with AsyncSessionLocal() as db:
            await db.execute(text("update ams_colleges set name = :n where id = :i"), {"n": f"{_LABEL} College", "i": S["COLLEGE"].id})
            await db.execute(text("update ams_users set designation = 'Professor', department_id = :d where id = :i"), {"d": S["D1"].id, "i": U["ma"]})
            await db.execute(text("update ams_users set department_id = :d where id = :i"), {"d": S["D1"].id, "i": U["s1"]})
            await db.commit()


async def t_document_contents_final():
    """Contents of the approved S1 package (frozen bytes captured before the live-data changes)."""
    pdf = S["frozen_bytes"]
    reader = PdfReader(io.BytesIO(pdf))
    assert all(is_a4(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages), "every page is A4 (Letter upload fitted)"
    pages = _pages_text(pdf)
    assert len(pages) == 1 + 3 + 1, len(pages)          # front + 3 uploaded + approval
    front, approval = pages[0], pages[-1]
    assert all(UPLOADED_TEXT in p for p in pages[1:4]), "uploaded pages sit between the AMS front and approval pages"
    for expected in ("Assam Veterinary And Fishery University", f"{_LABEL} College", "Master of Veterinary Science",
                     f"ZZTEST SYNS1", f"ZZTEST-SYN-s1-{_TAG}", "ZZTEST Prevalence and therapeutic management study",
                     f"Major Discipline : {S['D1'].name}", f"Minor Discipline : {S['D2'].name}", f"Supporting Discipline : {S['D3'].name}"):
        assert expected in front, f"front page is missing {expected!r}: {front[:400]}"
    assert "Assam Agricultural" not in " ".join(pages) and "AAU" not in " ".join(pages)
    for name in ("MA", "CO", "MM", "MN", "SP", "OT1", "OT2"):
        assert f"ZZTEST SYN{name}" in approval, name
    for label in ("Major Advisor", "Co-Major Advisor", "Member Major", "Member Minor", "Supporting"):
        assert label in approval, label
    assert approval.count("Members from Others") == 2, "two members share one role"
    assert "Declaration: The work could be completed with the existing facilities of the university/collaborating institutes." in approval
    assert "APPROVED" in approval and "Head of the Department" in approval and "Director of PG Studies" in approval
    assert approval.count("Signed") == 9, f"7 committee + HOD + DPGS signatures, none for Incharge: {approval.count('Signed')}"
    assert "Incharge Academic Cell approval: Approved" in approval
    assert "Incharge Academic Cell approval: Approved by Dr. ZZTEST SYNINCFAC" in approval
    idx = approval.index("Incharge Academic Cell approval")
    assert "Signed" not in approval[idx: idx + 90], "no signature is attributed to the Incharge Academic Cell"
    assert "SL No Name and Designation Advisory Action" in approval


async def t_document_in_progress_and_optional_roles():
    pdf = S["doc_mid"].content
    assert S["doc_mid"].status_code == 200
    pages = _pages_text(pdf)
    approval = pages[-1]
    assert approval.count("Signed") == 3, "MA + two members had signed"
    assert "Pending" in approval and "APPROVED" not in approval
    assert "Incharge Academic Cell approval" in approval and "Incharge Academic Cell approval: Approved" not in approval
    assert all(is_a4(float(p.mediabox.width), float(p.mediabox.height)) for p in PdfReader(io.BytesIO(pdf)).pages)

async def t_revert_matrix_and_resubmission():
    """One student (S2, committee = Major Advisor + one member) is reverted by each of the five roles in turn, resubmits
    through a NEW cycle every time, and is finally approved. History must stay intact throughout."""
    sid = await _prepare("s2", "ZZTEST revert-matrix title v1")
    stok = TOK["s2"]
    plan = [
        ("major_advisor", "Major Advisor", "ma", "the Major Advisor", []),
        ("committee_member", "Member Major", "incfac:faculty", "a committee member", ["ma"]),
        ("hod", "Head of the Department", "hod1", "the HOD", ["ma", "incfac:faculty"]),
        ("incharge_academic_cell", "Incharge Academic Cell", "incfac:incharge", "Incharge", ["ma", "incfac:faculty", "hod1"]),
        ("dpgs", "DPGS", "dpgs", "DPGS", ["ma", "incfac:faculty", "hod1", "incfac:incharge"]),
    ]
    seen_remarks = []
    for n, (stage_type, role_label, reverter, _who, before) in enumerate(plan, start=1):
        await _submit("s2")
        d = await _detail(sid, stok)
        assert d["current_cycle_number"] == n
        assert all(c["status"] == "pending" for c in d["committee"]), "a new cycle starts with NOTHING approved"
        for who in before:
            if who == "incfac:incharge":
                await _incharge_approve(sid, TOK[who])
            else:
                await _approve_ok(sid, TOK[who], who)
        # a revert needs a remark
        for bad in ({"remark": ""}, {"remark": "   "}, {}):
            assert (await _call("POST", f"/synopsis/{sid}/approval/revert", TOK[reverter], json=bad)).status_code in (400, 422), bad
        assert (await _row(sid)).status not in ("reverted", "draft")
        remark = f"ZZTEST remark {n}: please revise the {role_label.lower()} section"
        r = await _call("POST", f"/synopsis/{sid}/approval/revert", TOK[reverter], json={"remark": remark})
        assert r.status_code == 200, (role_label, r.text)
        seen_remarks.append(remark)
        d = await _detail(sid, stok)
        assert d["status"] == "reverted" and d["can_edit"] is True
        ri = d["revert_info"]
        assert ri["remark"] == remark and ri["role"] == role_label and ri["cycle_number"] == n and ri["reverted_at"] and ri["reverted_by"], ri
        assert "ZZTEST" in ri["reverted_by"]
        assert ri["acting_as"] in {"Faculty", "HOD", "Incharge Academic Cell", "DPGS"}
        cyc = (await _cycles(sid))[-1]
        assert cyc.status == "reverted" and cyc.revert_remark == remark and cyc.reverted_at
        stages = await _stages(cyc.id)
        assert sum(1 for s in stages if s.status == "reverted") == 1 and next(s for s in stages if s.status == "reverted").remark == remark
        assert all(s.status in ("approved", "reverted", "cancelled") for s in stages), "no dangling pending stage in a reverted cycle"
        # nobody can approve a reverted synopsis; the reverter cannot revert twice
        assert (await _call("POST", f"/synopsis/{sid}/approval/revert", TOK[reverter], json={"remark": "again"})).status_code == 403
        assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["ma"], json={"otp": "1"})).status_code == 403
        # the student corrects: edit title, upload a corrected PDF, then resubmit (loop) — history is intact
        assert (await _call("PATCH", f"/synopsis/{sid}", stok, json={"title": f"ZZTEST revert-matrix title v{n + 1}"})).status_code == 200
        r = await _call("POST", f"/synopsis/{sid}/file", stok, files={"file": (f"v{n + 1}.pdf", _pdf(1, f"ZZTEST CORRECTED v{n + 1}"), "application/pdf")})
        assert r.status_code == 201
        hist = (await _detail(sid, stok))["history"]
        assert [h["cycle_number"] for h in hist] == list(range(1, n + 1)) and [h["revert_remark"] for h in hist] == seen_remarks
        assert all(h["status"] == "reverted" for h in hist)
        assert hist[0]["title"] == "ZZTEST revert-matrix title v1", "each cycle keeps the title it was submitted with"
    # sixth cycle: everyone approves
    await _submit("s2")
    d = await _detail(sid, stok)
    assert d["current_cycle_number"] == 6 and all(c["status"] == "pending" for c in d["committee"]) and all(a["status"] == "pending" for a in d["approvals"])
    await _approve_ok(sid, TOK["ma"]); await _approve_ok(sid, TOK["incfac:faculty"]); await _approve_ok(sid, TOK["hod1"])
    await _incharge_approve(sid, TOK["incfac:incharge"])
    assert (await _approve_ok(sid, TOK["dpgs"]))["status"] == "approved"
    d = await _detail(sid, stok)
    assert d["status"] == "approved" and len(d["history"]) == 6 and [h["status"] for h in d["history"]] == ["reverted"] * 5 + ["approved"]
    assert d["revert_info"] is None
    # the approved document shows the FINAL cycle only, with optional roles absent (no Co-Major etc.), and the upload of the last cycle
    r = await _call("GET", f"/synopsis/{sid}/document", stok)
    assert r.status_code == 200
    pages = _pages_text(r.content)
    assert "ZZTEST CORRECTED v6" in " ".join(pages[1:-1]), "the latest uploaded PDF is the one approved"
    approval = pages[-1]
    assert "Co-Major Advisor" not in approval and "Member Minor" not in approval and "Supporting" not in approval and "Members from Others" not in approval
    assert approval.count("Signed") == 4, approval.count("Signed")      # MA + member + HOD + DPGS
    assert f"ZZTEST revert-matrix title v6" in pages[0]
    assert all(is_a4(float(p.mediabox.width), float(p.mediabox.height)) for p in PdfReader(io.BytesIO(r.content)).pages)


LOCK_REASSIGN = ("Cannot change the Major Advisor because this student has a Synopsis currently under approval. "
                 "The Synopsis approval workflow must be completed or reverted before the Major Advisor can be changed.")


async def t_major_advisor_change_lock_and_hod_authority():
    """S7. Existing rule (unchanged): only the HOD of the student's department may change the Major Advisor. New,
    additional protection: not while the student's Synopsis has an ACTIVE approval cycle."""
    cid = str(S["committee:s7"])
    both = [("ma", "major_advisor"), ("mm", "member_major")]
    reassign = lambda tok, extra=None: _call("POST", f"/research/committees/{cid}/reassign-major-advisor", tok, json={"major_advisor_id": str(U["x"]), **(extra or {})})
    # the state in which the existing rules let a HOD change the Major Advisor (returned from a higher-level revert)
    await _reset_committee("s7", both, status="hod_pending", reverted=True)
    baseline = await _committee_state("s7")

    # 1) AUTHORITY, no Synopsis at all: nobody but the student's department HOD may change the Major Advisor
    forged = {"department_id": str(S["D1"].id), "student_id": str(U["s7"]), "faculty_id": str(U["hod2"])}
    for who in ("s7", "ma", "mm", "x", "hod2", "mh:d2"):
        r = await reassign(TOK[who])
        assert r.status_code == 403, (who, r.status_code, r.text)
    r = await reassign(TOK["hod2"], forged)
    assert r.status_code == 403, "client-supplied department/student/faculty ids do not grant a HOD of another department any authority"
    assert await _committee_state("s7") == baseline, "a refused request changes nothing"

    # 2) allowed: the student's own department HOD (no Synopsis exists)
    r = await reassign(TOK["hod1"])
    assert r.status_code == 200, r.text
    assert (await _committee_state("s7"))[0] == "major_advisor_pending"
    await _reset_committee("s7", both, status="hod_pending", reverted=True)

    # 3) a draft that was NEVER submitted does not lock anything
    sid = await _prepare("s7")
    assert (await _row(sid)).status == "draft"
    r = await reassign(TOK["hod1"])
    assert r.status_code == 200, "an unsubmitted draft must not block the HOD"
    await _reset_committee("s7", both, status="hod_pending", reverted=True)

    # 4) ACTIVE approval cycle: blocked with 409 and the exact explanation; nothing changes
    await _submit("s7")
    baseline = await _committee_state("s7")
    assert baseline[0] == "hod_pending"
    r = await reassign(TOK["hod1"])
    assert r.status_code == 409 and r.json()["detail"] == LOCK_REASSIGN, (r.status_code, r.text)
    assert (await reassign(S["SA"])).status_code == 409, "the lock applies to every caller that is otherwise authorized"
    assert await _committee_state("s7") == baseline, "committee and Major Advisor untouched"
    d = await _detail(sid, TOK["s7"])
    assert d["status"] == "major_advisor_pending" and d["current_cycle_number"] == 1, "the Synopsis is not cancelled or altered"
    assert (await _cycles(sid))[0].status == "active"
    # the existing authorization still comes first: a HOD of another department gets 403 (never the Synopsis 409 detail)
    for who in ("hod2", "mh:d2", "x", "ma", "mm", "s7"):
        r = await reassign(TOK[who], forged)
        assert r.status_code == 403 and "Synopsis" not in r.text, (who, r.status_code, r.text)
    # committee MEMBER changes are blocked the same way (MA is the only one who may manage members; HOD never may)
    mm_row = S["member:s7:mm"]
    r = await _call("POST", f"/research/committees/{cid}/members", TOK["ma"], json={"faculty_id": str(U["x"]), "role": "supporting"})
    assert r.status_code == 409 and r.json()["detail"].startswith("Cannot add a committee member because this student has a Synopsis currently under approval."), r.text
    r = await _call("DELETE", f"/research/committees/{cid}/members/{mm_row}", TOK["ma"])
    assert r.status_code == 409 and r.json()["detail"].startswith("Cannot remove a committee member because this student has a Synopsis currently under approval."), r.text
    for who in ("hod1", "x", "mm", "s7"):
        assert (await _call("POST", f"/research/committees/{cid}/members", TOK[who], json={"faculty_id": str(U["x"]), "role": "supporting"})).status_code == 403, who
        assert (await _call("DELETE", f"/research/committees/{cid}/members/{mm_row}", TOK[who])).status_code == 403, who
    assert await _committee_state("s7") == baseline

    # 5) the lock ends when the cycle is no longer active: after a revert the HOD can change the Major Advisor again
    r = await _call("POST", f"/synopsis/{sid}/approval/revert", TOK["ma"], json={"remark": "ZZTEST revert before MA change"})
    assert r.status_code == 200
    r = await reassign(TOK["hod1"])
    assert r.status_code == 200, "a reverted (no active cycle) Synopsis no longer locks the committee"
    # ... and the approval HISTORY of the reverted cycle survives the change
    st = await _stages((await _cycles(sid))[0].id)
    ma_stage = next(x for x in st if x.stage_type == "major_advisor")
    assert ma_stage.committee_member_id is None and ma_stage.assignee_id == U["ma"] and ma_stage.status == "reverted" and ma_stage.remark
    assert next(x for x in st if x.stage_type == "committee_member").committee_member_id == mm_row
    # the NEW Major Advisor has not accepted yet, so the student cannot resubmit until they have
    r = await _call("PATCH", f"/synopsis/{sid}/submit", TOK["s7"])
    assert r.status_code == 400 and "Major Advisor" in r.json()["detail"], r.text


async def t_committee_changes_allowed_after_final_approval_and_document_unchanged():
    """S2 is approved (no active cycle): committee changes follow the normal rules again, the frozen document and the
    approval history are unaffected."""
    sid, cid = SYN["s2"], str(S["committee:s2"])
    assert (await _row(sid)).status == "approved"
    before = (await _call("GET", f"/synopsis/{sid}/document", TOK["s2"])).content
    hist_before = (await _detail(sid, TOK["s2"]))["history"]
    r = await _call("DELETE", f"/research/committees/{cid}/members/{S['member:s2:incfac']}", TOK["ma"])
    assert r.status_code == 204, "an approved Synopsis does not lock the committee"
    # the Major Advisor reassignment is not blocked by the Synopsis either (it fails only on the existing committee-state rule)
    r = await _call("POST", f"/research/committees/{cid}/reassign-major-advisor", TOK["hod1"], json={"major_advisor_id": str(U["x"])})
    assert r.status_code == 400 and "not awaiting" in r.json()["detail"], (r.status_code, r.text)
    assert (await _call("GET", f"/synopsis/{sid}/document", TOK["s2"])).content == before, "the approved document did not change"
    hist_after = (await _detail(sid, TOK["s2"]))["history"]
    assert hist_after == hist_before, "approval/signature history is intact after the committee change"
    st = await _stages((await _cycles(sid))[-1].id)
    member_stage = next(x for x in st if x.stage_type == "committee_member")
    assert member_stage.committee_member_id is None and member_stage.assignee_id == U["incfac"] and member_stage.status == "approved" and member_stage.approver_id == U["incfac"]


async def t_authorization_and_direct_id_attacks():
    s1, s2 = SYN["s1"], SYN["s2"]
    # cross-student reads: S3 (own draft) cannot read S1 or S2; nor the file/document
    for path in (f"/synopsis/{s1}", f"/synopsis/{s1}/file", f"/synopsis/{s1}/document", f"/synopsis/{s2}", f"/synopsis/{s2}/file"):
        assert (await _call("GET", path, TOK["s3"])).status_code == 404, path
    # query-string tampering has no effect: the path id decides, student_id in the query is ignored
    r = await _call("GET", f"/synopsis/{s1}", TOK["s1"], params={"student_id": str(U["s2"]), "cycle_id": str(uuid.uuid4())})
    assert r.status_code == 200 and r.json()["id"] == s1 and r.json()["student"]["student_roll"].endswith(f"s1-{_TAG}")
    # file_id must belong to THIS synopsis
    other_file = (await _detail(s2, TOK["s2"]))["history"][0]["file"]["id"]
    assert (await _call("GET", f"/synopsis/{s1}/file", TOK["s1"], params={"file_id": other_file})).status_code == 404
    assert (await _call("GET", f"/synopsis/{s1}/file", TOK["s1"], params={"file_id": str(uuid.uuid4())})).status_code == 404
    own_file = (await _detail(s1, TOK["s1"]))["history"][0]["file"]["id"]
    r = await _call("GET", f"/synopsis/{s1}/file", TOK["s1"], params={"file_id": own_file, "inline": "true"})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-" and r.headers["content-disposition"].startswith("inline")
    assert (await _call("GET", f"/synopsis/{s1}/file", None)).status_code in (401, 403)
    # approvers: unrelated faculty, other-department HOD, other students have no view; involved ones do
    assert (await _call("GET", f"/synopsis/{s1}", TOK["x"])).status_code == 404, "faculty on no committee"
    assert (await _call("GET", f"/synopsis/{s1}", TOK["hod2"])).status_code == 404, "HOD of another department"
    assert (await _call("GET", f"/synopsis/{s1}/document", TOK["hod2"])).status_code == 404
    for who in ("hod1", "ma", "ot2", "dpgs", "incfac:incharge", "s1"):
        assert (await _call("GET", f"/synopsis/{s1}", TOK[who])).status_code == 200, who
    assert (await _call("GET", f"/synopsis/{s1}", S["SA"])).status_code == 200
    assert (await _call("GET", f"/synopsis/{s2}", TOK["ot2"])).status_code == 404, "a member of S1's committee has no access to S2"
    # tampered bodies are rejected outright (no client-supplied identity is ever accepted)
    fresh = await _prepare("s5")
    await _submit("s5")
    for body in ({"otp": "1", "stage_id": str(uuid.uuid4())}, {"otp": "1", "faculty_id": str(U["ma"])}, {"otp": "1", "approver_id": str(U["ma"])},
                 {"otp": "1", "department_id": str(S["D1"].id)}, {"otp": "1", "cycle_id": str(uuid.uuid4())}, {"otp": "1", "committee_member_id": str(S[f"member:s5:ma"])}):
        assert (await _call("POST", f"/synopsis/{fresh}/approval/approve", TOK["ma"], json=body)).status_code == 422, body
    assert (await _call("POST", f"/synopsis/{fresh}/approval/revert", TOK["ma"], json={"remark": "r", "student_id": str(U["s1"])})).status_code == 422
    assert (await _call("PATCH", f"/synopsis/{fresh}", TOK["s5"], json={"title": "t", "student_id": str(U["s1"])})).status_code == 422
    # the Major Advisor of S5 (also MA of S1/S2) cannot act on a synopsis of a student they do not advise: S3's draft has no active cycle
    assert (await _call("POST", f"/synopsis/{SYN['s3']}/approval/approve", TOK["ma"], json={"otp": "1"})).status_code == 403
    # a committee member of another student's committee cannot use their membership here (S1's members on S5's stage)
    for who in ("mm", "mn", "sp", "ot1", "co", "x"):
        assert (await _call("POST", f"/synopsis/{fresh}/approval/approve", TOK[who], json={"otp": "1"})).status_code == 403, who
    # wrong-role sessions: a student cannot reach approver endpoints at all
    assert (await _call("POST", f"/synopsis/{fresh}/approval/approve", TOK["s5"], json={"otp": "1"})).status_code == 403
    assert (await _call("GET", f"/synopsis/{fresh}/approval/otp", TOK["s5"])).status_code == 403
    assert (await _call("GET", "/synopsis/pending-approvals", TOK["s5"])).status_code == 403
    assert (await _call("GET", "/synopsis/all", TOK["ma"])).status_code == 403
    r = await _call("GET", "/synopsis/all", S["SA"])
    assert r.status_code == 200 and any(row["id"] == s1 for row in r.json())
    S["s5_id"] = fresh


async def t_multi_role_active_assignment():
    """S5 (submitted, at the Major Advisor stage): INCFAC is a committee member (faculty) AND the Incharge; MH is HOD of two departments."""
    sid = S["s5_id"]
    await _approve_ok(sid, TOK["ma"], "s5 ma")
    assert (await _detail(sid, TOK["s5"]))["status"] == "committee_pending"
    # active session = INCHARGE: has no faculty powers, sees nothing in the inbox
    assert (await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["incfac:incharge"])).status_code == 403
    assert (await _call("POST", f"/synopsis/{sid}/approval/revert", TOK["incfac:incharge"], json={"remark": "no"})).status_code == 403
    assert (await _call("GET", "/synopsis/pending-approvals", TOK["incfac:incharge"])).json() == []
    # active session = FACULTY: the same person can act on the committee stage, and it is in the inbox
    inbox = (await _call("GET", "/synopsis/pending-approvals", TOK["incfac:faculty"])).json()
    assert [r["synopsis_id"] for r in inbox] == [sid] and inbox[0]["acting_as"] == "Member Major" and inbox[0]["requires_otp"] is True
    d = await _detail(sid, TOK["incfac:faculty"])
    assert d["my_pending_stage"]["role_label"] == "Member Major"
    assert (await _detail(sid, TOK["incfac:incharge"]))["my_pending_stage"] is None
    await _approve_ok(sid, TOK["incfac:faculty"], "s5 member")
    assert (await _detail(sid, TOK["s5"]))["status"] == "hod_pending"
    # HOD stage: MH with HOD@D2 active cannot act on a D1 student; with HOD@D1 active, can
    assert (await _call("GET", "/synopsis/pending-approvals", TOK["mh:d2"])).json() == []
    assert (await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["mh:d2"])).status_code == 403
    assert (await _call("POST", f"/synopsis/{sid}/approval/revert", TOK["mh:d2"], json={"remark": "wrong dept"})).status_code == 403
    assert (await _call("GET", f"/synopsis/{sid}", TOK["mh:d2"])).status_code == 404, "HOD@D2 session cannot even read a D1 student's synopsis"
    inbox = (await _call("GET", "/synopsis/pending-approvals", TOK["mh:d1"])).json()
    assert [r["synopsis_id"] for r in inbox] == [sid]
    await _approve_ok(sid, TOK["mh:d1"], "s5 hod")
    assert (await _detail(sid, TOK["s5"]))["status"] == "incharge_pending"
    # Incharge stage: the FACULTY session of the Incharge person is refused; the INCHARGE session is accepted
    assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["incfac:faculty"], json={})).status_code == 403
    assert (await _call("GET", "/synopsis/pending-approvals", TOK["incfac:faculty"])).json() == []
    assert [r["synopsis_id"] for r in (await _call("GET", "/synopsis/pending-approvals", TOK["incfac:incharge"])).json()] == [sid]
    # and the HOD@D1 session of MH is not an Incharge
    assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["mh:d1"], json={})).status_code == 403
    await _incharge_approve(sid, TOK["incfac:incharge"])
    assert (await _detail(sid, TOK["s5"]))["status"] == "dpgs_pending"
    assert [r["synopsis_id"] for r in (await _call("GET", "/synopsis/pending-approvals", TOK["dpgs"])).json()] == [sid]
    assert (await _call("GET", "/synopsis/pending-approvals", TOK["mh:d1"])).json() == []


async def t_committee_is_frozen_during_approval():
    """S5 is under approval (dpgs_pending): its committee cannot be changed through the committee endpoints."""
    cid, mid = S["committee:s5"], S["member:s5:incfac"]
    r = await _call("DELETE", f"/research/committees/{cid}/members/{mid}", TOK["ma"])
    assert r.status_code == 409 and "Cannot remove a committee member because this student has a Synopsis currently under approval" in r.json()["detail"], (r.status_code, r.text)
    r = await _call("POST", f"/research/committees/{cid}/members", TOK["ma"], json={"faculty_id": str(U["x"]), "role": "supporting"})
    assert r.status_code == 409
    r = await _call("POST", f"/research/committees/{cid}/reassign-major-advisor", TOK["hod1"], json={"major_advisor_id": str(U["x"])})
    assert r.status_code == 409 and r.json()["detail"] == LOCK_REASSIGN, r.text
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(CommitteeMember).where(CommitteeMember.committee_id == cid))).scalar_one() == 2


async def t_dev_otp_only_in_development():
    sid = S["s5_id"]     # dpgs_pending -> the DPGS may request an OTP
    settings.ENVIRONMENT = "production"
    try:
        r = await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["dpgs"])
        assert r.status_code == 200 and "dev_otp" not in r.json(), r.json()
        async with AsyncSessionLocal() as db:
            code = (await db.execute(select(SynopsisSignature.otp_code).order_by(SynopsisSignature.created_at.desc()).limit(1))).scalar_one()
        assert (await _call("POST", f"/synopsis/{sid}/approval/approve", TOK["dpgs"], json={"otp": "987317"})).status_code == 400, "no static dev OTP exists"
        assert code and len(code) == 6
    finally:
        settings.ENVIRONMENT = "development"
    r = await _call("GET", f"/synopsis/{sid}/approval/otp", TOK["dpgs"])
    assert "dev_otp" in r.json()


async def t_incharge_needs_no_signature_record_and_final_locks_committee():
    """DPGS approves S5; afterwards the committee guard no longer applies but the synopsis is frozen."""
    sid = S["s5_id"]
    d = await _approve_ok(sid, TOK["dpgs"], "s5 dpgs")
    assert d["status"] == "approved"
    cyc = (await _cycles(sid))[0]
    assert cyc.frozen_pdf_filename and cyc.snapshot["status"] == "approved" and cyc.snapshot["incharge"]["status"] == "approved"
    assert (await _call("PATCH", f"/synopsis/{sid}", TOK["s5"], json={"title": "nope"})).status_code == 400
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(Synopsis).where(Synopsis.student_id == U["s5"]))).scalar_one() == 1


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "SCHEMA: five tables; partial unique index for ONE First Synopsis per student; one active cycle per synopsis; head 0025": t_schema,
            "CREATE: one First Synopsis per student (409 + DB constraint), no client-supplied student/type, PG students only, cross-student access -> 404": t_create_and_uniqueness,
            "UPLOAD: PDF only — docx/doc/txt/png/garbage/fake-%PDF/encrypted/empty rejected, 413 over the limit, versioning, no path exposed, owner only": t_upload_validation,
            "SUBMIT: title and PDF required; committee must exist, be fully accepted and active; drafts are private": t_submit_preconditions,
            "WORKFLOW: MA -> ALL 6 committee members -> HOD -> Incharge -> DPGS; no stage skipped; OTP signatures; Incharge has none; approved": t_workflow_stage_order_and_all_members,
            "FINAL: frozen snapshot+PDF stored; approved synopsis locked for everyone but Super Admin; no second First Synopsis": t_final_freeze_and_locks,
            "DOCUMENT (final): A4 package, AVFU name, College/Programme/disciplines/roll/name, 7 committee rows incl. 2 'others', declaration, HOD+DPGS signed, no Incharge signature": t_document_contents_final,
            "DOCUMENT (in progress): renders current signatures live": t_document_in_progress_and_optional_roles,
            "SUPER ADMIN: audited override (mandatory reason); history untouched; frozen PDF stable when live data changes; rebuilt from snapshot; tamper detected": t_super_admin_override_and_frozen_stability,
            "REVERT x5: MA / member / HOD / Incharge / DPGS each revert with a remark; student sees who/role/when/remark; new cycle from the MA; history intact; then approved (optional roles absent)": t_revert_matrix_and_resubmission,
            "MAJOR ADVISOR LOCK: only the students department HOD may change the MA (forged ids / other roles refused); 409 + exact message while a cycle is ACTIVE; nothing changes; free again for draft/reverted": t_major_advisor_change_lock_and_hod_authority,
            "COMMITTEE AFTER APPROVAL: changes allowed again; approved document and history unchanged": t_committee_changes_allowed_after_final_approval_and_document_unchanged,
            "AUTHORIZATION: cross-student, cross-department, tampered ids in path/query/body, wrong-committee members, wrong roles": t_authorization_and_direct_id_attacks,
            "MULTI-ROLE: the ACTIVE session assignment decides (Incharge vs Faculty session; HOD@D2 vs HOD@D1); inbox follows the active role": t_multi_role_active_assignment,
            "COMMITTEE GUARD: committee endpoints refuse changes while a Synopsis is under approval": t_committee_is_frozen_during_approval,
            "OTP: dev_otp only when ENVIRONMENT == development; no static bypass code": t_dev_otp_only_in_development,
            "FINAL (second synopsis): DPGS approval freezes; locked": t_incharge_needs_no_signature_record_and_final_locks_committee,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            uids = select(User.id).where(User.email.like(f"{_PFX}%"))
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one(),
                "synopses": (await db.execute(select(func.count()).select_from(Synopsis).where(Synopsis.student_id.in_(uids)))).scalar_one(),
                "files": (await db.execute(select(func.count()).select_from(SynopsisFile))).scalar_one(),
                "cycles": (await db.execute(select(func.count()).select_from(SynopsisApprovalCycle))).scalar_one(),
                "stages": (await db.execute(select(func.count()).select_from(SynopsisApprovalStage))).scalar_one(),
                "signatures": (await db.execute(select(func.count()).select_from(SynopsisSignature))).scalar_one(),
                "committees": (await db.execute(select(func.count()).select_from(AdvisoryCommittee).where(AdvisoryCommittee.research_title == "ZZTEST committee"))).scalar_one(),
                "ppw": (await db.execute(select(func.count()).select_from(Ppw).where(Ppw.student_id.in_(uids)))).scalar_one(),
                "courses": (await db.execute(select(func.count()).select_from(Course).where(Course.course_number.like("ZZTEST-SYN-%")))).scalar_one(),
                "colleges/programmes": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one()
                                       + (await db.execute(select(func.count()).select_from(Program).where(Program.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "audit": (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action.like("synopsis.%")))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        left["upload_dir"] = int(_UPLOAD_ROOT.exists())
        record("cleanup: no ZZTEST_SYNOPSIS user / synopsis / file / cycle / stage / signature / committee / PPW / course / college / session / audit row / uploaded PDF remains",
               not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
