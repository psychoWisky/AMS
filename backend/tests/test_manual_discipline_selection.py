"""Standalone HTTP-level tests for the manual Major/Minor/Supporting
Discipline selection task (this revision, AVFU-confirmed): Major/Minor/
Supporting Discipline are no longer derived from the student's selected
courses — the student explicitly searches and selects each one from the
authoritative `Department` records, via:

  - `PATCH /ppw/{id}` (reuses the existing endpoint — `PpwIn` grew the three
    new optional id fields alongside `field_of_investigation`/`research_title`)
  - `PATCH /enrollment/registrations/{id}/discipline` (a NEW endpoint — no
    equivalent existed before this task on `CourseRegistration`)

Covers: searching/selecting (via the pre-existing, reused `GET /departments`
— no new search API was needed), saving/retrieving the ids, rejecting
invalid/nonexistent/inactive department ids, editing a previously-selected
discipline, the saved values appearing correctly in PPW/Course Registration
API responses AND in the actual generated Registration Card PDF and Synopsis
document context, backward compatibility (existing records with no
discipline ever selected remain fully readable), and that this task did NOT
touch the pre-existing course-count/LPM/mutual-exclusion/credit validations
(exercised exhaustively in test_minor_supporting_selection.py — not
duplicated here).

Modelled on test_minor_supporting_selection.py / test_ppw_research_title_sync.py
(same harness: real FastAPI app via `httpx.ASGITransport`, real JWT sessions,
a `_run(name, fn)` pass/fail tracker, `zztest_mds_...`-prefixed rows, full
teardown).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_manual_discipline_selection
"""
import asyncio
import hashlib
import io
import sys
import uuid
from datetime import date

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.api.v1.endpoints.synopsis import _student_block
from app.models.academic import Semester
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.enrollment import CourseRegistration, StudentEnrollment
from app.models.ppw import Ppw, PpwCourse
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_mds_"
_LABEL = "ZZTEST_MDS"
_DEVICE = "ZZTEST-mds"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
C: dict[str, uuid.UUID] = {}
O: dict[str, uuid.UUID] = {}


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


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None) -> None:
    is_student = role == UserRole.STUDENT
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"MDS{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            program_id=program.id if program else None, college_id=college.id if college else None,
            student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True,
            date_of_birth=date(2000, 1, 1) if is_student else None,
            father_name="ZZTEST Father" if is_student else None,
            address="ZZTEST Address" if is_student else None,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()
    TOK[key] = await _session(U[key])


async def _mk_committee(student_key: str, ma_key: str) -> None:
    async with AsyncSessionLocal() as db:
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST MDS committee", status="members_pending")
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


