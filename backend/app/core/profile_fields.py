"""Shared validation for user/student PROFILE fields edited by Super Admin
(User Management and Students management). One definition, used by both
request schemas, so the two edit forms can never drift apart.

Blank strings are treated as "not provided" (None) — the stored data already
contains empty-string mobiles from earlier bulk uploads, and a cleared form
input must clear the value rather than store "".
"""
import re
from datetime import date
from typing import Optional

GENDERS = ("Male", "Female", "Other")
BLOOD_GROUPS = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")
TITLES = ("Dr.", "Mr", "Mrs", "Miss")

_PHONE_RE = re.compile(r"^[0-9+\-\s()]{7,20}$")


def blank_to_none(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def is_blank(value) -> bool:
    return isinstance(value, str) and not value.strip()


def canonical_choice(value: Optional[str], choices: tuple, label: str) -> Optional[str]:
    """Case-insensitive match to the canonical spelling; blank -> None."""
    value = blank_to_none(value)
    if value is None:
        return None
    for c in choices:
        if c.lower() == value.lower():
            return c
    raise ValueError(f"{label} must be one of: {', '.join(choices)}.")


def check_mobile(value: Optional[str]) -> Optional[str]:
    value = blank_to_none(value)
    if value is None:
        return None
    if not _PHONE_RE.match(value):
        raise ValueError("Enter a valid mobile number (7-20 digits; spaces, +, - and brackets allowed).")
    return value


def check_date_of_birth(value: Optional[date]) -> Optional[date]:
    if value is None:
        return None
    if value > date.today():
        raise ValueError("Date of birth cannot be in the future.")
    if value.year < 1900:
        raise ValueError("Date of birth is not plausible.")
    return value


def check_max_length(value: Optional[str], limit: int, label: str) -> Optional[str]:
    if value is not None and len(value) > limit:
        raise ValueError(f"{label} must be at most {limit} characters.")
    return value
