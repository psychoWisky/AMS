"""Shared SMTP email helper.

Extracted from grading.py's previously-private `_send_notification_email`
(Section 28.4, STUDENT_SIDE_IMPLEMENTATION_PLAN.md) now that a second call
site (Orientation credential delivery) needs the same logic. grading.py's
own function now delegates here rather than duplicating the smtplib code.
"""
import smtplib
from email.mime.text import MIMEText
from app.core.config import settings


def send_email(to: str, subject: str, body: str) -> bool:
    """Best-effort SMTP send. Returns True on success, False on any failure
    (including SMTP not configured) so callers can distinguish and react —
    unlike the previous grading.py behavior of silently swallowing failures."""
    if not settings.SMTP_USER:
        return False
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as s:
            s.starttls()
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.sendmail(settings.SMTP_FROM, [to], msg.as_string())
        return True
    except Exception:
        return False
