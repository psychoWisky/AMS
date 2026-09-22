"""Student Migration (BUSINESS_LOGIC.md section AE).

Workflow: Student creates a draft -> submits -> Registrar approves or rejects.
There is exactly ONE approval level (Registrar) — no HOD/Incharge/DPGS/VC/
Faculty stage, no OTP, no signature, no email. This module deliberately reuses
the repository's LIGHTWEIGHT single-decision pattern
(`WithdrawalRequest`/`decide_withdrawal_request` in `enrollment.py`), not the
heavy multi-stage approval-cycle engine used by PPW/Synopsis/External Examiner
Selection/Thesis — a generic approval engine would be over-engineering for a
single Approve/Reject decision.

* A REJECTED application is a permanent historical record — it is never
  edited back to draft, and rejecting never overwrites/deletes it. A student
  may create a brand-new application after a rejection (or an approval); at
  most one application may be `draft`/`submitted` at a time (database partial
  unique index, `uq_migration_one_active_per_student`).
* Rejection remark is OPTIONAL — an empty body (`{}`) is a valid rejection.
* Approval/rejection changes ONLY this application's own status/decision
  fields — it never touches the student's role, department, programme,
  academic year, enrollments, grades, or any other account/academic data.
* Registrar is confirmed SINGLE-holder (unlike Librarian) and authorization
  here is scoped ONLY to this module — Registrar is never added to any
  generic student-management or user-listing authorization tuple; every piece
  of student information the Registrar needs to process an application is
  served through this application's own snapshot fields, never a generic
  student directory.
* Registration No. is student-entered application data — AMS has no
  canonical Registration Number field on `User` (confirmed by investigation)
  and this module does not add one.
"""
import hashlib
import os
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.db.base import get_db
from app.models.migration import MigrationApplication
from app.models.user import College, Program, User, UserRole
from app.utils.pdf_merge import InvalidPdf, inspect_pdf

router = APIRouter(prefix="/migration", tags=["Student Migration"])

_ACTIVE_STATUSES = ("draft", "submitted")
_STATUS_LABELS = {"draft": "Draft", "submitted": "Submitted", "approved": "Approved", "rejected": "Rejected"}
_REQUIRED_ON_SUBMIT = ("registration_no", "last_exam_name_and_roll", "passed_from_institution", "migration_reason", "address")

