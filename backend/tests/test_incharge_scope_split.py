"""Standalone HTTP-level tests for the Incharge Academic Cell scope-split
task: the 4 scoped roles (INCHARGE_ACADEMIC_CELL_PG_VETERINARY/_UG_VETERINARY/
_PG_FISHERY/_UG_FISHERY) replacing the single, institution-wide
INCHARGE_ACADEMIC_CELL role.

This file exists specifically to close the gap an independent audit found:
every test fixed during the original implementation proved the OLD green
tests still pass under the new role names, but none of them proved the NEW
restriction actually denies cross-scope access. Every test function below
is a POSITIVE or NEGATIVE authorization assertion over a real HTTP call —
never a bare helper-function check — except the small, explicitly-scoped
`_offering_incharge_scope_match` unit section (Section F), which the audit
asked for directly since Gradesheet has no per-student record to drive an
HTTP call through for this specific matrix.

All test Programmes/Departments are synthetic, zztest-tagged fixtures,
including the 2 UG programmes (no production UG programme exists yet) and
the 2 non-Veterinary-stream departments (the dev database has none —
confirmed by direct inspection before writing this file).

Modelled on test_manual_discipline_selection.py / test_comprehensive_exam.py
(same harness: real FastAPI app via `httpx.ASGITransport`, real JWT
sessions, a `_run(name, fn)` pass/fail tracker, `zztest_isc_...`-prefixed
rows, full teardown).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_incharge_scope_split
"""
import asyncio
import hashlib
import sys
import uuid
from datetime import date
from types import SimpleNamespace

import httpx
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.core.security import create_access_token
from app.core.student_scope import programme_group
from app.db.base import AsyncSessionLocal
from app.main import app
from app.api.v1.endpoints.grading import _offering_incharge_scope_match
from app.models.academic import AcademicCalendar, Semester
from app.models.course import Course, CourseOffering
from app.models.enrollment import CourseRegistration
from app.models.grading import GradeSheet, GradesheetCycle
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_isc_"
_LABEL = "ZZTEST_ISC"
_DEVICE = "ZZTEST-isc"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
TOK: dict[str, str] = {}
S: dict = {}

_ALL_SCOPE_ROLES = (
    "INCHARGE_ACADEMIC_CELL_PG_VETERINARY", "INCHARGE_ACADEMIC_CELL_UG_VETERINARY",
    "INCHARGE_ACADEMIC_CELL_PG_FISHERY", "INCHARGE_ACADEMIC_CELL_UG_FISHERY",
)


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


async def _mk_user(key, role, dept=None, *, program=None, roll=None, extra_roles: list | None = None) -> None:
    """`extra_roles` — additional (role, department) pairs for the SAME user
    (multi-role/multi-scope fixtures), mirroring every other harness's
    established convention in this test suite."""
    is_student = role == UserRole.STUDENT
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"ISC{key.upper()}",
            role=role, department_id=dept.id if dept else None, program_id=program.id if program else None,
            student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True,
            date_of_birth=date(2000, 1, 1) if is_student else None,
            father_name="ZZTEST Father" if is_student else None,
            address="ZZTEST Address" if is_student else None,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if not is_student:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
            for extra_role, extra_dept in (extra_roles or []):
                db.add(UserRoleAssignment(user_id=u.id, role=extra_role, department_id=extra_dept.id if extra_dept else None))
        await db.commit()
    TOK[key] = await _session(U[key])


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


