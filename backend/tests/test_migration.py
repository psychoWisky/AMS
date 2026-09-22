"""Standalone HTTP-level tests for Student Migration (BUSINESS_LOGIC.md section AE).

Modelled directly on `tests/test_thesis.py`: real FastAPI app via
`httpx.ASGITransport`, real JWT sessions via `create_access_token`/
`RefreshToken` rows, a `record(name, ok, detail)` pass/fail tracker, a
`_run(name, fn)` wrapper so one failing test doesn't stop the run, and a
`_setup()`/`_teardown()` pair using a unique `_TAG = uuid.uuid4().hex[:6]` and
`zztest_migration_...` prefix on every created row so cleanup is unambiguous.

REGISTRAR is now single-holder (like DPGS/Incharge/VC) — `_setup()` refuses to
run if a real REGISTRAR holder already exists, exactly like the DPGS/VC checks
in `test_thesis.py`. Each test function uses its OWN dedicated student(s) so
tests are independent of execution order/state, EXCEPT `t_single_registrar`
which must run LAST — it removes the shared `registrar` role assignment used
by every other test's approve/reject calls.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_migration
"""
import asyncio
import hashlib
import io
import shutil
import sys
import uuid
from pathlib import Path

import httpx
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfWriter
from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.core.security import create_access_token
from app.db.base import AsyncSessionLocal
from app.main import app
from app.models.migration import MigrationApplication
from app.models.user import College, Department, Program, RefreshToken, User, UserRole, UserRoleAssignment

_TAG = uuid.uuid4().hex[:6]
_PFX = "zztest_migration_"
_LABEL = "ZZTEST_MIGRATION"
_DEVICE = "ZZTEST-migration"
_UPLOAD_ROOT = Path(settings.UPLOAD_DIR) / "migration"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
U: dict[str, uuid.UUID] = {}      # user key -> id
TOK: dict[str, str] = {}          # user key -> access token
S: dict = {}                      # scratch
APP: dict[str, str] = {}          # student key -> current (most recently created) application id
APP_IDS: list[uuid.UUID] = []     # every application id created, for upload-dir cleanup

_STUDENT_KEYS = (
    "s_a", "s_b", "s_reg", "s_stat1", "s_stat2", "s_sub", "s_rec",
    "s_wf1", "s_wf2", "s_role", "s_sa", "s_idor",
)


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


def _pdf(pages: int = 1, text_: str = "ZZTEST MIGRATION RECEIPT") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(pages):
        c.drawString(72, 720, f"{text_} page {i + 1}")
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


async def _mk_user(key, role, dept=None, *, program=None, college=None, roll=None, title=None, designation=None) -> None:
    async with AsyncSessionLocal() as db:
        u = User(email=f"{_PFX}{key}_{_TAG}@avfu.ac.in", first_name="ZZTEST", last_name=f"MIG{key.upper()}", title=title,
                  designation=designation, role=role, department_id=dept.id if dept else None,
                  program_id=program.id if program else None, college_id=college.id if college else None,
                  student_roll=roll, mobile="9876543210", gender="Male", is_active=True, is_verified=True)
        db.add(u)
        await db.flush()
        U[key] = u.id
        if role != UserRole.STUDENT:
            db.add(UserRoleAssignment(user_id=u.id, role=role, department_id=dept.id if dept else None))
        await db.commit()


# ── DB read helpers ──────────────────────────────────────────────────────────

async def _row(aid) -> MigrationApplication:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(MigrationApplication).where(MigrationApplication.id == uuid.UUID(aid)))).scalar_one()


# ── HTTP action helpers ──────────────────────────────────────────────────────

async def _create(student_key: str, expect=201) -> httpx.Response:
    r = await _call("POST", "/migration", TOK[student_key], json={})
    assert r.status_code == expect, (r.status_code, r.text)
    if expect == 201:
        APP[student_key] = r.json()["id"]
        APP_IDS.append(uuid.UUID(APP[student_key]))
    return r


async def _detail(aid, token) -> dict:
    r = await _call("GET", f"/migration/{aid}", token)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


async def _upload_receipt(aid, token, data=None, filename="receipt.pdf", ct="application/pdf") -> httpx.Response:
    data = data if data is not None else _pdf()
    return await _call("POST", f"/migration/{aid}/receipt", token, files={"file": (filename, data, ct)})


