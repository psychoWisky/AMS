"""Shared SMTP email helper.

Extracted from grading.py's previously-private `_send_notification_email`
(Section 28.4, STUDENT_SIDE_IMPLEMENTATION_PLAN.md) now that a second call
site (Orientation credential delivery) needs the same logic. grading.py's
own function now delegates here rather than duplicating the smtplib code.

Bulk Upload SMTP timeout fix (this revision) — `send_email()` itself is
UNCHANGED (still a synchronous, blocking SMTP call) and is still exactly
right for the low-volume, interactive/time-sensitive flows that keep using
it directly (OTP delivery, single-candidate Orientation credential email —
see `app.api.v1.endpoints.auth`'s and other modules' own docstrings for why
each specific call site was or wasn't migrated). What changed is that the
two call sites that used to run this synchronously in a loop inside a
request (bulk user creation, and `create_faculty`) now call `enqueue_email`
instead, which durably records the email as a `EmailOutbox` row in the
caller's own transaction; the actual SMTP send later happens out-of-request
in the standalone `app.core.email_worker` process, which calls
`send_email_detailed` below (the same SMTP logic, just also returning the
failure reason so it can be recorded for retry/observability).
"""
import smtplib
from email.mime.text import MIMEText
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings


def _send_email_inner(to: str, subject: str, body: str) -> tuple[bool, Optional[str]]:
    if not settings.SMTP_USER:
        return False, "SMTP not configured (SMTP_USER is unset)."
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as s:
            s.starttls()
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.sendmail(settings.SMTP_FROM, [to], msg.as_string())
        return True, None
    except Exception as e:
        # Never include SMTP credentials/secrets — `str(e)` for smtplib's own
        # exceptions never echoes the password back, only server responses
        # (e.g. "(535, b'Authentication failed')") or connection errors.
        return False, f"{type(e).__name__}: {e}"


def send_email(to: str, subject: str, body: str) -> bool:
    """Best-effort SMTP send. Returns True on success, False on any failure
    (including SMTP not configured) so callers can distinguish and react —
    unlike the previous grading.py behavior of silently swallowing failures.
    Still used directly by every flow that intentionally stays synchronous
    (see this module's docstring) — unchanged behavior/signature."""
    sent, _error = _send_email_inner(to, subject, body)
    return sent


def send_email_detailed(to: str, subject: str, body: str) -> tuple[bool, Optional[str]]:
    """Same SMTP send as `send_email`, but also returns the failure reason
    (or None on success) — used only by `app.core.email_worker` so a failed
    delivery attempt can be recorded in `EmailOutbox.last_error` for retry/
    observability. Never used from a request path."""
    return _send_email_inner(to, subject, body)


def enqueue_email(db: AsyncSession, to: str, subject: str, body: str) -> None:
    """Durable, non-blocking alternative to `send_email()` for request-path
    credential emails (Bulk Upload SMTP timeout fix). Adds a PENDING
    `EmailOutbox` row to the CALLER's own session/transaction — it commits
    (or rolls back) atomically with whatever business rows the caller is
    also writing in that same transaction, so a queued email can never exist
    without its corresponding user, and vice versa. Does not send anything
    itself; actual delivery happens later in `app.core.email_worker`."""
    from app.models.email_outbox import EmailOutbox  # local import: avoids a
    # models -> core -> models import cycle at module load time.
    db.add(EmailOutbox(recipient=to, subject=subject, body=body))