async def _mk_registration(student_key: str, stage: str = "incharge_pending") -> uuid.UUID:
    """Directly constructs a `CourseRegistration` row (bypassing the full
    registration HTTP flow, which requires an established Advisory
    Committee) — this file is testing the INCHARGE-stage authorization
    boundary specifically, not the registration workflow itself."""
    async with AsyncSessionLocal() as db:
        r = CourseRegistration(student_id=U[student_key], semester_id=S["SEM"].id, calendar_id=S["SEM"].calendar_id, stage=stage)
        db.add(r)
        await db.commit()
        return r.id


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_isc_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role in "
            "('INCHARGE_ACADEMIC_CELL_PG_VETERINARY','INCHARGE_ACADEMIC_CELL_UG_VETERINARY',"
            "'INCHARGE_ACADEMIC_CELL_PG_FISHERY','INCHARGE_ACADEMIC_CELL_UG_FISHERY','DPGS')"
        ))).scalar_one()
        if holders:
            print("STOP: a real Incharge Academic Cell / DPGS holder exists (single-holder roles); refusing to run.")
            sys.exit(2)

        d_vet = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(1))).scalar_one()
        S["D_VET"] = d_vet
        # Fishery and dual-stream departments: confirmed via direct inspection
        # that NONE exist in this dev database (all 20 existing departments
        # are stream='Veterinary') — synthetic test-only fixtures, per the
        # explicit allowance for test-only fixtures in this task.
        d_fish = Department(name=f"{_LABEL} Fishery Dept", code=f"ZZISCF{_TAG.upper()}"[:20], stream="Fishery")
        d_dual = Department(name=f"{_LABEL} Dual-Stream Admin Dept", code=f"ZZISCD{_TAG.upper()}"[:20], stream="Veterinary/Fishery")
        col = College(name=f"{_LABEL} College", code=f"ZZISC{_TAG.upper()}"[:20])
        db.add_all([d_fish, d_dual, col])
        await db.flush()
        S["D_FISH"], S["D_DUAL"], S["COLLEGE"] = d_fish, d_dual, col

        # Synthetic Programmes — every (level, faculty) combination needed,
        # including the 2 UG ones (no production/dev UG programme exists).
        programs = {
            "PG_VET": ("PG", "Veterinary"), "PHD_VET": ("PhD", "Veterinary"),
            "PG_FISH": ("PG", "Fishery"), "PHD_FISH": ("PhD", "Fishery"),
            "UG_VET": ("UG", "Veterinary"), "UG_FISH": ("UG", "Fishery"),
            "NULL_FACULTY": ("PG", None),
        }
        for key, (level, faculty) in programs.items():
            p = Program(name=f"{_LABEL} {key}", code=f"ZZISC-{key}-{_TAG}"[:20], level=level, faculty=faculty)
            db.add(p)
            await db.flush()
            S[f"P_{key}"] = p

        sem = (await db.execute(select(Semester).limit(1))).scalars().first()
        assert sem is not None, "test requires at least 1 existing Semester in the local dev database"
        S["SEM"] = sem
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    S["SA"] = await _session(admin_id)

    # Students — one per Programme fixture above.
    await _mk_user("s_pg_vet", UserRole.STUDENT, S["D_VET"], program=S["P_PG_VET"], roll=f"ZZISC-pgvet-{_TAG}")
    await _mk_user("s_phd_vet", UserRole.STUDENT, S["D_VET"], program=S["P_PHD_VET"], roll=f"ZZISC-phdvet-{_TAG}")
    await _mk_user("s_pg_fish", UserRole.STUDENT, S["D_FISH"], program=S["P_PG_FISH"], roll=f"ZZISC-pgfish-{_TAG}")
    await _mk_user("s_phd_fish", UserRole.STUDENT, S["D_FISH"], program=S["P_PHD_FISH"], roll=f"ZZISC-phdfish-{_TAG}")
    await _mk_user("s_ug_vet", UserRole.STUDENT, S["D_VET"], program=S["P_UG_VET"], roll=f"ZZISC-ugvet-{_TAG}")
    await _mk_user("s_ug_fish", UserRole.STUDENT, S["D_FISH"], program=S["P_UG_FISH"], roll=f"ZZISC-ugfish-{_TAG}")
    await _mk_user("s_null_faculty", UserRole.STUDENT, S["D_VET"], program=S["P_NULL_FACULTY"], roll=f"ZZISC-nullfac-{_TAG}")

    # Incharge Academic Cell holders. Each of the 4 scopes is single-holder
    # (DB partial unique index) — exactly ONE user may hold each across this
    # whole fixture set, so the 4 scopes are distributed across exactly 3
    # identities: 2 dedicated single-scope holders (PG-Veterinary,
    # UG-Fishery) + 1 holder covering the OTHER 2 scopes at once
    # (UG-Veterinary + PG-Fishery — a deliberately CROSS-AXIS *and*
    # CROSS-FACULTY pair: this proves the union mechanism works across both
    # the programme-group axis AND the faculty axis simultaneously, and lets
    # the staff-directory tests exercise a genuine "holds both faculties"
    # identity without ever assigning the SAME scope to two different users
    # — which the single-holder partial unique index correctly refuses).
    await _mk_user("inc_pg_vet", UserRole.INCHARGE_ACADEMIC_CELL_PG_VETERINARY, None)
    await _mk_user("inc_ug_fish", UserRole.INCHARGE_ACADEMIC_CELL_UG_FISHERY, None)
    await _mk_user("inc_multi", UserRole.INCHARGE_ACADEMIC_CELL_UG_VETERINARY, None,
                   extra_roles=[(UserRole.INCHARGE_ACADEMIC_CELL_PG_FISHERY, None)])

    # Regression control group — unaffected roles.
    await _mk_user("dpgs", UserRole.DPGS, None)
    await _mk_user("hod_vet", UserRole.HOD, S["D_VET"])

    # Staff-directory fixtures (Section 9) — varying department/stream.
    await _mk_user("staff_vet", UserRole.FACULTY, S["D_VET"])
    await _mk_user("staff_fish", UserRole.FACULTY, S["D_FISH"])
    # Departmentless staff — LIBRARIAN is a genuinely departmentless role by
    # design (see UserRole's own docstring); used here rather than a
    # FACULTY/HOD row with a NULL department, which the application itself
    # never actually produces.
    await _mk_user("staff_nodept", UserRole.LIBRARIAN, None)
    # Dual-stream administrative department staff — directly constructed
    # (HOD normally requires a real department via add_user_role's own
    # validation; this constructs the row directly, mirroring how AVFU's
    # real DPGS department reportedly has HOD-equivalent staff today).
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}staff_dual_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name="ISCSTAFFDUAL",
            role=UserRole.HOD, department_id=S["D_DUAL"].id,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U["staff_dual"] = u.id
        db.add(UserRoleAssignment(user_id=u.id, role=UserRole.HOD, department_id=S["D_DUAL"].id))
        await db.commit()
    TOK["staff_dual"] = await _session(U["staff_dual"])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        reg_ids = select(CourseRegistration.id).where(CourseRegistration.student_id.in_(uids))
        cids_course = select(Course.id).where(Course.course_number.like(f"ZZISC-%-{_TAG}"))
        oids = select(CourseOffering.id).where(CourseOffering.course_id.in_(cids_course))
        sheet_ids = select(GradeSheet.id).where(GradeSheet.offering_id.in_(oids))
        cycle_ids = select(GradesheetCycle.id).where(GradesheetCycle.sheet_id.in_(sheet_ids))

        from app.models.enrollment import StudentEnrollment
        await db.execute(delete(StudentEnrollment).where(StudentEnrollment.offering_id.in_(oids)))
        await db.execute(delete(GradesheetCycle).where(GradesheetCycle.id.in_(cycle_ids)))
        await db.execute(delete(GradeSheet).where(GradeSheet.id.in_(sheet_ids)))
        await db.execute(delete(CourseOffering).where(CourseOffering.id.in_(oids)))
        await db.execute(delete(Course).where(Course.id.in_(cids_course)))
        await db.execute(delete(CourseRegistration).where(CourseRegistration.id.in_(reg_ids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(Program).where(Program.name.like(f"{_LABEL}%")))
        await db.execute(delete(Department).where(Department.name.like(f"{_LABEL}%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


# ════════════════════════════════════════════════════════════════════════════
# Section A — Individual record retrieval (Course Registration)
# ════════════════════════════════════════════════════════════════════════════

async def t_build_registrations():
    S["REG_PG_VET"] = await _mk_registration("s_pg_vet")
    S["REG_PHD_VET"] = await _mk_registration("s_phd_vet")
    S["REG_PG_FISH"] = await _mk_registration("s_pg_fish")
    S["REG_PHD_FISH"] = await _mk_registration("s_phd_fish")
    S["REG_UG_VET"] = await _mk_registration("s_ug_vet")
    S["REG_UG_FISH"] = await _mk_registration("s_ug_fish")
    S["REG_NULL_FACULTY"] = await _mk_registration("s_null_faculty")


async def t_pg_vet_can_view_pg_vet_registration():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PG_VET']}", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text


async def t_pg_vet_cannot_view_pg_fish_registration():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PG_FISH']}", TOK["inc_pg_vet"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_pg_vet_cannot_view_ug_vet_registration():
    """UG scope denial: a PG-scoped Incharge must not see a UG student's
    registration, even though the faculty (Veterinary) matches."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_UG_VET']}", TOK["inc_pg_vet"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_pg_fish_cannot_view_pg_vet_registration():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PG_VET']}", TOK["inc_multi"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_pg_fish_can_view_pg_fish_registration():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PG_FISH']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text


async def t_ug_vet_can_view_ug_vet_registration():
    """inc_multi holds UG_VETERINARY (among others)."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_UG_VET']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text


async def t_ug_fish_can_view_ug_fish_registration():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_UG_FISH']}", TOK["inc_ug_fish"])
    assert r.status_code == 200, r.text


async def t_ug_vet_cannot_view_pg_vet_registration():
    """UG scopes must not grant access to PG records, even same faculty."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PG_VET']}", TOK["inc_multi"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_ug_vet_cannot_view_phd_vet_registration():
    """UG scopes must not grant access to PhD records either."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PHD_VET']}", TOK["inc_multi"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_pg_vet_scope_covers_phd_vet_same_faculty():
    """PG scope must grant access to PhD of the SAME faculty (programme_group merges PG+PhD)."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PHD_VET']}", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text


async def t_pg_vet_scope_does_not_cover_phd_fish():
    """PG-Veterinary must NOT cover PhD-Fishery — faculty axis still applies."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PHD_FISH']}", TOK["inc_pg_vet"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_missing_faculty_denied_regardless_of_caller_scope():
    """Section 5 — Program.faculty IS NULL must fail closed for EVERY scope,
    not just the "wrong" ones."""
    for tok_key in ("inc_pg_vet", "inc_ug_fish", "inc_multi"):
        r = await _call("GET", f"/enrollment/registrations/{S['REG_NULL_FACULTY']}", TOK[tok_key])
        assert r.status_code == 403, (tok_key, r.status_code, r.text)


# ── Approval action + "unauthorized write does not modify state" ───────────

async def t_pg_vet_can_approve_pg_vet_registration():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG_PG_VET']}/incharge-approval", TOK["inc_pg_vet"], json={"approved": True})
    assert r.status_code == 200, r.text
    async with AsyncSessionLocal() as db:
        reg = await db.get(CourseRegistration, S["REG_PG_VET"])
        assert reg.stage == "dpgs_pending", reg.stage


async def t_pg_fish_cannot_approve_pg_vet_registration_and_state_unchanged():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG_PHD_VET']}/incharge-approval", TOK["inc_multi"], json={"approved": True})
    assert r.status_code == 403, (r.status_code, r.text)
    async with AsyncSessionLocal() as db:
        reg = await db.get(CourseRegistration, S["REG_PHD_VET"])
        assert reg.stage == "incharge_pending", "a denied approval attempt must NOT change the registration's stage"


async def t_ug_vet_cannot_approve_ug_fish_registration_and_state_unchanged():
    r = await _call("PATCH", f"/enrollment/registrations/{S['REG_UG_FISH']}/incharge-approval", TOK["inc_multi"], json={"approved": True})
    assert r.status_code == 403, (r.status_code, r.text)
    async with AsyncSessionLocal() as db:
        reg = await db.get(CourseRegistration, S["REG_UG_FISH"])
        assert reg.stage == "incharge_pending", "a denied approval attempt must NOT change the registration's stage"


async def t_invalid_faculty_value_cannot_be_stored():
    """Section 5 (invalid values) — the DB CHECK constraint (migration 0047,
    `ck_program_faculty`) means an invalid faculty can never reach the
    authorization layer at all; asserted here rather than bypassed."""
    from sqlalchemy.exc import IntegrityError
    rejected = False
    async with AsyncSessionLocal() as db:
        db.add(Program(name=f"{_LABEL} BAD_FACULTY", code=f"ZZISC-BAD-{_TAG}"[:20], level="PG", faculty="Aquaculture"))
        try:
            await db.commit()
        except IntegrityError:
            rejected = True
            await db.rollback()
    assert rejected, "ck_program_faculty must reject a faculty outside Veterinary/Fishery"


# ════════════════════════════════════════════════════════════════════════════
# Section B — Multi-scope union behavior
# ════════════════════════════════════════════════════════════════════════════

async def t_multi_scope_can_view_ug_vet():
    """inc_multi holds UG_VETERINARY + PG_FISHERY — a deliberately
    CROSS-AXIS *and* CROSS-FACULTY pair, chosen specifically so the
    single-holder constraint on the OTHER 2 scopes (held by inc_pg_vet/
    inc_ug_fish) is never violated, while still exercising a genuine
    "holds both faculties" identity."""
    r = await _call("GET", f"/enrollment/registrations/{S['REG_UG_VET']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text


async def t_multi_scope_can_view_pg_fish():
    r = await _call("GET", f"/enrollment/registrations/{S['REG_PG_FISH']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text


async def t_multi_scope_still_denied_scopes_it_does_not_hold():
    """A multi-scope holder is NOT institution-wide — it must still be
    denied every scope it was never granted (the other faculty's PG record,
    AND the same-faculty-different-group UG-Fishery record)."""
    r1 = await _call("GET", f"/enrollment/registrations/{S['REG_PG_VET']}", TOK["inc_multi"])
    assert r1.status_code == 403, (r1.status_code, r1.text)
    r2 = await _call("GET", f"/enrollment/registrations/{S['REG_UG_FISH']}", TOK["inc_multi"])
    assert r2.status_code == 403, (r2.status_code, r2.text)


async def t_multi_scope_list_returns_union_no_duplicates():
    """GET /enrollment/registrations as the multi-scope holder must include
    BOTH the UG-Veterinary and PG-Fishery registrations exactly once each
    (no duplicate rows from the OR'd scope clause), and must exclude the
    PG-Veterinary and UG-Fishery ones (neither of its held scopes)."""
    r = await _call("GET", "/enrollment/registrations", TOK["inc_multi"])
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()]
    assert ids.count(str(S["REG_UG_VET"])) == 1, ids
    assert ids.count(str(S["REG_PG_FISH"])) == 1, ids
    assert str(S["REG_PG_VET"]) not in ids
    assert str(S["REG_UG_FISH"]) not in ids


async def t_single_scope_list_excludes_other_faculty():
    r = await _call("GET", "/enrollment/registrations", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()]
    assert str(S["REG_PG_VET"]) in ids
    assert str(S["REG_PG_FISH"]) not in ids
    assert str(S["REG_UG_VET"]) not in ids


# ════════════════════════════════════════════════════════════════════════════
# Section C — Published results / GPA / Credit details
# ════════════════════════════════════════════════════════════════════════════

async def t_published_results_authorized_same_scope():
    r = await _call("GET", f"/results/student/{U['s_pg_vet']}", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text


async def t_published_results_denied_cross_faculty():
    r = await _call("GET", f"/results/student/{U['s_pg_fish']}", TOK["inc_pg_vet"])
    assert r.status_code == 404, (r.status_code, r.text)  # results.py's own convention: 404, never 403


async def t_published_results_denied_cross_level():
    r = await _call("GET", f"/results/student/{U['s_ug_vet']}", TOK["inc_pg_vet"])
    assert r.status_code == 404, (r.status_code, r.text)


async def t_gpa_authorized_same_scope():
    r = await _call("GET", f"/grading/student/{U['s_ug_fish']}/gpa", TOK["inc_ug_fish"])
    assert r.status_code == 200, r.text


async def t_gpa_denied_cross_scope():
    r = await _call("GET", f"/grading/student/{U['s_ug_fish']}/gpa", TOK["inc_pg_vet"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_credit_details_authorized_same_scope():
    r = await _call("GET", f"/credit-details/student/{U['s_phd_fish']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text


async def t_credit_details_denied_cross_scope():
    r = await _call("GET", f"/credit-details/student/{U['s_phd_fish']}", TOK["inc_pg_vet"])
    assert r.status_code == 403, (r.status_code, r.text)


async def t_credit_details_denied_missing_faculty():
    r = await _call("GET", f"/credit-details/student/{U['s_null_faculty']}", TOK["inc_pg_vet"])
    assert r.status_code == 403, (r.status_code, r.text)


# ════════════════════════════════════════════════════════════════════════════
# Section D — Course Students (per-student filtering WITHIN one offering)
# ════════════════════════════════════════════════════════════════════════════

async def t_build_course_students_fixture():
    """ONE offering (Veterinary department), with BOTH a PG-Veterinary and a
    PG-Fishery student enrolled — proves the roster is filtered per-student,
    not by the offering's own department."""
    async with AsyncSessionLocal() as db:
        crs = Course(course_number=f"ZZISC-CS1-{_TAG}", title="ZZTEST ISC course students",
                     department_id=S["D_VET"].id, program_level="PG", credit_theory=2, credit_practical=0,
                     created_by=S["ADMIN"])
        db.add(crs)
        await db.flush()
        S["CS_COURSE"] = crs
        off = CourseOffering(calendar_id=S["SEM"].calendar_id, semester_id=S["SEM"].id, course_id=crs.id,
                              department_id=S["D_VET"].id, status="published")
        db.add(off)
        await db.flush()
        S["CS_OFFERING"] = off.id
        await db.commit()
    from app.models.enrollment import StudentEnrollment
    async with AsyncSessionLocal() as db:
        db.add_all([
            StudentEnrollment(student_id=U["s_pg_vet"], offering_id=S["CS_OFFERING"], status="approved"),
            StudentEnrollment(student_id=U["s_pg_fish"], offering_id=S["CS_OFFERING"], status="approved"),
            StudentEnrollment(student_id=U["s_ug_vet"], offering_id=S["CS_OFFERING"], status="approved"),
        ])
        await db.commit()


async def t_course_students_pg_vet_sees_only_own_faculty_student():
    r = await _call("GET", f"/enrollment/offering/{S['CS_OFFERING']}", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text
    names = {row["student_name"] for row in r.json()}
    assert "ZZTEST ISCS_PG_VET" in {n.upper() for n in names if n}
    assert "ZZTEST ISCS_PG_FISH" not in {n.upper() for n in names if n}


async def t_course_students_pg_fish_sees_only_own_faculty_student():
    r = await _call("GET", f"/enrollment/offering/{S['CS_OFFERING']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text
    names = {row["student_name"] for row in r.json()}
    assert "ZZTEST ISCS_PG_FISH" in {n.upper() for n in names if n}
    assert "ZZTEST ISCS_PG_VET" not in {n.upper() for n in names if n}


async def t_course_students_multi_scope_sees_both_its_own_scopes_not_others():
    """inc_multi holds UG_VETERINARY + PG_FISHERY — must see both of ITS OWN
    scope-populations in this one shared roster (s_pg_fish and s_ug_vet),
    and still not the PG-Veterinary student (s_pg_vet) it was never
    granted."""
    r = await _call("GET", f"/enrollment/offering/{S['CS_OFFERING']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text
    names = {n.upper() for n in (row["student_name"] for row in r.json()) if n}
    assert "ZZTEST ISCS_PG_FISH" in names and "ZZTEST ISCS_UG_VET" in names
    assert "ZZTEST ISCS_PG_VET" not in names


async def t_course_students_dpgs_global_unchanged():
    """Section 8 regression — DPGS keeps unrestricted roster access."""
    r = await _call("GET", f"/enrollment/offering/{S['CS_OFFERING']}", TOK["dpgs"])
    assert r.status_code == 200, r.text
    names = {n.upper() for n in (row["student_name"] for row in r.json()) if n}
    assert "ZZTEST ISCS_PG_VET" in names and "ZZTEST ISCS_PG_FISH" in names


async def t_course_students_super_admin_unchanged():
    r = await _call("GET", f"/enrollment/offering/{S['CS_OFFERING']}", S["SA"])
    assert r.status_code == 200, r.text
    names = {n.upper() for n in (row["student_name"] for row in r.json()) if n}
    assert "ZZTEST ISCS_PG_VET" in names and "ZZTEST ISCS_PG_FISH" in names


async def t_course_students_hod_unchanged():
    """Section 8 regression — HOD's own pre-existing department-scoped
    access is untouched by this task."""
    r = await _call("GET", f"/enrollment/offering/{S['CS_OFFERING']}", TOK["hod_vet"])
    assert r.status_code == 200, r.text


# ════════════════════════════════════════════════════════════════════════════
# Section E — Staff directory (current behavior, AVFU confirmation pending)
# ════════════════════════════════════════════════════════════════════════════

async def t_staff_directory_veterinary_incharge_sees_veterinary_staff():
    r = await _call("GET", "/auth/users", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert any(U["staff_vet"] == uuid.UUID(u["id"]) for u in r.json() if "id" in u) or f"{_PFX}staff_vet_{_TAG}@avfu.ac.in" in emails


async def t_staff_directory_veterinary_incharge_excludes_fishery_staff():
    r = await _call("GET", "/auth/users", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert f"{_PFX}staff_fish_{_TAG}@avfu.ac.in" not in emails


async def t_staff_directory_fishery_incharge_sees_fishery_excludes_veterinary():
    """inc_ug_fish holds ONLY UG_FISHERY — a single-faculty (Fishery) scope,
    unlike inc_multi (which spans both faculties — see below)."""
    r = await _call("GET", "/auth/users", TOK["inc_ug_fish"])
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert f"{_PFX}staff_fish_{_TAG}@avfu.ac.in" in emails
    assert f"{_PFX}staff_vet_{_TAG}@avfu.ac.in" not in emails


async def t_staff_directory_departmentless_staff_excluded_from_both():
    r_vet = await _call("GET", "/auth/users", TOK["inc_pg_vet"])
    r_fish = await _call("GET", "/auth/users", TOK["inc_ug_fish"])
    emails_vet = {u["email"] for u in r_vet.json()}
    emails_fish = {u["email"] for u in r_fish.json()}
    target = f"{_PFX}staff_nodept_{_TAG}@avfu.ac.in"
    assert target not in emails_vet and target not in emails_fish


async def t_staff_directory_dual_stream_admin_dept_excluded_from_both():
    """Current documented behavior (Section 4/9 of the audit) — the DPGS-
    like dual-stream administrative department's staff are never shown to
    ANY single-faculty-scoped Incharge viewer. This is an AVFU-unconfirmed
    judgment call, not a specified rule — left open intentionally."""
    r_vet = await _call("GET", "/auth/users", TOK["inc_pg_vet"])
    r_fish = await _call("GET", "/auth/users", TOK["inc_ug_fish"])
    emails_vet = {u["email"] for u in r_vet.json()}
    emails_fish = {u["email"] for u in r_fish.json()}
    target = f"{_PFX}staff_dual_{_TAG}@avfu.ac.in"
    assert target not in emails_vet and target not in emails_fish


async def t_staff_directory_multi_scope_sees_both_faculties():
    """inc_multi holds UG_VETERINARY + PG_FISHERY — one scope of EACH
    faculty — so `held_faculties` is genuinely {"Veterinary","Fishery"}
    here, the one identity in this fixture set that legitimately sees both
    populations. Still excludes the dual-stream admin department's staff
    even though it "has" both faculties — confirms that exclusion is not
    merely an artifact of only ever testing single-faculty callers."""
    r = await _call("GET", "/auth/users", TOK["inc_multi"])
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert f"{_PFX}staff_vet_{_TAG}@avfu.ac.in" in emails
    assert f"{_PFX}staff_fish_{_TAG}@avfu.ac.in" in emails
    assert f"{_PFX}staff_dual_{_TAG}@avfu.ac.in" not in emails


async def t_staff_directory_dpgs_global_unchanged():
    r = await _call("GET", "/auth/users", TOK["dpgs"])
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert f"{_PFX}staff_vet_{_TAG}@avfu.ac.in" in emails and f"{_PFX}staff_fish_{_TAG}@avfu.ac.in" in emails


async def t_staff_directory_super_admin_unchanged():
    r = await _call("GET", "/auth/users", S["SA"])
    assert r.status_code == 200, r.text
    emails = {u["email"] for u in r.json()}
    assert f"{_PFX}staff_vet_{_TAG}@avfu.ac.in" in emails and f"{_PFX}staff_fish_{_TAG}@avfu.ac.in" in emails


# ════════════════════════════════════════════════════════════════════════════
# Section F — Gradesheet: _offering_incharge_scope_match (function-level,
# per the audit's explicit request — Gradesheet has no single student
# record an HTTP call can target for this exact matrix) + one real HTTP case
# ════════════════════════════════════════════════════════════════════════════

def _offering_stub(department_stream, program_level):
    """Lightweight stand-in for a `CourseOffering` row — only the two
    attribute paths `_offering_incharge_scope_match` actually reads
    (`offering.course.program_level`, `offering.department.stream`)."""
    department = SimpleNamespace(stream=department_stream) if department_stream is not None else None
    course = SimpleNamespace(program_level=program_level) if program_level is not None else None
    return SimpleNamespace(course=course, department=department)


async def t_scope_match_veterinary_department():
    offering = _offering_stub("Veterinary", "PG")
    assert _offering_incharge_scope_match({("PG", "Veterinary")}, offering) is True
    assert _offering_incharge_scope_match({("PG", "Fishery")}, offering) is False


async def t_scope_match_fishery_department():
    offering = _offering_stub("Fishery", "PG")
    assert _offering_incharge_scope_match({("PG", "Fishery")}, offering) is True
    assert _offering_incharge_scope_match({("PG", "Veterinary")}, offering) is False


async def t_scope_match_dpgs_dual_stream_department_never_matches():
    offering = _offering_stub("Veterinary/Fishery", "PG")
    for scope in ({("PG", "Veterinary")}, {("PG", "Fishery")}, {("UG", "Veterinary")}, {("UG", "Fishery")},
                  {("PG", "Veterinary"), ("PG", "Fishery")}):
        assert _offering_incharge_scope_match(scope, offering) is False, scope


async def t_scope_match_missing_department():
    offering = _offering_stub(None, "PG")
    assert _offering_incharge_scope_match({("PG", "Veterinary")}, offering) is False


async def t_scope_match_invalid_programme_level():
    offering = _offering_stub("Veterinary", "NOT_A_REAL_LEVEL")
    assert _offering_incharge_scope_match({("PG", "Veterinary")}, offering) is False
    assert _offering_incharge_scope_match({("UG", "Veterinary")}, offering) is False


async def t_scope_match_phd_grouped_as_pg():
    offering = _offering_stub("Veterinary", "PhD")
    assert _offering_incharge_scope_match({("PG", "Veterinary")}, offering) is True
    assert _offering_incharge_scope_match({("UG", "Veterinary")}, offering) is False


async def t_scope_match_same_course_different_faculty_offerings_isolated():
    """Two offerings of the SAME underlying course, one per department/
    faculty — each must be scoped independently, with no cross-faculty
    leakage between them."""
    course = SimpleNamespace(program_level="PG")
    offering_vet = SimpleNamespace(course=course, department=SimpleNamespace(stream="Veterinary"))
    offering_fish = SimpleNamespace(course=course, department=SimpleNamespace(stream="Fishery"))
    vet_scope = {("PG", "Veterinary")}
    fish_scope = {("PG", "Fishery")}
    assert _offering_incharge_scope_match(vet_scope, offering_vet) is True
    assert _offering_incharge_scope_match(vet_scope, offering_fish) is False
    assert _offering_incharge_scope_match(fish_scope, offering_vet) is False
    assert _offering_incharge_scope_match(fish_scope, offering_fish) is True


async def t_build_gradesheet_fixture():
    """One real, submitted (cycle exists) GradeSheet per department, for a
    genuine HTTP-level Gradesheet authorization check alongside the
    function-level unit tests above."""
    async with AsyncSessionLocal() as db:
        for key, dept in (("GS_VET", S["D_VET"]), ("GS_FISH", S["D_FISH"])):
            crs = Course(course_number=f"ZZISC-{key}-{_TAG}", title=f"ZZTEST ISC {key}",
                         department_id=dept.id, program_level="PG", credit_theory=2, credit_practical=0,
                         created_by=S["ADMIN"])
            db.add(crs)
            await db.flush()
            off = CourseOffering(calendar_id=S["SEM"].calendar_id, semester_id=S["SEM"].id, course_id=crs.id,
                                  department_id=dept.id, status="published")
            db.add(off)
            await db.flush()
            sheet = GradeSheet(offering_id=off.id, status="incharge_pending", created_by=S["ADMIN"])
            db.add(sheet)
            await db.flush()
            db.add(GradesheetCycle(sheet_id=sheet.id, cycle_number=1, status="open", submitted_by=S["ADMIN"]))
            S[key] = sheet.id
        await db.commit()


async def t_gradesheet_http_pg_vet_sees_veterinary_sheet():
    r = await _call("GET", f"/grading/sheets/{S['GS_VET']}", TOK["inc_pg_vet"])
    assert r.status_code == 200, r.text


async def t_gradesheet_http_pg_vet_denied_fishery_sheet():
    r = await _call("GET", f"/grading/sheets/{S['GS_FISH']}", TOK["inc_pg_vet"])
    assert r.status_code == 404, (r.status_code, r.text)


async def t_gradesheet_http_pg_fish_sees_fishery_sheet():
    r = await _call("GET", f"/grading/sheets/{S['GS_FISH']}", TOK["inc_multi"])
    assert r.status_code == 200, r.text


async def t_gradesheet_http_pg_fish_denied_veterinary_sheet():
    r = await _call("GET", f"/grading/sheets/{S['GS_VET']}", TOK["inc_multi"])
    assert r.status_code == 404, (r.status_code, r.text)


async def t_gradesheet_http_dpgs_sees_both_unchanged():
    r1 = await _call("GET", f"/grading/sheets/{S['GS_VET']}", TOK["dpgs"])
    r2 = await _call("GET", f"/grading/sheets/{S['GS_FISH']}", TOK["dpgs"])
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)


# ════════════════════════════════════════════════════════════════════════════
# Section G — Single-holder constraint + multi-scope assignment
# ════════════════════════════════════════════════════════════════════════════

async def _holders_of(role: UserRole) -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.role == role))).scalar_one()


async def t_single_holder_second_assignment_rejected_by_api():
    """All 4 scopes are already held (by the fixture identities), so a
    second user — holder2 — must be refused EVERY one of them (409), and
    no row may be persisted for any refused attempt."""
    await _mk_user("holder2", UserRole.FACULTY, S["D_VET"])
    for role in _ALL_SCOPE_ROLES:
        r = await _call("POST", f"/auth/users/{U['holder2']}/roles", S["SA"], json={"role": role.lower()})
        assert r.status_code == 409, (role, r.status_code, r.text)
        assert await _holders_of(UserRole[role]) >= 1
    for role in _ALL_SCOPE_ROLES:
        n = await _holders_of(UserRole[role])
        assert n == 1, f"{role}: expected exactly 1 holder, found {n}"
    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(
            UserRoleAssignment.user_id == U["holder2"], UserRoleAssignment.role.in_([UserRole[r] for r in _ALL_SCOPE_ROLES])))).scalar_one()
        assert n == 0, "a refused assignment must not leave a row behind"


async def t_single_holder_enforced_by_database_index():
    """Bypass the API pre-flight entirely: a direct INSERT of an already-held
    scope must be rejected by the partial unique index itself (migration 0047)."""
    from sqlalchemy.exc import IntegrityError
    rejected = False
    async with AsyncSessionLocal() as db:
        db.add(UserRoleAssignment(user_id=U["holder2"], role=UserRole.INCHARGE_ACADEMIC_CELL_PG_VETERINARY, department_id=None))
        try:
            await db.commit()
        except IntegrityError:
            rejected = True
            await db.rollback()
    assert rejected, "the DB partial unique index must reject a second holder of the same scope"
    assert await _holders_of(UserRole.INCHARGE_ACADEMIC_CELL_PG_VETERINARY) == 1


async def t_one_user_can_hold_multiple_scopes():
    roles = (await _call("GET", f"/auth/users/{U['inc_multi']}/roles", S["SA"])).json()
    assert "incharge_academic_cell_ug_veterinary" in roles["assigned_roles"], roles
    assert "incharge_academic_cell_pg_fishery" in roles["assigned_roles"], roles


async def t_scope_reassignable_after_removal():
    """Super Admin flow: remove a scope from its holder, assign it to someone
    else — succeeds exactly once. (inc_multi holds two roles, so removing one
    is permitted.) Runs LAST — it changes inc_multi's scopes."""
    r = await _call("DELETE", f"/auth/users/{U['inc_multi']}/roles/incharge_academic_cell_pg_fishery", S["SA"])
    assert r.status_code == 200, r.text
    r = await _call("POST", f"/auth/users/{U['holder2']}/roles", S["SA"], json={"role": "incharge_academic_cell_pg_fishery"})
    assert r.status_code == 201, r.text
    assert await _holders_of(UserRole.INCHARGE_ACADEMIC_CELL_PG_FISHERY) == 1


# ════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    try:
        await _setup()
    except Exception:
        await _teardown()
        raise
    try:
        cases = {
            "Build registrations fixture": t_build_registrations,
            "PG-Vet can view PG-Vet registration": t_pg_vet_can_view_pg_vet_registration,
            "PG-Vet cannot view PG-Fishery registration": t_pg_vet_cannot_view_pg_fish_registration,
            "PG-Vet cannot view UG-Vet registration": t_pg_vet_cannot_view_ug_vet_registration,
            "PG-Fishery cannot view PG-Vet registration": t_pg_fish_cannot_view_pg_vet_registration,
            "PG-Fishery can view PG-Fishery registration": t_pg_fish_can_view_pg_fish_registration,
            "UG-Vet can view UG-Vet registration": t_ug_vet_can_view_ug_vet_registration,
            "UG-Fishery can view UG-Fishery registration": t_ug_fish_can_view_ug_fish_registration,
            "UG-Vet cannot view PG-Vet registration": t_ug_vet_cannot_view_pg_vet_registration,
            "UG-Vet cannot view PhD-Vet registration": t_ug_vet_cannot_view_phd_vet_registration,
            "PG-Vet scope covers PhD-Vet (same faculty)": t_pg_vet_scope_covers_phd_vet_same_faculty,
            "PG-Vet scope does NOT cover PhD-Fishery": t_pg_vet_scope_does_not_cover_phd_fish,
            "Missing Program.faculty denied for every scope": t_missing_faculty_denied_regardless_of_caller_scope,
            "PG-Vet can approve PG-Vet registration": t_pg_vet_can_approve_pg_vet_registration,
            "PG-Fishery cannot approve PhD-Vet registration; stage unchanged": t_pg_fish_cannot_approve_pg_vet_registration_and_state_unchanged,
            "UG-Vet cannot approve UG-Fishery registration; stage unchanged": t_ug_vet_cannot_approve_ug_fish_registration_and_state_unchanged,
            # Multi-scope
            "Invalid Program.faculty value rejected by the DB CHECK constraint": t_invalid_faculty_value_cannot_be_stored,
            "Multi-scope holder can view UG-Veterinary": t_multi_scope_can_view_ug_vet,
            "Multi-scope holder can view PG-Fishery": t_multi_scope_can_view_pg_fish,
            "Multi-scope holder still denied scopes it does not hold (PG-Vet, UG-Fishery)": t_multi_scope_still_denied_scopes_it_does_not_hold,
            "Multi-scope list returns union, no duplicates, excludes unheld scopes": t_multi_scope_list_returns_union_no_duplicates,
            "Single-scope list excludes other faculty": t_single_scope_list_excludes_other_faculty,
            # Published results / GPA / credit details
            "Published results: authorized same scope": t_published_results_authorized_same_scope,
            "Published results: denied cross-faculty": t_published_results_denied_cross_faculty,
            "Published results: denied cross-level": t_published_results_denied_cross_level,
            "GPA: authorized same scope": t_gpa_authorized_same_scope,
            "GPA: denied cross-scope": t_gpa_denied_cross_scope,
            "Credit details: authorized same scope": t_credit_details_authorized_same_scope,
            "Credit details: denied cross-scope": t_credit_details_denied_cross_scope,
            "Credit details: denied missing faculty": t_credit_details_denied_missing_faculty,
            # Course Students
            "Build Course Students fixture": t_build_course_students_fixture,
            "Course Students: PG-Vet sees only own-faculty student": t_course_students_pg_vet_sees_only_own_faculty_student,
            "Course Students: PG-Fishery sees only own-faculty student": t_course_students_pg_fish_sees_only_own_faculty_student,
            "Course Students: multi-scope sees both its own scopes, not others": t_course_students_multi_scope_sees_both_its_own_scopes_not_others,
            "Course Students: DPGS global access unchanged": t_course_students_dpgs_global_unchanged,
            "Course Students: Super Admin unchanged": t_course_students_super_admin_unchanged,
            "Course Students: HOD unchanged": t_course_students_hod_unchanged,
            # Staff directory
            "Staff directory: Veterinary Incharge sees Veterinary staff": t_staff_directory_veterinary_incharge_sees_veterinary_staff,
            "Staff directory: Veterinary Incharge excludes Fishery staff": t_staff_directory_veterinary_incharge_excludes_fishery_staff,
            "Staff directory: Fishery Incharge sees Fishery, excludes Veterinary": t_staff_directory_fishery_incharge_sees_fishery_excludes_veterinary,
            "Staff directory: departmentless staff excluded from both": t_staff_directory_departmentless_staff_excluded_from_both,
            "Staff directory: dual-stream admin dept staff excluded from both (AVFU confirmation pending)": t_staff_directory_dual_stream_admin_dept_excluded_from_both,
            "Staff directory: multi-scope holder sees both faculties": t_staff_directory_multi_scope_sees_both_faculties,
            "Staff directory: DPGS global access unchanged": t_staff_directory_dpgs_global_unchanged,
            "Staff directory: Super Admin unchanged": t_staff_directory_super_admin_unchanged,
            # Gradesheet
            "Scope match: Veterinary department": t_scope_match_veterinary_department,
            "Scope match: Fishery department": t_scope_match_fishery_department,
            "Scope match: DPGS dual-stream department never matches": t_scope_match_dpgs_dual_stream_department_never_matches,
            "Scope match: missing department": t_scope_match_missing_department,
            "Scope match: invalid programme level": t_scope_match_invalid_programme_level,
            "Scope match: PhD grouped as PG": t_scope_match_phd_grouped_as_pg,
            "Scope match: same course, different-faculty offerings isolated": t_scope_match_same_course_different_faculty_offerings_isolated,
            "Build Gradesheet fixture": t_build_gradesheet_fixture,
            "Gradesheet HTTP: PG-Vet sees Veterinary sheet": t_gradesheet_http_pg_vet_sees_veterinary_sheet,
            "Gradesheet HTTP: PG-Vet denied Fishery sheet": t_gradesheet_http_pg_vet_denied_fishery_sheet,
            "Gradesheet HTTP: PG-Fishery sees Fishery sheet": t_gradesheet_http_pg_fish_sees_fishery_sheet,
            "Gradesheet HTTP: PG-Fishery denied Veterinary sheet": t_gradesheet_http_pg_fish_denied_veterinary_sheet,
            "Gradesheet HTTP: DPGS sees both, unchanged": t_gradesheet_http_dpgs_sees_both_unchanged,
            # Single-holder constraint
            "Single-holder: second user refused every already-held scope (409), nothing persisted": t_single_holder_second_assignment_rejected_by_api,
            "Single-holder: DB partial unique index rejects a direct duplicate INSERT": t_single_holder_enforced_by_database_index,
            "Single-holder: one user can hold multiple different scopes": t_one_user_can_hold_multiple_scopes,
            "Single-holder: scope reassignable after removal (Super Admin flow)": t_scope_reassignable_after_removal,
        }
        for name, fn in cases.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "programs": (await db.execute(select(func.count()).select_from(Program).where(Program.name.like(f"{_LABEL}%")))).scalar_one(),
                "departments": (await db.execute(select(func.count()).select_from(Department).where(Department.name.like(f"{_LABEL}%")))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_ISC row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
