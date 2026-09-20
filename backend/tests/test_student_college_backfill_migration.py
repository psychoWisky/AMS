"""Standalone test of migration 0023_backfill_student_college's SQL.

The migration's own statement (`_BACKFILL_SQL`, imported from the migration file) is executed
against `ZZTESTSCB...` rows that cover every case, and the outcome is checked row by row:

  * a student with NULL college and a candidate college  -> populated from the candidate
  * same, for an Orientation-style account (no assignment rows, legacy role STUDENT)
  * a staff user who also holds a STUDENT assignment      -> counts as a student (same rule as `student_user_clause()`)
  * a student that already has a college                  -> untouched (never overwritten)
  * a student whose candidate has no college / no candidate -> stays NULL (nothing invented)
  * a NON-student user linked to a candidate               -> untouched
  * candidate rows are never modified; running the statement twice changes nothing more.

The dev database has already been backfilled (0 eligible rows), so a guard first proves the statement
would touch NOTHING but the ZZTEST rows; if a real eligible row existed the test stops instead of
running it. Everything created is deleted in `finally` (candidates before users, then colleges).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_student_college_backfill_migration
"""
import asyncio
import importlib.util
import pathlib
import sys
import uuid

from sqlalchemy import select, delete, func, text

import app.main  # noqa: F401  (configures every mapper)
from app.db.base import AsyncSessionLocal
from app.models.orientation import OrientationCandidate
from app.models.user import College, Department, Program, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_student_college_backfill_"
_CODE_PFX = "ZZTESTSCB"

_spec = importlib.util.spec_from_file_location(
    "mig0023", pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0023_backfill_student_college.py")
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}
C: dict[str, uuid.UUID] = {}


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _college(db, key: str) -> uuid.UUID:
    c = College(name=f"ZZTEST SCB College {key}", code=f"{_CODE_PFX}{key}{_TAG.upper()}")
    db.add(c)
    await db.flush()
    C[key] = c.id
    return c.id


async def _user(db, key, role, assignments, dept, prog, college=None, cand_college="none"):
    u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"SCB{key}", role=role, department_id=dept.id,
             program_id=prog.id if role == UserRole.STUDENT else None, college_id=college, is_active=True, is_verified=True)
    db.add(u)
    await db.flush()
    U[key] = u.id
    for r in assignments:
        db.add(UserRoleAssignment(user_id=u.id, role=r, department_id=dept.id if r in (UserRole.FACULTY, UserRole.HOD) else None))
    if cand_college != "none":  # a linked candidate; cand_college may be None (candidate without a college)
        db.add(OrientationCandidate(personal_email=f"{_PFX}{key}_{_TAG}@example.com", first_name="ZZTEST", last_name=f"SCB{key}", academic_year="ZZTEST",
                                    program_id=prog.id, department_id=dept.id, college_id=cand_college, student_user_id=u.id))


async def _college_of(key):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User.college_id).where(User.id == U[key]))).scalar_one()


async def _candidates_snapshot():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(OrientationCandidate.id, OrientationCandidate.college_id, OrientationCandidate.student_user_id,
                                        OrientationCandidate.avfu_email, OrientationCandidate.personal_email, OrientationCandidate.program_id,
                                        OrientationCandidate.department_id).order_by(OrientationCandidate.id))).all()
    return [tuple(r) for r in rows]


async def main() -> None:
    try:
        async with AsyncSessionLocal() as db:
            dept = (await db.execute(select(Department).order_by(Department.code).limit(1))).scalar_one()
            prog = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
            ca, cb = await _college(db, "A"), await _college(db, "B")
            STU, FAC = UserRole.STUDENT, UserRole.FACULTY
            await _user(db, "assigned", STU, [STU], dept, prog, None, ca)                 # NULL + candidate CA          -> CA
            await _user(db, "noassign", STU, [], dept, prog, None, ca)                    # Orientation-style, no rows   -> CA
            await _user(db, "staffstudent", FAC, [FAC, STU], dept, prog, None, ca)        # staff who is also a student  -> CA
            await _user(db, "hascollege", STU, [STU], dept, prog, cb, ca)                 # already CB, candidate CA     -> stays CB
            await _user(db, "candnocollege", STU, [STU], dept, prog, None, None)          # candidate has no college     -> stays NULL
            await _user(db, "nocandidate", STU, [STU], dept, prog, None)                  # no candidate                 -> stays NULL
            await _user(db, "staffonly", FAC, [FAC], dept, prog, None, ca)                # NOT a student, has candidate -> stays NULL
            await db.commit()

        # guard: nothing REAL is eligible (the dev DB is already backfilled), so the statement can only touch ZZTEST rows
        async with AsyncSessionLocal() as db:
            eligible_real = (await db.execute(text(
                "SELECT count(*) FROM ams_users u JOIN ams_orientation_candidates c ON c.student_user_id = u.id "
                "WHERE u.college_id IS NULL AND c.college_id IS NOT NULL AND u.email NOT LIKE :p"), {"p": f"{_PFX}%"})).scalar_one()
        if eligible_real:
            print(f"STOP: {eligible_real} real, not-yet-backfilled student row(s) exist; refusing to run the statement here.")
            sys.exit(2)

        cand_before = await _candidates_snapshot()
        async with AsyncSessionLocal() as db:
            res = await db.execute(text(_mig._BACKFILL_SQL))
            await db.commit()
            updated = res.rowcount
        record("the statement updates exactly the 3 eligible rows (assigned, no-assignment, staff+student)", updated == 3, f"rowcount={updated}")
        record("student with NULL college gets the candidate's college", await _college_of("assigned") == C["A"])
        record("Orientation-style student (no assignment rows, legacy role STUDENT) gets it too", await _college_of("noassign") == C["A"])
        record("staff who also hold a STUDENT assignment are students (same rule as student_user_clause)", await _college_of("staffstudent") == C["A"])
        record("a student that already has a college is NOT overwritten", await _college_of("hascollege") == C["B"])
        record("candidate without a college -> student stays NULL (nothing invented)", await _college_of("candnocollege") is None)
        record("student without a candidate stays NULL", await _college_of("nocandidate") is None)
        record("a non-student user linked to a candidate is untouched", await _college_of("staffonly") is None)
        record("no Orientation candidate row was modified", await _candidates_snapshot() == cand_before)

        snap = {k: await _college_of(k) for k in U}
        async with AsyncSessionLocal() as db:
            res = await db.execute(text(_mig._BACKFILL_SQL))
            await db.commit()
            again = res.rowcount
        record("idempotent: running it again changes nothing", again == 0 and {k: await _college_of(k) for k in U} == snap, f"rowcount={again}")
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(OrientationCandidate).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))
            await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(select(User.id).where(User.email.like(f"{_PFX}%")))))
            await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
            await db.execute(delete(College).where(College.code.like(f"{_CODE_PFX}%")))
            await db.commit()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one(),
                "colleges": (await db.execute(select(func.count()).select_from(College).where(College.code.like(f"{_CODE_PFX}%")))).scalar_one(),
                "candidates": (await db.execute(select(func.count()).select_from(OrientationCandidate).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_STUDENT_COLLEGE_BACKFILL user / college / candidate / assignment remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
