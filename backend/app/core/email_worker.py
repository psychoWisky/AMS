"""Standalone email-outbox worker (Bulk Upload SMTP timeout fix).

Runs as its OWN OS process — deliberately NEVER imported or started by
`app.main` / the FastAPI app / Uvicorn. That separation is the entire point
of this fix: SMTP latency/hangs must never again be able to block the
single Uvicorn worker that serves API requests (that was the exact
mechanism behind the production 504 this change addresses — see
`app.models.email_outbox`'s module docstring for the full root-cause
writeup).

Run it with:

    python -m app.core.email_worker

Intended to run continuously under its own process supervisor (a separate
systemd service in production — not created by this change; see the
implementation report for what that unit will need). It:

  1. Recovers stale PROCESSING jobs (a previous worker instance claimed them
     and then crashed/restarted before finishing) back to PENDING (or
     FAILED, if they've already exhausted their attempt budget).
  2. Claims a batch of due PENDING jobs using `SELECT ... FOR UPDATE SKIP
     LOCKED`, so multiple worker instances can safely run concurrently
     without ever double-processing the same row.
  3. Sends each claimed job's email via `app.core.email.send_email_detailed`
     (the same SMTP logic `send_email` already used, synchronously — that
     part is intentionally NOT rewritten, only moved out of the request
     path), then finalizes the result via `_finalize_job`'s CONDITIONAL
     UPDATE (fenced on `status='PROCESSING' AND attempts=:claimed_attempts`
     — see its docstring), recording SENT / a retry / permanent FAILED per
     the outcome ONLY IF this worker still owns the job; otherwise the
     result is discarded rather than overwriting a job that was reclaimed
     by someone else after this worker's processing lease expired.

     Delivery is therefore AT-LEAST-ONCE, not exactly-once: if a worker's
     SMTP call genuinely succeeds but the process crashes before
     `_finalize_job` commits, the job is later recovered and resent by
     another attempt (see `docs/BUSINESS_LOGIC.md` T.7.1). This is an
     accepted trade-off for a low-value credential email, not a defect.

Exits cleanly on SIGINT/SIGTERM (Windows: signal handlers for these are
best-effort — see the try/except below — Ctrl+C still works via
KeyboardInterrupt during local development).
"""
import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update

from app.core.config import settings
from app.core.email import send_email_detailed
from app.db.base import AsyncSessionLocal
from app.models.email_outbox import EmailOutbox, EmailOutboxStatus

logger = logging.getLogger("app.core.email_worker")

_shutdown = asyncio.Event()

# Production-readiness review fix — `last_error` is `Text` (unbounded in
# Postgres), but an exotic/malformed exception could in principle produce an
# unusually large `str(e)`. Defensive cap only; normal smtplib/socket error
# strings are always far shorter than this.
_MAX_LAST_ERROR_LENGTH = 2000


def _request_shutdown(*_args) -> None:
    _shutdown.set()


def _backoff_seconds(attempts: int) -> int:
    """1st retry ~1min, doubling, capped at 30min. Simple and understandable
    per the task's explicit "do not over-engineer" instruction."""
    return min(60 * (2 ** max(attempts - 1, 0)), 1800)


def _truncate_error(error: Optional[str]) -> Optional[str]:
    if error is None or len(error) <= _MAX_LAST_ERROR_LENGTH:
        return error
    return error[:_MAX_LAST_ERROR_LENGTH] + "...(truncated)"


