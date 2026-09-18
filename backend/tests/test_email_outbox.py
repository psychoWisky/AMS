"""Standalone, dependency-free test script for the durable email-outbox
architecture (Bulk Upload SMTP timeout fix).

No pytest: this repository has no pre-existing test framework or dependency
(confirmed absent from `requirements/requirements.txt` and there was no
`tests/` directory before this file), and installing one is out of scope
for this fix. This script mirrors the ad-hoc-verification-script style
already used throughout this project's own local investigation/
implementation history.

Runs against the SAME local Postgres database configured in `.env`
(`DATABASE_URL`) — never production. Every row this script creates uses an
`outboxtest` email/recipient marker with a random suffix, and is deleted
again at the end of each scenario in a `finally` block (so a failed
assertion still cleans up). No real SMTP is ever contacted:
`send_email_detailed` is monkeypatched for the duration of each scenario
that needs a specific, deterministic success/failure outcome.

Run with:
    cd backend && ./.venv/Scripts/python.exe -m tests.test_email_outbox
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, delete

import app.core.email as email_module
from app.db.base import AsyncSessionLocal
from app.models.user import User, UserRole, UserRoleAssignment
from app.models.email_outbox import EmailOutbox, EmailOutboxStatus
from app.api.v1.endpoints import auth as auth_module
from app.core import email_worker
from app.core.config import settings

_MARKER = "outboxtest"
_DOMAIN = "avfu.ac.in"


def _fake_email(tag: str) -> str:
    return f"{_MARKER}.{tag}.{uuid.uuid4().hex[:8]}@{_DOMAIN}"


async def _cleanup_by_emails(emails: list[str]) -> None:
    if not emails:
        return
    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User).where(User.email.in_(emails)))).scalars().all()
        user_ids = [u.id for u in users]
        if user_ids:
            await db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(user_ids)))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.execute(delete(EmailOutbox).where(EmailOutbox.recipient.in_(emails)))
        await db.commit()


def _row(email: str, role=UserRole.FACULTY) -> dict:
    return {
        "first_name": "Outbox", "middle_name": None, "last_name": "Test",
        "email": email, "designation": "Assistant Professor", "role": role,
        "college_id": None, "department_id": None, "gender": None, "mobile": None,
    }


class _Results:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
        else:
            self.failed.append((name, detail))
        status = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail and not ok else ""
        print(f"{status}: {name}{suffix}")


RESULTS = _Results()


async def test_outbox_creation_same_transaction():
    name = "outbox creation: N users + N role assignments + N outbox rows, one transaction"
    emails = [_fake_email("create1"), _fake_email("create2"), _fake_email("create3")]
    try:
        async with AsyncSessionLocal() as db:
            created = await auth_module._create_bulk_users([_row(e) for e in emails], db)
        assert len(created) == 3
        async with AsyncSessionLocal() as db:
            users = (await db.execute(select(User).where(User.email.in_(emails)))).scalars().all()
            assignments = (await db.execute(
                select(UserRoleAssignment).where(UserRoleAssignment.user_id.in_([u.id for u in users]))
            )).scalars().all()
            outbox = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient.in_(emails)))).scalars().all()
        assert len(users) == 3, f"expected 3 users, got {len(users)}"
        assert len(assignments) == 3, f"expected 3 role assignments, got {len(assignments)}"
        assert len(outbox) == 3, f"expected 3 outbox rows, got {len(outbox)}"
        assert all(o.status == EmailOutboxStatus.PENDING for o in outbox)
        assert all(o.attempts == 0 for o in outbox)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails(emails)


async def test_transaction_rollback_on_conflict():
    name = "transaction rollback: duplicate-email conflict leaves 0 EXTRA users/assignments/outbox rows"
    email = _fake_email("dup")
    try:
        async with AsyncSessionLocal() as db:
            await auth_module._create_bulk_users([_row(email)], db)
        raised = False
        async with AsyncSessionLocal() as db:
            try:
                await auth_module._create_bulk_users([_row(email)], db)
            except Exception:
                raised = True
        assert raised, "expected the second (duplicate-email) call to raise"
        async with AsyncSessionLocal() as db:
            users = (await db.execute(select(User).where(User.email == email))).scalars().all()
            outbox = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient == email))).scalars().all()
        assert len(users) == 1, f"expected exactly 1 user (from the first call only), got {len(users)}"
        assert len(outbox) == 1, f"expected exactly 1 outbox row (from the first call only), got {len(outbox)}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails([email])


async def test_api_response_reports_queued_not_sent():
    name = "bulk-create path never calls SMTP; every row is PENDING immediately after commit"
    emails = [_fake_email("apiqueue1"), _fake_email("apiqueue2")]
    original = email_module.send_email_detailed
    called = {"count": 0}

    def spy(*_a, **_k):
        called["count"] += 1
        return True, None

    email_module.send_email_detailed = spy
    try:
        async with AsyncSessionLocal() as db:
            created = await auth_module._create_bulk_users([_row(e) for e in emails], db)
        assert len(created) == 2
        assert called["count"] == 0, "SMTP must NOT be invoked during bulk user creation (that was the timeout bug)"
        async with AsyncSessionLocal() as db:
            outbox = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient.in_(emails)))).scalars().all()
        assert all(o.status == EmailOutboxStatus.PENDING for o in outbox)
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        email_module.send_email_detailed = original
        await _cleanup_by_emails(emails)


async def test_worker_success():
    name = "worker: PENDING -> PROCESSING -> SENT on successful send"
    email = _fake_email("success")
    original = email_worker.send_email_detailed
    email_worker.send_email_detailed = lambda *a, **k: (True, None)
    try:
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(recipient=email, subject="Test", body="Test body")
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id
        async with AsyncSessionLocal() as db:
            claimed = await email_worker._claim_batch(db)
        assert job_id in {r.id for r in claimed}, "expected our job to be claimed"
        claimed_attempts = next(r.attempts for r in claimed if r.id == job_id)
        await email_worker._process_job(job_id, claimed_attempts)
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.SENT, f"expected SENT, got {row.status}"
        assert row.sent_at is not None
        assert row.attempts == 1
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        email_worker.send_email_detailed = original
        await _cleanup_by_emails([email])


async def test_worker_transient_failure_retry():
    name = "worker: PENDING -> PROCESSING -> PENDING retry on failure (attempts+=1, last_error, backoff set)"
    email = _fake_email("retry")
    original = email_worker.send_email_detailed
    email_worker.send_email_detailed = lambda *a, **k: (False, "SMTPConnectError: simulated transient failure")
    try:
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(recipient=email, subject="Test", body="Test body")
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id
        async with AsyncSessionLocal() as db:
            claimed = await email_worker._claim_batch(db)
        assert job_id in {r.id for r in claimed}
        claimed_attempts = next(r.attempts for r in claimed if r.id == job_id)
        await email_worker._process_job(job_id, claimed_attempts)
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.PENDING, f"expected PENDING (retry), got {row.status}"
        assert row.attempts == 1
        assert row.last_error and "simulated transient failure" in row.last_error
        assert row.next_attempt_at > datetime.now(timezone.utc), "expected a future retry time (backoff)"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        email_worker.send_email_detailed = original
        await _cleanup_by_emails([email])


async def test_worker_permanent_failure_after_max_attempts():
    name = "worker: FAILED after EMAIL_WORKER_MAX_ATTEMPTS consecutive failures"
    email = _fake_email("permfail")
    original = email_worker.send_email_detailed
    email_worker.send_email_detailed = lambda *a, **k: (False, "SMTPAuthError: simulated permanent failure")
    try:
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(recipient=email, subject="Test", body="Test body")
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id

        for i in range(settings.EMAIL_WORKER_MAX_ATTEMPTS):
            async with AsyncSessionLocal() as db:
                row = await db.get(EmailOutbox, job_id)
                row.next_attempt_at = datetime.now(timezone.utc)  # skip backoff wait for the test
                await db.commit()
            async with AsyncSessionLocal() as db:
                claimed = await email_worker._claim_batch(db)
            assert job_id in {r.id for r in claimed}, f"expected job claimable on attempt {i + 1}"
            claimed_attempts = next(r.attempts for r in claimed if r.id == job_id)
            await email_worker._process_job(job_id, claimed_attempts)

        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.FAILED, f"expected FAILED, got {row.status}"
        assert row.attempts == settings.EMAIL_WORKER_MAX_ATTEMPTS
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        email_worker.send_email_detailed = original
        await _cleanup_by_emails([email])


async def test_stale_processing_recovery():
    name = "worker restart: stale PROCESSING job (expired lease) is recovered back to PENDING"
    email = _fake_email("stale")
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(
                recipient=email, subject="Test", body="Test body",
                status=EmailOutboxStatus.PROCESSING, attempts=1, next_attempt_at=past,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id

        async with AsyncSessionLocal() as db:
            recovered = await email_worker._recover_stale_jobs(db)
        assert recovered >= 1

        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.PENDING, f"expected recovered row back to PENDING, got {row.status}"
        assert row.last_error and "stale" in row.last_error.lower()
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails([email])


async def test_stale_processing_exhausted_marks_failed():
    name = "worker restart: stale PROCESSING job with attempts already exhausted is marked FAILED, never left stuck"
    email = _fake_email("staleexhausted")
    try:
        past = datetime.now(timezone.utc) - timedelta(seconds=5)
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(
                recipient=email, subject="Test", body="Test body",
                status=EmailOutboxStatus.PROCESSING, attempts=settings.EMAIL_WORKER_MAX_ATTEMPTS,
                next_attempt_at=past,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id

        async with AsyncSessionLocal() as db:
            await email_worker._recover_stale_jobs(db)

        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.FAILED, f"expected FAILED, got {row.status}"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails([email])


async def test_concurrent_workers_no_double_claim():
    name = "concurrent workers: two simultaneous claims never double-claim the same row (FOR UPDATE SKIP LOCKED)"
    emails = [_fake_email(f"concurrent{i}") for i in range(4)]
    try:
        async with AsyncSessionLocal() as db:
            for e in emails:
                db.add(EmailOutbox(recipient=e, subject="Test", body="Test body"))
            await db.commit()

        async def claim():
            async with AsyncSessionLocal() as db:
                return await email_worker._claim_batch(db)

        batch_a, batch_b = await asyncio.gather(claim(), claim())
        all_claimed_ids = [r.id for r in batch_a] + [r.id for r in batch_b]
        assert len(all_claimed_ids) == len(set(all_claimed_ids)), "a row was claimed by both concurrent workers"

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(EmailOutbox).where(EmailOutbox.recipient.in_(emails)))).scalars().all()
        assert len(rows) == 4
        assert all(r.status == EmailOutboxStatus.PROCESSING for r in rows), "expected every test row claimed exactly once"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails(emails)


async def test_one_failure_does_not_stop_batch():
    name = "one failing email in a batch does not stop the others from being processed"
    e1, e2, e3 = _fake_email("batch1"), _fake_email("batch2"), _fake_email("batch3")
    original = email_worker.send_email_detailed

    def fake_send(to, _subject, _body):
        if to == e2:
            return False, "simulated failure for email #2 only"
        return True, None

    email_worker.send_email_detailed = fake_send
    try:
        async with AsyncSessionLocal() as db:
            for e in (e1, e2, e3):
                db.add(EmailOutbox(recipient=e, subject="Test", body="Test body"))
            await db.commit()

        async with AsyncSessionLocal() as db:
            claimed = await email_worker._claim_batch(db)
        assert len(claimed) == 3
        for row in claimed:
            await email_worker._process_job(row.id, row.attempts)

        async with AsyncSessionLocal() as db:
            rows = {r.recipient: r for r in (await db.execute(
                select(EmailOutbox).where(EmailOutbox.recipient.in_([e1, e2, e3]))
            )).scalars().all()}
        assert rows[e1].status == EmailOutboxStatus.SENT, "email #1 should be SENT"
        assert rows[e2].status == EmailOutboxStatus.PENDING, "email #2 should be scheduled for retry, not stuck"
        assert rows[e3].status == EmailOutboxStatus.SENT, "email #3 should still be processed and SENT"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        email_worker.send_email_detailed = original
        await _cleanup_by_emails([e1, e2, e3])


async def test_fencing_prevents_stale_worker_overwrite():
    name = "fencing: a stale worker's late finalize cannot overwrite a job reclaimed and finished by another worker"
    email = _fake_email("fencing")
    try:
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(recipient=email, subject="Test", body="Test body")
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id

        # Worker A claims the job (attempts -> 1) but — in this simulation —
        # never gets to finalize before its lease expires (e.g. it is stuck
        # deep inside a slow SMTP call; see the review's duplicate-email
        # analysis for why the lease can legitimately expire mid-send).
        async with AsyncSessionLocal() as db:
            claimed_a = await email_worker._claim_batch(db)
        assert job_id in {r.id for r in claimed_a}
        claimed_attempts_a = next(r.attempts for r in claimed_a if r.id == job_id)
        assert claimed_attempts_a == 1

        # Force Worker A's lease into the past, simulating EMAIL_WORKER_
        # PROCESSING_TIMEOUT having elapsed while it is still "in" SMTP.
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        # Stale-job recovery (run by any worker's poll cycle) reclaims it
        # back to PENDING — attempts is untouched by recovery itself.
        async with AsyncSessionLocal() as db:
            recovered = await email_worker._recover_stale_jobs(db)
        assert recovered >= 1
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.PENDING
        assert row.attempts == claimed_attempts_a

        # Worker B claims (attempts -> 2) and successfully finalizes it.
        async with AsyncSessionLocal() as db:
            claimed_b = await email_worker._claim_batch(db)
        assert job_id in {r.id for r in claimed_b}
        claimed_attempts_b = next(r.attempts for r in claimed_b if r.id == job_id)
        assert claimed_attempts_b == 2

        owned_b = await email_worker._finalize_job(
            job_id, claimed_attempts_b,
            {"status": EmailOutboxStatus.SENT, "sent_at": datetime.now(timezone.utc), "last_error": None},
        )
        assert owned_b is True, "Worker B should still own the job at this point and finalize successfully"

        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.SENT
        assert row.attempts == 2

        # THE RACE: Worker A, unaware any of the above happened, now finally
        # finishes its own (stale) SMTP attempt and tries to finalize using
        # its ORIGINAL claimed attempt number (1) — exactly the scenario the
        # production-readiness review flagged as the critical, previously-
        # unguarded race. Against the OLD (unfenced) implementation this
        # would have blindly overwritten Worker B's SENT state; the fix
        # under test must reject it instead.
        owned_a = await email_worker._finalize_job(
            job_id, claimed_attempts_a,
            {"status": EmailOutboxStatus.FAILED, "last_error": "stale worker A's late (and wrong) result"},
        )
        assert owned_a is False, "Worker A must detect it no longer owns the job (fencing must reject this write)"

        # The database must still reflect EXACTLY Worker B's outcome.
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.SENT, f"Worker A must not have overwritten Worker B's SENT, got {row.status}"
        assert row.attempts == 2, "attempts must remain exactly what Worker B set"
        assert row.last_error is None, "Worker A's stale last_error must never have been written"
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails([email])


async def test_at_least_once_crash_before_commit():
    name = "at-least-once (documented, not a defect): SMTP succeeds but crash-before-commit means the job is later resent"
    email = _fake_email("crashbeforecommit")
    try:
        async with AsyncSessionLocal() as db:
            row = EmailOutbox(recipient=email, subject="Test", body="Test body")
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job_id = row.id

        async with AsyncSessionLocal() as db:
            claimed = await email_worker._claim_batch(db)
        assert job_id in {r.id for r in claimed}
        claimed_attempts = next(r.attempts for r in claimed if r.id == job_id)

        # Simulate: the worker's SMTP call to `send_email_detailed` genuinely
        # SUCCEEDS, but the process is killed (crash, OOM, deploy restart)
        # before `_finalize_job`'s UPDATE...SENT can be committed. We
        # deliberately never call `_finalize_job`/`_process_job` here — that
        # omission IS the simulated crash. The row is left exactly as
        # `_claim_batch` left it: PROCESSING, with its lease already set.
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.PROCESSING, "row must still show PROCESSING immediately after the simulated crash"

        # Time passes; the lease (EMAIL_WORKER_PROCESSING_TIMEOUT) expires —
        # simulated here by forcing next_attempt_at into the past.
        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
            row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        async with AsyncSessionLocal() as db:
            recovered = await email_worker._recover_stale_jobs(db)
        assert recovered >= 1

        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.PENDING, "job must become eligible again, never stuck in PROCESSING"
        assert row.attempts == claimed_attempts, "recovery itself must not change attempts"

        # The job is not lost: it gets reclaimed and (in this simulation)
        # successfully sent again by a subsequent attempt.
        original = email_worker.send_email_detailed
        email_worker.send_email_detailed = lambda *a, **k: (True, None)
        try:
            async with AsyncSessionLocal() as db:
                reclaimed = await email_worker._claim_batch(db)
            assert job_id in {r.id for r in reclaimed}
            new_claimed_attempts = next(r.attempts for r in reclaimed if r.id == job_id)
            await email_worker._process_job(job_id, new_claimed_attempts)
        finally:
            email_worker.send_email_detailed = original

        async with AsyncSessionLocal() as db:
            row = await db.get(EmailOutbox, job_id)
        assert row.status == EmailOutboxStatus.SENT

        # DOCUMENTED LIMITATION, not a bug: if the ORIGINAL (crashed)
        # worker's SMTP call had genuinely reached the SMTP server before
        # the crash, the recipient would receive TWO copies of this email
        # once the recovered attempt above also succeeds. This architecture
        # provides AT-LEAST-ONCE delivery, not exactly-once — an accepted
        # trade-off for a low-value credential email (see BUSINESS_LOGIC.md
        # T.7.1). No amount of DB-side fencing can close this window,
        # because there is no atomic way to make "send the SMTP email" and
        # "commit the SENT row" a single transaction across two systems.
        RESULTS.record(name, True)
    except Exception as e:
        RESULTS.record(name, False, str(e))
    finally:
        await _cleanup_by_emails([email])


async def main() -> None:
    scenarios = [
        test_outbox_creation_same_transaction,
        test_transaction_rollback_on_conflict,
        test_api_response_reports_queued_not_sent,
        test_worker_success,
        test_worker_transient_failure_retry,
        test_worker_permanent_failure_after_max_attempts,
        test_stale_processing_recovery,
        test_stale_processing_exhausted_marks_failed,
        test_concurrent_workers_no_double_claim,
        test_one_failure_does_not_stop_batch,
        test_fencing_prevents_stale_worker_overwrite,
        test_at_least_once_crash_before_commit,
    ]
    for scenario in scenarios:
        await scenario()
    print(f"\n{len(RESULTS.passed)} passed, {len(RESULTS.failed)} failed")
    if RESULTS.failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