_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.pdf$")
_ALLOWED_CONTENT_TYPES = {"", "application/pdf", "application/x-pdf", "application/octet-stream"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _not_found() -> HTTPException:
    return HTTPException(404, "Migration application not found.")


def _person_name(u: Optional[User]) -> Optional[str]:
    return u.full_name if u else None


# ── Schemas (extra="forbid" — identity/status fields are never client-supplied) ──

class MigrationFieldsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registration_no: Optional[str] = None
    last_exam_name_and_roll: Optional[str] = None
    passed_from_institution: Optional[str] = None
    fee_payment_date: Optional[date] = None
    migration_reason: Optional[str] = None
    address: Optional[str] = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: Optional[str] = None


# ── Load / lookup / storage helpers ───────────────────────────────────────────

_LOAD_OPTIONS = (selectinload(MigrationApplication.student), selectinload(MigrationApplication.decider))


async def _get_application(application_id: UUID, db: AsyncSession) -> MigrationApplication:
    m = (await db.execute(
        select(MigrationApplication).options(*_LOAD_OPTIONS).where(MigrationApplication.id == application_id)
    )).scalar_one_or_none()
    if not m:
        raise _not_found()
    return m


def _migration_dir(application_id: UUID) -> Path:
    return Path(settings.UPLOAD_DIR) / "migration" / str(application_id)


def _stored_path(application_id: UUID, stored_filename: str) -> Path:
    if not _STORED_NAME_RE.match(stored_filename or ""):
        raise HTTPException(500, "Invalid stored file reference.")
    return _migration_dir(application_id) / stored_filename


def _write_stored(application_id: UUID, stored_filename: str, data: bytes) -> None:
    directory = _migration_dir(application_id)
    directory.mkdir(parents=True, exist_ok=True)
    with open(_stored_path(application_id, stored_filename), "xb") as f:
        f.write(data)


def _read_stored(application_id: UUID, stored_filename: str) -> bytes:
    path = _stored_path(application_id, stored_filename)
    if not path.is_file():
        raise HTTPException(404, "The stored file is missing on the server.")
    return path.read_bytes()


def _unlink_quietly(application_id: UUID, stored_filename: Optional[str]) -> None:
    if not stored_filename:
        return
    try:
        _stored_path(application_id, stored_filename).unlink(missing_ok=True)
    except (OSError, HTTPException):  # noqa: BLE001 - best-effort cleanup only
        pass


async def _validate_pdf_upload(upload: UploadFile) -> tuple[bytes, str]:
    """PDF ONLY — the exact rigor already established by Synopsis's
    `_validate_pdf_upload`: extension, declared content type, the real
    `%PDF-` signature, the size limit, and a genuine structural parse via the
    existing generic `app.utils.pdf_merge.inspect_pdf` (never re-implemented
    here). Returns (bytes, sha256)."""
    name = (upload.filename or "").strip()
    if os.path.splitext(name)[1].lower() != ".pdf":
        raise HTTPException(400, "Only PDF files are accepted.")
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, "Only PDF files are accepted.")
    data = await upload.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, f"The file exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "The file is not a PDF document.")
    try:
        inspect_pdf(data)
    except InvalidPdf as exc:
        raise HTTPException(400, str(exc)) from exc
    return data, hashlib.sha256(data).hexdigest()


# ── Authorization ────────────────────────────────────────────────────────────

def _authorize_view(m: MigrationApplication, user: User) -> None:
    """Who may READ a Migration application (and its receipt): the owning
    student, the (single) Registrar, or Super Admin. Anything else is a 404
    so the existence of another student's application is never revealed —
    the same convention used by every prior module's `_authorize_view`."""
    role = user.active_role
    if role in (UserRole.SUPER_ADMIN, UserRole.REGISTRAR):
        return
    if role == UserRole.STUDENT and m.student_id == user.id:
        return
    raise _not_found()


def _require_owned_editable(m: MigrationApplication, user: User) -> None:
    if m.student_id != user.id:
        raise _not_found()
    if m.status != "draft":
        raise HTTPException(400, "This Migration application has already been submitted and can no longer be edited.")


# ── Serialization ─────────────────────────────────────────────────────────────

def _receipt_dict(m: MigrationApplication) -> Optional[dict]:
    if not m.receipt_stored_filename:
        return None
    return {
        "original_filename": m.receipt_original_filename, "size_bytes": m.receipt_size_bytes,
        "uploaded_at": m.receipt_uploaded_at.isoformat() if m.receipt_uploaded_at else None,
    }


def _application_dict(m: MigrationApplication, viewer: User) -> dict:
    is_owner = viewer.active_role == UserRole.STUDENT and m.student_id == viewer.id
    return {
        "id": str(m.id), "status": m.status, "status_label": _STATUS_LABELS.get(m.status, m.status),
        "student_name": m.student_name_snapshot, "student_roll": m.student_roll_snapshot,
        "degree": m.degree_snapshot, "college": m.college_snapshot,
        "registration_no": m.registration_no,
        "last_exam_name_and_roll": m.last_exam_name_and_roll,
        "passed_from_institution": m.passed_from_institution,
        "fee_payment_date": m.fee_payment_date.isoformat() if m.fee_payment_date else None,
        "migration_reason": m.migration_reason,
        "address": m.address,
        "receipt": _receipt_dict(m),
        "decided_by": _person_name(m.decider), "decided_at": m.decided_at.isoformat() if m.decided_at else None,
        "decision_remark": m.decision_remark,
        "submitted_at": m.submitted_at.isoformat() if m.submitted_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "is_owner": is_owner, "can_edit": is_owner and m.status == "draft",
    }