_ALL_REQUIRED_FIELDS = {
    "registration_no": "ZZTEST-REG-001",
    "last_exam_name_and_roll": "ZZTEST Final Exam, Roll 42",
    "passed_from_institution": "ZZTEST Institution of Veterinary Science",
    "migration_reason": "ZZTEST relocation of family",
    "address": "ZZTEST 123 Main Street",
}


async def _fill_all_fields(aid, token, fee_date="2026-01-15") -> None:
    body = dict(_ALL_REQUIRED_FIELDS)
    body["fee_payment_date"] = fee_date
    r = await _call("PATCH", f"/migration/{aid}", token, json=body)
    assert r.status_code == 200, r.text


async def _make_submittable(student_key: str) -> None:
    aid = APP[student_key]
    tok = TOK[student_key]
    await _fill_all_fields(aid, tok)
    assert (await _upload_receipt(aid, tok)).status_code == 201


async def _submit(student_key: str, expect=200) -> httpx.Response:
    r = await _call("POST", f"/migration/{APP[student_key]}/submit", TOK[student_key])
    assert r.status_code == expect, (r.status_code, r.text)
    return r


async def _approve(aid, token, body=None, expect=200) -> httpx.Response:
    r = await _call("POST", f"/migration/{aid}/approval/approve", token, json=body if body is not None else {})
    assert r.status_code == expect, (r.status_code, r.text)
    return r


async def _reject(aid, token, body=None, expect=200) -> httpx.Response:
    r = await _call("POST", f"/migration/{aid}/approval/reject", token, json=body if body is not None else {})
    assert r.status_code == expect, (r.status_code, r.text)
    return r


# ── setup / teardown ─────────────────────────────────────────────────────────

async def _setup() -> None:
    settings.ENVIRONMENT = "development"
    async with AsyncSessionLocal() as db:
        holders = (await db.execute(text(
            "select count(*) from ams_user_role_assignments where role = 'REGISTRAR'"
        ))).scalar_one()
        if holders:
            print("STOP: a real Registrar holder exists (single-holder role); refusing to run.")
            sys.exit(2)
        stray = (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{_PFX}%")))).scalar_one()
        if stray:
            print("STOP: stray zztest_migration_ users already exist from a previous failed run; refusing to run.")
            sys.exit(2)
        depts = (await db.execute(select(Department).where(Department.code != "LPM").order_by(Department.code).limit(1))).scalars().all()
        S["D1"] = depts[0]
        S["PG"] = (await db.execute(select(Program).where(Program.code == "MVSc"))).scalar_one()
        col = College(name=f"{_LABEL} College", code=f"ZZMG{_TAG.upper()}"[:20])
        db.add(col)
        await db.flush()
        S["COLLEGE"] = col
        admin_id = (await db.execute(select(User.id).where(User.role == UserRole.SUPER_ADMIN).limit(1))).scalar_one()
        await db.commit()
    S["ADMIN"] = admin_id
    d1, pg, college = S["D1"], S["PG"], S["COLLEGE"]

    for k in _STUDENT_KEYS:
        await _mk_user(k, UserRole.STUDENT, d1, program=pg, college=college, roll=f"ZZMG-{k}-{_TAG}")
    await _mk_user("faculty1", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("hod1", UserRole.HOD, d1, title="Dr.", designation="Professor")
    # The single Registrar used throughout every approve/reject test, created directly
    # (bypassing the HTTP assignment flow — same shortcut used for every other
    # single-holder role in test_thesis.py). Removed and reassigned ONLY in
    # t_single_registrar, which therefore must run LAST. Given a SECOND (dummy)
    # role assignment directly at the DB level so the REGISTRAR assignment can
    # actually be removed later — `remove_user_role` refuses to strip a user's
    # only remaining role assignment ("Cannot remove a user's only assigned
    # role"), which is correct, unrelated app behavior, not a Migration bug.
    await _mk_user("registrar", UserRole.REGISTRAR, None, title="Mr.", designation="Registrar")
    async with AsyncSessionLocal() as db:
        db.add(UserRoleAssignment(user_id=U["registrar"], role=UserRole.FACULTY, department_id=S["D1"].id))
        await db.commit()
    # Two identities used ONLY by t_single_registrar's HTTP-assignment probes.
    await _mk_user("reg_cand1", UserRole.FACULTY, d1, title="Dr.", designation="Professor")
    await _mk_user("reg_cand2", UserRole.FACULTY, d1, title="Dr.", designation="Professor")

    S["SA"] = await _session(admin_id)
    for k in (*_STUDENT_KEYS, "faculty1", "hod1", "registrar", "reg_cand1", "reg_cand2"):
        TOK[k] = await _session(U[k])


async def _teardown() -> None:
    async with AsyncSessionLocal() as db:
        uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
        await db.execute(delete(MigrationApplication).where(
            MigrationApplication.student_id.in_(uids) | MigrationApplication.decided_by.in_(uids)
        ))
        await db.execute(delete(RefreshToken).where(RefreshToken.device_info == _DEVICE))
        await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))
        await db.execute(delete(User).where(User.email.like(f"%{_TAG}@%")))
        await db.execute(delete(College).where(College.name.like(f"{_LABEL}%")))
        await db.commit()

    for aid in APP_IDS:
        shutil.rmtree(_UPLOAD_ROOT / str(aid), ignore_errors=True)
    if _UPLOAD_ROOT.exists() and not any(_UPLOAD_ROOT.iterdir()):
        _UPLOAD_ROOT.rmdir()


