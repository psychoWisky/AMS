"""Orientation / Student Intake (STUDENT_SIDE_IMPLEMENTATION_PLAN.md Section 28).

Incharge Academic Cell -> Orientation -> Present/Absent -> Select ->
roll number -> student account -> credential email -> student login.

RBAC NOTE: the confirmed business role "Incharge Academic Cell" does not exist
in the current UserRole enum (Section 11/28.19 of the plan). Per that section's
documented demo mitigation, this module is temporarily gated to
SUPER_ADMIN/ACADEMIC_ADMIN. This is an explicit, isolated, documented
substitution — not a silent permanent mapping — and should be replaced with a
real Incharge Academic Cell role check the moment that role is added (Phase 0).
"""
import re
import unicodedata
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.core.security import hash_password, generate_temp_password
from app.core.email import send_email
from app.models.user import User, UserRole, Program
from app.models.orientation import OrientationCandidate

router = APIRouter(prefix="/orientation", tags=["Orientation"])

# Student university email domain (AVFU-confirmed production format,
# firstname.lastname@avfu.ac.in — see BUSINESS_LOGIC.md Section 28 investigation
# on student email generation). Deliberately a LOCAL constant, independent of
# settings.ORIENTATION_EMAIL_DOMAIN (kept unchanged for now) and independent of
# auth.py's faculty-only _AVFU_STAFF_EMAIL_DOMAIN — student and faculty email
# generation are separate concerns that happen to share the same domain today.
_STUDENT_EMAIL_DOMAIN = "avfu.ac.in"

_NON_ALPHA_RE = re.compile(r"[^a-z]")


def _ascii_alpha(token: str) -> str:
    """Lowercase, strip diacritics (NFKD decompose + drop combining marks),
    then keep only ASCII letters. 'José' -> 'jose', "O'Brien" -> 'obrien'."""
    decomposed = unicodedata.normalize("NFKD", token)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return _NON_ALPHA_RE.sub("", ascii_only.lower())


def _student_email_local_part(candidate_name: str) -> str:
    """Deterministic firstname.lastname local-part rule (BUSINESS_LOGIC.md
    Section 28 investigation, Section 3/4/5 of the implementation task):
    trim/collapse whitespace, split on whitespace, first token = first name,
    last token = last name (middle tokens ignored for the email), each
    normalized to ASCII-letters-only. Single usable token -> first-name-only
    fallback, never a trailing/malformed dot. Raises ValueError if no
    alphabetic characters survive normalization at all (caller turns this
    into a 400, per instruction not to invent a placeholder name)."""
    tokens = candidate_name.strip().split()
    if not tokens:
        raise ValueError("Candidate name has no usable characters for an email address.")

    first = _ascii_alpha(tokens[0])
    last = _ascii_alpha(tokens[-1]) if len(tokens) > 1 else ""

    if first and last:
        return f"{first}.{last}"
    if first:
        return first
    if last:
        return last
    raise ValueError("Candidate name has no usable characters for an email address.")

# Demo substitution for "Incharge Academic Cell" — see module docstring.
_INCHARGE_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)


class CandidateIn(BaseModel):
    name: str
    personal_email: EmailStr
    mobile: Optional[str] = None
    entrance_exam_name: Optional[str] = None
    entrance_exam_marks: Optional[float] = None
    academic_year: str
    program_id: UUID


def _candidate_dict(c: OrientationCandidate) -> dict:
    return {
        "id": str(c.id),
        "name": c.name,
        "personal_email": c.personal_email,
        "mobile": c.mobile,
        "entrance_exam_name": c.entrance_exam_name,
        "entrance_exam_marks": c.entrance_exam_marks,
        "academic_year": c.academic_year,
        "program_id": str(c.program_id),
        "program_name": c.program.name if c.program else None,
        "program_code": c.program.code if c.program else None,
        "attendance_status": c.attendance_status,
        "selection_status": c.selection_status,
        "credential_status": c.credential_status,
        "roll_no": c.roll_no,
        "created_at": c.created_at.isoformat(),
    }