# ── Create / read ─────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_migration(
    body: MigrationFieldsIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """A student creates a Migration application. The caller is always the
    student — no `student_id` is ever accepted. Name/Roll/Degree/College are
    snapshotted from the student's own `User` record right now, server-side;
    the client cannot supply or influence them. Blocked (409) if the student
    already has an active (draft/submitted) application — the database's
    partial unique index is the final guarantee; this pre-check only gives a
    friendly message."""
    existing = (await db.execute(
        select(MigrationApplication.id).where(MigrationApplication.student_id == user.id, MigrationApplication.status.in_(_ACTIVE_STATUSES))
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "You already have an active Migration application. Wait for it to be decided before creating another.")
    # `user` (from `get_current_user`) does not eager-load `program`/`college` —
    # accessing those relationships directly here would lazy-load on an AsyncSession
    # outside of a greenlet context and raise MissingGreenlet. Look them up by id
    # instead (same pattern as every other snapshot-on-create in this codebase).
    program = await db.get(Program, user.program_id) if user.program_id else None
    college = await db.get(College, user.college_id) if user.college_id else None
    m = MigrationApplication(
        student_id=user.id, status="draft",
        student_name_snapshot=user.full_name, student_roll_snapshot=user.student_roll,
        degree_snapshot=program.name if program else None,
        college_snapshot=college.name if college else None,
        **body.model_dump(exclude_none=True),
    )
    db.add(m)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You already have an active Migration application. Wait for it to be decided before creating another.")
    return {"id": str(m.id), "message": "Migration application draft created.", "status": m.status}


@router.get("/mine")
async def list_my_applications(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    rows = (await db.execute(
        select(MigrationApplication).where(MigrationApplication.student_id == user.id).order_by(MigrationApplication.created_at.desc())
    )).scalars().all()
    return [{
        "id": str(m.id), "student_name": m.student_name_snapshot, "student_roll": m.student_roll_snapshot,
        "degree": m.degree_snapshot, "college": m.college_snapshot,
        "status": m.status, "status_label": _STATUS_LABELS.get(m.status, m.status),
    } for m in rows]


@router.get("/pending-approvals")
async def list_pending_approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.REGISTRAR))):
    """The Registrar's inbox — submitted applications only. Registrar gets no
    generic student-directory access anywhere in this module; every field
    shown here comes from the application's own snapshot."""
    rows = (await db.execute(
        select(MigrationApplication).options(*_LOAD_OPTIONS).where(MigrationApplication.status == "submitted")
        .order_by(MigrationApplication.submitted_at)
    )).scalars().all()
    return [{
        "id": str(m.id), "student_name": m.student_name_snapshot, "student_roll": m.student_roll_snapshot,
        "degree": m.degree_snapshot, "college": m.college_snapshot,
        "status": m.status, "status_label": _STATUS_LABELS.get(m.status, m.status),
        "submitted_at": m.submitted_at.isoformat() if m.submitted_at else None,
    } for m in rows]