async def _run(name, fn):
    try:
        await fn()
        record(name, True)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"{type(e).__name__}: {e}")


# ── tests ────────────────────────────────────────────────────────────────────

async def t_ownership_and_isolation():
    r = await _create("s_a")
    assert r.json()["status"] == "draft"
    d = await _detail(APP["s_a"], TOK["s_a"])
    assert d["is_owner"] is True and d["can_edit"] is True and d["status"] == "draft"
    assert d["student_roll"] == f"ZZMG-s_a-{_TAG}"
    r = await _call("PATCH", f"/migration/{APP['s_a']}", TOK["s_a"], json={"address": "ZZTEST Address v1"})
    assert r.status_code == 200 and r.json()["address"] == "ZZTEST Address v1"
    await _make_submittable("s_a")
    r = await _submit("s_a")
    assert r.json()["status"] == "submitted"

    await _create("s_b")
    sid_a, tok_b = APP["s_a"], TOK["s_b"]
    assert (await _call("GET", f"/migration/{sid_a}", tok_b)).status_code == 404
    assert (await _call("PATCH", f"/migration/{sid_a}", tok_b, json={"address": "hijack"})).status_code == 404
    assert (await _call("POST", f"/migration/{sid_a}/submit", tok_b)).status_code == 404
    assert (await _upload_receipt(sid_a, tok_b)).status_code == 404
    assert (await _call("GET", f"/migration/{sid_a}/receipt", tok_b)).status_code == 404
    row = await _row(sid_a)
    # `_make_submittable` (called after the address PATCH above) overwrites `address`
    # with `_ALL_REQUIRED_FIELDS["address"]` as part of filling every required field —
    # that is the correct final value; the point of this assertion is just that
    # Student B's "hijack" PATCH attempt above never took effect.
    assert row.address == _ALL_REQUIRED_FIELDS["address"], "Student B must not have mutated Student A's application"


async def t_registration_no_and_snapshots():
    await _create("s_reg")
    aid = APP["s_reg"]
    tok = TOK["s_reg"]
    # freely settable, no format validation, not derived from student_roll
    r = await _call("PATCH", f"/migration/{aid}", tok, json={"registration_no": "FREEFORM-!!!-not-a-roll-9999"})
    assert r.status_code == 200 and r.json()["registration_no"] == "FREEFORM-!!!-not-a-roll-9999"
    row = await _row(aid)
    orig_name, orig_roll, orig_degree, orig_college = row.student_name_snapshot, row.student_roll_snapshot, row.degree_snapshot, row.college_snapshot
    assert orig_roll == f"ZZMG-s_reg-{_TAG}"
    assert row.registration_no != orig_roll
    # sending snapshot fields as extra fields in PATCH body is rejected (422, extra=forbid)
    r = await _call("PATCH", f"/migration/{aid}", tok, json={
        "address": "x", "student_name_snapshot": "HACKED", "student_roll_snapshot": "HACKED",
        "degree_snapshot": "HACKED", "college_snapshot": "HACKED",
    })
    assert r.status_code == 422, r.text
    row2 = await _row(aid)
    assert (row2.student_name_snapshot, row2.student_roll_snapshot, row2.degree_snapshot, row2.college_snapshot) == \
           (orig_name, orig_roll, orig_degree, orig_college)