async def _recover_stale_jobs(db) -> int:
    """A PROCESSING job whose lease (`next_attempt_at`) has already passed
    means the worker that claimed it died (crash, `kill -9`, host reboot,
    Uvicorn/deploy restart of the worker process) before it could mark the
    job SENT/PENDING/FAILED. Recovered back to PENDING for another attempt,
    unless it has already used up its attempt budget, in which case it is
    marked FAILED directly — this is the only way a job could otherwise be
    left stuck in PROCESSING forever (see `email_outbox.py`'s docstring)."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(EmailOutbox)
        .where(EmailOutbox.status == EmailOutboxStatus.PROCESSING, EmailOutbox.next_attempt_at <= now)
        .with_for_update(skip_locked=True)
    )
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        if row.attempts < settings.EMAIL_WORKER_MAX_ATTEMPTS:
            row.status = EmailOutboxStatus.PENDING
            row.next_attempt_at = now
            row.last_error = "Recovered stale PROCESSING job (worker crash or restart before completion)."
            logger.warning("email_outbox stale job recovered id=%s attempts=%d", row.id, row.attempts)
        else:
            row.status = EmailOutboxStatus.FAILED
            row.last_error = "Max attempts exhausted after a crash during send."
            logger.error("email_outbox permanently failed (stale, max attempts) id=%s attempts=%d", row.id, row.attempts)
    if rows:
        await db.commit()
    else:
        await db.rollback()
    return len(rows)


async def _claim_batch(db) -> list[EmailOutbox]:
    """`FOR UPDATE SKIP LOCKED` is what makes this safe under concurrent
    worker instances: two workers racing this query will never lock (or
    return) the same row — the loser simply skips it and claims a different
    one, rather than blocking on the first worker's row lock."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(EmailOutbox)
        .where(
            EmailOutbox.status == EmailOutboxStatus.PENDING,
            EmailOutbox.next_attempt_at <= now,
            EmailOutbox.attempts < settings.EMAIL_WORKER_MAX_ATTEMPTS,
        )
        .order_by(EmailOutbox.next_attempt_at)
        .limit(settings.EMAIL_WORKER_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        row.status = EmailOutboxStatus.PROCESSING
        row.attempts += 1
        # Doubles as this claim's lease expiry (see email_outbox.py) — if
        # this process dies before finishing, _recover_stale_jobs reclaims
        # it once this time passes.
        row.next_attempt_at = now + timedelta(seconds=settings.EMAIL_WORKER_PROCESSING_TIMEOUT)
        logger.info("email_outbox job claimed id=%s recipient=%s attempt=%d", row.id, row.recipient, row.attempts)
    if rows:
        await db.commit()
    else:
        await db.rollback()
    return rows


async def _finalize_job(job_id, claimed_attempts: int, values: dict) -> bool:
    """Production-readiness review fix (ownership/fencing) — applies
    `values` to the job at `job_id` via a single CONDITIONAL UPDATE, never a
    blind write of a previously-loaded ORM object. The WHERE clause
    (`status='PROCESSING' AND attempts=:claimed_attempts`) requires the row
    to still be in EXACTLY the state this worker itself put it in when it
    claimed the job (see `_claim_batch`) — i.e. this worker must still be
    its current, sole owner.

    If the job's processing lease expired while this worker was still
    inside the (duration-unbounded, see review) SMTP call, `_recover_stale_
    jobs` may already have reclaimed it — possibly to PENDING, then
    re-claimed and even finalized again by a different worker/cycle,
    changing both `status` and `attempts`. In that case this UPDATE matches
    zero rows (`rowcount == 0`): this worker has LOST ownership, and the
    caller must discard its own SMTP outcome rather than overwrite
    whatever the current, more up-to-date state is. Returns True if this
    worker still owned the job (and the write was committed), False
    otherwise (nothing was written)."""
    async with AsyncSessionLocal() as db:
        stmt = (
            update(EmailOutbox)
            .where(
                EmailOutbox.id == job_id,
                EmailOutbox.status == EmailOutboxStatus.PROCESSING,
                EmailOutbox.attempts == claimed_attempts,
            )
            .values(**values)
        )
        result = await db.execute(stmt)
        owned = result.rowcount == 1
        if owned:
            await db.commit()
        else:
            await db.rollback()
        return owned


async def _process_job(job_id, claimed_attempts: int) -> None:
    """Each claimed job is sent and finalized OUTSIDE any open DB
    transaction during the SMTP call itself — one slow or hanging SMTP
    call must never hold a lock on, or delay finalizing, any other job's
    row, and one bad email must never stop the rest of the batch.

    `claimed_attempts` is the `attempts` value `_claim_batch` itself set
    when claiming this job — captured by the caller at claim time, not
    re-read here — and is passed through to `_finalize_job` as the
    ownership fence (see its docstring)."""
    async with AsyncSessionLocal() as db:
        row = await db.get(EmailOutbox, job_id)
        if row is None or row.status != EmailOutboxStatus.PROCESSING or row.attempts != claimed_attempts:
            return  # already handled by another worker instance/cycle — cheap early exit, not the safety mechanism itself
        recipient, subject, body = row.recipient, row.subject, row.body

    sent, error = send_email_detailed(recipient, subject, body)
    now = datetime.now(timezone.utc)

    if sent:
        values = {"status": EmailOutboxStatus.SENT, "sent_at": now, "last_error": None}
    elif claimed_attempts >= settings.EMAIL_WORKER_MAX_ATTEMPTS:
        values = {"status": EmailOutboxStatus.FAILED, "last_error": _truncate_error(error)}
    else:
        values = {
            "status": EmailOutboxStatus.PENDING,
            "last_error": _truncate_error(error),
            "next_attempt_at": now + timedelta(seconds=_backoff_seconds(claimed_attempts)),
        }

    owned = await _finalize_job(job_id, claimed_attempts, values)
    if not owned:
        # Never log recipient-adjacent secrets — only job id, recipient
        # address, and the claimed attempt number (safe metadata).
        logger.warning(
            "email_outbox finalize skipped: lost ownership (lease expired and job was "
            "reclaimed/finalized elsewhere) id=%s recipient=%s claimed_attempt=%d",
            job_id, recipient, claimed_attempts,
        )
        return

    if sent:
        logger.info("email_outbox sent id=%s recipient=%s attempt=%d", job_id, recipient, claimed_attempts)
    elif values["status"] == EmailOutboxStatus.FAILED:
        logger.error(
            "email_outbox permanently failed id=%s recipient=%s attempts=%d error=%s",
            job_id, recipient, claimed_attempts, error,
        )
    else:
        logger.warning(
            "email_outbox retry scheduled id=%s recipient=%s attempt=%d next_attempt_at=%s error=%s",
            job_id, recipient, claimed_attempts, values["next_attempt_at"].isoformat(), error,
        )


async def run_forever() -> None:
    logger.info(
        "email worker started poll_interval=%ss batch_size=%s max_attempts=%s processing_timeout=%ss",
        settings.EMAIL_WORKER_POLL_INTERVAL, settings.EMAIL_WORKER_BATCH_SIZE,
        settings.EMAIL_WORKER_MAX_ATTEMPTS, settings.EMAIL_WORKER_PROCESSING_TIMEOUT,
    )
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows' default ProactorEventLoop doesn't support
            # add_signal_handler for SIGTERM — local/dev only impact;
            # Ctrl+C still raises KeyboardInterrupt as usual.
            pass

    while not _shutdown.is_set():
        try:
            async with AsyncSessionLocal() as db:
                await _recover_stale_jobs(db)
            async with AsyncSessionLocal() as db:
                claimed = await _claim_batch(db)
            for row in claimed:
                if _shutdown.is_set():
                    break
                # row.attempts is the value _claim_batch itself just set —
                # this IS the ownership fence passed through to _finalize_job.
                await _process_job(row.id, row.attempts)
        except Exception:
            logger.exception("email worker poll cycle failed; will retry next cycle")

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=settings.EMAIL_WORKER_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass

    logger.info("email worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logger.info("email worker stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
