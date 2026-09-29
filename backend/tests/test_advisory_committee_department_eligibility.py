"""Standalone HTTP-level tests for the Advisory Committee department-eligibility
task: faculty eligibility for Major Advisor / Member Major / Member Minor /
Supporting / Co-Major Advisor / Member of Others, all based on CURRENT
`FACULTY` `UserRoleAssignment` membership (never `User.department_id`), with a
multi-department faculty member as the central case.

Modelled on `tests/test_advisory_committee_students.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions, a `record`/`_run` pass-fail tracker,
`zztest_ade_...`-prefixed rows, full teardown.

Fixture (BUSINESS_LOGIC.md's confirmed AVFU rule set, this task's spec):
    Student A  -> Department A
    Faculty 1  -> FACULTY Department A
    Faculty 2  -> FACULTY Department B
    Faculty 3  -> FACULTY Department A + Department B
    Faculty 4  -> FACULTY Department C
    HOD-only   -> HOD Department A, NO FACULTY assignment anywhere (regression
                  guard: must never appear in a faculty-eligibility endpoint)

Expected matrix:
    Role              F1      F2      F3      F4
    Major Advisor     Allow   Reject  Allow   Reject
    Member Major      Allow   Reject  Allow   Reject
    Member Minor      Reject  Allow   Allow   Allow
    Supporting        Allow   Allow   Allow   Allow
    Co-Major Advisor  Allow   Allow   Allow   Allow   (no confirmed restriction)

Every "reject" case here IS the hostile/direct-API-manipulation test: there is
no separate client-side filter to bypass — the endpoint under test is the only
authorization boundary, so calling it directly with a disallowed faculty_id
(exactly what a hostile client would do) is the whole test.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_advisory_committee_department_eligibility
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import Department, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_ade_"
_DEVICE = "ZZTEST-ade"

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


async def _mk_user(key, role, depts):
    """`depts` is a list of Department rows the user holds `role` in (0, 1 or 2
    entries) — mirrors real multi-department FACULTY membership via multiple
    UserRoleAssignment rows, never a single scalar column."""
    async with AsyncSessionLocal() as db:
        primary = depts[0] if depts else None
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"ADE{key.upper()}",
            role=role, department_id=primary.id if primary else None,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            for d in depts:
                db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=d.id))
            if not depts:
                db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=None))
        await db.commit()


async def _mk_committee(student_key: str, ma_key: str) -> uuid.UUID:
    """Creates a committee for `student_key` with `ma_key` as Major Advisor and
    immediately accepts it (as the MA), landing the committee in
    `member_selection` so `add_member` calls succeed."""
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={
        "student_id": str(U[student_key]), "major_advisor_id": str(U[ma_key]),
    })
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r2 = await _call("PATCH", f"/research/committees/{cid}/major-advisor-response", TOK[ma_key], json={"accepted": True})
    assert r2.status_code == 200, r2.text
    return cid


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_ade_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(3))).scalars().all()
        S["A"], S["B"], S["C"] = depts
        await db.commit()
    A, B, Cd = S["A"], S["B"], S["C"]

    await _mk_user("hod_a", UserRole.HOD, [A])
    # Dedicated Major Advisor for the member-role-matrix committees below —
    # distinct from f1..f4, which are the candidates UNDER TEST as members;
    # f1 cannot simultaneously be that committee's Major Advisor AND a member
    # candidate (uq_committee_member forbids the same faculty twice on one
    # committee, correctly, and that is not what this suite is testing).
    await _mk_user("f_ma", UserRole.FACULTY, [A])
    await _mk_user("f1", UserRole.FACULTY, [A])
    await _mk_user("f2", UserRole.FACULTY, [B])
    await _mk_user("f3", UserRole.FACULTY, [A, B])
    await _mk_user("f4", UserRole.FACULTY, [Cd])
    await _mk_user("hod_only", UserRole.HOD, [A])  # no FACULTY assignment anywhere
    await _mk_user("sa", UserRole.SUPER_ADMIN, [])
    for k in ("stu_ma1", "stu_ma2", "stu_ma3", "stu_ma4", "stu_mm_major", "stu_mm_minor",
              "stu_supporting", "stu_external", "stu_comajor"):
        await _mk_user(k, UserRole.STUDENT, [A])

    for k in ("hod_a", "f_ma", "f1", "f2", "f3", "f4", "hod_only", "sa"):
        TOK[k] = await _session(U[k])


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


# ── Major Advisor eligibility (create_committee) ────────────────────────────────

async def t_eligible_major_advisors_lists_f1_f3_excludes_f2_f4_hodonly():
    r = await _call("GET", "/research/committees/eligible-major-advisors", TOK["hod_a"], params={"student_id": str(U["stu_ma1"])})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(U["f1"]) in ids and str(U["f3"]) in ids, ids
    assert str(U["f2"]) not in ids and str(U["f4"]) not in ids, ids
    assert str(U["hod_only"]) not in ids, "an HOD-only account with no FACULTY assignment must never appear as a Major Advisor candidate"


async def t_major_advisor_f1_allowed():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={"student_id": str(U["stu_ma1"]), "major_advisor_id": str(U["f1"])})
    assert r.status_code == 201, r.text


async def t_major_advisor_f3_allowed_multidept():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={"student_id": str(U["stu_ma3"]), "major_advisor_id": str(U["f3"])})
    assert r.status_code == 201, r.text


async def t_major_advisor_f2_rejected():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={"student_id": str(U["stu_ma2"]), "major_advisor_id": str(U["f2"])})
    assert r.status_code == 400, r.text


async def t_major_advisor_f4_rejected():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={"student_id": str(U["stu_ma4"]), "major_advisor_id": str(U["f4"])})
    assert r.status_code == 400, r.text


# ── Member Major / Member Minor / Supporting / Co-Major Advisor ────────────────

async def t_member_major_matrix():
    cid = await _mk_committee("stu_mm_major", "f_ma")
    r1 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_major", "faculty_id": str(U["f1"])})
    assert r1.status_code == 201, r1.text
    r3 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_major", "faculty_id": str(U["f3"])})
    assert r3.status_code == 201, r3.text
    r2 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_major", "faculty_id": str(U["f2"])})
    assert r2.status_code == 400, r2.text
    r4 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_major", "faculty_id": str(U["f4"])})
    assert r4.status_code == 400, r4.text


async def t_member_minor_matrix_and_f3_dual_eligibility():
    cid = await _mk_committee("stu_mm_minor", "f_ma")
    r1 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_minor", "faculty_id": str(U["f1"])})
    assert r1.status_code == 400, r1.text
    r2 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_minor", "faculty_id": str(U["f2"])})
    assert r2.status_code == 201, r2.text
    r4 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_minor", "faculty_id": str(U["f4"])})
    assert r4.status_code == 201, r4.text
    # Faculty 3 (A+B) as Member Minor for a Department-A student: allowed, because
    # they hold FACULTY in B (an "other" department) — their own membership in A
    # does not disqualify them. Combined with t_member_major_matrix's success for
    # the SAME faculty member, this proves the two checks are independent, not
    # mutually exclusive.
    r3 = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "member_minor", "faculty_id": str(U["f3"])})
    assert r3.status_code == 201, r3.text


async def t_supporting_no_department_restriction():
    cid = await _mk_committee("stu_supporting", "f_ma")
    for key in ("f1", "f2", "f3", "f4"):
        r = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "supporting", "faculty_id": str(U[key])})
        assert r.status_code == 201, f"{key}: {r.text}"


async def t_co_major_advisor_architecturally_available_no_invented_restriction():
    cid = await _mk_committee("stu_comajor", "f_ma")
    r = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={"role": "co_major_advisor", "faculty_id": str(U["f2"])})
    assert r.status_code == 201, r.text


# ── Member of Others (external) ─────────────────────────────────────────────────

async def t_external_member_success():
    cid = await _mk_committee("stu_external", "f_ma")
    r = await _call("POST", f"/research/committees/{cid}/members", TOK["f_ma"], json={
        "role": "member_of_others", "external_name": "Dr. Zztest External",
        "external_designation": "Principal Scientist", "external_institute": "ZZTEST ICAR Institute",
    })
    assert r.status_code == 201, r.text
    detail = await _call("GET", f"/research/committees/student/{U['stu_external']}", TOK["hod_a"])
    row = next(m for m in detail.json()["members"] if m["role"] == "member_of_others")
    assert row["is_external"] is True and row["faculty_id"] is None, row
    assert row["accepted"] is True, "an external member must be pre-accepted (no offline invitation/response flow)"
    assert row["institute"] == "ZZTEST ICAR Institute", row
    C["ext_committee"] = cid


async def t_external_member_rejects_faculty_id_and_external_together():
    r = await _call("POST", f"/research/committees/{C['ext_committee']}/members", TOK["f_ma"], json={
        "role": "member_of_others", "faculty_id": str(U["f2"]),
        "external_name": "X", "external_designation": "Y", "external_institute": "Z",
    })
    assert r.status_code == 422, r.text


async def t_external_role_rejects_internal_faculty_id_only():
    r = await _call("POST", f"/research/committees/{C['ext_committee']}/members", TOK["f_ma"], json={
        "role": "member_of_others", "faculty_id": str(U["f2"]),
    })
    assert r.status_code == 400, r.text


async def t_internal_role_rejects_external_fields_only():
    r = await _call("POST", f"/research/committees/{C['ext_committee']}/members", TOK["f_ma"], json={
        "role": "member_major", "external_name": "X", "external_designation": "Y", "external_institute": "Z",
    })
    assert r.status_code == 400, r.text


# ── Multi-department dedup ──────────────────────────────────────────────────────

async def t_faculty3_appears_once_no_duplication():
    r = await _call("GET", "/research/committees/eligible-major-advisors", TOK["hod_a"], params={"student_id": str(U["stu_ma1"])})
    ids = [row["id"] for row in r.json()]
    assert ids.count(str(U["f3"])) <= 1, "a multi-department faculty member must appear at most once, never duplicated by a join"


async def main() -> None:
    await _setup()
    try:
        cases = {
            "eligible-major-advisors lists F1/F3, excludes F2/F4/HOD-only": t_eligible_major_advisors_lists_f1_f3_excludes_f2_f4_hodonly,
            "Major Advisor: F1 (own dept) allowed": t_major_advisor_f1_allowed,
            "Major Advisor: F3 (multi-dept, own dept included) allowed": t_major_advisor_f3_allowed_multidept,
            "Major Advisor: F2 (other dept only) rejected": t_major_advisor_f2_rejected,
            "Major Advisor: F4 (unrelated dept) rejected": t_major_advisor_f4_rejected,
            "Member Major matrix (F1/F3 allow, F2/F4 reject)": t_member_major_matrix,
            "Member Minor matrix + F3 dual-eligibility with Member Major": t_member_minor_matrix_and_f3_dual_eligibility,
            "Supporting: no department restriction, all four allowed": t_supporting_no_department_restriction,
            "Co-Major Advisor: architecturally available, no invented restriction": t_co_major_advisor_architecturally_available_no_invented_restriction,
            "External member: create succeeds, pre-accepted, institute shown": t_external_member_success,
            "External member: faculty_id + external fields together -> 422": t_external_member_rejects_faculty_id_and_external_together,
            "member_of_others with only faculty_id -> 400": t_external_role_rejects_internal_faculty_id_only,
            "internal role with only external fields -> 400": t_internal_role_rejects_external_fields_only,
            "Multi-department faculty never duplicated in candidate list": t_faculty3_appears_once_no_duplication,
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
        record("cleanup: no ZZTEST_ADE user / session row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
