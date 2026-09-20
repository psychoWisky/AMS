"""Standalone HTTP-level tests for student data-model consistency:

  1. College <-> Programme: a student's (college, programme) must be a MAPPED pair
     (`ams_college_programs`); judged on the COMPLETE RESULTING state, enforced by
     the backend, and a rejected request changes nothing.
  2. Department <-> Programme: the existing association is enforced the same way.
  3. Orientation: a student account created from a candidate now carries the
     candidate's college on `User.college_id` (section T.5).
  4. Emails: Orientation's `avfu_email` / `personal_email` and the account's
     `User.email` are intentionally separate — a Super Admin email edit does not
     touch the candidate record, and credential resend uses the live account email.

Real FastAPI app via `httpx.ASGITransport`, real JWT sessions. Everything created is
`zztest_student_college_programme_...` / `ZZTESTSCP...` and deleted in `finally`
(candidates before users; the college delete cascades its mappings).

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_student_college_programme
"""
import asyncio
import hashlib
import sys
import uuid

import httpx
from sqlalchemy import select, delete, func

from app.db.base import AsyncSessionLocal
from app.main import app
from app.api.v1.endpoints import orientation as orientation_module
from app.core.security import create_access_token
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Department, Program, College, CollegeProgram
from app.models.orientation import OrientationCandidate

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_student_college_programme_"
_CODE_PFX = "ZZTESTSCP"


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        (self.passed if ok else self.failed).append(name if ok else (name, detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


RESULTS = _Results()
S: dict = {}
SENT: list[tuple[str, str, str]] = []
_N = [0]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _call(method: str, url: str, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with _client() as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _mk_student(dept, program, college=None) -> uuid.UUID:
    _N[0] += 1
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}s{_N[0]}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"SCP{_N[0]}", role=UserRole.STUDENT,
            department_id=dept.id, program_id=program.id, college_id=college, student_roll=f"ZZTEST-SCP-{_N[0]}-{_TAG}",
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        uid = u.id
        db.add(UserRoleAssignment(user_id=uid, role=UserRole.STUDENT))
        await db.commit()
    return uid


def _state(u: User) -> tuple:
    return (u.email, u.first_name, u.last_name, u.mobile, u.gender, u.student_roll, u.department_id, u.program_id, u.college_id, u.is_active)


async def _row(uid) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == uid))).scalar_one()