async def t_status_transitions():
    # ── s_stat1: draft -> submitted -> approved, then a brand-new application ──
    await _create("s_stat1")
    aid1 = APP["s_stat1"]
    # draft -> approved / draft -> rejected BLOCKED
    await _approve(aid1, TOK["registrar"], expect=400)
    await _reject(aid1, TOK["registrar"], expect=400)
    assert (await _row(aid1)).status == "draft"

    await _make_submittable("s_stat1")
    await _submit("s_stat1")
    assert (await _row(aid1)).status == "submitted"
    # submitted -> submitted (double submit) BLOCKED
    await _submit("s_stat1", expect=400)

    await _approve(aid1, TOK["registrar"])
    assert (await _row(aid1)).status == "approved"
    # approved -> rejected / approved -> approved BLOCKED
    await _reject(aid1, TOK["registrar"], expect=400)
    await _approve(aid1, TOK["registrar"], expect=400)

    # a brand-new application succeeds now that the first is terminal (approved)
    r = await _create("s_stat1")
    assert r.status_code == 201
    aid1b = APP["s_stat1"]
    assert aid1b != aid1
    # cannot create a second one while active (draft) -> 409
    r = await _call("POST", "/migration", TOK["s_stat1"], json={})
    assert r.status_code == 409, r.text
    # a direct second active row at the DB level cannot bypass the partial unique index either
    async with AsyncSessionLocal() as db:
        dup = MigrationApplication(
            student_id=U["s_stat1"], status="submitted",
            student_name_snapshot="ZZTEST dup", student_roll_snapshot="dup",
        )
        db.add(dup)
        raised = False
        try:
            await db.commit()
        except Exception:
            raised = True
            await db.rollback()
        assert raised, "DB partial unique index did not block a second active row"

    # ── s_stat2: submitted -> rejected, historical query, resubmit blocked, new app allowed ──
    await _create("s_stat2")
    aid2 = APP["s_stat2"]
    await _make_submittable("s_stat2")
    await _submit("s_stat2")
    await _reject(aid2, TOK["registrar"])
    assert (await _row(aid2)).status == "rejected"
    # rejected -> approved / rejected -> submitted (resubmit) BLOCKED
    await _approve(aid2, TOK["registrar"], expect=400)
    r = await _call("POST", f"/migration/{aid2}/submit", TOK["s_stat2"])
    assert r.status_code == 400, r.text
    # a rejected application remains queryable/historical: GET still works, fields intact
    d = await _detail(aid2, TOK["s_stat2"])
    assert d["status"] == "rejected" and d["registration_no"] == _ALL_REQUIRED_FIELDS["registration_no"]
    # student CAN create a brand-new application afterward (first is terminal: rejected)
    r = await _create("s_stat2")
    assert r.status_code == 201


async def t_submission_validation():
    await _create("s_sub")
    aid = APP["s_sub"]
    tok = TOK["s_sub"]

    # submit with nothing filled -> 400
    r = await _submit("s_sub", expect=400)
    assert r.json()["detail"], r.text

    ordered_fields = [
        ("registration_no", _ALL_REQUIRED_FIELDS["registration_no"]),
        ("last_exam_name_and_roll", _ALL_REQUIRED_FIELDS["last_exam_name_and_roll"]),
        ("passed_from_institution", _ALL_REQUIRED_FIELDS["passed_from_institution"]),
        ("migration_reason", _ALL_REQUIRED_FIELDS["migration_reason"]),
        ("address", _ALL_REQUIRED_FIELDS["address"]),
    ]
    # fill each required text field one at a time; each intermediate submit attempt
    # still fails until ALL text fields + fee_payment_date + receipt are present
    for field, value in ordered_fields:
        r = await _submit("s_sub", expect=400)
        assert r.status_code == 400, (field, r.text)
        assert (await _call("PATCH", f"/migration/{aid}", tok, json={field: value})).status_code == 200
    # still missing fee_payment_date
    r = await _submit("s_sub", expect=400)
    assert r.status_code == 400, r.text
    assert (await _call("PATCH", f"/migration/{aid}", tok, json={"fee_payment_date": "2026-02-01"})).status_code == 200
    # still missing the receipt
    r = await _submit("s_sub", expect=400)
    assert "receipt" in r.json()["detail"].lower(), r.text
    assert (await _upload_receipt(aid, tok)).status_code == 201
    # now fully complete -> succeeds
    r = await _submit("s_sub", expect=200)
    assert r.json()["status"] == "submitted"