@router.get("/{application_id}")
async def get_application(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    m = await _get_application(application_id, db)
    _authorize_view(m, user)
    return _application_dict(m, user)


@router.patch("/{application_id}")
async def update_application(
    application_id: UUID, body: MigrationFieldsIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    m = await _get_application(application_id, db)
    _require_owned_editable(m, user)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(m, k, v)
    await db.commit()
    return _application_dict(await _get_application(application_id, db), user)


# ── Receipt ───────────────────────────────────────────────────────────────────

@router.post("/{application_id}/receipt", status_code=201)
async def upload_receipt(
    application_id: UUID, file: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Upload or replace the Payment Receipt. PDF only. Only while the
    application is still `draft` — once submitted the receipt is immutable.
    Replacing while draft safely removes the previous stored file (no
    orphaned files left on disk)."""
    m = await _get_application(application_id, db)
    _require_owned_editable(m, user)
    data, digest = await _validate_pdf_upload(file)
    old_stored = m.receipt_stored_filename
    stored_name = f"{secrets.token_hex(16)}.pdf"
    _write_stored(m.id, stored_name, data)
    m.receipt_stored_filename = stored_name
    m.receipt_original_filename = os.path.basename((file.filename or "receipt.pdf").replace("\\", "/"))[:255] or "receipt.pdf"
    m.receipt_content_type = "application/pdf"
    m.receipt_size_bytes = len(data)
    m.receipt_sha256 = digest
    m.receipt_uploaded_at = _now()
    await db.commit()
    _unlink_quietly(m.id, old_stored if old_stored != stored_name else None)
    return {"message": "Payment receipt uploaded."}


@router.get("/{application_id}/receipt")
async def download_receipt(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    m = await _get_application(application_id, db)
    _authorize_view(m, user)
    if not m.receipt_stored_filename:
        raise HTTPException(404, "No payment receipt has been uploaded yet.")
    data = _read_stored(m.id, m.receipt_stored_filename)
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{m.receipt_original_filename or "receipt.pdf"}"'},
    )


# ── Submit ────────────────────────────────────────────────────────────────────

@router.post("/{application_id}/submit")
async def submit_application(application_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """Submit for Registrar review. Fully revalidated server-side regardless
    of what the frontend already checked: every required field and the
    Payment Receipt must be present. `draft -> submitted` only."""
    m = await _get_application(application_id, db)
    _require_owned_editable(m, user)
    missing = [f for f in _REQUIRED_ON_SUBMIT if not (getattr(m, f) or "").strip()]
    if missing:
        raise HTTPException(400, "Please complete all required fields before submitting: " + ", ".join(f.replace("_", " ") for f in missing) + ".")
    if not m.fee_payment_date:
        raise HTTPException(400, "Enter the date of payment of the migration fee before submitting.")
    if not m.receipt_stored_filename:
        raise HTTPException(400, "Upload your Payment Receipt (PDF) before submitting.")
    m.status = "submitted"
    m.submitted_at = _now()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This application could not be submitted; please reload.")
    return {"message": "Migration application submitted for Registrar review.", "status": m.status}


# ── Registrar approval ────────────────────────────────────────────────────────

@router.post("/{application_id}/approval/approve")
async def approve_application(
    application_id: UUID, body: DecisionIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.REGISTRAR)),
):
    """Only `submitted` applications may be approved. Approval changes ONLY
    this application's own status/decision fields — it never mutates the
    student's role, department, programme, academic year, enrollments,
    grades, or any other account/academic data. No OTP, no signature."""
    m = await _get_application(application_id, db)
    if m.status != "submitted":
        raise HTTPException(400, "Only a submitted Migration application can be approved.")
    m.status = "approved"
    m.decided_by = user.id
    m.decided_at = _now()
    m.decision_remark = (body.remark or "").strip() or None
    await db.commit()
    return {"message": "Migration application approved.", "status": m.status}


@router.post("/{application_id}/approval/reject")
async def reject_application(
    application_id: UUID, body: DecisionIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.REGISTRAR)),
):
    """Only `submitted` applications may be rejected. The remark is OPTIONAL
    — an empty body (`{}`) is valid. Rejection is terminal for THIS
    application (never edited back to draft); the student may create a new
    one afterward."""
    m = await _get_application(application_id, db)
    if m.status != "submitted":
        raise HTTPException(400, "Only a submitted Migration application can be rejected.")
    m.status = "rejected"
    m.decided_by = user.id
    m.decided_at = _now()
    m.decision_remark = (body.remark or "").strip() or None
    await db.commit()
    return {"message": "Migration application rejected.", "status": m.status}
