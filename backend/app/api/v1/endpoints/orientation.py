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
import csv
import io
from collections import Counter, defaultdict
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr, ValidationError, field_validator
import openpyxl

from app.db.base import get_db
from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.security import hash_password, generate_temp_password
from app.core.email import send_email
from app.models.user import User, UserRole, Program, Department, College
from app.models.orientation import OrientationCandidate
# Programme<->Department many-to-many redesign — shared pair validation.
from app.api.v1.endpoints.departments import validate_program_department_pair

router = APIRouter(prefix="/orientation", tags=["Orientation"])

# AVFU Email domain (this task's confirmed business requirement — the IT team
# issues this address for each shortlisted student ahead of Orientation, and
# it becomes the created student's AMS login). Deliberately a LOCAL constant,
# independent of settings.ORIENTATION_EMAIL_DOMAIN and auth.py's faculty-only
# _AVFU_STAFF_EMAIL_DOMAIN — mirrors the same domain string only because both
# happen to use the university's real domain, not because they share config.
#
# NOTE: this REPLACES the previous system-generated "firstname.lastname@..."
# derivation (_student_email_local_part, now removed) — AVFU Email is
# supplied externally now, never generated from the candidate's name.
_AVFU_EMAIL_DOMAIN = "avfu.ac.in"

# Demo substitution for "Incharge Academic Cell" — see module docstring.
_INCHARGE_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)
# Bulk upload (this task's confirmed requirement) — deliberately narrower than
# the general Orientation roles above: Section 4 restricts this specific
# capability to Super Admin only, independent of the frontend's own gating.
_BULK_UPLOAD_ROLES = (UserRole.SUPER_ADMIN,)