async def _patch(uid, body, token=None):
    return await _call("PATCH", f"/students/{uid}", token or S["super"], json=body)


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        S["D1"], S["D2"] = (await db.execute(select(Department).order_by(Department.code).limit(2))).scalars().all()
        S["P1"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()      # linked to every department
        S["P2"] = (await db.execute(select(Program).where(Program.code == "Ph.D(V)"))).scalar_one()   # linked to every department
        S["P3"] = (await db.execute(select(Program).where(Program.code == "MFSc"))).scalar_one()      # linked to NO department
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info="ZZTEST-student-college-programme"))
        await db.commit()
    S["super"] = create_access_token(str(admin_id), {"sid": str(rt)})
    for key in ("CA", "CB"):
        r = await _call("POST", "/admin/colleges", S["super"], json={"name": f"ZZTEST SCP College {key}", "code": f"{_CODE_PFX}{key}{_TAG.upper()}"})
        assert r.status_code == 201, r.text
        S[key] = uuid.UUID(r.json()["id"])
    # The configured mapping: CA offers only P1, CB offers only P2.
    for college, prog in (("CA", "P1"), ("CB", "P2")):
        r = await _call("POST", f"/admin/colleges/{S[college]}/programs", S["super"], json={"program_id": str(S[prog].id)})
        assert r.status_code == 201, r.text
    # a HOD, to prove the endpoint is closed to non-Super-Admin
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}hod_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name="HOD", role=UserRole.HOD, department_id=S["D1"].id, is_active=True, is_verified=True)
        db.add(u)
        await db.flush()
        db.add(UserRoleAssignment(user_id=u.id, role=UserRole.HOD, department_id=S["D1"].id))
        rt2 = uuid.uuid4()
        db.add(RefreshToken(id=rt2, user_id=u.id, token_hash=hashlib.sha256(str(rt2).encode()).hexdigest(), device_info="ZZTEST-student-college-programme"))
        await db.commit()
        S["hod"] = create_access_token(str(u.id), {"sid": str(rt2)})


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(OrientationCandidate).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == "ZZTEST-student-college-programme"))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.execute(delete(College).where(College.code.like(f"{_CODE_PFX}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        RESULTS.record(name, True)
    except Exception as e:  # noqa: BLE001
        RESULTS.record(name, False, f"{type(e).__name__}: {e}")


# ── College <-> Programme ───────────────────────────────────────────────────

async def t_valid_mapped_pairs_are_allowed():
    x = await _mk_student(S["D1"], S["P1"], S["CA"])
    assert (await _patch(x, {"college_id": str(S["CA"]), "program_id": str(S["P1"].id)})).status_code == 200, "re-saving the same valid pair"
    r = await _patch(x, {"college_id": str(S["CB"]), "program_id": str(S["P2"].id)})
    assert r.status_code == 200, f"changing BOTH to another mapped pair is valid even though each alone is not: {r.text}"
    u = await _row(x)
    assert (u.college_id, u.program_id) == (S["CB"], S["P2"].id)
    assert (await _patch(x, {"college_id": str(S["CA"]), "program_id": str(S["P1"].id)})).status_code == 200


async def t_invalid_pair_rejected_and_nothing_changes():
    x = await _mk_student(S["D1"], S["P1"], S["CA"])
    before = _state(await _row(x))
    r = await _patch(x, {"college_id": str(S["CB"]), "program_id": str(S["P1"].id)})
    assert r.status_code == 400 and "not offered by College" in r.json()["detail"], r.text
    assert S["P1"].name in r.json()["detail"] and "ZZTEST SCP College CB" in r.json()["detail"], "the error names the programme and college"
    assert _state(await _row(x)) == before


async def t_existing_valid_student_stays_valid():
    x = await _mk_student(S["D1"], S["P1"], S["CA"])
    assert (await _patch(x, {"mobile": "9000000009"})).status_code == 200
    assert (await _patch(x, {"gender": "Female", "first_name": "Renamed"})).status_code == 200
    assert (await _patch(x, {})).status_code == 200
    u = await _row(x)
    assert (u.college_id, u.program_id) == (S["CA"], S["P1"].id) and u.mobile == "9000000009"


async def t_changing_only_college_or_only_programme_to_incompatible():
    a = await _mk_student(S["D1"], S["P1"], S["CA"])
    before = _state(await _row(a))
    assert (await _patch(a, {"college_id": str(S["CB"])})).status_code == 400, "only the college changes: (CB, P1) is unmapped"
    assert (await _patch(a, {"program_id": str(S["P2"].id)})).status_code == 400, "only the programme changes: (CA, P2) is unmapped"
    assert _state(await _row(a)) == before


async def t_direct_api_requests_cannot_bypass_the_rule():
    x = await _mk_student(S["D1"], S["P1"], S["CA"])
    before = _state(await _row(x))
    # crafted requests, exactly what a client bypassing the dropdowns would send
    for body in ({"college_id": str(S["CB"]), "program_id": str(S["P1"].id)},
                 {"program_id": str(S["P2"].id), "college_id": str(S["CA"])},
                 {"college_id": str(S["CB"]), "department_id": str(S["D2"].id)}):
        assert (await _patch(x, body)).status_code == 400, body
    assert (await _patch(x, {"college_id": str(S["CB"]), "program_id": str(S["P1"].id)}, token=S["hod"])).status_code == 403, "a HOD cannot even reach the endpoint"
    assert _state(await _row(x)) == before


async def t_validation_judges_the_complete_resulting_state_atomically():
    x = await _mk_student(S["D1"], S["P1"], S["CA"])
    before = _state(await _row(x))
    # valid changes bundled with an invalid pair: NOTHING may be written
    r = await _patch(x, {"first_name": "Changed", "mobile": "9111111111", "gender": "Female", "college_id": str(S["CB"]), "program_id": str(S["P1"].id)})
    assert r.status_code == 400, r.text
    assert _state(await _row(x)) == before, "a rejected request must not apply its valid fields"
    r = await _patch(x, {"first_name": "Changed", "department_id": str(S["D2"].id), "program_id": str(S["P3"].id)})
    assert r.status_code == 400 and "Department is not associated" in r.json()["detail"], r.text
    assert _state(await _row(x)) == before, "invalid department/programme pair: nothing written"
    r = await _patch(x, {"department_id": str(S["D2"].id), "college_id": str(S["CB"])})
    assert r.status_code == 400 and _state(await _row(x)) == before, "the valid department change must not leak through a rejected college change"
    # ...whereas the same three fields changed TOGETHER into a valid final state are accepted in one request
    r = await _patch(x, {"college_id": str(S["CB"]), "program_id": str(S["P2"].id), "department_id": str(S["D2"].id), "first_name": "Final"})
    assert r.status_code == 200, r.text
    u = await _row(x)
    assert (u.college_id, u.program_id, u.department_id, u.first_name) == (S["CB"], S["P2"].id, S["D2"].id, "Final")


# ── Department <-> Programme ────────────────────────────────────────────────

async def t_department_programme_pair_is_enforced():
    x = await _mk_student(S["D1"], S["P1"], S["CA"])
    before = _state(await _row(x))
    assert (await _patch(x, {"department_id": str(S["D2"].id)})).status_code == 200, "P1 is offered by every department"
    assert (await _patch(x, {"department_id": str(S["D1"].id)})).status_code == 200
    before = _state(await _row(x))
    r = await _patch(x, {"program_id": str(S["P3"].id)})
    assert r.status_code == 400 and "Department is not associated" in r.json()["detail"], r.text
    assert _state(await _row(x)) == before, "rejected: original state unchanged"


async def t_college_is_the_accounts_own_and_orientation_is_never_consulted():
    # X: canonical college CB, programme P2 (mapped). Its linked Orientation candidate names a DIFFERENT college, CA.
    x = await _mk_student(S["D1"], S["P2"], S["CB"])
    async with AsyncSessionLocal() as db:
        db.add(OrientationCandidate(personal_email=f"{_PFX}canon_{_TAG}@example.com", first_name="ZZTEST", last_name="Canon", academic_year="ZZTEST",
                                    program_id=S["P2"].id, department_id=S["D1"].id, college_id=S["CA"], student_user_id=x))
        await db.commit()
    # detail returns the canonical college and no fallback marker
    d = (await _call("GET", f"/students/{x}", S["super"])).json()
    assert d["college_id"] == str(S["CB"]) and d["college_name"] == f"ZZTEST SCP College CB", d
    assert "college_from_orientation" not in d, "the Orientation-fallback flag no longer exists"
    # filtering uses User.college_id directly: CB finds X, the candidate's CA does not
    ids = lambda rows: {r_["id"] for r_ in rows}
    assert str(x) in ids((await _call("GET", "/students", S["super"], params={"college_id": str(S["CB"]), "q": "ZZTEST"})).json()["items"])
    assert str(x) not in ids((await _call("GET", "/students", S["super"], params={"college_id": str(S["CA"]), "q": "ZZTEST"})).json()["items"]), "the candidate's college is not a filter source"
    # PATCH validates against the canonical college: (CB, P1) is unmapped even though the candidate's CA offers P1
    before = _state(await _row(x))
    r = await _patch(x, {"program_id": str(S["P1"].id)})
    assert r.status_code == 400 and "College 'ZZTEST SCP College CB'" in r.json()["detail"], r.text
    assert _state(await _row(x)) == before, "rejected: nothing changed"
    assert (await _patch(x, {"mobile": "9222222222"})).status_code == 200
    # clearing college_id is allowed (existing PATCH contract): the student then has NO college — nothing falls back to Orientation
    assert (await _patch(x, {"college_id": None})).status_code == 200
    assert (await _row(x)).college_id is None
    d = (await _call("GET", f"/students/{x}", S["super"])).json()
    assert d["college_id"] is None and d["college_name"] is None, "cleared means cleared — the candidate's CA must not reappear"
    assert str(x) not in ids((await _call("GET", "/students", S["super"], params={"college_id": str(S["CA"]), "q": "ZZTEST"})).json()["items"])
    # with no college there is nothing to validate against; choosing one re-enables the rule on the resulting pair
    assert (await _patch(x, {"program_id": str(S["P1"].id)})).status_code == 200
    assert (await _row(x)).college_id is None, "editing the programme must not silently write a college"
    assert (await _patch(x, {"college_id": str(S["CB"])})).status_code == 400, "(CB, P1) is unmapped"
    assert (await _patch(x, {"college_id": str(S["CA"])})).status_code == 200, "(CA, P1) is mapped"


# ── Orientation ─────────────────────────────────────────────────────────────

async def t_orientation_copies_college_to_the_account():
    orig = orientation_module.send_email
    orientation_module.send_email = lambda to, subject, body: (SENT.append((to, subject, body)) or True)
    try:
        personal, avfu = f"{_PFX}orient_{_TAG}@example.com", f"{_PFX}orient_{_TAG}@avfu.ac.in"
        r = await _call("POST", "/orientation/candidates", S["super"], json={
            "first_name": "ZZTEST", "last_name": "Orient", "personal_email": personal, "mobile": "9876543210", "avfu_email": avfu,
            "academic_year": "2026", "college_id": str(S["CA"]), "program_id": str(S["P1"].id), "department_id": str(S["D1"].id)})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        assert (await _call("PATCH", f"/orientation/candidates/{cid}/attendance", S["super"], params={"status": "present"})).status_code == 200
        r = await _call("PATCH", f"/orientation/candidates/{cid}/selection", S["super"], params={"status": "selected"})
        assert r.status_code == 200, r.text
        async with AsyncSessionLocal() as db:
            cand = (await db.execute(select(OrientationCandidate).where(OrientationCandidate.id == uuid.UUID(cid)))).scalar_one()
            student = (await db.execute(select(User).where(User.id == cand.student_user_id))).scalar_one()
        S["orient_student"], S["orient_candidate"] = student.id, cid
        assert student.college_id == S["CA"], "the candidate's college must be copied onto User.college_id"
        assert (student.program_id, student.department_id, student.email) == (S["P1"].id, S["D1"].id, avfu)
        d = (await _call("GET", f"/students/{student.id}", S["super"])).json()
        assert d["college_id"] == str(S["CA"]) and "college_from_orientation" not in d, "college comes from the account itself"
        assert student.id and any(r_["id"] == str(student.id) for r_ in (await _call("GET", "/students", S["super"], params={"college_id": str(S["CA"]), "q": "ZZTEST Orient"})).json()["items"])
    finally:
        orientation_module.send_email = orig


async def t_emails_stay_separate_and_resend_uses_the_account_email():
    assert "orient_student" in S, "requires the Orientation test above"
    orig = orientation_module.send_email
    orientation_module.send_email = lambda to, subject, body: (SENT.append((to, subject, body)) or True)
    try:
        sid = S["orient_student"]
        async with AsyncSessionLocal() as db:
            cand = (await db.execute(select(OrientationCandidate).where(OrientationCandidate.student_user_id == sid))).scalar_one()
            avfu_before, personal_before = cand.avfu_email, cand.personal_email
        new_email = f"{_PFX}orient_renamed_{_TAG}@avfu.ac.in"
        assert (await _patch(sid, {"email": new_email})).status_code == 200
        async with AsyncSessionLocal() as db:
            cand = (await db.execute(select(OrientationCandidate).where(OrientationCandidate.student_user_id == sid))).scalar_one()
        assert (cand.avfu_email, cand.personal_email) == (avfu_before, personal_before), "an account-email edit must not rewrite the Orientation record"
        SENT.clear()
        r = await _call("POST", f"/orientation/candidates/{S['orient_candidate']}/resend-credentials", S["super"])
        assert r.status_code == 200, r.text
        to, _subject, body = SENT[-1]
        assert to == personal_before, "credentials go to the candidate's contact (personal) address"
        assert f"Login email: {new_email}" in body and avfu_before not in body, "the message carries the account's CURRENT login email"
    finally:
        orientation_module.send_email = orig


async def main() -> None:
    await _setup()
    try:
        for name, fn in {
            "COLLEGE<->PROGRAMME: mapped pairs allowed (re-saving, and changing both fields together to another mapped pair)": t_valid_mapped_pairs_are_allowed,
            "COLLEGE<->PROGRAMME: unmapped pair -> 400 naming programme and college; nothing changed": t_invalid_pair_rejected_and_nothing_changes,
            "COLLEGE<->PROGRAMME: an existing valid student stays editable/valid": t_existing_valid_student_stays_valid,
            "COLLEGE<->PROGRAMME: changing only the college, or only the programme, to an incompatible one -> 400": t_changing_only_college_or_only_programme_to_incompatible,
            "SECURITY: crafted direct API requests cannot bypass the mapping; HOD -> 403": t_direct_api_requests_cannot_bypass_the_rule,
            "ATOMICITY: the COMPLETE resulting state is validated; a rejected request writes none of its valid fields; a valid final state passes": t_validation_judges_the_complete_resulting_state_atomically,
            "DEPARTMENT<->PROGRAMME: existing association enforced on student edit; rejected request changes nothing": t_department_programme_pair_is_enforced,
            "CANONICAL COLLEGE: User.college_id only — detail, filter and PATCH validation ignore the Orientation candidate; clearing leaves no college": t_college_is_the_accounts_own_and_orientation_is_never_consulted,
            "ORIENTATION: account created from a candidate carries the candidate's college on User.college_id; Students API returns and filters by it": t_orientation_copies_college_to_the_account,
            "EMAILS: editing the account email leaves the Orientation record alone; credential resend uses the live account email": t_emails_stay_separate_and_resend_uses_the_account_email,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(User.id).where(User.email.like(f"{_PFX}%")))).scalars().all(),
                "colleges": (await db.execute(select(College.id).where(College.code.like(f"{_CODE_PFX}%")))).scalars().all(),
                "candidates": (await db.execute(select(OrientationCandidate.id).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))).scalars().all(),
                "sessions": (await db.execute(select(RefreshToken.id).where(RefreshToken.device_info == "ZZTEST-student-college-programme"))).scalars().all(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
            orphan_maps = (await db.execute(select(func.count()).select_from(CollegeProgram).where(~CollegeProgram.college_id.in_(select(College.id))))).scalar_one()
        RESULTS.record("cleanup: no ZZTEST_STUDENT_COLLEGE_PROGRAMME user / college / candidate / session / assignment / mapping remains",
                       not any(left.values()) and orphans == 0 and orphan_maps == 0, str(left))
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