async def t_receipt_handling():
    await _create("s_rec")
    aid = APP["s_rec"]
    tok = TOK["s_rec"]
    # valid PDF accepted
    assert (await _upload_receipt(aid, tok)).status_code == 201
    # .docx rejected
    assert (await _upload_receipt(aid, tok, data=b"PK\x03\x04" + b"0" * 100, filename="r.docx",
                                    ct="application/vnd.openxmlformats-officedocument.wordprocessingml.document")).status_code == 400
    # .jpg renamed to .pdf rejected
    jpg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 200
    assert (await _upload_receipt(aid, tok, data=jpg_bytes, filename="r.pdf", ct="application/pdf")).status_code == 400
    # .exe renamed to .pdf rejected
    exe_bytes = b"MZ" + b"\x00" * 200
    assert (await _upload_receipt(aid, tok, data=exe_bytes, filename="r.pdf", ct="application/pdf")).status_code == 400
    # corrupt/truncated PDF rejected
    assert (await _upload_receipt(aid, tok, data=b"%PDF-1.4\nthis is not really a pdf\n%%EOF", filename="r.pdf")).status_code == 400
    # encrypted PDF rejected
    assert (await _upload_receipt(aid, tok, data=_encrypted_pdf(), filename="r.pdf")).status_code == 400
    # oversized rejected
    big = b"%PDF-1.4\n" + b"0" * (settings.MAX_FILE_SIZE_MB * 1024 * 1024 + 100)
    r = await _upload_receipt(aid, tok, data=big, filename="big.pdf")
    assert r.status_code in (400, 413), r.text

    # replacing the receipt while draft works and does not leave the old file orphaned
    files_before = sorted(p.name for p in (_UPLOAD_ROOT / aid).iterdir())
    r = await _upload_receipt(aid, tok, data=_pdf(2, "ZZTEST v2"), filename="v2.pdf")
    assert r.status_code == 201, r.text
    files_after = sorted(p.name for p in (_UPLOAD_ROOT / aid).iterdir())
    assert len(files_after) == 1 and files_after != files_before, (files_before, files_after)

    # replacing once submitted is rejected (immutable)
    await _fill_all_fields(aid, tok)
    await _submit("s_rec")
    r = await _upload_receipt(aid, tok, data=_pdf(1, "ZZTEST post-submit"), filename="v3.pdf")
    assert r.status_code == 400, r.text

    # downloading a foreign application's receipt by id is blocked (s_b's token already
    # exists from setup; we don't need s_b to have any application of its own here)
    assert (await _call("GET", f"/migration/{aid}/receipt", TOK["s_b"])).status_code == 404
    # student downloading their OWN receipt after submission works
    r = await _call("GET", f"/migration/{aid}/receipt", tok)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    # ... and after a decision (approve) still works
    await _approve(aid, TOK["registrar"])
    r = await _call("GET", f"/migration/{aid}/receipt", tok)
    assert r.status_code == 200


async def t_registrar_workflow():
    await _create("s_wf1")
    aid1 = APP["s_wf1"]
    r = await _call("GET", "/migration/pending-approvals", TOK["registrar"])
    assert r.status_code == 200
    assert aid1 not in {row["id"] for row in r.json()}, "a draft must not appear in pending-approvals"

    await _make_submittable("s_wf1")
    await _submit("s_wf1")
    r = await _call("GET", "/migration/pending-approvals", TOK["registrar"])
    assert aid1 in {row["id"] for row in r.json()}, "a submitted application must appear in pending-approvals"

    # approve with empty body {}
    r = await _approve(aid1, TOK["registrar"], body={})
    assert r.status_code == 200
    row = await _row(aid1)
    assert row.status == "approved" and row.decided_by == U["registrar"] and row.decided_at is not None and row.decision_remark is None
    r = await _call("GET", "/migration/pending-approvals", TOK["registrar"])
    assert aid1 not in {row2["id"] for row2 in r.json()}, "an approved application must not remain in pending-approvals"
    # cannot act again once approved
    await _approve(aid1, TOK["registrar"], expect=400)
    await _reject(aid1, TOK["registrar"], expect=400)

    # a fresh submitted application, rejected WITH a remark
    await _create("s_wf2")
    aid2 = APP["s_wf2"]
    await _make_submittable("s_wf2")
    await _submit("s_wf2")
    r = await _reject(aid2, TOK["registrar"], body={"remark": "ZZTEST insufficient documentation"})
    assert r.status_code == 200
    row2 = await _row(aid2)
    assert row2.status == "rejected" and row2.decision_remark == "ZZTEST insufficient documentation" and row2.decided_by == U["registrar"]
    await _approve(aid2, TOK["registrar"], expect=400)
    await _reject(aid2, TOK["registrar"], expect=400)

    # registrar cannot approve a draft, cannot reject a draft (re-confirm on a fresh app)
    await _create("s_wf1")
    aid3 = APP["s_wf1"]
    await _approve(aid3, TOK["registrar"], expect=400)
    await _reject(aid3, TOK["registrar"], expect=400)


