"""Standalone HTTP-level tests for the Course Registration Card download-
restriction task: a Student may download the official PDF
(`GET /enrollment/registrations/{id}/document`) only once
`CourseRegistration.stage == "dpgs_approved"`; every other staff role keeps
its pre-existing `teacher_pending`-only gate, unchanged. Tracking
(`GET /enrollment/registrations/{id}`) must remain available at every stage
for the student, unaffected by this restriction.

Modelled on `tests/test_withdraw_reregistration.py` (same harness: real
FastAPI app via `httpx.ASGITransport`, real JWT sessions, a `_run(name, fn)`
pass/fail tracker, `zztest_rcd_...`-prefixed rows, full teardown) — reuses
its exact fixture pattern (an `AdvisoryCommittee` at `status="members_pending"`
with an accepted Major Advisor is sufficient to satisfy the Course
Registration eligibility gate; no Advisory Committee approval chain needs to
be driven for this task).

Drives ONE real registration through the ENTIRE confirmed chain end to end
(Course Teacher -> card submission -> Major Advisor -> HOD -> Incharge
Academic Cell -> DPGS), asserting the download is rejected at every single
intermediate stage and succeeds only at the end — then reverts and re-drives
the chain a second time to confirm the restriction re-applies correctly
after a revert.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_registration_card_download
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
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import Semester
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.enrollment import CourseRegistration, StudentEnrollment
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_rcd_"
_LABEL = "ZZTEST_RCD"
_DEVICE = "ZZTEST-rcd"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
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


async def _mk_user(key, role, dept, *, program=None, college=None, roll=None) -> None:
    is_student = role == UserRole.STUDENT
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"RCD{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            program_id=program.id if program else None, college_id=college.id if college else None,
            student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True,
            # require_complete_profile (core/dependencies.py) is composed into
            # require_advisory_committee_established, which every Course
            # Registration endpoint a student calls depends on — these four
            # fields must be non-empty or every such call is rejected 403
            # before even reaching the registration logic.
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
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST RCD committee", status="members_pending")
        db.add(c)
        await db.flush()
        db.add(CommitteeMember(committee_id=c.id, faculty_id=U[ma_key], role="major_advisor", accepted=True))
        await db.commit()


async def _download(registration_id, token):
    return await _call("GET", f"/enrollment/registrations/{registration_id}/document", token)


async def _registration_stage(registration_id) -> str:
    async with AsyncSessionLocal() as db:
        r = await db.get(CourseRegistration, registration_id)
        return r.stage


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
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in ('INCHARGE_ACADEMIC_CELL','DPGS')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS holder exists (single-holder roles); refusing to run.")
            sys.exit(2)
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_rcd_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        d1 = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(1))).scalar_one()
        S["D1"] = d1
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        sem = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert sem is not None, "test requires at least 1 existing Semester in the local dev database"
        S["SEM"] = sem
        col = College(name=f"{_LABEL} College", code=f"ZZRCD{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, pg, college, sem = S["D1"], S["PG"], S["COLLEGE"], S["SEM"]

    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("ma1", UserRole.FACULTY, d1)
    await _mk_user("teacher1", UserRole.FACULTY, d1)
    await _mk_user("incharge", UserRole.INCHARGE_ACADEMIC_CELL, None)
    await _mk_user("dpgs", UserRole.DPGS, None)
    await _mk_user("super_admin_probe", UserRole.SUPER_ADMIN, None)
    await _mk_user("s1", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRCD-s1-{_TAG}")
    await _mk_user("s2", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZRCD-s2-{_TAG}")
    S["SA"] = await _session(S["ADMIN"])

    await _mk_committee("s1", "ma1")
    await _mk_committee("s2", "ma1")

    async with AsyncSessionLocal() as db:
        course = Course(course_number=f"ZZRCD-{_TAG}", title="ZZTEST Registration Card Download Course",
                         department_id=d1.id, program_level="PG", category="core", created_by=S["ADMIN"])
        db.add(course)
        await db.commit()
        S["COURSE"] = course.id

    r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
        "calendar_id": str(sem.calendar_id), "semester_id": str(sem.id), "course_id": str(S["COURSE"]),
        "department_id": str(d1.id), "max_enrollment": 10,
        "faculty_ids": [str(U["teacher1"])], "leader_id": str(U["teacher1"]),
    })
    assert r.status_code == 201, r.text
    S["OFFERING"] = uuid.UUID(r.json()["id"])
    pub = await _call("PATCH", f"/courses/offerings/{S['OFFERING']}/status", TOK["hod1"], params={"status": "published"})
    assert pub.status_code == 200, pub.text


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        oids = select(CourseOffering.id).where(CourseOffering.course_id == S.get("COURSE", uuid.uuid4()))
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(oids)))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.student_id.in_(uids)))
        await db.execute(delete(OfferingFaculty).where(OfferingFaculty.offering_id.in_(oids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(oids)))
        await db.execute(delete(Course).where(Course.id == S.get("COURSE", uuid.uuid4())))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


# ── Full chain helper ─────────────────────────────────────────────────────────

async def _register_and_get_to_teacher_approved(student_key: str) -> uuid.UUID:
    r = await _call("POST", "/enrollment/register", TOK[student_key], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "offering_ids": [str(S["OFFERING"])],
    })
    assert r.status_code == 201, r.text
    reg_id = uuid.UUID(r.json()["id"])
    async with AsyncSessionLocal() as db:
        e = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == S["OFFERING"],
        ))).scalar_one()
        enrollment_id = e.id
    r2 = await _call("PATCH", f"/enrollment/{enrollment_id}", TOK["teacher1"], params={"status": "approved"})
    assert r2.status_code == 200, r2.text
    assert await _registration_stage(reg_id) == "card_pending", await _registration_stage(reg_id)
    return reg_id


async def _drive_to_dpgs_approved(student_key: str, reg_id: uuid.UUID) -> None:
    r = await _call("POST", f"/enrollment/registrations/{reg_id}/submit", TOK[student_key])
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "major_advisor_pending"

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/major-advisor-approval", TOK["ma1"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "hod_pending"

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "incharge_pending"

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "dpgs_pending"

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "dpgs_approved"


# ── 1-6: student download rejected at every stage, succeeds only at dpgs_approved ──

async def t_full_chain_student_download_blocked_until_dpgs_approved():
    reg_id = await _register_and_get_to_teacher_approved("s1")
    S["REG_S1"] = reg_id

    # card_pending
    r = await _download(reg_id, TOK["s1"])
    assert r.status_code == 400 and "DPGS" in r.json()["detail"], r.text
    # 7: tracking must remain available at every stage
    rt = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["s1"])
    assert rt.status_code == 200, rt.text

    r = await _call("POST", f"/enrollment/registrations/{reg_id}/submit", TOK["s1"])
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "major_advisor_pending"
    r = await _download(reg_id, TOK["s1"])
    assert r.status_code == 400, r.text
    rt = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["s1"])
    assert rt.status_code == 200, rt.text

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/major-advisor-approval", TOK["ma1"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "hod_pending"
    r = await _download(reg_id, TOK["s1"])
    assert r.status_code == 400, r.text
    rt = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["s1"])
    assert rt.status_code == 200, rt.text

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/hod-approval", TOK["hod1"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "incharge_pending"
    r = await _download(reg_id, TOK["s1"])
    assert r.status_code == 400, r.text
    rt = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["s1"])
    assert rt.status_code == 200, rt.text

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/incharge-approval", TOK["incharge"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "dpgs_pending"
    r = await _download(reg_id, TOK["s1"])
    assert r.status_code == 400, r.text
    rt = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["s1"])
    assert rt.status_code == 200, rt.text

    r = await _call("PATCH", f"/enrollment/registrations/{reg_id}/dpgs-approval", TOK["dpgs"], json={"approved": True})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "dpgs_approved"

    # 6: succeeds now
    r = await _download(reg_id, TOK["s1"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
    rt = await _call("GET", f"/enrollment/registrations/{reg_id}", TOK["s1"])
    assert rt.status_code == 200, rt.text


# ── 8: student cannot download another student's registration document ─────

async def t_cross_student_idor_blocked():
    r = await _download(S["REG_S1"], TOK["s2"])
    assert r.status_code == 403, (r.status_code, r.text)


# ── 9: staff access behavior remains unchanged (teacher_pending-only gate) ──

async def t_staff_access_unchanged_at_dpgs_approved_stage():
    # confirm staff can download at the final stage too (sanity)
    for key in ("hod1", "incharge", "dpgs", "super_admin_probe"):
        r = await _download(S["REG_S1"], TOK[key])
        assert r.status_code == 200, (key, r.status_code, r.text)


async def t_staff_access_unchanged_at_earlier_stages():
    reg_id = await _register_and_get_to_teacher_approved("s2")  # card_pending — not teacher_pending
    for key in ("hod1", "incharge", "dpgs", "super_admin_probe"):
        r = await _download(reg_id, TOK[key])
        assert r.status_code == 200, f"staff role {key} must still be able to download at card_pending (unchanged behavior) — got {r.status_code} {r.text}"
    S["REG_S2"] = reg_id


# ── 10/11: revert re-blocks download; the next cycle's DPGS approval re-allows it ──

async def t_revert_reblocks_and_next_cycle_reapproves():
    """Confirmed during this task's implementation (not assumed): there is NO
    endpoint in this codebase that reverts an ALREADY-`dpgs_approved`
    CourseRegistration — `dpgs_registration_approval`'s own precondition
    (`r.stage != "dpgs_pending"`) means DPGS can only approve/revert while a
    decision is still genuinely pending, never undo a decision already made
    (unlike Advisory Committee's post-DPGS-approval revision workflow, a
    separate module). Simulated directly at the DB layer here — exactly
    mirroring what `_revert_registration_to_student` itself does
    (`enrollment.py:251-268`: `stage = "teacher_pending"`, clear
    `dpgs_approved_by`/`dpgs_approved_at`, reset sibling enrollment statuses)
    — so this test verifies the DOWNLOAD ENDPOINT's own reactive behavior
    (the actual thing under test) without depending on a revert mechanism
    that does not exist for an already-finalized registration. Flagged as an
    open question in the final report, not silently assumed."""
    reg_id = S["REG_S2"]
    await _drive_to_dpgs_approved("s2", reg_id)
    r = await _download(reg_id, TOK["s2"])
    assert r.status_code == 200, r.text

    async with AsyncSessionLocal() as db:
        r_row = await db.get(CourseRegistration, reg_id)
        r_row.stage = "teacher_pending"
        r_row.dpgs_approved_by = None
        r_row.dpgs_approved_at = None
        r_row.revert_remark = "ZZTEST simulated revert for download-restriction test"
        items = (await db.execute(select(StudentEnrollment).where(StudentEnrollment.registration_id == reg_id))).scalars().all()
        for item in items:
            item.status = "pending"; item.processed_by = None; item.processed_at = None
        await db.commit()
    assert await _registration_stage(reg_id) == "teacher_pending"

    # 10: download blocked again immediately after the revert
    r = await _download(reg_id, TOK["s2"])
    assert r.status_code == 400, (r.status_code, r.text)

    # Re-drive the SAME registration through the full chain a second time.
    r = await _call("PATCH", f"/enrollment/{(await _enrollment_row('s2')).id}", TOK["teacher1"], params={"status": "approved"})
    assert r.status_code == 200, r.text
    assert await _registration_stage(reg_id) == "card_pending"
    r = await _download(reg_id, TOK["s2"])
    assert r.status_code == 400, (r.status_code, r.text)

    await _drive_to_dpgs_approved("s2", reg_id)

    # 11: download available again after the new cycle's DPGS approval
    r = await _download(reg_id, TOK["s2"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text


async def _enrollment_row(student_key: str) -> StudentEnrollment:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == S["OFFERING"],
        ))).scalar_one()


async def main() -> None:
    await _setup()
    try:
        cases = {
            "1-6: student download blocked at every stage, succeeds only at dpgs_approved (7: tracking unaffected)": t_full_chain_student_download_blocked_until_dpgs_approved,
            "8: cross-student IDOR blocked": t_cross_student_idor_blocked,
            "9a: staff access unchanged at dpgs_approved": t_staff_access_unchanged_at_dpgs_approved_stage,
            "9b: staff access unchanged at an earlier (card_pending) stage": t_staff_access_unchanged_at_earlier_stages,
            "10/11: revert re-blocks download; next DPGS approval re-allows it": t_revert_reblocks_and_next_cycle_reapproves,
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
        record("cleanup: no ZZTEST_RCD row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