def _extract_pdf_text(data: bytes) -> str:
    """A generated PDF's text lives in compressed content streams — a raw byte/latin-1 substring
    search over the file will almost never find it. Use pypdf's real text extraction instead.
    Newlines are collapsed to single spaces — a narrow table cell wraps a long Department name
    across multiple extracted lines (e.g. "Department of Animal\\nBiotechnology"), which would
    otherwise make a plain substring check fail on text that is actually present and correct."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    return " ".join(text.split())


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_mds_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        others = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(3))).scalars().all()
        assert len(others) >= 3, "need at least 3 existing non-LPM Department rows in the dev DB"
        S["D_A"], S["D_B"], S["D_C"] = others
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        sem = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert sem is not None, "test requires at least 1 existing Semester in the local dev database"
        S["SEM"] = sem
        col = College(name=f"{_LABEL} College", code=f"ZZMDS{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        # A real, INACTIVE Department — exercises `update_ppw`/
        # `update_registration_discipline`'s "must be real AND active" check
        # specifically (not just "id doesn't exist at all").
        inactive_dept = Department(name=f"{_LABEL} Inactive Dept", code=f"ZZMDSX{_TAG.upper()}"[:20], is_active=False)
        db.add(inactive_dept)
        await db.flush()
        S["D_INACTIVE"] = inactive_dept
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    S["SA"] = await _session(admin_id)
    d_a, pg, college, sem = S["D_A"], S["PG"], S["COLLEGE"], S["SEM"]

    await _mk_user("hod1", UserRole.HOD, d_a)
    await _mk_user("ma1", UserRole.FACULTY, d_a)
    await _mk_user("teacher1", UserRole.FACULTY, d_a)
    await _mk_user("student_ppw", UserRole.STUDENT, d_a, program=pg, college=college, roll=f"ZZMDS-sppw-{_TAG}")
    await _mk_committee("student_ppw", "ma1")
    await _mk_user("student_old", UserRole.STUDENT, d_a, program=pg, college=college, roll=f"ZZMDS-sold-{_TAG}")
    await _mk_committee("student_old", "ma1")
    await _mk_user("student_reg", UserRole.STUDENT, d_a, program=pg, college=college, roll=f"ZZMDS-sreg-{_TAG}")
    await _mk_committee("student_reg", "ma1")
    await _mk_user("student_no_ppw", UserRole.STUDENT, d_a, program=pg, college=college, roll=f"ZZMDS-snoppw-{_TAG}")

    # One course + offering so student_reg can actually register (needed for
    # the Course Registration discipline PATCH + Registration Card PDF tests).
    async with AsyncSessionLocal() as db:
        crs = Course(
            course_number=f"ZZMDS-C1-{_TAG}", title="ZZTEST MDS course 1",
            department_id=d_a.id, program_level="PG", category="core", credit_theory=1, credit_practical=0,
            created_by=S["ADMIN"],
        )
        db.add(crs)
        await db.flush()
        C["c1"] = crs.id
        await db.commit()
    r = await _call("POST", "/courses/offerings", S["SA"], json={
        "calendar_id": str(sem.calendar_id), "semester_id": str(sem.id), "course_id": str(C["c1"]),
        "department_id": str(d_a.id), "max_enrollment": 10,
        "faculty_ids": [str(U["teacher1"])], "leader_id": str(U["teacher1"]),
    })
    assert r.status_code == 201, r.text
    O["c1"] = uuid.UUID(r.json()["id"])
    pub = await _call("PATCH", f"/courses/offerings/{O['c1']}/status", S["SA"], params={"status": "published"})
    assert pub.status_code == 200, pub.text


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids_committee = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        cids_course = select(Course.id).where(Course.course_number.like(f"ZZMDS-%-{_TAG}"))
        oids = select(CourseOffering.id).where(CourseOffering.course_id.in_(cids_course))
        pids = select(Ppw.id).where(Ppw.student_id.in_(uids))

        await db.execute(delete(PpwCourse).where(PpwCourse.ppw_id.in_(pids)))
        await db.execute(delete(Ppw).where(Ppw.id.in_(pids)))
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(oids)))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.student_id.in_(uids)))
        await db.execute(delete(OfferingFaculty).where(OfferingFaculty.offering_id.in_(oids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(oids)))
        await db.execute(delete(Course).where(Course.id.in_(cids_course)))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids_committee)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.execute(delete(Department).where(Department.name.like(f"{_LABEL}%")))
        await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# PPW
# ════════════════════════════════════════════════════════════════════════════

async def t_ppw_create():
    r = await _call("POST", "/ppw", TOK["student_ppw"], json={})
    assert r.status_code == 201, r.text
    S["PPW"] = uuid.UUID(r.json()["id"])


async def t_ppw_set_valid_disciplines_and_retrieve():
    r = await _call("PATCH", f"/ppw/{S['PPW']}", TOK["student_ppw"], json={
        "major_department_id": str(S["D_A"].id), "minor_department_id": str(S["D_B"].id),
        "supporting_department_id": str(S["D_C"].id),
    })
    assert r.status_code == 200, r.text
    g = await _call("GET", f"/ppw/{S['PPW']}", TOK["student_ppw"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["major_department_id"] == str(S["D_A"].id), body
    assert body["minor_department_id"] == str(S["D_B"].id), body
    assert body["supporting_department_id"] == str(S["D_C"].id), body
    assert body["major_discipline_name"] == S["D_A"].name, body
    assert body["minor_discipline_name"] == S["D_B"].name, body
    assert body["supporting_discipline_name"] == S["D_C"].name, body


async def t_ppw_reject_nonexistent_department_id():
    r = await _call("PATCH", f"/ppw/{S['PPW']}", TOK["student_ppw"], json={
        "major_department_id": str(uuid.uuid4()),
    })
    assert r.status_code == 400, (r.status_code, r.text)
    # Confirm the rejected request wrote nothing (still D_A from the prior test).
    g = await _call("GET", f"/ppw/{S['PPW']}", TOK["student_ppw"])
    assert g.json()["major_department_id"] == str(S["D_A"].id), g.json()


async def t_ppw_reject_inactive_department_id():
    r = await _call("PATCH", f"/ppw/{S['PPW']}", TOK["student_ppw"], json={
        "minor_department_id": str(S["D_INACTIVE"].id),
    })
    assert r.status_code == 400, (r.status_code, r.text)
    g = await _call("GET", f"/ppw/{S['PPW']}", TOK["student_ppw"])
    assert g.json()["minor_department_id"] == str(S["D_B"].id), g.json()


async def t_ppw_edit_previously_selected_discipline():
    r = await _call("PATCH", f"/ppw/{S['PPW']}", TOK["student_ppw"], json={
        "minor_department_id": str(S["D_C"].id),
    })
    assert r.status_code == 200, r.text
    g = await _call("GET", f"/ppw/{S['PPW']}", TOK["student_ppw"])
    assert g.json()["minor_department_id"] == str(S["D_C"].id), g.json()
    assert g.json()["minor_discipline_name"] == S["D_C"].name, g.json()
    # Restore to D_B so the remaining tests (submission/document) see the
    # intended Major/Minor/Supporting triple.
    r2 = await _call("PATCH", f"/ppw/{S['PPW']}", TOK["student_ppw"], json={"minor_department_id": str(S["D_B"].id)})
    assert r2.status_code == 200, r2.text


async def t_ppw_submit_and_document_shows_explicit_disciplines():
    """Requirement not invented, not derived: the official PPW PDF must show
    exactly the three explicitly-selected Department names — never anything
    computed from course selections (this PPW has zero courses added)."""
    r = await _call("PATCH", f"/ppw/{S['PPW']}", TOK["student_ppw"], json={
        "field_of_investigation": "ZZTEST MDS field", "research_title": "ZZTEST MDS title",
    })
    assert r.status_code == 200, r.text
    r2 = await _call("PATCH", f"/ppw/{S['PPW']}/submit", TOK["student_ppw"])
    assert r2.status_code == 200, r2.text
    doc = await _call("GET", f"/ppw/{S['PPW']}/document", TOK["student_ppw"])
    assert doc.status_code == 200 and doc.headers["content-type"] == "application/pdf", doc.text
    text = _extract_pdf_text(doc.content)
    assert S["D_A"].name in text, text
    assert S["D_B"].name in text, text
    assert S["D_C"].name in text, text


async def t_ppw_backward_compat_no_discipline_selected_still_readable():
    """A PPW that never had any discipline explicitly selected (the
    pre-this-task state every existing PPW is in) must remain fully readable,
    with null discipline fields — never an error, never a fabricated value."""
    r = await _call("POST", "/ppw", TOK["student_old"], json={})
    assert r.status_code == 201, r.text
    ppw_id = uuid.UUID(r.json()["id"])
    g = await _call("GET", f"/ppw/{ppw_id}", TOK["student_old"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["major_department_id"] is None, body
    assert body["minor_department_id"] is None, body
    assert body["supporting_department_id"] is None, body
    assert body["major_discipline_name"] is None, body
    assert body["minor_discipline_name"] is None, body
    assert body["supporting_discipline_name"] is None, body


async def t_synopsis_student_block_uses_explicit_ppw_disciplines():
    """Section 3's "all documents must use the explicitly saved values"
    requirement, verified directly against `synopsis.py`'s `_student_block`
    (the function `get_synopsis_document`'s render context is built from) —
    Major Discipline used to be hardcoded to the student's own department and
    never actually read the PPW at all; it must now read the PPW's explicitly
    selected Major Discipline, exactly like Minor/Supporting already did."""
    async with AsyncSessionLocal() as db:
        student = (await db.execute(
            select(User)
            .options(selectinload(User.program), selectinload(User.college), selectinload(User.department))
            .where(User.id == U["student_ppw"])
        )).scalar_one()
        block = await _student_block(student, db)
    assert block["major_discipline"] == S["D_A"].name, block
    assert block["minor_discipline"] == S["D_B"].name, block
    assert block["supporting_discipline"] == S["D_C"].name, block


async def t_synopsis_student_block_falls_back_when_no_ppw_major_selected():
    """A student with no PPW at all (so no explicit Major Discipline ever
    selected) falls back to their own department for Major — never blank —
    so no existing Synopsis document's display regresses; Minor/Supporting
    (which never had a non-PPW fallback) remain None."""
    async with AsyncSessionLocal() as db:
        student = (await db.execute(
            select(User)
            .options(selectinload(User.program), selectinload(User.college), selectinload(User.department))
            .where(User.id == U["student_no_ppw"])
        )).scalar_one()
        block = await _student_block(student, db)
    assert block["major_discipline"] == S["D_A"].name, block
    assert block["minor_discipline"] is None, block
    assert block["supporting_discipline"] is None, block


# ════════════════════════════════════════════════════════════════════════════
# Course Registration
# ════════════════════════════════════════════════════════════════════════════

async def t_registration_create():
    r = await _call("POST", "/enrollment/register", TOK["student_reg"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "offering_ids": [str(O["c1"])],
        # The Registration Card now requires every course to carry a valid
        # classification; "compulsory" is independent of the selected
        # disciplines under test here.
        "classifications": {str(O["c1"]): "compulsory"},
    })
    assert r.status_code == 201, r.text
    S["REG"] = r.json()["id"]


async def t_registration_set_valid_disciplines_and_retrieve():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_reg"], json={
        "major_department_id": str(S["D_A"].id), "minor_department_id": str(S["D_B"].id),
        "supporting_department_id": str(S["D_C"].id),
    })
    assert r.status_code == 200, r.text
    g = await _call("GET", f"/enrollment/registrations/{S['REG']}", TOK["student_reg"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["major_department_id"] == str(S["D_A"].id), body
    assert body["minor_department_id"] == str(S["D_B"].id), body
    assert body["supporting_department_id"] == str(S["D_C"].id), body
    assert body["major_discipline_name"] == S["D_A"].name, body
    assert body["minor_discipline_name"] == S["D_B"].name, body
    assert body["supporting_discipline_name"] == S["D_C"].name, body


async def t_registration_reject_nonexistent_department_id():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_reg"], json={
        "supporting_department_id": str(uuid.uuid4()),
    })
    assert r.status_code == 400, (r.status_code, r.text)
    g = await _call("GET", f"/enrollment/registrations/{S['REG']}", TOK["student_reg"])
    assert g.json()["supporting_department_id"] == str(S["D_C"].id), g.json()


async def t_registration_reject_inactive_department_id():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_reg"], json={
        "major_department_id": str(S["D_INACTIVE"].id),
    })
    assert r.status_code == 400, (r.status_code, r.text)
    g = await _call("GET", f"/enrollment/registrations/{S['REG']}", TOK["student_reg"])
    assert g.json()["major_department_id"] == str(S["D_A"].id), g.json()


async def t_registration_edit_previously_selected_discipline():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_reg"], json={
        "major_department_id": str(S["D_B"].id),
    })
    assert r.status_code == 200, r.text
    g = await _call("GET", f"/enrollment/registrations/{S['REG']}", TOK["student_reg"])
    assert g.json()["major_department_id"] == str(S["D_B"].id), g.json()
    # Restore to D_A for the document test below.
    r2 = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_reg"], json={"major_department_id": str(S["D_A"].id)})
    assert r2.status_code == 200, r2.text


async def t_registration_reject_other_students_registration():
    """Ownership check — `update_registration_discipline` must 404 for a
    registration that belongs to a DIFFERENT student (never a silent
    cross-student write), exactly like every other student-owned mutation
    endpoint in this file."""
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_ppw"], json={
        "major_department_id": str(S["D_A"].id),
    })
    assert r.status_code == 404, (r.status_code, r.text)


async def t_registration_card_document_shows_explicit_disciplines():
    """Approve the one selected course (reaching `card_pending`) and fetch
    the real Registration Card PDF as Super Admin (the student-only
    `dpgs_approved`-only gate is a separate, pre-existing restriction this
    task must not touch — see `get_registration_document`'s own docstring;
    exercising the full Major-Advisor->HOD->Incharge->DPGS chain is already
    covered by other test files and would be redundant here). Confirms the
    PDF shows exactly the three explicitly-selected Department names."""
    async with AsyncSessionLocal() as db:
        enrollment = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["student_reg"], StudentEnrollment.offering_id == O["c1"],
        ))).scalar_one()
    ra = await _call("PATCH", f"/enrollment/{enrollment.id}", TOK["teacher1"], params={"status": "approved"})
    assert ra.status_code == 200, ra.text
    doc = await _call("GET", f"/enrollment/registrations/{S['REG']}/document", S["SA"])
    assert doc.status_code == 200 and doc.headers["content-type"] == "application/pdf", doc.text
    text = _extract_pdf_text(doc.content)
    assert S["D_A"].name in text, text
    assert S["D_B"].name in text, text
    assert S["D_C"].name in text, text


async def t_registration_discipline_locked_once_not_editable():
    """Once a registration leaves the student-editable window
    (`_EDITABLE_STAGES`), the discipline PATCH must be rejected exactly like
    every other student-side mutation on it — never a separate, looser gate."""
    async with AsyncSessionLocal() as db:
        r = await db.get(CourseRegistration, uuid.UUID(S["REG"]))
        r.stage = "major_advisor_pending"
        await db.commit()
    r2 = await _call("PATCH", f"/enrollment/registrations/{S['REG']}/discipline", TOK["student_reg"], json={
        "major_department_id": str(S["D_B"].id),
    })
    assert r2.status_code == 400, (r2.status_code, r2.text)


async def t_registration_backward_compat_no_discipline_selected_still_readable():
    """A registration that never had any discipline explicitly selected
    (the pre-this-task state every existing registration is in) must remain
    fully readable, with null discipline fields."""
    await _mk_user("student_reg2", UserRole.STUDENT, S["D_A"], program=S["PG"], college=S["COLLEGE"], roll=f"ZZMDS-sreg2-{_TAG}")
    await _mk_committee("student_reg2", "ma1")
    r = await _call("POST", "/enrollment/register", TOK["student_reg2"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "offering_ids": [str(O["c1"])],
    })
    assert r.status_code == 201, r.text
    reg_id = r.json()["id"]
    g = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["student_reg2"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["major_department_id"] is None, body
    assert body["minor_department_id"] is None, body
    assert body["supporting_department_id"] is None, body
    assert body["major_discipline_name"] is None, body
    assert body["minor_discipline_name"] is None, body
    assert body["supporting_discipline_name"] is None, body


async def main() -> None:
    try:
        await _setup()
    except Exception:
        await _teardown()
        raise
    try:
        cases = {
            # PPW
            "PPW: create draft": t_ppw_create,
            "PPW: set valid Major/Minor/Supporting disciplines, retrieve ids+names": t_ppw_set_valid_disciplines_and_retrieve,
            "PPW: nonexistent department id rejected, nothing written": t_ppw_reject_nonexistent_department_id,
            "PPW: inactive department id rejected, nothing written": t_ppw_reject_inactive_department_id,
            "PPW: editing a previously-selected discipline": t_ppw_edit_previously_selected_discipline,
            "PPW: submit + document shows the explicitly-selected disciplines": t_ppw_submit_and_document_shows_explicit_disciplines,
            "PPW: backward-compat — no discipline ever selected, still readable": t_ppw_backward_compat_no_discipline_selected_still_readable,
            "Synopsis: _student_block uses the PPW's explicit disciplines": t_synopsis_student_block_uses_explicit_ppw_disciplines,
            "Synopsis: _student_block falls back to own department when no PPW Major selected": t_synopsis_student_block_falls_back_when_no_ppw_major_selected,
            # Course Registration
            "Registration: create": t_registration_create,
            "Registration: set valid Major/Minor/Supporting disciplines, retrieve ids+names": t_registration_set_valid_disciplines_and_retrieve,
            "Registration: nonexistent department id rejected, nothing written": t_registration_reject_nonexistent_department_id,
            "Registration: inactive department id rejected, nothing written": t_registration_reject_inactive_department_id,
            "Registration: editing a previously-selected discipline": t_registration_edit_previously_selected_discipline,
            "Registration: cannot set discipline on another student's registration": t_registration_reject_other_students_registration,
            "Registration: Card PDF shows the explicitly-selected disciplines": t_registration_card_document_shows_explicit_disciplines,
            "Registration: discipline PATCH locked once no longer editable": t_registration_discipline_locked_once_not_editable,
            "Registration: backward-compat — no discipline ever selected, still readable": t_registration_backward_compat_no_discipline_selected_still_readable,
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
        record("cleanup: no ZZTEST_MDS row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