async def t_role_isolation():
    await _create("s_role")
    aid = APP["s_role"]
    await _make_submittable("s_role")
    await _submit("s_role")
    for who, tok in (("student", TOK["s_role"]), ("faculty", TOK["faculty1"]), ("hod", TOK["hod1"])):
        assert (await _call("POST", f"/migration/{aid}/approval/approve", tok, json={})).status_code == 403, who
        assert (await _call("POST", f"/migration/{aid}/approval/reject", tok, json={})).status_code == 403, who
    # Registrar gains no capability on unrelated endpoints
    assert (await _call("GET", "/students", TOK["registrar"])).status_code == 403
    assert (await _call("GET", "/auth/users", TOK["registrar"])).status_code == 403


async def t_super_admin_oversight():
    await _create("s_sa")
    aid = APP["s_sa"]
    await _make_submittable("s_sa")
    await _submit("s_sa")
    # Super Admin can VIEW any application and its receipt (oversight)
    r = await _call("GET", f"/migration/{aid}", S["SA"])
    assert r.status_code == 200, r.text
    r = await _call("GET", f"/migration/{aid}/receipt", S["SA"])
    assert r.status_code == 200, r.text
    # Super Admin does NOT gain Migration-specific mutation capability: approve/reject
    # require exactly UserRole.REGISTRAR, so Super Admin is blocked too — confirmed by
    # reading migration.py (require_roles(UserRole.REGISTRAR) on both endpoints, no
    # SUPER_ADMIN in the tuple). This is by design, not a bug.
    r_approve = await _call("POST", f"/migration/{aid}/approval/approve", S["SA"], json={})
    r_reject = await _call("POST", f"/migration/{aid}/approval/reject", S["SA"], json={})
    assert r_approve.status_code == 403, r_approve.text
    assert r_reject.status_code == 403, r_reject.text


async def t_idor_and_tampering():
    await _create("s_idor")
    aid = APP["s_idor"]
    await _make_submittable("s_idor")
    # query-parameter tampering has zero effect
    base = await _detail(aid, TOK["s_idor"])
    r = await _call("GET", f"/migration/{aid}?debug=1&student_id={U['s_a']}", TOK["s_idor"])
    assert r.status_code == 200 and r.json() == base
    # body role/identity injection rejected via extra=forbid (422)
    assert (await _call("PATCH", f"/migration/{aid}", TOK["s_idor"], json={"address": "x", "student_id": str(U["s_a"]), "role": "registrar"})).status_code == 422
    assert (await _call("POST", "/migration", TOK["s_idor"], json={"student_id": str(U["s_a"])})).status_code in (409, 422)
    await _submit("s_idor")
    assert (await _call("POST", f"/migration/{aid}/approval/approve", TOK["registrar"],
                          json={"remark": "ok", "role": "registrar", "student_id": str(U["s_a"])})).status_code == 422


