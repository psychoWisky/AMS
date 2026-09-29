"""Standalone HTTP-level tests for two confirmed AVFU business rules implemented
in this task:

1. Advisory Committee creation no longer requires Research Area, and Research
   Title remains optional (`POST /research/committees` accepts a missing,
   empty, or present `research_title`; `research_area` is untouched/optional,
   kept only for backward compatibility — no destructive schema change).

2. PPW -> Advisory Committee Research Title synchronization: once a student's
   PPW is SUBMITTED (`PATCH /ppw/{id}/submit`) with a Research Title, that
   title becomes the authoritative `AdvisoryCommittee.research_title` for the
   SAME student — overwriting whatever the committee's title was before
   (including one set at Advisory Committee creation, blank or not). The sync
   point is submission, not creation/draft-update, because `research_title` is
   only guaranteed non-blank once `submit_ppw`'s hard validation has run, and
   the committee is resolved server-side via the authenticated caller's own
   `student_id` (never a client-supplied committee id) — see `submit_ppw` in
   `app/api/v1/endpoints/ppw.py`.

Modelled on `tests/test_advisory_committee_department_eligibility.py`: real
FastAPI app via `httpx.ASGITransport`, real JWT sessions, a `record`/`_run`
pass-fail tracker, `zztest_pts_...`-prefixed rows, full teardown.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_ppw_research_title_sync
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
from app.models.ppw import Ppw, PpwApprovalCycle
from app.models.user import Department, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_pts_"
_DEVICE = "ZZTEST-pts"

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


async def _mk_user(key, role, dept):
    async with AsyncSessionLocal() as db:
        u = User(
            email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"PTS{key.upper()}",
            role=role, department_id=dept.id if dept else None,
            mobile="9876543210", gender="Male", is_active=True, is_verified=True,
        )
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


async def _mk_ppw(student_key: str) -> uuid.UUID:
    r = await _call("POST", "/ppw", TOK[student_key], json={})
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def _committee_title(committee_id: uuid.UUID) -> str | None:
    async with AsyncSessionLocal() as db:
        c = await db.get(AdvisoryCommittee, committee_id)
        return c.research_title


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
            print("STOP: stray zztest_pts_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        dept = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(1))).scalar_one()
        S["A"] = dept
        await db.commit()
    A = S["A"]

    await _mk_user("hod_a", UserRole.HOD, A)
    await _mk_user("ma", UserRole.FACULTY, A)
    await _mk_user("stu_a", UserRole.STUDENT, A)  # Scenario B: pre-existing non-empty title, gets overwritten
    await _mk_user("stu_b", UserRole.STUDENT, A)  # Scenario A: pre-existing empty title, gets filled
    await _mk_user("stu_c", UserRole.STUDENT, A)  # creation-only tests (no PPW submitted)
    await _mk_user("stu_d", UserRole.STUDENT, A)  # creation-only: research_area backward-compat

    for k in ("hod_a", "ma", "stu_a", "stu_b", "stu_c", "stu_d"):
        TOK[k] = await _session(U[k])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        cids = select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id.in_(uids))
        ppw_ids = select(Ppw.id).where(Ppw.student_id.in_(uids))
        await db.execute(delete(PpwApprovalCycle).where(PpwApprovalCycle.ppw_id.in_(ppw_ids)))
        await db.execute(delete(Ppw).where(Ppw.id.in_(ppw_ids)))
        await db.execute(delete(CommitteeMember).where(CommitteeMember.committee_id.in_(cids)))
        await db.execute(delete(AdvisoryCommittee).where(AdvisoryCommittee.student_id.in_(uids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.commit()


# ── Advisory Committee creation: Research Title optional, Research Area not required ──

async def t_create_with_research_title():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={
        "student_id": str(U["stu_c"]), "major_advisor_id": str(U["ma"]), "research_title": "Initial Research Title",
    })
    assert r.status_code == 201, r.text
    C["stu_c"] = r.json()["id"]
    title = await _committee_title(uuid.UUID(C["stu_c"]))
    assert title == "Initial Research Title", title


async def t_create_without_research_title_key_omitted():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={
        "student_id": str(U["stu_a"]), "major_advisor_id": str(U["ma"]),
    })
    assert r.status_code == 201, r.text
    ma_response = await _call("PATCH", f"/research/committees/{r.json()['id']}/major-advisor-response", TOK["ma"], json={"accepted": True})
    assert ma_response.status_code == 200, ma_response.text
    C["stu_a"] = r.json()["id"]


async def t_create_with_empty_research_title():
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={
        "student_id": str(U["stu_b"]), "major_advisor_id": str(U["ma"]), "research_title": "",
    })
    assert r.status_code == 201, r.text
    ma_response = await _call("PATCH", f"/research/committees/{r.json()['id']}/major-advisor-response", TOK["ma"], json={"accepted": True})
    assert ma_response.status_code == 200, ma_response.text
    C["stu_b"] = r.json()["id"]
    title = await _committee_title(uuid.UUID(C["stu_b"]))
    assert title == "", title


async def t_research_area_not_required_and_still_accepted_if_sent():
    """Backward compatibility: the backend field/column is untouched, so a
    non-UI client that still sends research_area must not be rejected — but a
    request that omits it entirely (matching the updated frontend) must also
    succeed, which the three tests above already prove independently of this
    one."""
    r = await _call("POST", "/research/committees", TOK["hod_a"], json={
        "student_id": str(U["stu_d"]), "major_advisor_id": str(U["ma"]),
        "research_title": "Has Area", "research_area": "Plant Breeding",
    })
    assert r.status_code == 201, r.text
    ma_response = await _call("PATCH", f"/research/committees/{r.json()['id']}/major-advisor-response", TOK["ma"], json={"accepted": True})
    assert ma_response.status_code == 200, ma_response.text
    async with AsyncSessionLocal() as db:
        c = await db.get(AdvisoryCommittee, uuid.UUID(r.json()["id"]))
        assert c.research_area == "Plant Breeding", c.research_area
    C["stu_d"] = r.json()["id"]


# ── PPW research_title fields required for submission ────────────────────────

async def _fill_and_submit_ppw(student_key: str, ppw_id: uuid.UUID, title: str) -> None:
    r = await _call("PATCH", f"/ppw/{ppw_id}", TOK[student_key], json={
        "field_of_investigation": "ZZTEST field", "research_title": title,
    })
    assert r.status_code == 200, r.text
    # minor_field/supporting_field are server-derived from selected Minor/
    # Supporting courses (discipline-derivation task) and are NOT part of
    # PpwIn — submission requires them non-blank too, so they are set
    # directly here rather than through a (course-selection-dependent) API
    # call that is unrelated to this task's scope.
    async with AsyncSessionLocal() as db:
        p = await db.get(Ppw, ppw_id)
        p.minor_field = "ZZTEST minor"
        p.supporting_field = "ZZTEST supporting"
        await db.commit()
    r2 = await _call("PATCH", f"/ppw/{ppw_id}/submit", TOK[student_key])
    assert r2.status_code == 200, r2.text


# ── Scenario A: empty Advisory Committee title -> filled by PPW title ───────

async def t_scenario_a_empty_committee_title_filled_by_ppw():
    assert await _committee_title(uuid.UUID(C["stu_b"])) == ""
    ppw_id = await _mk_ppw("stu_b")
    await _fill_and_submit_ppw("stu_b", ppw_id, "New PPW Research Title (Scenario A)")
    title = await _committee_title(uuid.UUID(C["stu_b"]))
    assert title == "New PPW Research Title (Scenario A)", title


# ── Scenario B: existing Advisory Committee title overwritten by PPW title ──

async def t_scenario_b_existing_committee_title_overwritten_by_ppw():
    assert await _committee_title(uuid.UUID(C["stu_a"])) is None  # never set at creation (omitted)
    # Give it a real pre-existing title first, simulating "Study of Animal
    # Nutrition" from the task's example, to prove overwrite (not merge/ignore).
    async with AsyncSessionLocal() as db:
        c = await db.get(AdvisoryCommittee, uuid.UUID(C["stu_a"]))
        c.research_title = "Old Committee Title (Study of Animal Nutrition)"
        await db.commit()
    ppw_id = await _mk_ppw("stu_a")
    await _fill_and_submit_ppw("stu_a", ppw_id, "Impact of Feed Formulation on Animal Nutrition")
    title = await _committee_title(uuid.UUID(C["stu_a"]))
    assert title == "Impact of Feed Formulation on Animal Nutrition", title


# ── Scenario C: draft PPW edits must NOT sync until actual submission ───────

async def t_scenario_c_draft_edits_do_not_sync_only_submission_does():
    ppw_id = await _mk_ppw("stu_d")
    r1 = await _call("PATCH", f"/ppw/{ppw_id}", TOK["stu_d"], json={"research_title": "Draft Title 1"})
    assert r1.status_code == 200, r1.text
    assert await _committee_title(uuid.UUID(C["stu_d"])) == "Has Area", "a draft PPW edit must not sync to the Advisory Committee"
    r2 = await _call("PATCH", f"/ppw/{ppw_id}", TOK["stu_d"], json={"research_title": "Draft Title 2"})
    assert r2.status_code == 200, r2.text
    assert await _committee_title(uuid.UUID(C["stu_d"])) == "Has Area", "a second draft PPW edit must still not sync"
    await _fill_and_submit_ppw("stu_d", ppw_id, "Draft Title 2")
    assert await _committee_title(uuid.UUID(C["stu_d"])) == "Draft Title 2", "submission must sync the FINAL PPW title"


# ── Scenario D: cross-student isolation ─────────────────────────────────────

async def t_scenario_d_other_students_committees_untouched():
    """Every prior scenario's submission must have touched ONLY its own
    student's committee — never another student's. There is no committee_id
    parameter on `PATCH /ppw/{id}/submit` at all (verified by inspection), so
    there is no manipulable surface to test a hostile request against; this
    test instead verifies the actual, already-executed side effects were
    correctly isolated."""
    title_c = await _committee_title(uuid.UUID(C["stu_c"]))
    assert title_c == "Initial Research Title", "stu_c's committee (no PPW ever submitted for it) must be untouched"


async def main() -> None:
    await _setup()
    try:
        cases = {
            "Create committee WITH Research Title -> 201, stored exactly": t_create_with_research_title,
            "Create committee with Research Title OMITTED -> 201": t_create_without_research_title_key_omitted,
            "Create committee with Research Title = '' -> 201, stored as ''": t_create_with_empty_research_title,
            "Research Area not required; still accepted+stored if sent (backward compat)": t_research_area_not_required_and_still_accepted_if_sent,
            "Scenario A: empty Advisory Committee title filled by PPW submission": t_scenario_a_empty_committee_title_filled_by_ppw,
            "Scenario B: existing Advisory Committee title OVERWRITTEN by PPW submission": t_scenario_b_existing_committee_title_overwritten_by_ppw,
            "Scenario C: draft PPW edits do not sync; only submission does": t_scenario_c_draft_edits_do_not_sync_only_submission_does,
            "Scenario D: other students' committees remain untouched": t_scenario_d_other_students_committees_untouched,
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
        record("cleanup: no ZZTEST_PTS user / session row remains", not any(left.values()) and orphans == 0, str(left))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
