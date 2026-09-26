"""Standalone Gradesheet 24-hour auto-forward sweeper.

Runs as its OWN OS process — never imported by `app.main` — mirroring
`app.core.email_worker`:

    python -m app.core.gradesheet_worker

Each cycle calls `gradesheet_flow.sweep_overdue`, which deems still-pending
other-instructor approvals approved once the server-side 24h deadline of an open
approval cycle has passed and advances the sheet to the HOD. The rule is ALSO
enforced lazily on every gradesheet read/act and in the approver inboxes, so this
worker is an optimisation (an overdue sheet reaches the HOD without anyone opening
it), not the only enforcement point. `sweep_overdue` row-locks each cycle and is
idempotent, so several worker instances (or the API concurrently) are safe.
"""
import asyncio
import logging
import signal

from app.core.config import settings
from app.core.gradesheet_flow import sweep_overdue
from app.db.base import AsyncSessionLocal

logger = logging.getLogger("gradesheet_worker")
_shutdown = asyncio.Event()


def _request_shutdown() -> None:
    _shutdown.set()


async def run_forever() -> None:
    logger.info("gradesheet worker started poll_interval=%ss", settings.GRADESHEET_WORKER_POLL_INTERVAL)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            pass  # Windows ProactorEventLoop: Ctrl+C still raises KeyboardInterrupt

    while not _shutdown.is_set():
        try:
            async with AsyncSessionLocal() as db:
                forwarded = await sweep_overdue(db)
            if forwarded:
                logger.info("gradesheet auto-forwarded %d overdue approval cycle(s) to HOD", forwarded)
        except Exception:
            logger.exception("gradesheet worker sweep failed; will retry next cycle")
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=settings.GRADESHEET_WORKER_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
    logger.info("gradesheet worker stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        logger.info("gradesheet worker stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