async def t_single_registrar():
    """Must run AFTER every other test — it removes the shared `registrar`
    role assignment that every prior approve/reject call above relied on."""
    # a second REGISTRAR assignment while "registrar" is still active is rejected (409)
    r = await _call("POST", f"/auth/users/{U['reg_cand1']}/roles", S["SA"], json={"role": "registrar"})
    assert r.status_code == 409, r.text
    # department_id sent with a REGISTRAR assignment is rejected (400), matching the
    # existing DPGS/Incharge/VC convention
    r = await _call("POST", f"/auth/users/{U['reg_cand2']}/roles", S["SA"], json={"role": "registrar", "department_id": str(S["D1"].id)})
    assert r.status_code == 400, r.text

    # remove the original Registrar's role assignment
    r = await _call("DELETE", f"/auth/users/{U['registrar']}/roles/registrar", S["SA"])
    assert r.status_code in (200, 204), r.text

    # now assigning REGISTRAR to a new user succeeds
    r = await _call("POST", f"/auth/users/{U['reg_cand1']}/roles", S["SA"], json={"role": "registrar"})
    assert r.status_code in (200, 201), r.text

    # a second REGISTRAR assignment while reg_cand1 is active is rejected (409)
    r = await _call("POST", f"/auth/users/{U['reg_cand2']}/roles", S["SA"], json={"role": "registrar"})
    assert r.status_code == 409, r.text

    async with AsyncSessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.role == UserRole.REGISTRAR))).scalar_one()
    assert n == 1, n

    # tidy up: remove reg_cand1's REGISTRAR assignment so no REGISTRAR holder
    # remains once this test file finishes (teardown deletes the user rows
    # regardless, but this keeps the final DB-wide REGISTRAR count at 0 even
    # before that runs).
    await _call("DELETE", f"/auth/users/{U['reg_cand1']}/roles/registrar", S["SA"])


async def main() -> None:
    try:
        await _setup()
        for name, fn in {
            "OWNERSHIP: Student A create/read/edit/submit own draft; Student B blocked (404) on A's application": t_ownership_and_isolation,
            "REGISTRATION NO / SNAPSHOTS: freeform registration_no; snapshot fields rejected via extra=forbid, unchanged in DB": t_registration_no_and_snapshots,
            "STATUS TRANSITIONS: valid transitions pass; every invalid transition blocked (400); DB partial unique index holds": t_status_transitions,
            "SUBMISSION VALIDATION: each missing required field / receipt rejected (400); complete submission succeeds": t_submission_validation,
            "RECEIPT HANDLING: pdf accepted; docx/renamed-jpg/renamed-exe/corrupt/encrypted/oversized rejected; replace-while-draft no orphan; immutable once submitted; foreign download 404; owner download always works": t_receipt_handling,
            "REGISTRAR WORKFLOW: pending-approvals shows only submitted; approve/reject with {} and remark; decision fields recorded; no double-decide; draft blocked": t_registrar_workflow,
            "ROLE ISOLATION: Student/Faculty/HOD blocked (403) on approve/reject; Registrar blocked (403) on /students and /auth/users": t_role_isolation,
            "SUPER ADMIN OVERSIGHT: can view application+receipt; blocked (403) on approve/reject (Registrar-only)": t_super_admin_oversight,
            "IDOR/TAMPERING: query params inert; extra body fields -> 422 (extra=forbid)": t_idor_and_tampering,
            "SINGLE REGISTRAR (run last): second assignment 409 while active; department_id rejected (400); remove+reassign works; DB holds exactly 1": t_single_registrar,
        }.items():
            await _run(name, fn)
    finally:
        await _teardown()
        async with AsyncSessionLocal() as db:
            uids = select(User.id).where(User.email.like(f"%{_TAG}@%"))
            left = {
                "users": (await db.execute(select(func.count()).select_from(User).where(User.email.like(f"%{_TAG}@%")))).scalar_one(),
                "applications": (await db.execute(select(func.count()).select_from(MigrationApplication).where(
                    MigrationApplication.student_id.in_(uids)))).scalar_one(),
                "college": (await db.execute(select(func.count()).select_from(College).where(College.name.like(f"{_LABEL}%")))).scalar_one(),
                "sessions": (await db.execute(select(func.count()).select_from(RefreshToken).where(RefreshToken.device_info == _DEVICE))).scalar_one(),
                "role_assignments": (await db.execute(select(func.count()).select_from(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(uids)))).scalar_one(),
            }
            registrar_holders = (await db.execute(text(
                "select count(*) from ams_user_role_assignments where role = 'REGISTRAR'"
            ))).scalar_one()
        upload_leftover = _UPLOAD_ROOT.exists()
        record("cleanup: no ZZTEST_MIGRATION user / application / college / session / role-assignment row remains; no leftover REGISTRAR holder; no uploaded files remain",
               not any(left.values()) and registrar_holders == 0 and not upload_leftover,
               str(left) + f" registrar_holders={registrar_holders} upload_dir_exists={upload_leftover}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
