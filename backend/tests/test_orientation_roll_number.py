"""Standalone tests for Orientation roll-number sequencing (`_next_roll_no`).

The FORMAT is unchanged: `{candidate.academic_year}-{program.code}-{N}`. What is fixed is N:
it is now the highest numeric suffix already used for that exact prefix plus one — NOT a row
count, which re-issued a taken number once a Super Admin edited a roll (the count dropped).

  * 1,2 -> 3;  1,2,7 -> 8 (gap);  none -> 1
  * an edited roll (renamed away from the prefix) never causes a collision: 1,X,3 -> 4
  * a label with '-', digits, spaces and brackets: `<label> (Phase-II)-Ph.D(V)-10` -> ...-11
  * malformed suffixes ('', 'abc', '5x', '-3', '1.5', non-ASCII digits) are ignored, never crash
  * LIKE wildcards in a label ('_', '%') do not match other labels; programmes are independent
  * END TO END: three candidates selected through the API get -1,-2,-3; Super Admin edits one
    roll; the next selected candidate gets -4 (no 409, no duplicate) and the format is unchanged

Everything created is `ZZTEST_ACADEMIC_YEAR_ROLL...` / `zztest_academic_year_roll_...` and deleted
in `finally` (candidates before users). Emails are stubbed; nothing is sent.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_orientation_roll_number
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
from app.models.orientation import OrientationCandidate
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Department, Program, College

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_academic_year_roll_"
_LABEL = "ZZTEST_ACADEMIC_YEAR_ROLL"
_DEVICE = "ZZTEST-academic-year-roll"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
S: dict = {}
_N = [0]


def record(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f" — {detail}" if detail and not ok else ""))


async def _call(method, url, token, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{url}", headers=headers, **kw)


async def _with_rolls(*rolls: str) -> None:
    async with AsyncSessionLocal() as db:
        for roll in rolls:
            _N[0] += 1
            db.add(User(email=f"{_PFX}{_N[0]}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"Roll{_N[0]}", role=UserRole.STUDENT,
                        student_roll=roll, is_active=True, is_verified=True))
        await db.commit()


async def _next(label: str, code: str) -> str:
    async with AsyncSessionLocal() as db:
        return await orientation_module._next_roll_no(label, code, db)


def _label(name: str) -> str:
    return f"ZZTESTAYR-{name}-{_TAG}"


# ── generator, directly ────────────────────────────────────────────────────

async def t_sequence_basics():
    a = _label("basic")
    assert await _next(a, "MVSc") == f"{a}-MVSc-1", "no roll yet -> 1"
    await _with_rolls(f"{a}-MVSc-1", f"{a}-MVSc-2")
    assert await _next(a, "MVSc") == f"{a}-MVSc-3", "existing behaviour: 1,2 -> 3"


async def t_gap_uses_the_highest_suffix():
    a = _label("gap")
    await _with_rolls(f"{a}-MVSc-1", f"{a}-MVSc-2", f"{a}-MVSc-7")
    assert await _next(a, "MVSc") == f"{a}-MVSc-8", "1,2,7 -> 8, not count+1 = 4"


async def t_edited_roll_never_collides():
    a = _label("edit")
    await _with_rolls(f"{a}-MVSc-1", f"{a}-MVSc-3", f"ZZTEST-CUSTOM-{_TAG}-2")      # #2 was renamed to a custom roll
    assert await _next(a, "MVSc") == f"{a}-MVSc-4", "count+1 would give 3 (taken); highest+1 gives 4"
    b = _label("edit2")
    await _with_rolls(f"{b}-MVSc-1", f"{b}-MVSc-2", f"ZZTEST-CUSTOM-{_TAG}-3")      # the last one was renamed
    assert await _next(b, "MVSc") == f"{b}-MVSc-3"


async def t_complex_label():
    a = f"ZZTESTAYR {_TAG} 2025-26 (Phase-II)"          # keeps roll <= 50 chars (User.student_roll is String(50))
    await _with_rolls(f"{a}-Ph.D(V)-9", f"{a}-Ph.D(V)-10", f"{a}-Ph.D(V)-2")
    assert await _next(a, "Ph.D(V)") == f"{a}-Ph.D(V)-11", "10 is the highest suffix (2 < 9 < 10 numerically, not lexically)"
    assert (await _next(a, "Ph.D(V)")).endswith("2025-26 (Phase-II)-Ph.D(V)-11")


async def t_malformed_suffixes_are_ignored():
    a = _label("bad")
    await _with_rolls(f"{a}-MVSc-", f"{a}-MVSc-abc", f"{a}-MVSc-5x", f"{a}-MVSc--3", f"{a}-MVSc-1.5", f"{a}-MVSc-٣", f"{a}-MVSc- 4", f"{a}-MVSc-1", f"{a}-MVSc-2")
    assert await _next(a, "MVSc") == f"{a}-MVSc-3", "only the numeric suffixes 1 and 2 count"
    c = _label("allbad")
    await _with_rolls(f"{c}-MVSc-x", f"{c}-MVSc-")
    assert await _next(c, "MVSc") == f"{c}-MVSc-1", "no valid suffix at all -> 1"


async def t_wildcards_and_isolation():
    a, other = f"ZZTESTAYR_{_TAG}wild", f"ZZTESTAYRX{_TAG}wild"          # '_' would match 'X' if the prefix were used as a LIKE pattern
    await _with_rolls(f"{other}-MVSc-50")
    assert await _next(a, "MVSc") == f"{a}-MVSc-1", "another label must not affect this sequence (LIKE wildcards are escaped)"
    pct = f"ZZTESTAYR%{_TAG}wild"          # an unescaped '%' would match the other label's roll
    await _with_rolls(f"{other}-MVSc-60")
    assert await _next(pct, "MVSc") == f"{pct}-MVSc-1"
    p = _label("prog")
    await _with_rolls(f"{p}-MVSc-5")
    assert await _next(p, "Ph.D(V)") == f"{p}-Ph.D(V)-1", "a different programme has its own sequence"
    assert await _next(p, "MVSc") == f"{p}-MVSc-6"
    q = _label("pre")
    await _with_rolls(f"{q}-MVSc-3")
    assert await _next(q, "MV") == f"{q}-MV-1", "programme code 'MV' is not a prefix match of 'MVSc'"


# ── end to end through Orientation ─────────────────────────────────────────

async def _select(label: str, n: int) -> tuple[int, str]:
    personal, avfu = f"{_PFX}cand{n}_{_TAG}@example.com", f"{_PFX}cand{n}_{_TAG}@avfu.ac.in"
    r = await _call("POST", "/orientation/candidates", S["super"], json={
        "first_name": "ZZTEST", "last_name": f"Cand{n}", "personal_email": personal, "mobile": "9876543210", "avfu_email": avfu,
        "academic_year": label, "college_id": str(S["college"]), "program_id": str(S["P1"].id), "department_id": str(S["D1"].id)})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert (await _call("PATCH", f"/orientation/candidates/{cid}/attendance", S["super"], params={"status": "present"})).status_code == 200
    r = await _call("PATCH", f"/orientation/candidates/{cid}/selection", S["super"], params={"status": "selected"})
    return r.status_code, cid


async def t_end_to_end_edit_then_select():
    label = f"ZZTESTAYRE2E{_TAG}"                     # <= 20 chars (candidate.academic_year is String(20))
    orig = orientation_module.send_email
    orientation_module.send_email = lambda to, subject, body: True
    try:
        cids = []
        for n in (1, 2, 3):
            code, cid = await _select(label, n)
            assert code == 200, (n, code)
            cids.append(cid)
        async with AsyncSessionLocal() as db:
            rolls = [(await db.execute(select(User.student_roll).join(OrientationCandidate, OrientationCandidate.student_user_id == User.id)
                                       .where(OrientationCandidate.id == uuid.UUID(c)))).scalar_one() for c in cids]
        assert rolls == [f"{label}-MVSc-1", f"{label}-MVSc-2", f"{label}-MVSc-3"], f"format unchanged: {rolls}"
        # Super Admin edits the SECOND roll to another unique value (valid per the existing rules)
        async with AsyncSessionLocal() as db:
            sid = (await db.execute(select(OrientationCandidate.student_user_id).where(OrientationCandidate.id == uuid.UUID(cids[1])))).scalar_one()
        r = await _call("PATCH", f"/students/{sid}", S["super"], json={"student_roll": f"ZZTEST-EDITED-{_TAG}"})
        assert r.status_code == 200, r.text
        # the next selection used to compute count(2)+1 = 3 -> collide with -3 -> 409 after 20 identical attempts
        code, cid4 = await _select(label, 4)
        assert code == 200, f"selection after a roll edit must succeed (got {code})"
        async with AsyncSessionLocal() as db:
            cand4 = (await db.execute(select(OrientationCandidate).where(OrientationCandidate.id == uuid.UUID(cid4)))).scalar_one()
            all_rolls = (await db.execute(select(User.student_roll).where(User.student_roll.startswith(f"{label}-MVSc-")))).scalars().all()
        assert cand4.roll_no == f"{label}-MVSc-4", cand4.roll_no
        assert sorted(all_rolls) == [f"{label}-MVSc-1", f"{label}-MVSc-3", f"{label}-MVSc-4"] and len(set(all_rolls)) == len(all_rolls), all_rolls
        # editing the LAST one and issuing again also works
        async with AsyncSessionLocal() as db:
            sid4 = cand4.student_user_id
        assert (await _call("PATCH", f"/students/{sid4}", S["super"], json={"student_roll": f"ZZTEST-EDITED4-{_TAG}"})).status_code == 200
        code, cid5 = await _select(label, 5)
        assert code == 200
        async with AsyncSessionLocal() as db:
            assert (await db.execute(select(OrientationCandidate.roll_no).where(OrientationCandidate.id == uuid.UUID(cid5)))).scalar_one() == f"{label}-MVSc-4"
    finally:
        orientation_module.send_email = orig


async def _setup() -> None:
    async with AsyncSessionLocal() as db:
        S["D1"] = (await db.execute(select(Department).order_by(Department.code).limit(1))).scalar_one()
        S["P1"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        c = College(name=f"{_LABEL} College", code=f"ZZTESTAYR{_TAG.upper()}")
        db.add(c)
        await db.flush()
        S["college"] = c.id
        rt = uuid.uuid4()
        db.add(RefreshToken(id=rt, user_id=admin_id, token_hash=hashlib.sha256(str(rt).encode()).hexdigest(), device_info=_DEVICE))
        await db.commit()
    S["super"] = create_access_token(str(admin_id), {"sid": str(rt)})


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(OrientationCandidate).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        ids = select(User.id).where(User.email.like(f"{_PFX}%"))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(ids)))
        await db.execute(delete(User).where(User.email.like(f"{_PFX}%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "ROLL: no roll -> 1; 1,2 -> 3 (format {label}-{programme code}-{N} unchanged)": t_sequence_basics,
            "ROLL: a gap uses the highest suffix — 1,2,7 -> 8": t_gap_uses_the_highest_suffix,
            "ROLL: an edited/renamed roll never causes a collision (1,X,3 -> 4)": t_edited_roll_never_collides,
            "ROLL: complex label '... 2025-26 (Phase-II)-Ph.D(V)-10' -> ...-11 (numeric, not lexical)": t_complex_label,
            "ROLL: malformed matching-prefix values are ignored and never crash": t_malformed_suffixes_are_ignored,
            "ROLL: LIKE wildcards in a label, other labels and other programmes do not interfere": t_wildcards_and_isolation,
            "ORIENTATION END TO END: select 3, Super Admin edits a roll, the next selection succeeds without a duplicate": t_end_to_end_edit_then_select,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one(),
                "rolls": (await db.execute(select(func.count()).select_from(User).where(User.student_roll.like("ZZTEST%")))).scalar_one(),
                "candidates": (await db.execute(select(func.count()).select_from(OrientationCandidate).where(OrientationCandidate.personal_email.like(f"{_PFX}%")))).scalar_one(),
                "colleges": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
            }
            orphans = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(~UserRoleAssignment.user_id.in_(select(User.id))))).scalar_one()
        record("cleanup: no ZZTEST_ACADEMIC_YEAR_ROLL user / roll / candidate / college / session / assignment remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