def _require_nonblank(v: str, field_label: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError(f"{field_label} is required.")
    return v


def _normalize_avfu_email(v: str) -> str:
    """The single source of truth for the AVFU-domain rule — used by both the
    manual Add Candidate Pydantic validator below and the bulk-upload row
    validator (via CandidateIn itself, see bulk section), so there is never a
    second, divergent implementation of this business rule."""
    if not v.lower().endswith("@" + _AVFU_EMAIL_DOMAIN):
        raise ValueError(f"AVFU Email must be an @{_AVFU_EMAIL_DOMAIN} address.")
    return v.lower()


class CandidateIn(BaseModel):
    first_name: str
    middle_name: Optional[str] = None
    last_name: str
    personal_email: EmailStr
    mobile: str
    # AVFU Email (this task's confirmed requirement) — distinct from
    # personal_email; becomes the created student's AMS login. Required for
    # every new/updated candidate; validated as an AVFU address below.
    avfu_email: EmailStr
    academic_year: str
    # College (this task's confirmed requirement) — reuses the existing
    # ams_colleges master-data table (app.models.user.College), not a new
    # concept.
    college_id: UUID
    program_id: UUID
    # Programme<->Department many-to-many redesign — required for new/updated
    # candidates going forward (a student's academic identity is Programme +
    # Department together now), validated against ams_program_departments
    # below. The underlying column stays nullable at the DB level only to
    # accommodate the two pre-existing candidate rows from before this field
    # existed — never guessed/backfilled for those rows (see migration
    # 0009_program_department_m2m).
    department_id: UUID
    entrance_exam_name: Optional[str] = None
    entrance_exam_marks: Optional[float] = None

    @field_validator("first_name")
    @classmethod
    def _first_name_required(cls, v: str) -> str:
        return _require_nonblank(v, "First name")

    @field_validator("last_name")
    @classmethod
    def _last_name_required(cls, v: str) -> str:
        return _require_nonblank(v, "Last name")

    @field_validator("mobile")
    @classmethod
    def _mobile_required(cls, v: str) -> str:
        return _require_nonblank(v, "Mobile")

    @field_validator("avfu_email")
    @classmethod
    def _avfu_email_must_be_avfu_domain(cls, v: str) -> str:
        return _normalize_avfu_email(v)


def _candidate_full_name(c: OrientationCandidate) -> str:
    """Composite display/greeting name — prefers the new structured fields;
    falls back to the legacy single `name` column for candidates created
    before this task (never fabricated, never guessed)."""
    if c.first_name or c.last_name:
        parts = [c.first_name, c.middle_name, c.last_name]
        return " ".join(p for p in parts if p)
    return c.name or ""


def _candidate_dict(c: OrientationCandidate) -> dict:
    return {
        "id": str(c.id),
        "name": _candidate_full_name(c),
        "first_name": c.first_name,
        "middle_name": c.middle_name,
        "last_name": c.last_name,
        "personal_email": c.personal_email,
        "mobile": c.mobile,
        "avfu_email": c.avfu_email,
        "entrance_exam_name": c.entrance_exam_name,
        "entrance_exam_marks": c.entrance_exam_marks,
        "academic_year": c.academic_year,
        "college_id": str(c.college_id) if c.college_id else None,
        "college_name": c.college.name if c.college else None,
        "program_id": str(c.program_id),
        "program_name": c.program.name if c.program else None,
        "program_code": c.program.code if c.program else None,
        "department_id": str(c.department_id) if c.department_id else None,
        "department_name": c.department.name if c.department else None,
        "attendance_status": c.attendance_status,
        "selection_status": c.selection_status,
        "credential_status": c.credential_status,
        "roll_no": c.roll_no,
        "created_at": c.created_at.isoformat(),
    }


async def _validate_college_and_avfu_email(
    body: "CandidateIn", db: AsyncSession, exclude_candidate_id: Optional[UUID] = None,
) -> None:
    """Shared create/update validation for the two fields this task adds
    beyond Programme/Department: College must exist (existing ams_colleges
    table — no new College concept); AVFU Email must not already belong to
    another candidate or an existing AMS user (it is about to become one).
    The backend never trusts frontend filtering for either check."""
    if not await db.get(College, body.college_id):
        raise HTTPException(404, "College not found.")

    dup_candidate_q = select(OrientationCandidate.id).where(OrientationCandidate.avfu_email == body.avfu_email)
    if exclude_candidate_id:
        dup_candidate_q = dup_candidate_q.where(OrientationCandidate.id != exclude_candidate_id)
    if (await db.execute(dup_candidate_q)).scalar_one_or_none():
        raise HTTPException(409, "This AVFU Email is already used by another candidate.")

    existing_user = await db.execute(select(User.id).where(User.email == body.avfu_email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(409, "This AVFU Email is already registered to an existing AMS user.")


@router.post("/candidates", status_code=201)
async def create_candidate(
    body: CandidateIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    if not await db.get(Program, body.program_id):
        raise HTTPException(404, "Programme not found.")
    if not await db.get(Department, body.department_id):
        raise HTTPException(404, "Department not found.")
    await validate_program_department_pair(body.program_id, body.department_id, db)
    await _validate_college_and_avfu_email(body, db)

    existing = await db.execute(select(OrientationCandidate).where(
        OrientationCandidate.personal_email == body.personal_email.lower(),
        OrientationCandidate.academic_year == body.academic_year,
        OrientationCandidate.program_id == body.program_id,
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "A candidate with this email is already registered for this academic year and programme.")

    c = OrientationCandidate(
        first_name=body.first_name, middle_name=body.middle_name, last_name=body.last_name,
        personal_email=body.personal_email.lower(), mobile=body.mobile, avfu_email=body.avfu_email,
        entrance_exam_name=body.entrance_exam_name, entrance_exam_marks=body.entrance_exam_marks,
        academic_year=body.academic_year, college_id=body.college_id,
        program_id=body.program_id, department_id=body.department_id,
        created_by=user.id,
    )
    db.add(c)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This AVFU Email is already used by another candidate.")
    await db.refresh(c)
    return {"id": str(c.id), "message": "Candidate added."}


@router.get("/candidates")
async def list_candidates(
    academic_year: Optional[str] = None, program_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_INCHARGE_ROLES)),
):
    q = select(OrientationCandidate).options(
        selectinload(OrientationCandidate.program),
        selectinload(OrientationCandidate.department),
        selectinload(OrientationCandidate.college),
    )
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
    if not await db.get(Program, body.program_id):
        raise HTTPException(404, "Programme not found.")
    if not await db.get(Department, body.department_id):
        raise HTTPException(404, "Department not found.")
    await validate_program_department_pair(body.program_id, body.department_id, db)
    await _validate_college_and_avfu_email(body, db, exclude_candidate_id=candidate_id)

    c.first_name = body.first_name; c.middle_name = body.middle_name; c.last_name = body.last_name
    c.personal_email = body.personal_email.lower(); c.mobile = body.mobile; c.avfu_email = body.avfu_email
    c.entrance_exam_name = body.entrance_exam_name; c.entrance_exam_marks = body.entrance_exam_marks
    c.academic_year = body.academic_year; c.college_id = body.college_id
    c.program_id = body.program_id; c.department_id = body.department_id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This AVFU Email is already used by another candidate.")
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

    # AVFU Email is now externally supplied (IT team) and stored on the
    # candidate at create/update time — it is what becomes the student's AMS
    # login, NEVER derived from the name anymore (the previous
    # firstname.lastname@... generator is removed). A candidate created
    # before this task's fields existed has no avfu_email/first_name/
    # last_name/mobile/college_id on file and cannot be selected until an
    # admin edits it in via PUT /candidates/{id} — never fabricated here.
    missing = [
        label for value, label in [
            (c.avfu_email, "AVFU Email"), (c.first_name, "First Name"), (c.last_name, "Last Name"),
            (c.mobile, "Mobile"), (c.college_id, "College"), (c.department_id, "Department"),
        ] if not value
    ]
    if missing:
        raise HTTPException(
            400,
            f"This candidate is missing required information before selection: {', '.join(missing)}. "
            "Please edit the candidate to add it first.",
        )
    # Re-check the AVFU email isn't already taken by an existing user — race
    # safety on top of the same check already run at create/update time.
    existing_user = await db.execute(select(User.id).where(User.email == c.avfu_email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(409, "This candidate's AVFU Email is already registered to an existing AMS user.")

    # ── Selected: generate roll number + account, under a SAVEPOINT so a rare
    # concurrent roll-number collision can be retried without discarding the
    # candidate/program rows already loaded in this session (Section 28.11 —
    # deliberately NOT a bare SELECT COUNT(*)-then-insert, which the project's
    # existing admission-number generator already demonstrates is unsafe).
    # AVFU Email is fixed, externally-supplied input now — unlike the old
    # system-generated email, it is never mutated/suffixed on retry; only the
    # roll number is retried.
    program = c.program
    temp_password = generate_temp_password()
    student = None
    _MAX_ROLL_NO_ATTEMPTS = 20
    for _ in range(_MAX_ROLL_NO_ATTEMPTS):
        roll_no = await _next_roll_no(c.academic_year, program.code, db)
        try:
            async with db.begin_nested():
                student = User(
                    email=c.avfu_email,
                    hashed_password=hash_password(temp_password),
                    first_name=c.first_name, middle_name=c.middle_name, last_name=c.last_name,
                    mobile=c.mobile,
                    role=UserRole.STUDENT,
                    student_roll=roll_no,
                    program_id=c.program_id,
                    # Programme<->Department many-to-many redesign — both
                    # fields copied directly onto the new student; department
                    # is NEVER inferred from the Programme (a Programme can
                    # now have several Departments).
                    department_id=c.department_id,
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
        raise HTTPException(409, "Could not generate a unique student roll number after several attempts. Please try again.")

    c.selection_status = "selected"
    c.roll_no = student.student_roll
    c.student_user_id = student.id
    c.credential_status = "generated"
    await db.commit()

    sent = send_email(
        c.personal_email,
        "Your AVFU AMS Student Account",
        f"Dear {_candidate_full_name(c)},\n\nYou have been selected. Your AVFU AMS account has been created.\n\n"
        f"Login email: {c.avfu_email}\nTemporary password: {temp_password}\n\n"
        f"Please log in and change your password, then complete your student profile.\n\nAVFU Academic Cell",
    )
    c.credential_status = "sent" if sent else "failed"
    await db.commit()

    return {
        "message": "Candidate selected and account created." + ("" if sent else " Credential email could not be sent — use Resend."),
        "roll_no": c.roll_no,
        "university_email": c.avfu_email,
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
        f"Dear {_candidate_full_name(c)},\n\nYour AVFU AMS login credentials have been reset.\n\n"
        f"Login email: {student.email}\nTemporary password: {new_password}\n\nAVFU Academic Cell",
    )
    c.credential_status = "sent" if sent else "failed"
    await db.commit()
    return {"message": "Credentials resent." if sent else "Account updated, but the email could not be sent.", "credential_status": c.credential_status}


# ── Bulk Upload (Super Admin only) ──────────────────────────────────────────
# Lets a Super Admin create many Orientation candidates at once from an
# uploaded .xlsx/.csv file, using the EXACT SAME business rules as the manual
# Add Candidate form above (Section 22's explicit requirement): the same
# CandidateIn model (required-field checks, AVFU-domain validation, email
# format), the same validate_program_department_pair() for the Programme<->
# Department M:N pair, and the same avfu_email/(personal_email, academic_year,
# program_id) uniqueness rules. The only difference is control flow — this
# collects ALL row errors across the WHOLE file before writing anything
# (Phase 1), and only if every row is valid does it create all candidates in
# one transaction (Phase 2). No AMS User is ever created here — that still
# only happens through the existing /selection endpoint above.

_BULK_UPLOAD_COLUMNS = [
    "First Name", "Middle Name", "Last Name", "Personal Email", "Mobile",
    "AVFU Email", "Academic Year", "College", "Programme", "Department",
]
_BULK_REQUIRED_COLUMNS = [c for c in _BULK_UPLOAD_COLUMNS if c != "Middle Name"]
_MAX_BULK_UPLOAD_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024


def _bulk_cell_to_str(v) -> str:
    """Cell -> trimmed string. Excel stores whole numbers (e.g. an Academic
    Year typed as 2026, or a Mobile number) as floats like 2026.0 — normalized
    back to a plain integer string rather than passed through with a
    misleading '.0' suffix. Never executes formulas: openpyxl (data_only=True)
    only ever returns a cached literal value or None, never evaluates one."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _parse_bulk_headers(raw_headers: list) -> dict[str, int]:
    """Maps each canonical column name -> its column index. Exact match only
    (after trimming surrounding whitespace) — a misspelled or renamed required
    column is NEVER silently accepted, per this task's explicit requirement."""
    seen: dict[str, int] = {}
    unknown: list[str] = []
    for idx, h in enumerate(raw_headers):
        name = _bulk_cell_to_str(h)
        if not name:
            continue
        if name not in _BULK_UPLOAD_COLUMNS:
            unknown.append(name)
            continue
        if name in seen:
            raise HTTPException(400, f"Duplicate column header: '{name}'.")
        seen[name] = idx
    if unknown:
        raise HTTPException(
            400,
            f"Unrecognized column header(s): {', '.join(unknown)}. "
            f"Expected exactly: {', '.join(_BULK_UPLOAD_COLUMNS)}.",
        )
    missing = [c for c in _BULK_UPLOAD_COLUMNS if c not in seen]
    if missing:
        raise HTTPException(400, f"Missing required column(s): {', '.join(missing)}.")
    return seen


def _parse_bulk_upload_file(filename: str, content: bytes) -> list[dict]:
    """Structural file parsing only (extension, encoding, headers, blank-row
    handling) — raises HTTPException(400, ...) for anything that means the
    file itself cannot be read at all. Row-level BUSINESS validation (required
    fields, name resolution, email rules, duplicates) happens separately in
    _validate_bulk_rows so every row's errors can be collected together."""
    ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if ext not in ("xlsx", "csv"):
        raise HTTPException(400, "Unsupported file type. Please upload a .xlsx or .csv file.")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")

    if ext == "xlsx":
        try:
            # data_only=True reads each cell's last-saved literal value, never
            # a formula string, and openpyxl never evaluates formulas anyway —
            # no spreadsheet content is ever executed.
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            raw_rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "Could not read the uploaded file. Please ensure it is a valid .xlsx file.")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(400, "Could not read the uploaded file. Please ensure it is a UTF-8 encoded .csv file.")
        raw_rows = list(csv.reader(io.StringIO(text)))

    if not raw_rows:
        raise HTTPException(400, "The uploaded file has no data rows.")

    col_index = _parse_bulk_headers(raw_rows[0])

    parsed: list[dict] = []
    for i, r in enumerate(raw_rows[1:], start=2):  # row 1 is the header row
        if not any(_bulk_cell_to_str(c) for c in r):
            continue  # blank row — skipped safely, not an error
        values = {name: _bulk_cell_to_str(r[idx]) if idx < len(r) else "" for name, idx in col_index.items()}
        parsed.append({"row": i, "values": values})

    if not parsed:
        raise HTTPException(400, "The uploaded file has no data rows.")
    return parsed


def _pydantic_field_errors(exc: ValidationError) -> list[str]:
    """Turns CandidateIn's validation errors into the same plain, row-friendly
    messages already used across this file, instead of Pydantic's raw
    internal phrasing."""
    labels = {
        "first_name": "First Name", "last_name": "Last Name", "mobile": "Mobile",
        "personal_email": "Personal Email", "avfu_email": "AVFU Email",
    }
    out = []
    for err in exc.errors():
        field = err["loc"][-1] if err["loc"] else ""
        label = labels.get(field, str(field))
        msg = err["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        if "valid email address" in msg.lower():
            msg = f"{label} is not a valid email address."
        out.append(msg)
    return out


async def _validate_bulk_rows(rows: list[dict], db: AsyncSession) -> tuple[list[dict], list["CandidateIn"]]:
    """Phase 1 — validates the ENTIRE file against the database with no writes
    of its own. Returns (errors, valid_candidates); if `errors` is non-empty
    the caller must create nothing at all (all-or-nothing, Section 12)."""
    # Cached lookups (Section 21) — one query per master table/uniqueness set
    # instead of one per cell/row.
    college_rows = (await db.execute(select(College.id, College.name))).all()
    program_rows = (await db.execute(select(Program.id, Program.name))).all()
    department_rows = (await db.execute(select(Department.id, Department.name))).all()

    def _name_index(rows_) -> tuple[dict[str, UUID], Counter]:
        idx: dict[str, UUID] = {}
        counts: Counter = Counter()
        for _id, name in rows_:
            key = name.strip().lower()
            counts[key] += 1
            idx[key] = _id
        return idx, counts

    college_idx, college_counts = _name_index(college_rows)
    program_idx, program_counts = _name_index(program_rows)
    department_idx, department_counts = _name_index(department_rows)

    existing_avfu = {
        e.lower() for e in (await db.execute(
            select(OrientationCandidate.avfu_email).where(OrientationCandidate.avfu_email.is_not(None))
        )).scalars().all()
    }
    existing_user_emails = {e.lower() for e in (await db.execute(select(User.email))).scalars().all()}
    existing_triples = {
        (pe.lower(), ay, str(pid))
        for pe, ay, pid in (await db.execute(
            select(OrientationCandidate.personal_email, OrientationCandidate.academic_year, OrientationCandidate.program_id)
        )).all()
    }

    errors_by_row: dict[int, list[str]] = defaultdict(list)
    row_candidates: dict[int, CandidateIn] = {}
    seen_avfu: dict[str, int] = {}
    seen_triples: dict[tuple, int] = {}

    def _resolve(label: str, raw_name: str, idx: dict, counts: Counter, row_errs: list[str]) -> Optional[UUID]:
        key = raw_name.strip().lower()
        if counts.get(key, 0) > 1:
            row_errs.append(f"{label} '{raw_name}' matches more than one existing {label} record — ask an admin to resolve the name clash.")
            return None
        found = idx.get(key)
        if not found:
            row_errs.append(f"{label} '{raw_name}' was not found.")
        return found

    for entry in rows:
        row_no = entry["row"]
        v = entry["values"]
        row_errs: list[str] = []

        for field in _BULK_REQUIRED_COLUMNS:
            if not v.get(field):
                row_errs.append(f"{field} is required.")
        if row_errs:
            errors_by_row[row_no].extend(row_errs)
            continue  # can't safely resolve names/emails without required data

        college_id = _resolve("College", v["College"], college_idx, college_counts, row_errs)
        program_id = _resolve("Programme", v["Programme"], program_idx, program_counts, row_errs)
        department_id = _resolve("Department", v["Department"], department_idx, department_counts, row_errs)

        candidate_in: Optional[CandidateIn] = None
        if college_id and program_id and department_id:
            try:
                candidate_in = CandidateIn(
                    first_name=v["First Name"], middle_name=v["Middle Name"] or None, last_name=v["Last Name"],
                    personal_email=v["Personal Email"], mobile=v["Mobile"], avfu_email=v["AVFU Email"],
                    academic_year=v["Academic Year"], college_id=college_id, program_id=program_id, department_id=department_id,
                )
            except ValidationError as e:
                row_errs.extend(_pydantic_field_errors(e))

            try:
                await validate_program_department_pair(program_id, department_id, db)
            except HTTPException as e:
                row_errs.append(e.detail if isinstance(e.detail, str) else "This Department is not associated with the selected Programme.")

        if candidate_in:
            avfu_l = candidate_in.avfu_email  # already lowercased by the validator
            if avfu_l in existing_avfu:
                row_errs.append("This AVFU Email is already used by another candidate.")
            elif avfu_l in existing_user_emails:
                row_errs.append("This AVFU Email is already registered to an existing AMS user.")
            elif avfu_l in seen_avfu:
                other = seen_avfu[avfu_l]
                row_errs.append(f"AVFU Email '{avfu_l}' is duplicated in row {other} of this upload.")
                errors_by_row[other].append(f"AVFU Email '{avfu_l}' is duplicated in row {row_no} of this upload.")
            else:
                seen_avfu[avfu_l] = row_no

            triple = (candidate_in.personal_email.lower(), candidate_in.academic_year, str(program_id))
            if triple in existing_triples:
                row_errs.append("A candidate with this Personal Email is already registered for this Academic Year and Programme.")
            elif triple in seen_triples:
                other = seen_triples[triple]
                row_errs.append(f"This row duplicates the Personal Email/Academic Year/Programme combination already seen in row {other} of this upload.")
                errors_by_row[other].append(f"This row duplicates the Personal Email/Academic Year/Programme combination also seen in row {row_no} of this upload.")
            else:
                seen_triples[triple] = row_no

        if row_errs:
            errors_by_row[row_no].extend(row_errs)
        elif candidate_in:
            row_candidates[row_no] = candidate_in

    # A row that looked valid in isolation may have been retroactively
    # invalidated once a LATER duplicate row was found (see seen_avfu/
    # seen_triples above) — drop it from the create list.
    for r in list(row_candidates):
        if r in errors_by_row:
            del row_candidates[r]

    errors = [{"row": r, "errors": errs} for r, errs in sorted(errors_by_row.items())]
    valid = [row_candidates[r] for r in sorted(row_candidates)]
    return errors, valid


@router.post("/candidates/bulk-upload", status_code=201)
async def bulk_upload_candidates(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_BULK_UPLOAD_ROLES)),
):
    content = await file.read()
    if len(content) > _MAX_BULK_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")

    rows = _parse_bulk_upload_file(file.filename or "", content)
    errors, valid = await _validate_bulk_rows(rows, db)
    if errors:
        # Structured, all-row error response (Section 15) — deliberately NOT
        # raise HTTPException, since that would nest the payload under
        # {"detail": ...} instead of the flat shape this task specifies.
        return JSONResponse(status_code=400, content={"success": False, "imported_count": 0, "errors": errors})

    # Phase 2 — every row passed, now create them all in one transaction. No
    # AMS User is created here (Section 14) — only the existing /selection
    # endpoint above ever does that, using avfu_email as the login, never
    # personal_email.
    try:
        for ci in valid:
            db.add(OrientationCandidate(
                first_name=ci.first_name, middle_name=ci.middle_name, last_name=ci.last_name,
                personal_email=ci.personal_email.lower(), mobile=ci.mobile, avfu_email=ci.avfu_email,
                academic_year=ci.academic_year, college_id=ci.college_id,
                program_id=ci.program_id, department_id=ci.department_id,
                created_by=user.id,
            ))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return JSONResponse(status_code=409, content={
            "success": False, "imported_count": 0,
            "errors": [{"row": 0, "errors": ["One or more rows conflict with existing data (e.g. a duplicate AVFU Email). No candidates were imported."]}],
        })
    except Exception:
        await db.rollback()
        raise HTTPException(500, "An unexpected error occurred while importing candidates. No candidates were imported.")

    return {"success": True, "imported_count": len(valid), "filename": file.filename}


@router.get("/candidates/bulk-upload/template")
async def download_bulk_upload_template(
    _: User = Depends(require_roles(*_BULK_UPLOAD_ROLES)),
):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orientation Candidates"
    ws.append(_BULK_UPLOAD_COLUMNS)
    # One clearly-marked example row (Section 3) — the leading marker text
    # makes it obviously not a real name, so it is never mistaken for genuine
    # data even if an admin forgets to delete it (it will also legitimately
    # fail "Programme not found" if left in, since no real Programme is named
    # that way, so it can never be silently imported as a real candidate).
    ws.append([
        "EXAMPLE — DELETE THIS ROW", "", "Do Not Import",
        "example.personal@example.com", "9999999999", "example@avfu.ac.in",
        "2026", "<exact existing College name>", "<exact existing Programme name>", "<exact existing Department name>",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=orientation_candidates_template.xlsx"},
    )