@router.post("/candidates", status_code=201)
async def create_candidate(
    body: CandidateIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    program = await db.get(Program, body.program_id)
    if not program:
        raise HTTPException(404, "Programme not found.")

    existing = await db.execute(select(OrientationCandidate).where(
        OrientationCandidate.personal_email == body.personal_email.lower(),
        OrientationCandidate.academic_year == body.academic_year,
        OrientationCandidate.program_id == body.program_id,
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A candidate with this email is already registered for this academic year and programme.")

    c = OrientationCandidate(
        name=body.name, personal_email=body.personal_email.lower(), mobile=body.mobile,
        entrance_exam_name=body.entrance_exam_name, entrance_exam_marks=body.entrance_exam_marks,
        academic_year=body.academic_year, program_id=body.program_id, created_by=user.id,
    )
    db.add(c); await db.commit(); await db.refresh(c)
    return {"id": str(c.id), "message": "Candidate added."}


@router.get("/candidates")
async def list_candidates(
    academic_year: Optional[str] = None, program_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    q = select(OrientationCandidate).options(selectinload(OrientationCandidate.program))
    if academic_year: q = q.where(OrientationCandidate.academic_year == academic_year)
    if program_id: q = q.where(OrientationCandidate.program_id == program_id)
    result = await db.execute(q.order_by(OrientationCandidate.created_at))
    return [_candidate_dict(c) for c in result.scalars().all()]


@router.put("/candidates/{candidate_id}")
async def update_candidate(
    candidate_id: UUID, body: CandidateIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    c = await db.get(OrientationCandidate, candidate_id)
    if not c: raise HTTPException(404, "Candidate not found.")
    if c.selection_status != "pending":
        raise HTTPException(400, "Cannot edit a candidate that has already been selected or rejected.")
    program = await db.get(Program, body.program_id)
    if not program: raise HTTPException(404, "Programme not found.")
    c.name = body.name; c.personal_email = body.personal_email.lower(); c.mobile = body.mobile
    c.entrance_exam_name = body.entrance_exam_name; c.entrance_exam_marks = body.entrance_exam_marks
    c.academic_year = body.academic_year; c.program_id = body.program_id
    await db.commit()
    return {"message": "Candidate updated."}


@router.patch("/candidates/{candidate_id}/attendance")
async def mark_attendance(
    candidate_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    if status not in ("present", "absent"):
        raise HTTPException(400, "Status must be 'present' or 'absent'.")
    c = await db.get(OrientationCandidate, candidate_id)
    if not c: raise HTTPException(404, "Candidate not found.")
    if c.selection_status != "pending":
        raise HTTPException(400, "Cannot change attendance after a selection decision has been made.")
    c.attendance_status = status
    await db.commit()
    return {"message": f"Attendance marked {status}."}


async def _next_roll_no(academic_year: str, program_code: str, db: AsyncSession) -> str:
    prefix = f"{academic_year}-{program_code}-"
    result = await db.execute(
        select(func.count()).select_from(User).where(User.student_roll.like(f"{prefix}%"))
    )
    n = (result.scalar() or 0) + 1
    return f"{prefix}{n}"


@router.patch("/candidates/{candidate_id}/selection")
async def decide_selection(
    candidate_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    if status not in ("selected", "not_selected"):
        raise HTTPException(400, "Status must be 'selected' or 'not_selected'.")

    result = await db.execute(
        select(OrientationCandidate).options(selectinload(OrientationCandidate.program)).where(OrientationCandidate.id == candidate_id)
    )
    c = result.scalar_one_or_none()
    if not c: raise HTTPException(404, "Candidate not found.")
    if c.selection_status != "pending":
        raise HTTPException(409, "This candidate's selection has already been decided.")
    # Hard rule (Section 11/28.10): an absent candidate must never be selected,
    # never receive a roll number, and never receive credentials.
    if status == "selected" and c.attendance_status != "present":
        raise HTTPException(400, "Only a candidate marked Present can be selected.")

    if status == "not_selected":
        c.selection_status = "not_selected"
        await db.commit()
        return {"message": "Candidate marked as not selected."}

    # University email local-part is derived once, up front, from the
    # candidate's name (Section 3/4 of the student-email task) — a ValueError
    # here means the name has no usable alphabetic characters at all, which is
    # a genuine input problem, not something to paper over with a placeholder.
    try:
        email_local = _student_email_local_part(c.name)
    except ValueError:
        raise HTTPException(400, "Candidate name has no usable characters to generate a university email address.")

    # ── Selected: generate roll number + account, under a SAVEPOINT so a rare
    # concurrent-roll-number collision can be retried without discarding the
    # candidate/program rows already loaded in this session (Section 28.11 —
    # deliberately NOT a bare SELECT COUNT(*)-then-insert, which the project's
    # existing admission-number generator already demonstrates is unsafe).
    # The SAME savepoint/IntegrityError-retry pattern now also absorbs
    # firstname.lastname email collisions: attempt 1 uses the unsuffixed
    # local-part, each retry appends the next numeric suffix (2, 3, 4, ...)
    # before '@avfu.ac.in' — never after the domain, never reusing a value
    # the unique index still considers occupied. _next_roll_no itself is
    # unchanged; roll-number and email generation remain independent concerns
    # that simply happen to share this retry loop.
    program = c.program
    university_email = None
    temp_password = generate_temp_password()
    student = None
    _MAX_ACCOUNT_CREATION_ATTEMPTS = 20
    for attempt in range(_MAX_ACCOUNT_CREATION_ATTEMPTS):
        roll_no = await _next_roll_no(c.academic_year, program.code, db)
        suffix = attempt + 1  # 1 -> unsuffixed, 2 -> "…2", 3 -> "…3", ...
        local_part = email_local if suffix == 1 else f"{email_local}{suffix}"
        university_email = f"{local_part}@{_STUDENT_EMAIL_DOMAIN}"
        try:
            async with db.begin_nested():
                student = User(
                    email=university_email,
                    hashed_password=hash_password(temp_password),
                    first_name=c.name.split(" ")[0],
                    last_name=" ".join(c.name.split(" ")[1:]) or None,
                    mobile=c.mobile,
                    role=UserRole.STUDENT,
                    student_roll=roll_no,
                    program_id=c.program_id,
                    admission_year=int(c.academic_year) if c.academic_year.isdigit() else None,
                    is_active=True, is_verified=True,
                    must_change_password=True,
                )
                db.add(student)
                await db.flush()
            break
        except IntegrityError:
            student = None
            continue
    if not student:
        raise HTTPException(409, "Could not generate a unique student roll number/email after several attempts. Please try again.")

    c.selection_status = "selected"
    c.roll_no = student.student_roll
    c.student_user_id = student.id
    c.credential_status = "generated"
    await db.commit()

    sent = send_email(
        c.personal_email,
        "Your AVFU AMS Student Account",
        f"Dear {c.name},\n\nYou have been selected. Your AVFU AMS account has been created.\n\n"
        f"Login email: {university_email}\nTemporary password: {temp_password}\n\n"
        f"Please log in and change your password, then complete your student profile.\n\nAVFU Academic Cell",
    )
    c.credential_status = "sent" if sent else "failed"
    await db.commit()

    return {
        "message": "Candidate selected and account created." + ("" if sent else " Credential email could not be sent — use Resend."),
        "roll_no": c.roll_no,
        "university_email": university_email,
        "credential_status": c.credential_status,
    }


@router.post("/candidates/{candidate_id}/resend-credentials")
async def resend_credentials(
    candidate_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    c = await db.get(OrientationCandidate, candidate_id)
    if not c: raise HTTPException(404, "Candidate not found.")
    if c.selection_status != "selected" or not c.student_user_id:
        raise HTTPException(400, "Credentials can only be resent for a selected candidate with an existing account.")

    student = await db.get(User, c.student_user_id)
    if not student: raise HTTPException(404, "Linked student account not found.")

    # Regenerate rather than resend the original — the original plaintext
    # password was never stored (Section 28.12), only its hash.
    new_password = generate_temp_password()
    student.hashed_password = hash_password(new_password)
    student.must_change_password = True
    await db.commit()

    sent = send_email(
        c.personal_email,
        "Your AVFU AMS Student Account — Credentials Resent",
        f"Dear {c.name},\n\nYour AVFU AMS login credentials have been reset.\n\n"
        f"Login email: {student.email}\nTemporary password: {new_password}\n\nAVFU Academic Cell",
    )
    c.credential_status = "sent" if sent else "failed"
    await db.commit()
    return {"message": "Credentials resent." if sent else "Account updated, but the email could not be sent.", "credential_status": c.credential_status}
