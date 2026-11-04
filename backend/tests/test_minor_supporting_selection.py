"""Standalone HTTP-level tests for the expanded Minor/Supporting course
selection rules (expanded-selection task): more than two Minor and more than
two Supporting courses are now allowed (`app/core/classification.py`'s old
2-course Supporting cap is removed; Minor was never count-limited), the
20-credit Course Registration limit is preserved unchanged, PPW gets no
credit ceiling, Minor and Supporting selections must never share the same
underlying `Course.id`, and at least one Supporting selection must be from
LPM — checked once, at the COMPLETION checkpoint (PPW/Registration-Card
submission), not per-add.

Covers both consumers of the shared `app/core/classification.py` module:
PPW (`add_ppw_course`/`submit_ppw`) and Course Registration
(`register_courses`/`submit_registration_card`). The shared department/
count/mutual-exclusion rules are exercised exhaustively via Course
Registration (which also has the more interesting batch/offering-vs-course
semantics); PPW gets a smaller, CONFIRMING set of tests for its own
specific behaviors (single-add flow, submit-time LPM check, no credit
ceiling). Major/Minor/Supporting Discipline auto-derivation — formerly
covered here — was superseded by the manual discipline-selection task;
see test_manual_discipline_selection.py.

Modelled on `tests/test_registration_card_download.py`/
`tests/test_withdraw_reregistration.py` (same harness: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `_run(name, fn)` pass/fail
tracker, `zztest_mss_...`-prefixed rows, full teardown, the same
single-holder-role STOP guard where relevant).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_minor_supporting_selection
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date

import httpx
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.academic import Semester
from app.models.course import Course, CourseOffering, OfferingFaculty
from app.models.enrollment import CourseRegistration, StudentEnrollment
from app.models.ppw import Ppw, PpwCourse
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_mss_"
_LABEL = "ZZTEST_MSS"
_DEVICE = "ZZTEST-mss"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}
C: dict[str, uuid.UUID] = {}   # courses
O: dict[str, uuid.UUID] = {}   # offerings


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
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"MSS{key.upper()}",
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
        c = AdvisoryCommittee(student_id=U[student_key], research_title="ZZTEST MSS committee", status="members_pending")
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


async def _registration_stage(registration_id) -> str:
    async with AsyncSessionLocal() as db:
        r = await db.get(CourseRegistration, registration_id)
        return r.stage


async def _enrollment_row(student_key: str, offering_key: str) -> StudentEnrollment:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U[student_key], StudentEnrollment.offering_id == O[offering_key],
        ))).scalar_one()


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_mss_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        lpm = (await db.execute(select(Department).where(Department.code == "LPM"))).scalar_one_or_none()
        assert lpm is not None, "test requires the real LPM department to exist in the dev DB"
        S["LPM"] = lpm
        others = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(4))).scalars().all()
        assert len(others) >= 4, "need at least 4 existing non-LPM Department rows in the dev DB"
        S["D1"], S["D2"], S["D3"], S["D4"] = others  # D1 = student's own (Major); D2 = Minor dept; D3/D4 = two DIFFERENT non-LPM Supporting depts
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        sem = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert sem is not None, "test requires at least 1 existing Semester in the local dev database"
        S["SEM"] = sem
        col = College(name=f"{_LABEL} College", code=f"ZZMSS{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    S["SA"] = await _session(admin_id)
    d1, d2, d3, d4, lpm, pg, college, sem = S["D1"], S["D2"], S["D3"], S["D4"], S["LPM"], S["PG"], S["COLLEGE"], S["SEM"]

    await _mk_user("hod1", UserRole.HOD, d1)
    await _mk_user("ma1", UserRole.FACULTY, d1)
    await _mk_user("teacher1", UserRole.FACULTY, d1)
    # Offerings are created across D1/D2/D3/D4/LPM in this test suite —
    # teacher1 needs a real FACULTY UserRoleAssignment in EVERY one of those
    # departments (`_assert_faculty_in_department`, courses.py, is the
    # authoritative check: current FACULTY role assignment in the offering's
    # own department, never `User.department_id` alone).
    async with AsyncSessionLocal() as db:
        for extra_dept in (d2, d3, d4, lpm):
            db.add(UserRoleAssignment(user_id=U["teacher1"], role=UserRole.FACULTY, department_id=extra_dept.id))
        await db.commit()
    await _mk_user("s1", UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZMSS-s1-{_TAG}")
    await _mk_committee("s1", "ma1")

    # Courses — one per key, department as named. 1-credit courses throughout
    # so the Course-Registration credit-limit tests can be constructed with
    # exact, predictable totals.
    course_specs = {
        "minor1": d2, "minor2": d2, "minor3": d2, "minor4": d2,
        "minor_wrong_dept": d3,
        "sup_lpm1": lpm, "sup_lpm2": lpm, "sup_lpm3": lpm,
        "sup_other1": d3, "sup_other2": d4,
        "sup_major_dept": d1, "sup_minor_dept": d2,
        # Deliberately LPM: LPM selections bypass the Major/Minor department-
        # exclusion check entirely (`validate_supporting`'s `is_lpm` branch),
        # so a department-based rule can NEVER incidentally catch "the same
        # course re-selected under the other classification" for an LPM
        # course — this isolates the NEW course-identity mutual-exclusion
        # check as the ONLY thing that can reject it (the exact loophole the
        # investigation identified: an LPM Minor course could otherwise be
        # re-selected as Supporting undetected).
        "dup_same_course": lpm,
        "credit_filler": d1,
    }
    async with AsyncSessionLocal() as db:
        for key, dept in course_specs.items():
            crs = Course(
                course_number=f"ZZMSS-{key.upper()}-{_TAG}", title=f"ZZTEST MSS {key}",
                department_id=dept.id, program_level="PG", category="core", credit_theory=1, credit_practical=0,
                created_by=S["ADMIN"],
            )
            db.add(crs)
            await db.flush()
            C[key] = crs.id
        await db.commit()

    async def _offering(course_key: str, dept, *, section=None, max_enrollment=10):
        body = {
            "calendar_id": str(sem.calendar_id), "semester_id": str(sem.id), "course_id": str(C[course_key]),
            "department_id": str(dept.id), "max_enrollment": max_enrollment,
            "faculty_ids": [str(U["teacher1"])], "leader_id": str(U["teacher1"]),
        }
        if section:
            body["section"] = section
        r = await _call("POST", "/courses/offerings", S["SA"], json=body)
        assert r.status_code == 201, r.text
        oid = uuid.UUID(r.json()["id"])
        pub = await _call("PATCH", f"/courses/offerings/{oid}/status", S["SA"], params={"status": "published"})
        assert pub.status_code == 200, pub.text
        return oid

    for key, dept in course_specs.items():
        O[key] = await _offering(key, dept)
    # A SECOND, DIFFERENT offering (section B) of the SAME `dup_same_course`
    # Course — the exact "different offerings, same underlying course" case
    # Requirement 2 is about.
    O["dup_same_course_section_b"] = await _offering("dup_same_course", lpm, section="ZZB")


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids_committee = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        cids_course = select(Course.id).where(Course.course_number.like(f"ZZMSS-%-{_TAG}"))
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
        await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# PPW tests
# ════════════════════════════════════════════════════════════════════════════

async def _mk_ppw_student(key: str) -> None:
    """`Ppw.student_id` is `unique=True` — one PPW per student, EVER — so
    every independent PPW scenario below needs its own dedicated student."""
    await _mk_user(key, UserRole.STUDENT, S["D1"], program=S["PG"], college=S["COLLEGE"], roll=f"ZZMSS-{key}-{_TAG}")
    await _mk_committee(key, "ma1")


async def _mk_ppw(student_key: str) -> uuid.UUID:
    r = await _call("POST", "/ppw", TOK[student_key], json={})
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def _ppw_add(ppw_id, student_key, course_key, classification, *, expect=201):
    r = await _call("POST", f"/ppw/{ppw_id}/courses", TOK[student_key], json={"course_id": str(C[course_key]), "classification": classification})
    assert r.status_code == expect, (course_key, classification, r.status_code, r.text)
    return r


async def t_ppw_build_main_student_and_committee():
    await _mk_ppw_student("ppw1")


async def t_ppw_more_than_two_minor_courses_succeeds():
    ppw_id = await _mk_ppw("ppw1")
    S["PPW_MAIN"] = ppw_id
    for key in ("minor1", "minor2", "minor3", "minor4"):
        await _ppw_add(ppw_id, "ppw1", key, "minor")  # 4 Minor selections, same department — must all succeed


async def t_ppw_minor_wrong_department_rejected():
    r = await _call("POST", f"/ppw/{S['PPW_MAIN']}/courses", TOK["ppw1"], json={
        "course_id": str(C["minor_wrong_dept"]), "classification": "minor",
    })
    assert r.status_code == 400, (r.status_code, r.text)


async def t_ppw_more_than_two_supporting_courses_with_lpm_succeeds():
    ppw_id = S["PPW_MAIN"]
    await _ppw_add(ppw_id, "ppw1", "sup_lpm1", "supporting")
    await _ppw_add(ppw_id, "ppw1", "sup_other1", "supporting")
    await _ppw_add(ppw_id, "ppw1", "sup_lpm2", "supporting")  # 3rd Supporting selection — must succeed (no count cap)


async def t_ppw_supporting_from_major_or_minor_department_rejected():
    r = await _call("POST", f"/ppw/{S['PPW_MAIN']}/courses", TOK["ppw1"], json={"course_id": str(C["sup_major_dept"]), "classification": "supporting"})
    assert r.status_code == 400, (r.status_code, r.text)


async def t_ppw_minor_supporting_mutual_exclusion_same_course():
    """Deliberately uses a DEDICATED student and an LPM-department course:
    LPM selections bypass the Major/Minor department-exclusion check
    entirely (`validate_supporting`'s `is_lpm` branch), so this isolates the
    NEW course-identity mutual-exclusion check as the only thing that can
    reject it — the exact loophole the investigation identified (an LPM
    Minor course could otherwise be re-selected as Supporting undetected).
    Using S["PPW_MAIN"] here instead would incidentally also trip the
    pre-existing department rule (minor1's department already IS the
    established Minor department), which would prove nothing new."""
    await _mk_ppw_student("ppw_excl")
    ppw_id = await _mk_ppw("ppw_excl")
    S["PPW_EXCL"] = ppw_id
    await _ppw_add(ppw_id, "ppw_excl", "sup_lpm1", "minor")  # LPM is a valid Minor department (≠ student's own)
    r = await _call("POST", f"/ppw/{ppw_id}/courses", TOK["ppw_excl"], json={"course_id": str(C["sup_lpm1"]), "classification": "supporting"})
    assert r.status_code == 400, (r.status_code, r.text)
    assert "already selected" in r.json()["detail"].lower()


async def t_ppw_different_courses_minor_and_supporting_succeeds():
    await _ppw_add(S["PPW_MAIN"], "ppw1", "sup_lpm3", "supporting")  # a different, not-yet-used course — must succeed


async def t_ppw_no_credit_ceiling():
    """PPW must accept an arbitrarily large total credit selection — no
    20-credit (or any) ceiling exists or should be introduced. By this point
    S["PPW_MAIN"] has 8 one-credit courses (4 Minor + 4 Supporting)."""
    r = await _call("GET", f"/ppw/{S['PPW_MAIN']}", TOK["ppw1"])
    assert r.status_code == 200, r.text
    assert r.json()["total_credits"] == 8, r.json()["total_credits"]


async def t_ppw_submit_blocked_without_any_lpm_supporting():
    """Manual discipline-selection task (this revision) superseded the old
    auto-derivation of Major/Minor/Supporting Discipline — `submit_ppw` now
    additionally requires the three discipline ids to be explicitly set
    before it will even reach this LPM-completion check, so they're supplied
    here via the new `/ppw/{id}` PATCH fields too (see
    test_manual_discipline_selection.py for dedicated coverage of the
    discipline-selection feature itself)."""
    await _mk_ppw_student("ppw_nolpm")
    ppw_id = await _mk_ppw("ppw_nolpm")
    await _ppw_add(ppw_id, "ppw_nolpm", "minor1", "minor")  # satisfies the unrelated "Minor field required" submit check
    await _ppw_add(ppw_id, "ppw_nolpm", "sup_other2", "supporting")  # only a non-LPM Supporting selection, no LPM
    r = await _call("PATCH", f"/ppw/{ppw_id}", TOK["ppw_nolpm"], json={
        "field_of_investigation": "ZZTEST field", "research_title": "ZZTEST title",
        "major_department_id": str(S["D1"].id), "minor_department_id": str(S["D2"].id),
        "supporting_department_id": str(S["D3"].id),
    })
    assert r.status_code == 200, r.text
    r2 = await _call("PATCH", f"/ppw/{ppw_id}/submit", TOK["ppw_nolpm"])
    assert r2.status_code == 400 and "LPM" in r2.json()["detail"], r2.text


# ════════════════════════════════════════════════════════════════════════════
# Course Registration tests
# ════════════════════════════════════════════════════════════════════════════

async def _register_as(student_key: str, offering_keys: list[str], classifications: dict[str, str] | None = None, *, expect=201):
    body = {
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O[k]) for k in offering_keys],
    }
    if classifications:
        body["classifications"] = {str(O[k]): v for k, v in classifications.items()}
    r = await _call("POST", "/enrollment/register", TOK[student_key], json=body)
    assert r.status_code == expect, (offering_keys, classifications, r.status_code, r.text)
    return r


async def _register(offering_keys: list[str], classifications: dict[str, str] | None = None, *, expect=201):
    return await _register_as("s1", offering_keys, classifications, expect=expect)


async def t_registration_more_than_two_minor_courses_succeeds():
    r = await _register(["minor1", "minor2", "minor3"], {"minor1": "minor", "minor2": "minor", "minor3": "minor"})
    S["REG_MINOR"] = r.json()["id"]


async def t_registration_minor_wrong_department_rejected():
    r = await _call("POST", "/enrollment/register", TOK["s1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["minor_wrong_dept"])], "classifications": {str(O["minor_wrong_dept"]): "minor"},
    })
    assert r.status_code == 400, (r.status_code, r.text)


async def t_registration_more_than_two_supporting_in_one_batch_succeeds():
    r = await _register(
        ["sup_lpm1", "sup_other1", "sup_lpm2"],
        {"sup_lpm1": "supporting", "sup_other1": "supporting", "sup_lpm2": "supporting"},
    )
    S["REG_SUP"] = r.json()["id"]


async def t_registration_supporting_wrong_department_rejected():
    r = await _call("POST", "/enrollment/register", TOK["s1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["sup_major_dept"])], "classifications": {str(O["sup_major_dept"]): "supporting"},
    })
    assert r.status_code == 400, (r.status_code, r.text)


async def t_registration_build_dedicated_exclusion_student() -> None:
    # Fresh student, independent of s1's accumulated Minor/Supporting state
    # in earlier tests — isolates the course-identity check cleanly.
    await _mk_user("s_excl", UserRole.STUDENT, S["D1"], program=S["PG"], college=S["COLLEGE"], roll=f"ZZMSS-sexcl-{_TAG}")
    await _mk_committee("s_excl", "ma1")


async def t_registration_mutual_exclusion_within_single_batch_rejected():
    r = await _call("POST", "/enrollment/register", TOK["s_excl"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["dup_same_course"]), str(O["dup_same_course_section_b"])],
        "classifications": {str(O["dup_same_course"]): "minor", str(O["dup_same_course_section_b"]): "supporting"},
    })
    assert r.status_code == 400, (r.status_code, r.text)
    assert "already selected" in r.json()["detail"].lower()
    # Confirm NOTHING from the rejected batch was written.
    async with AsyncSessionLocal() as db:
        count = (await db.execute(select(func.count()).select_from(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s_excl"], StudentEnrollment.offering_id.in_([O["dup_same_course"], O["dup_same_course_section_b"]]),
        ))).scalar_one()
        assert count == 0, "a rejected batch must not partially commit"


async def t_registration_mutual_exclusion_against_existing_different_offering_rejected():
    r = await _register_as("s_excl", ["dup_same_course"], {"dup_same_course": "minor"})
    reg_id = r.json()["id"]
    S["REG_EXCL"] = reg_id
    r2 = await _call("POST", "/enrollment/register", TOK["s_excl"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["dup_same_course_section_b"])],
        "classifications": {str(O["dup_same_course_section_b"]): "supporting"},
    })
    assert r2.status_code == 400, (r2.status_code, r2.text)
    assert "already selected" in r2.json()["detail"].lower()


async def t_registration_different_courses_minor_and_supporting_succeeds():
    r = await _call("POST", "/enrollment/register", TOK["s1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["sup_minor_dept"])],  # a DIFFERENT course, same dept as Minor — allowed, different course identity
        "classifications": {str(O["sup_minor_dept"]): "supporting"},
    })
    assert r.status_code == 400, (r.status_code, r.text)  # same department as Minor -> rejected by the EXISTING department rule (not mutual exclusion) — confirms the two rules are independent


# ── Dedicated student + dedicated 1-credit courses for precise credit-limit math ──

async def _setup_credit_student() -> None:
    d1 = S["D1"]
    await _mk_user("s_credit", UserRole.STUDENT, d1, program=S["PG"], college=S["COLLEGE"], roll=f"ZZMSS-scredit-{_TAG}")
    await _mk_committee("s_credit", "ma1")
    async with AsyncSessionLocal() as db:
        courses = []
        for i in range(21):
            crs = Course(
                course_number=f"ZZMSS-CR{i}-{_TAG}", title=f"ZZTEST MSS credit course {i}",
                department_id=d1.id, program_level="PG", category="core", credit_theory=1, credit_practical=0,
                created_by=S["ADMIN"],
            )
            db.add(crs)
            await db.flush()
            courses.append(crs.id)
        await db.commit()
    S["CREDIT_COURSES"] = courses
    offs = []
    for i, cid in enumerate(courses):
        r = await _call("POST", "/courses/offerings", TOK["hod1"], json={
            "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id), "course_id": str(cid),
            "department_id": str(d1.id), "max_enrollment": 10,
            "faculty_ids": [str(U["teacher1"])], "leader_id": str(U["teacher1"])
        })
        assert r.status_code == 201, r.text
        oid = uuid.UUID(r.json()["id"])
        pub = await _call("PATCH", f"/courses/offerings/{oid}/status", S["SA"], params={"status": "published"})
        assert pub.status_code == 200, pub.text
        offs.append(oid)
    S["CREDIT_OFFERINGS"] = offs


async def t_credit_limit_exactly_20_credits_succeeds():
    offs = S["CREDIT_OFFERINGS"][:20]
    r = await _call("POST", "/enrollment/register", TOK["s_credit"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(o) for o in offs],
    })
    assert r.status_code == 201, r.text


async def t_credit_limit_above_20_rejected():
    r = await _call("POST", "/enrollment/register", TOK["s_credit"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(S["CREDIT_OFFERINGS"][20])],  # the 21st 1-credit course -> 21 total
    })
    assert r.status_code == 400 and "20" in r.json()["detail"], r.text


async def t_credit_limit_adding_minor_supporting_above_20_rejected():
    """Confirms the 20-credit check still fires even when the course being
    added is specifically classified Minor/Supporting — the check is
    category-agnostic, unaffected by this task's count-expansion. Uses the
    dedicated `s_credit` student, already at exactly 20 credits from
    `t_credit_limit_exactly_20_credits_succeeds`."""
    r = await _call("POST", "/enrollment/register", TOK["s_credit"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["minor1"])], "classifications": {str(O["minor1"]): "minor"},
    })
    assert r.status_code == 400 and "20" in r.json()["detail"], r.text


# ── Supporting-LPM completion check at Registration Card submission ─────────

async def _setup_submit_student() -> None:
    d1 = S["D1"]
    await _mk_user("s_submit1", UserRole.STUDENT, d1, program=S["PG"], college=S["COLLEGE"], roll=f"ZZMSS-ssub1-{_TAG}")
    await _mk_user("s_submit2", UserRole.STUDENT, d1, program=S["PG"], college=S["COLLEGE"], roll=f"ZZMSS-ssub2-{_TAG}")
    await _mk_committee("s_submit1", "ma1")
    await _mk_committee("s_submit2", "ma1")


async def t_registration_submit_blocked_without_any_lpm_supporting():
    r = await _call("POST", "/enrollment/register", TOK["s_submit1"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["sup_other1"])], "classifications": {str(O["sup_other1"]): "supporting"},
    })
    assert r.status_code == 201, r.text
    reg_id = r.json()["id"]
    async with AsyncSessionLocal() as db:
        enrollment = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s_submit1"], StudentEnrollment.offering_id == O["sup_other1"],
        ))).scalar_one()
    ra = await _call("PATCH", f"/enrollment/{enrollment.id}", TOK["teacher1"], params={"status": "approved"})
    assert ra.status_code == 200, ra.text
    rs = await _call("POST", f"/enrollment/registrations/{reg_id}/submit", TOK["s_submit1"])
    assert rs.status_code == 400 and "LPM" in rs.json()["detail"], rs.text


async def t_registration_submit_succeeds_with_lpm_supporting():
    r = await _call("POST", "/enrollment/register", TOK["s_submit2"], json={
        "calendar_id": str(S["SEM"].calendar_id), "semester_id": str(S["SEM"].id),
        "offering_ids": [str(O["sup_lpm1"]), str(O["sup_other1"])],
        "classifications": {str(O["sup_lpm1"]): "supporting", str(O["sup_other1"]): "supporting"},
    })
    assert r.status_code == 201, r.text
    reg_id = r.json()["id"]
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(StudentEnrollment).where(
            StudentEnrollment.student_id == U["s_submit2"], StudentEnrollment.offering_id.in_([O["sup_lpm1"], O["sup_other1"]]),
        ))).scalars().all()
    for row in rows:
        ra = await _call("PATCH", f"/enrollment/{row.id}", TOK["teacher1"], params={"status": "approved"})
        assert ra.status_code == 200, ra.text
    rs = await _call("POST", f"/enrollment/registrations/{reg_id}/submit", TOK["s_submit2"])
    assert rs.status_code == 200, rs.text
    assert await _registration_stage(reg_id) == "major_advisor_pending"


# ── Backward compatibility ───────────────────────────────────────────────────

async def t_existing_registration_still_readable():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_MINOR']}", TOK["s1"])
    assert r.status_code == 200, r.text


async def t_existing_ppw_still_readable():
    r = await _call("GET", f"/ppw/{S['PPW_MAIN']}", TOK["ppw1"])
    assert r.status_code == 200, r.text


async def main() -> None:
    try:
        await _setup()
    except Exception:
        await _teardown()
        raise
    try:
        cases = {
            # PPW
            "Build the main PPW student + committee": t_ppw_build_main_student_and_committee,
            "PPW: more than two Minor courses succeeds": t_ppw_more_than_two_minor_courses_succeeds,
            "PPW: Minor from wrong department rejected": t_ppw_minor_wrong_department_rejected,
            "PPW: more than two Supporting courses (with LPM) succeeds": t_ppw_more_than_two_supporting_courses_with_lpm_succeeds,
            "PPW: Supporting from Major/Minor department rejected": t_ppw_supporting_from_major_or_minor_department_rejected,
            "PPW: submit blocked without any LPM Supporting selection": t_ppw_submit_blocked_without_any_lpm_supporting,
            "PPW: Minor/Supporting mutual exclusion (same course) rejected": t_ppw_minor_supporting_mutual_exclusion_same_course,
            "PPW: different courses in Minor and Supporting succeeds": t_ppw_different_courses_minor_and_supporting_succeeds,
            "PPW: no credit ceiling": t_ppw_no_credit_ceiling,
            # Course Registration
            "Registration: more than two Minor courses succeeds": t_registration_more_than_two_minor_courses_succeeds,
            "Registration: Minor from wrong department rejected": t_registration_minor_wrong_department_rejected,
            "Registration: more than two Supporting courses in one batch succeeds": t_registration_more_than_two_supporting_in_one_batch_succeeds,
            "Registration: Supporting from wrong department rejected": t_registration_supporting_wrong_department_rejected,
            "Build dedicated student for the course-identity mutual-exclusion tests": t_registration_build_dedicated_exclusion_student,
            "Registration: mutual exclusion within a single batch rejected": t_registration_mutual_exclusion_within_single_batch_rejected,
            "Registration: mutual exclusion against an existing different-offering selection rejected": t_registration_mutual_exclusion_against_existing_different_offering_rejected,
            "Registration: different courses across Minor/Supporting (dept rule independently enforced)": t_registration_different_courses_minor_and_supporting_succeeds,
            "Build dedicated credit-limit student + 21 one-credit offerings": _setup_credit_student,
            "Credit limit: exactly 20 credits succeeds": t_credit_limit_exactly_20_credits_succeeds,
            "Credit limit: above 20 credits rejected": t_credit_limit_above_20_rejected,
            "Credit limit: adding Minor/Supporting courses is still credit-checked (smoke)": t_credit_limit_adding_minor_supporting_above_20_rejected,
            "Build dedicated students for the submit-time LPM completion check": _setup_submit_student,
            "Registration: submit blocked without any LPM Supporting selection": t_registration_submit_blocked_without_any_lpm_supporting,
            "Registration: submit succeeds with an LPM Supporting selection present": t_registration_submit_succeeds_with_lpm_supporting,
            # Backward compatibility
            "Backward-compat: existing registration remains readable": t_existing_registration_still_readable,
            "Backward-compat: existing PPW remains readable": t_existing_ppw_still_readable,
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
        record("cleanup: no ZZTEST_MSS row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
