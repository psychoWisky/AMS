"""Initial Thesis Management (BUSINESS_LOGIC.md section AD). Final Thesis is OUT OF SCOPE.

Workflow (per submission cycle):

    Student submits -> Major Advisor -> HOD -> Librarian -> Incharge Academic Cell -> DPGS
        (sends for evaluation) -> External Examiner(s) evaluate -> DPGS (final approval)

* Major Advisor / HOD / Librarian / DPGS sign with an email OTP (same mechanism as PPW/
  Synopsis/External Examiner Selection); the Incharge Academic Cell is a workflow approver
  only — no OTP, no signature, matching the established convention across every prior
  multi-stage workflow in this repository. DPGS's final approval (of the external
  evaluation) also signs with an OTP.
* A revert from Major Advisor/HOD/Librarian/Incharge/DPGS ALWAYS returns the Thesis to the
  STUDENT (Synopsis's convention, not External Examiner Selection's "always to Major
  Advisor" one — there is no student in that other workflow). There is deliberately no
  revert path once the Thesis has moved past DPGS into external evaluation (see the
  implementation report's open questions).
* Authorization is derived on the server from the caller's ACTIVE session role/department
  and the student's real Advisory Committee / External Examiner Assignment rows — never
  from an id in the request (`_find_my_stage`, `_authorize_view`).
* The Thesis Title is never typed by the student: it is copied ONCE, at creation, from the
  student's own `Ppw.research_title` (`title_snapshot`) — never resynced afterward.
* Examiner identity is never duplicated here: External Examiner evaluation rows point at the
  existing `ams_external_examiner_assignments` row created by the already-implemented
  External Examiner Selection module.
* Only ONE Initial Thesis per student, enforced by a partial unique index in the database.
"""
import hashlib
import logging
import os
import re
import secrets
import string
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.email import send_email
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.external_examiner import ExternalExaminer, ExternalExaminerAssignment
from app.models.ppw import Ppw
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.thesis import (
    CONFIDENTIAL_DOCUMENT_TYPES, THESIS_DOCUMENT_TYPES,
    Thesis, ThesisApprovalCycle, ThesisApprovalStage, ThesisDocument, ThesisExternalEvaluation, ThesisSignature,
)
from app.models.user import Department, Program, User, UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/thesis", tags=["Initial Thesis"])

_PG_PHD_LEVELS = ("PG", "PhD")
_EDITABLE_STATUSES = ("draft", "reverted")
_APPROVER_ROLES = (UserRole.FACULTY, UserRole.HOD, UserRole.LIBRARIAN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)
_SIGNATORY_STAGES = ("major_advisor", "hod", "librarian", "dpgs", "dpgs_final")  # OTP-verified; incharge_academic_cell is workflow-only
_REVERTIBLE_STAGES = ("major_advisor", "hod", "librarian", "incharge_academic_cell", "dpgs")  # dpgs_final has no revert path this phase

_PHASE_FOR_STAGE = {
    "major_advisor": "major_advisor_pending",
    "hod": "hod_pending",
    "librarian": "librarian_pending",
    "incharge_academic_cell": "incharge_pending",
    "dpgs": "dpgs_pending",
    "dpgs_final": "dpgs_final_pending",
}
_STATUS_LABELS = {
    "draft": "Draft",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "librarian_pending": "Library Approval Pending",
    "incharge_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending (Send for Evaluation)",
    "external_examiner_pending": "Pending External Examiner Evaluation",
    "dpgs_final_pending": "Pending DPGS Final Approval",
    "approved": "Approved",
    "reverted": "Reverted",
}
# Business-document terminology (BUSINESS_LOGIC.md AD) — the role stays LIBRARIAN in the
# data model; only the printed/displayed label differs, exactly like External Examiner
# Selection's role_label convention.
_STAGE_ROLE_LABELS = {
    "major_advisor": "Major Advisor", "hod": "Head of the Department", "librarian": "Chief Librarian",
    "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "dpgs_final": "DPGS (Final Approval)",
}
_ROLE_DISPLAY = {
    "faculty": "Faculty", "hod": "HOD", "librarian": "Librarian",
    "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "super_admin": "Super Admin",
}
# Which document types the STUDENT may upload/replace themselves; the library plagiarism
# report is entered only by a Librarian (see the dedicated library-plagiarism endpoints).
_STUDENT_DOCUMENT_TYPES = tuple(t for t in THESIS_DOCUMENT_TYPES if t not in CONFIDENTIAL_DOCUMENT_TYPES)
# The two document types an assigned External Examiner may read, once the Thesis has been
# sent for evaluation — never the four purely-administrative student documents.
_EXAMINER_DOCUMENT_TYPES = {"thesis_file", "plagiarism_student_report", "plagiarism_library_report"}

_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(docx|pdf)$")
_ALLOWED_DOCX_CONTENT_TYPES = {"", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/octet-stream"}
_OTP_TTL_MINUTES = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _not_found() -> HTTPException:
    return HTTPException(404, "Thesis not found.")


def _person_name(u: Optional[User]) -> Optional[str]:
    return u.full_name if u else None


def _name_designation(u: Optional[User]) -> str:
    if not u:
        return "—"
    return f"{u.full_name} ({u.designation})" if u.designation else u.full_name


def _is_dev_environment() -> bool:
    return settings.ENVIRONMENT == "development"


# ── Schemas ───────────────────────────────────────────────────────────────────

class ThesisUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plagiarism_student_percent: Optional[float] = None
    plagiarism_software_name: Optional[str] = None
    abstract: Optional[str] = None


class LibraryPlagiarismIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plagiarism_library_percent: float


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp: Optional[str] = None


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: str


# ── Load / lookup helpers ─────────────────────────────────────────────────────

_LOAD_OPTIONS = (
    selectinload(Thesis.student).selectinload(User.program),
    selectinload(Thesis.student).selectinload(User.department),
    selectinload(Thesis.student).selectinload(User.college),
    selectinload(Thesis.documents),
    selectinload(Thesis.cycles).selectinload(ThesisApprovalCycle.stages).selectinload(ThesisApprovalStage.assignee),
    selectinload(Thesis.cycles).selectinload(ThesisApprovalCycle.stages).selectinload(ThesisApprovalStage.approver),
    selectinload(Thesis.cycles).selectinload(ThesisApprovalCycle.stages).selectinload(ThesisApprovalStage.committee_member),
    selectinload(Thesis.evaluations).selectinload(ThesisExternalEvaluation.assignment).selectinload(ExternalExaminerAssignment.examiner).selectinload(ExternalExaminer.user),
)


async def _get_thesis(thesis_id: UUID, db: AsyncSession) -> Thesis:
    t = (await db.execute(select(Thesis).options(*_LOAD_OPTIONS).where(Thesis.id == thesis_id))).scalar_one_or_none()
    if not t:
        raise _not_found()
    return t


def _latest_cycle(t: Thesis) -> Optional[ThesisApprovalCycle]:
    return max(t.cycles, key=lambda c: c.cycle_number) if t.cycles else None


def _active_cycle(t: Thesis) -> Optional[ThesisApprovalCycle]:
    c = _latest_cycle(t)
    return c if c and c.status == "active" else None


def _latest_document(t: Thesis, document_type: str) -> Optional[ThesisDocument]:
    versions = [d for d in t.documents if d.document_type == document_type]
    return max(versions, key=lambda d: d.version_number) if versions else None


# ── File storage (server-generated names; the client never sees a path) ──────

def _thesis_dir(thesis_id: UUID) -> Path:
    return Path(settings.UPLOAD_DIR) / "thesis" / str(thesis_id)


def _stored_path(thesis_id: UUID, stored_filename: str) -> Path:
    if not _STORED_NAME_RE.match(stored_filename or ""):
        raise HTTPException(500, "Invalid stored file reference.")
    return _thesis_dir(thesis_id) / stored_filename


def _write_stored(thesis_id: UUID, stored_filename: str, data: bytes) -> None:
    directory = _thesis_dir(thesis_id)
    directory.mkdir(parents=True, exist_ok=True)
    with open(_stored_path(thesis_id, stored_filename), "xb") as f:
        f.write(data)


def _read_stored(thesis_id: UUID, stored_filename: str) -> bytes:
    path = _stored_path(thesis_id, stored_filename)
    if not path.is_file():
        raise HTTPException(404, "The stored file is missing on the server.")
    return path.read_bytes()


async def _validate_docx_upload(upload: UploadFile) -> tuple[bytes, str]:
    """.docx ONLY: extension, declared content type, real ZIP/OOXML signature and a real parse
    (docx is a ZIP archive containing `word/document.xml`) — the same rigor as Synopsis's
    `_validate_pdf_upload`, never trusting the filename alone. Returns (bytes, sha256)."""
    name = (upload.filename or "").strip()
    if os.path.splitext(name)[1].lower() != ".docx":
        raise HTTPException(400, "Only .docx files are accepted.")
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared not in _ALLOWED_DOCX_CONTENT_TYPES:
        raise HTTPException(400, "Only .docx files are accepted.")
    data = await upload.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, f"The file exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if not data.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "The file is not a valid .docx document.")
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = zf.namelist()
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise HTTPException(400, "The file is not a valid .docx document.")
    except zipfile.BadZipFile:
        raise HTTPException(400, "The file is not a valid .docx document.")
    return data, hashlib.sha256(data).hexdigest()


# ── Authorization ────────────────────────────────────────────────────────────

async def _committee_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    return (await db.execute(select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id == student_id))).scalar_one_or_none()


async def _my_examiner_evaluation(t: Thesis, user: User) -> Optional[ThesisExternalEvaluation]:
    """The caller's OWN evaluation row on THIS thesis, or None — resolved from the actual
    ExternalExaminerAssignment chain (assignment.examiner.user_id == caller), never from an
    id in the request. An examiner assigned to a DIFFERENT student never matches here, even
    if they hold another, unrelated assignment for this same student's evaluation module."""
    if user.active_role != UserRole.EXTERNAL_EXAMINER:
        return None
    for ev in t.evaluations:
        examiner = ev.assignment.examiner if ev.assignment else None
        if examiner and examiner.user_id == user.id and ev.assignment.student_id == t.student_id:
            return ev
    return None


async def _authorize_view(t: Thesis, user: User, db: AsyncSession) -> None:
    """Who may READ a Thesis. Anything else is a 404 so the existence of another student's
    Thesis is never revealed. A draft is private to the student (and Super Admin)."""
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return
    if role == UserRole.STUDENT:
        if t.student_id == user.id:
            return
        raise _not_found()
    if role == UserRole.EXTERNAL_EXAMINER:
        if await _my_examiner_evaluation(t, user):
            return
        raise _not_found()
    if t.status == "draft":
        raise _not_found()
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.LIBRARIAN):
        return
    if role in (UserRole.FACULTY, UserRole.HOD):
        assigned = await db.execute(
            select(ThesisApprovalStage.id)
            .join(ThesisApprovalCycle, ThesisApprovalCycle.id == ThesisApprovalStage.cycle_id)
            .where(ThesisApprovalCycle.thesis_id == t.id, ThesisApprovalStage.assignee_id == user.id)
            .limit(1)
        )
        if assigned.scalar_one_or_none():
            return
    if role == UserRole.HOD:
        dept_id = await resolve_student_department_id(t.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
    raise _not_found()


async def _find_my_stage(t: Thesis, user: User, db: AsyncSession) -> Optional[ThesisApprovalStage]:
    """The SOLE authorization check for every approval action: the caller's own currently-
    actionable stage in the ACTIVE cycle, or None. Never a bare role match and never an id
    from the request. Librarian is explicitly NOT scoped to any department/library — ANY
    active Librarian may act on the pending Librarian stage (Section 12 of the confirmed
    business rules; no evidence anywhere in the repository of a library/department scoping
    requirement, so none was invented)."""
    cycle = _active_cycle(t)
    if not cycle:
        return None
    committee_id = None
    for stage in cycle.stages:
        if stage.status != "pending" or _PHASE_FOR_STAGE.get(stage.stage_type) != t.status:
            continue
        if stage.stage_type == "major_advisor":
            if user.active_role not in (UserRole.FACULTY, UserRole.HOD):
                continue
            member = stage.committee_member
            if not member or member.faculty_id != user.id or member.role != "major_advisor":
                continue
            if committee_id is None:
                committee_id = await _committee_id(t.student_id, db)
            if committee_id and member.committee_id == committee_id:
                return stage
        elif stage.stage_type == "hod":
            if user.active_role == UserRole.HOD and user.active_department_id:
                dept_id = await resolve_student_department_id(t.student_id, db)
                if dept_id and dept_id == user.active_department_id:
                    return stage
        elif stage.stage_type == "librarian":
            if user.active_role == UserRole.LIBRARIAN:
                return stage
        elif stage.stage_type == "incharge_academic_cell":
            if user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
                return stage
        elif stage.stage_type in ("dpgs", "dpgs_final"):
            if user.active_role == UserRole.DPGS:
                return stage
    return None


async def _require_my_stage(t: Thesis, user: User, db: AsyncSession) -> ThesisApprovalStage:
    stage = await _find_my_stage(t, user, db)
    if not stage:
        raise HTTPException(403, "You have no pending Initial Thesis approval action.")
    return stage


# ── Serialization ─────────────────────────────────────────────────────────────

async def _department_names(db: AsyncSession) -> dict:
    return {i: n for i, n in (await db.execute(select(Department.id, Department.name))).all()}


def _document_dict(d: Optional[ThesisDocument]) -> Optional[dict]:
    if not d:
        return None
    return {
        "id": str(d.id), "version": d.version_number, "original_filename": d.original_filename,
        "size_bytes": d.size_bytes, "uploaded_at": _iso(d.uploaded_at),
        "uploaded_by": _person_name(d.uploader),
    }


def _stage_dict(stage: ThesisApprovalStage, dept_names: dict) -> dict:
    return {
        "sequence": stage.sequence, "stage_type": stage.stage_type, "role_label": stage.role_label,
        "assigned_to": _name_designation(stage.assignee) if stage.assignee else None,
        "status": stage.status,
        "acted_by": _name_designation(stage.approver) if stage.approver else None,
        "acted_role": _ROLE_DISPLAY.get(stage.acted_role or "", stage.acted_role),
        "acted_department": dept_names.get(stage.acted_department_id),
        "acted_at": _iso(stage.acted_at), "remark": stage.remark,
        "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
    }


def _signature_row(cycle: Optional[ThesisApprovalCycle], stage_type: str, label: str) -> dict:
    stage = next((st for st in cycle.stages if st.stage_type == stage_type), None) if cycle else None
    return {
        "role_label": label,
        "status": "Signed" if (stage and stage.status == "approved") else "Pending",
        "acted_at": _iso(stage.acted_at) if stage else None,
    }


def _signature_table(t: Thesis, cycle: Optional[ThesisApprovalCycle]) -> list[dict]:
    """The printed Signature table — exactly the 6 rows confirmed in the business
    requirement (Student, Major Advisor, HOD, Chief Librarian, Incharge Academic Cell,
    DPGS). "Student Signature" is derived from the submission act itself (no OTP, no
    separate stage/signature row — a formal student e-signature is not a mechanism that
    exists anywhere else in AMS, so none was invented here); its date is the cycle's
    `submitted_at`. DPGS's SECOND, later action (approving the external evaluation) is
    tracked as its own `dpgs_final` stage for audit history, but is deliberately NOT a
    7th row here — it is not part of the business form's confirmed table."""
    return [
        {"role_label": "Student Signature", "status": "Signed" if (cycle and cycle.submitted_at) else "Pending",
         "acted_at": _iso(cycle.submitted_at) if cycle else None},
        _signature_row(cycle, "major_advisor", "Major Advisor"),
        _signature_row(cycle, "hod", "HOD"),
        _signature_row(cycle, "librarian", "Chief Librarian"),
        _signature_row(cycle, "incharge_academic_cell", "Incharge Academic Cell"),
        _signature_row(cycle, "dpgs", "DPGS"),
    ]


def _evaluation_row(ev: ThesisExternalEvaluation) -> dict:
    examiner_user = ev.assignment.examiner.user if (ev.assignment and ev.assignment.examiner) else None
    return {
        "id": str(ev.id),
        "name": _person_name(examiner_user) or "External Examiner",
        "status": ev.status,
        "report_available": ev.status in ("submitted", "approved"),
        "submitted_at": _iso(ev.submitted_at),
        "dpgs_approved_at": _iso(ev.dpgs_approved_at),
    }


def _external_report_section(t: Thesis, viewer: User) -> dict:
    """Confidentiality-by-omission (External Examiner Selection's own established pattern):
    a Student sees ONLY evaluations DPGS has approved (Section 22/23 — before that, the field
    is genuinely absent, never an empty/pending placeholder). An assigned Examiner sees only
    their OWN row. DPGS/Super Admin see every row, any status. Every other approver
    (HOD/Librarian/Incharge/Major Advisor) gets no examiner-identifying detail at all — only
    a plain completion boolean — mirroring the confidentiality boundary the External Examiner
    Selection module already enforces for the very same VC decision."""
    role = viewer.active_role
    any_approved = any(ev.status == "approved" for ev in t.evaluations)
    if role == UserRole.STUDENT:
        rows = [_evaluation_row(ev) for ev in t.evaluations if ev.status == "approved"]
        if rows:
            return {"external_report": rows}
        return {"external_evaluation_completed": False}
    if role == UserRole.EXTERNAL_EXAMINER:
        return {"external_evaluation_completed": any_approved}
    if role in (UserRole.DPGS, UserRole.SUPER_ADMIN):
        return {"external_report": [_evaluation_row(ev) for ev in t.evaluations]}
    return {"external_evaluation_completed": any_approved}


async def _thesis_dict(t: Thesis, viewer: User, db: AsyncSession) -> dict:
    dept_names = await _department_names(db)
    cycle = _latest_cycle(t)
    my_stage = await _find_my_stage(t, viewer, db) if viewer.active_role in _APPROVER_ROLES else None
    is_examiner = viewer.active_role == UserRole.EXTERNAL_EXAMINER
    my_evaluation = await _my_examiner_evaluation(t, viewer) if is_examiner else None

    revert_info = None
    if t.status == "reverted" and cycle and cycle.status == "reverted":
        stage = next((st for st in cycle.stages if st.status == "reverted"), None)
        revert_info = {
            "reverted_by": _name_designation(stage.approver) if stage and stage.approver else None,
            "role": stage.role_label if stage else None,
            "department": dept_names.get(stage.acted_department_id) if stage else None,
            "reverted_at": _iso(cycle.reverted_at), "remark": cycle.revert_remark,
        }

    doc_types = _EXAMINER_DOCUMENT_TYPES if is_examiner else THESIS_DOCUMENT_TYPES
    documents = {dt: _document_dict(_latest_document(t, dt)) for dt in doc_types}

    body = {
        "id": str(t.id), "thesis_type": t.thesis_type,
        "status": t.status, "status_label": _STATUS_LABELS.get(t.status, t.status),
        "title": t.title_snapshot,
        "student": {
            "name": t.student.full_name, "roll_no": t.student.student_roll,
            "degree_name": t.student.program.name if t.student.program else None,
            "department_name": t.student.department.name if t.student.department else None,
            "college_name": t.student.college.name if t.student.college else None,
        },
        "plagiarism_student_percent": t.plagiarism_student_percent,
        "plagiarism_software_name": t.plagiarism_software_name,
        "plagiarism_library_percent": t.plagiarism_library_percent,
        "abstract": t.abstract if not is_examiner else None,
        "documents": documents,
        "is_owner": viewer.active_role == UserRole.STUDENT and t.student_id == viewer.id,
        "can_edit": viewer.active_role == UserRole.STUDENT and t.student_id == viewer.id and t.status in _EDITABLE_STATUSES,
        "revert_info": revert_info,
        "my_pending_stage": (
            {"stage_type": my_stage.stage_type, "role_label": my_stage.role_label, "requires_otp": my_stage.stage_type in _SIGNATORY_STAGES}
            if my_stage else None
        ),
        "my_evaluation": _evaluation_row(my_evaluation) if my_evaluation else None,
    }
    if not is_examiner:
        body["stages"] = [_stage_dict(st, dept_names) for st in cycle.stages] if cycle else []
        body["signature_table"] = _signature_table(t, cycle)
        body.update(_external_report_section(t, viewer))
    return body


# ── Create / read ─────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_thesis(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """A student creates their ONE Initial Thesis. The student is always the caller — no
    student id is accepted anywhere. The database's partial unique index is the final
    guarantee; the pre-check below only gives a friendly message. The Thesis Title is
    copied ONCE from the student's own PPW `research_title` (never re-typed, never
    resynced later — see thesis.py's module docstring)."""
    program = await db.get(Program, user.program_id) if user.program_id else None
    if not program or program.level not in _PG_PHD_LEVELS:
        raise HTTPException(403, "The Initial Thesis is available to postgraduate students only.")
    existing = (await db.execute(select(Thesis.id).where(Thesis.student_id == user.id, Thesis.thesis_type == "initial"))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "You already have an Initial Thesis. A student can have only one.")
    ppw = (await db.execute(select(Ppw).where(Ppw.student_id == user.id))).scalar_one_or_none()
    title = (ppw.research_title or "").strip() if ppw else ""
    if not title:
        raise HTTPException(400, "Fill in your Research Title in your PPW before creating your Initial Thesis.")
    t = Thesis(student_id=user.id, thesis_type="initial", ppw_id=ppw.id, title_snapshot=title, status="draft")
    db.add(t)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You already have an Initial Thesis. A student can have only one.")
    return {"id": str(t.id), "message": "Initial Thesis draft created.", "title": title}


@router.get("/mine")
async def list_my_theses(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """The Thesis Applications table — for now, at most one row (Initial Submission only)."""
    rows = (await db.execute(select(Thesis).where(Thesis.student_id == user.id))).scalars().all()
    return [{
        "id": str(t.id), "title": t.title_snapshot,
        "thesis_type_label": "Initial Submission" if t.thesis_type == "initial" else "Final Submission",
        "status": t.status, "status_label": _STATUS_LABELS.get(t.status, t.status),
    } for t in rows]


@router.get("/my-evaluations")
async def list_my_evaluations(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.EXTERNAL_EXAMINER))):
    """Every Thesis this examiner actually holds an `ExternalExaminerAssignment` for — resolved
    entirely from that existing chain, never from a client-supplied student/thesis id. A thesis
    only appears once it has actually been sent for evaluation (`external_examiner_pending` or
    later); an examiner never sees a thesis before DPGS releases it (Section 20)."""
    my_assignment_ids = (await db.execute(
        select(ExternalExaminerAssignment.id).join(ExternalExaminer, ExternalExaminer.id == ExternalExaminerAssignment.examiner_id)
        .where(ExternalExaminer.user_id == user.id, ExternalExaminerAssignment.status == "active")
    )).scalars().all()
    if not my_assignment_ids:
        return []
    rows = []
    theses = (await db.execute(
        select(Thesis).options(*_LOAD_OPTIONS)
        .join(ThesisExternalEvaluation, ThesisExternalEvaluation.thesis_id == Thesis.id)
        .where(ThesisExternalEvaluation.assignment_id.in_(my_assignment_ids))
    )).unique().scalars().all()
    for t in theses:
        ev = await _my_examiner_evaluation(t, user)
        if not ev:
            continue
        rows.append({
            "thesis_id": str(t.id), "evaluation_id": str(ev.id), "student_name": t.student.full_name,
            "title": t.title_snapshot, "status": ev.status, "submitted_at": _iso(ev.submitted_at),
        })
    return rows


@router.get("/pending-approvals")
async def list_pending_approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """"My Thesis approvals" inbox — every Thesis whose CURRENT stage the caller (in their
    active session role) can act on."""
    ids: set[UUID] = set()
    if user.active_role == UserRole.FACULTY:
        ids.update((await db.execute(
            select(Thesis.id)
            .join(ThesisApprovalCycle, ThesisApprovalCycle.thesis_id == Thesis.id)
            .join(ThesisApprovalStage, ThesisApprovalStage.cycle_id == ThesisApprovalCycle.id)
            .where(ThesisApprovalCycle.status == "active", ThesisApprovalStage.status == "pending",
                   ThesisApprovalStage.stage_type == "major_advisor", ThesisApprovalStage.assignee_id == user.id)
        )).scalars().all())
    phase = {
        UserRole.HOD: "hod_pending", UserRole.LIBRARIAN: "librarian_pending",
        UserRole.INCHARGE_ACADEMIC_CELL: "incharge_pending",
    }.get(user.active_role)
    if user.active_role == UserRole.DPGS:
        ids.update((await db.execute(select(Thesis.id).where(Thesis.status.in_(("dpgs_pending", "dpgs_final_pending"))))).scalars().all())
    elif phase:
        q = select(Thesis.id).where(Thesis.status == phase)
        if user.active_role == UserRole.HOD:
            if not user.active_department_id:
                return []
            q = q.join(User, User.id == Thesis.student_id).where(User.department_id == user.active_department_id)
        ids.update((await db.execute(q)).scalars().all())

    rows = []
    for tid in ids:
        t = await _get_thesis(tid, db)
        stage = await _find_my_stage(t, user, db)
        if not stage:
            continue
        cycle = _active_cycle(t)
        rows.append({
            "thesis_id": str(t.id), "student_name": t.student.full_name, "student_roll": t.student.student_roll,
            "degree_name": t.student.program.name if t.student.program else None,
            "department_name": t.student.department.name if t.student.department else None,
            "title": t.title_snapshot, "status": t.status, "status_label": _STATUS_LABELS.get(t.status, t.status),
            "acting_as": stage.role_label, "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
            "cycle_number": cycle.cycle_number if cycle else None, "submitted_at": _iso(cycle.submitted_at) if cycle else None,
        })
    rows.sort(key=lambda r: r["submitted_at"] or "")
    return rows


@router.get("/{thesis_id}")
async def get_thesis(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    t = await _get_thesis(thesis_id, db)
    await _authorize_view(t, user, db)
    return await _thesis_dict(t, user, db)


# ── Student edit (plagiarism-by-student, abstract) ────────────────────────────

def _require_owned_editable_thesis(t: Thesis, user: User) -> None:
    if t.student_id != user.id:
        raise _not_found()
    if t.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Initial Thesis is under approval or approved and can no longer be edited.")


@router.patch("/{thesis_id}")
async def update_thesis(
    thesis_id: UUID, body: ThesisUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    t = await _get_thesis(thesis_id, db)
    _require_owned_editable_thesis(t, user)
    data = body.model_dump(exclude_unset=True)
    if "plagiarism_student_percent" in data and data["plagiarism_student_percent"] is not None:
        if not (0 <= data["plagiarism_student_percent"] <= 100):
            raise HTTPException(400, "Plagiarism percentage must be between 0 and 100.")
    for k, v in data.items():
        setattr(t, k, v)
    await db.commit()
    return await _thesis_dict(await _get_thesis(thesis_id, db), user, db)


# ── Documents (student-owned types) ───────────────────────────────────────────

@router.post("/{thesis_id}/documents/{document_type}", status_code=201)
async def upload_document(
    thesis_id: UUID, document_type: str, file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Upload (or replace) one of the student's own document categories. `.docx` only for
    every category in this phase (no automatic document generation yet — Section 5 of the
    confirmed rules). Each upload is a new immutable version; nothing is ever overwritten."""
    if document_type not in _STUDENT_DOCUMENT_TYPES:
        raise HTTPException(404, "Unknown document type.")
    t = await _get_thesis(thesis_id, db)
    _require_owned_editable_thesis(t, user)
    data, digest = await _validate_docx_upload(file)
    next_version = max((d.version_number for d in t.documents if d.document_type == document_type), default=0) + 1
    stored_name = f"{secrets.token_hex(16)}.docx"
    _write_stored(t.id, stored_name, data)
    doc = ThesisDocument(
        thesis_id=t.id, document_type=document_type, version_number=next_version,
        original_filename=os.path.basename((file.filename or document_type).replace("\\", "/"))[:255] or document_type,
        stored_filename=stored_name, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(data), sha256=digest, is_confidential=document_type in CONFIDENTIAL_DOCUMENT_TYPES, uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    return {"message": "Document uploaded.", "document_id": str(doc.id), "version": next_version}


@router.get("/{thesis_id}/documents/{document_id}/download")
async def download_document(
    thesis_id: UUID, document_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Streamed through this authorized endpoint only — no path or public URL is ever exposed.
    `thesis_id`/`document_id` are both re-verified against the real record (a mismatched pair,
    or a document belonging to a different Thesis, is a 404, never a 403 that would confirm
    the id exists elsewhere). The Library plagiarism report is confidential from the student —
    enforced HERE, never by omitting a frontend button."""
    t = await _get_thesis(thesis_id, db)
    await _authorize_view(t, user, db)
    doc = next((d for d in t.documents if d.id == document_id), None)
    if not doc:
        raise HTTPException(404, "Document not found.")
    if user.active_role == UserRole.STUDENT and doc.is_confidential:
        raise HTTPException(404, "Document not found.")
    if user.active_role == UserRole.EXTERNAL_EXAMINER and doc.document_type not in _EXAMINER_DOCUMENT_TYPES:
        raise HTTPException(404, "Document not found.")
    data = _read_stored(t.id, doc.stored_filename)
    return Response(
        content=data, media_type=doc.content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(doc.original_filename, safe='')}"},
    )


# ── Librarian workflow (plagiarism entry is SEPARATE from approval) ──────────

def _require_librarian_stage(t: Thesis) -> ThesisApprovalCycle:
    cycle = _active_cycle(t)
    if not cycle or t.status != "librarian_pending":
        raise HTTPException(400, "This Initial Thesis is not currently at the Librarian stage.")
    return cycle


@router.patch("/{thesis_id}/library-plagiarism")
async def set_library_plagiarism(
    thesis_id: UUID, body: LibraryPlagiarismIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.LIBRARIAN)),
):
    t = await _get_thesis(thesis_id, db)
    _require_librarian_stage(t)
    if not (0 <= body.plagiarism_library_percent <= 100):
        raise HTTPException(400, "Plagiarism percentage must be between 0 and 100.")
    t.plagiarism_library_percent = body.plagiarism_library_percent
    await db.commit()
    return {"message": "Library plagiarism percentage recorded."}


@router.post("/{thesis_id}/library-plagiarism/report", status_code=201)
async def upload_library_plagiarism_report(
    thesis_id: UUID, file: UploadFile = File(...), db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.LIBRARIAN)),
):
    t = await _get_thesis(thesis_id, db)
    _require_librarian_stage(t)
    data, digest = await _validate_docx_upload(file)
    document_type = "plagiarism_library_report"
    next_version = max((d.version_number for d in t.documents if d.document_type == document_type), default=0) + 1
    stored_name = f"{secrets.token_hex(16)}.docx"
    _write_stored(t.id, stored_name, data)
    doc = ThesisDocument(
        thesis_id=t.id, document_type=document_type, version_number=next_version,
        original_filename=os.path.basename((file.filename or document_type).replace("\\", "/"))[:255] or document_type,
        stored_filename=stored_name, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(data), sha256=digest, is_confidential=True, uploaded_by=user.id,
    )
    db.add(doc)
    await db.commit()
    return {"message": "Library plagiarism report uploaded.", "document_id": str(doc.id), "version": next_version}


# ── Submission ────────────────────────────────────────────────────────────────

@router.post("/{thesis_id}/submit")
async def submit_thesis(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    """Submit (or RE-submit after a revert): revalidates everything server-side (never trusts
    that the frontend already checked), then seeds a brand-new approval cycle starting at the
    Major Advisor. Nothing from an earlier cycle carries over."""
    t = await _get_thesis(thesis_id, db)
    if t.student_id != user.id:
        raise _not_found()
    if t.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Initial Thesis is already under approval or approved.")
    if not _latest_document(t, "thesis_file"):
        raise HTTPException(400, "Upload your Thesis File (.docx) before submitting.")
    if t.plagiarism_student_percent is None:
        raise HTTPException(400, "Enter your plagiarism percentage before submitting.")
    if not (t.plagiarism_software_name or "").strip():
        raise HTTPException(400, "Enter the plagiarism-check software name before submitting.")
    if not _latest_document(t, "plagiarism_student_report"):
        raise HTTPException(400, "Upload your plagiarism report before submitting.")
    if not (t.abstract or "").strip():
        raise HTTPException(400, "Enter your Thesis Abstract before submitting.")

    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == user.id)
    )).scalar_one_or_none()
    major = next((m for m in committee.members if m.role == "major_advisor"), None) if committee else None
    if not major or major.accepted is not True:
        raise HTTPException(400, "Your Major Advisor must be assigned and must have accepted before you can submit your Initial Thesis.")
    if not major.faculty or not major.faculty.is_active:
        raise HTTPException(400, "Your Major Advisor's account is inactive. Ask your HOD to assign a replacement.")

    next_number = (await db.execute(select(ThesisApprovalCycle.cycle_number).where(ThesisApprovalCycle.thesis_id == t.id).order_by(ThesisApprovalCycle.cycle_number.desc()).limit(1))).scalar_one_or_none()
    next_number = (next_number or 0) + 1
    now = _now()
    cycle = ThesisApprovalCycle(thesis_id=t.id, cycle_number=next_number, status="active", submitted_at=now)
    db.add(cycle)
    await db.flush()
    seq = 1
    db.add(ThesisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type="major_advisor", role_label=_STAGE_ROLE_LABELS["major_advisor"],
                               committee_member_id=major.id, assignee_id=major.faculty_id))
    for stage_type in ("hod", "librarian", "incharge_academic_cell", "dpgs", "dpgs_final"):
        seq += 1
        db.add(ThesisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type=stage_type, role_label=_STAGE_ROLE_LABELS[stage_type]))
    t.status = "major_advisor_pending"
    t.submitted_at = now
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This Initial Thesis already has an active approval cycle.")
    return {"message": "Initial Thesis submitted for approval.", "status": t.status, "cycle_number": next_number}


# ── OTP / approve / revert ────────────────────────────────────────────────────

def _send_otp_email(to: str, code: str) -> None:
    send_email(
        to, "AVFU AMS — Initial Thesis Approval OTP",
        f"Your OTP for Initial Thesis approval is: {code}\n\nThis code is valid for {_OTP_TTL_MINUTES} minutes. "
        "If you did not request this, you can safely ignore this email.",
    )


@router.get("/{thesis_id}/approval/otp")
async def request_approval_otp(thesis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    if stage.stage_type not in _SIGNATORY_STAGES:
        raise HTTPException(400, "The Incharge Academic Cell approval is a workflow action and does not need an OTP.")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    db.add(ThesisSignature(approval_stage_id=stage.id, user_id=user.id, otp_code=code, otp_expires_at=_now() + timedelta(minutes=_OTP_TTL_MINUTES)))
    await db.commit()
    _send_otp_email(user.email, code)
    response = {"message": "OTP sent to your email."}
    if _is_dev_environment():
        response["dev_otp"] = code
    return response


async def _consume_otp(stage: ThesisApprovalStage, user: User, otp: str, request: Request, db: AsyncSession) -> None:
    sig = (await db.execute(
        select(ThesisSignature).where(
            ThesisSignature.approval_stage_id == stage.id, ThesisSignature.user_id == user.id,
            ThesisSignature.otp_used == False, ThesisSignature.otp_expires_at > _now(),  # noqa: E712
        ).order_by(ThesisSignature.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sig:
        raise HTTPException(400, "No valid OTP found. Please request a new OTP.")
    if not otp or not secrets.compare_digest(otp, sig.otp_code or ""):
        raise HTTPException(400, "Invalid OTP.")
    sig.otp_used = True
    sig.verified_at = _now()
    sig.ip_address = request.client.host if request.client else None
    sig.user_agent = request.headers.get("user-agent")


async def _lock_and_reload(t: Thesis, stage: ThesisApprovalStage, cycle: ThesisApprovalCycle, db: AsyncSession) -> list[ThesisApprovalStage]:
    await db.execute(select(Thesis.id).where(Thesis.id == t.id).with_for_update())
    current_status = (await db.execute(select(Thesis.status).where(Thesis.id == t.id))).scalar_one()
    stages = list(cycle.stages)
    for st in stages:
        await db.refresh(st, attribute_names=["status"])
    mine = next((x for x in stages if x.id == stage.id), None)
    if not mine or mine.status != "pending" or _PHASE_FOR_STAGE.get(mine.stage_type) != current_status:
        raise HTTPException(409, "This Initial Thesis has already moved on; please reload.")
    t.status = current_status
    return stages


def _record_action(stage: ThesisApprovalStage, user: User, status: str, now: datetime, remark: Optional[str] = None) -> None:
    stage.status = status
    stage.approver = user
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id
    stage.acted_at = now
    if remark is not None:
        stage.remark = remark


@router.post("/{thesis_id}/approval/approve")
async def approve_stage(
    thesis_id: UUID, body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    cycle = _active_cycle(t)
    stages = await _lock_and_reload(t, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()

    if stage.stage_type == "librarian" and (t.plagiarism_library_percent is None or not any(d.document_type == "plagiarism_library_report" for d in t.documents)):
        raise HTTPException(400, "Enter the Library plagiarism percentage and upload the Library plagiarism report before approving.")

    if stage.stage_type in _SIGNATORY_STAGES:
        await _consume_otp(stage, user, body.otp or "", request, db)
    _record_action(stage, user, "approved", now)

    if stage.stage_type == "major_advisor":
        t.status = "hod_pending"
    elif stage.stage_type == "hod":
        t.status = "librarian_pending"
    elif stage.stage_type == "librarian":
        t.status = "incharge_pending"
    elif stage.stage_type == "incharge_academic_cell":
        t.status = "dpgs_pending"
    elif stage.stage_type == "dpgs":
        assignments = (await db.execute(
            select(ExternalExaminerAssignment).where(ExternalExaminerAssignment.student_id == t.student_id, ExternalExaminerAssignment.status == "active")
        )).scalars().all()
        if not assignments:
            raise HTTPException(400, "This student has no assigned External Examiner(s) yet. Complete External Examiner Selection before sending this thesis for evaluation.")
        for a in assignments:
            exists = (await db.execute(select(ThesisExternalEvaluation.id).where(ThesisExternalEvaluation.thesis_id == t.id, ThesisExternalEvaluation.assignment_id == a.id))).scalar_one_or_none()
            if not exists:
                db.add(ThesisExternalEvaluation(thesis_id=t.id, assignment_id=a.id, status="pending"))
        t.status = "external_examiner_pending"
    elif stage.stage_type == "dpgs_final":
        for ev in t.evaluations:
            if ev.status == "submitted":
                ev.status = "approved"
                ev.dpgs_approved_at = now
                ev.dpgs_approved_by = user.id
        t.status = "approved"
        t.approved_at = now
        cycle.status = "approved"
        cycle.completed_at = now

    await db.commit()
    return {"message": f"{stage.role_label} approved.", "status": t.status}


@router.post("/{thesis_id}/approval/revert")
async def revert_stage(thesis_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """Revert to the STUDENT (Synopsis's convention — see module docstring). A remark is
    REQUIRED; the cycle's other still-pending stages are marked `cancelled`."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    t = await _get_thesis(thesis_id, db)
    stage = await _require_my_stage(t, user, db)
    if stage.stage_type not in _REVERTIBLE_STAGES:
        raise HTTPException(400, "This stage cannot be reverted.")
    cycle = _active_cycle(t)
    stages = await _lock_and_reload(t, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()
    _record_action(stage, user, "reverted", now, remark)
    for other in stages:
        if other.id != stage.id and other.status == "pending":
            other.status = "cancelled"
    cycle.status = "reverted"
    cycle.reverted_at = now
    cycle.revert_remark = remark
    t.status = "reverted"
    await db.commit()
    return {"message": "Initial Thesis reverted to the student for correction.", "status": t.status}


# ── External Examiner evaluation ─────────────────────────────────────────────

@router.post("/{thesis_id}/evaluations/{evaluation_id}/report", status_code=201)
async def upload_evaluation_report(
    thesis_id: UUID, evaluation_id: UUID, file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.EXTERNAL_EXAMINER)),
):
    """The assigned External Examiner uploads their evaluation report. Authorization is based
    entirely on the actual `ExternalExaminerAssignment` chain — never on `evaluation_id`/
    `thesis_id` alone: an examiner assigned to a DIFFERENT student is blocked even if they
    hold another, unrelated assignment for this same one (Section 20 of the confirmed rules)."""
    t = await _get_thesis(thesis_id, db)
    ev = next((e for e in t.evaluations if e.id == evaluation_id), None)
    if not ev:
        raise HTTPException(404, "Evaluation not found.")
    mine = await _my_examiner_evaluation(t, user)
    if not mine or mine.id != ev.id:
        raise HTTPException(404, "Evaluation not found.")
    if t.status != "external_examiner_pending" or ev.status != "pending":
        raise HTTPException(400, "This evaluation is not currently open for submission.")
    data, digest = await _validate_docx_upload(file)
    stored_name = f"{secrets.token_hex(16)}.docx"
    _write_stored(t.id, stored_name, data)
    ev.report_stored_filename = stored_name
    ev.report_original_filename = os.path.basename((file.filename or "evaluation_report").replace("\\", "/"))[:255]
    ev.report_content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ev.report_size_bytes = len(data)
    ev.report_sha256 = digest
    ev.status = "submitted"
    ev.submitted_at = _now()
    await db.flush()
    if all(e.status in ("submitted", "approved") for e in t.evaluations):
        t.status = "dpgs_final_pending"
    await db.commit()
    return {"message": "Evaluation report submitted.", "status": ev.status}


@router.get("/{thesis_id}/evaluations/{evaluation_id}/report")
async def download_evaluation_report(
    thesis_id: UUID, evaluation_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """Visible ONLY once DPGS has approved (`status == 'approved'`) to the student and to
    every non-DPGS/Super-Admin/examiner viewer; DPGS/Super Admin/the assigned examiner may
    read it at any status (Section 21/23)."""
    t = await _get_thesis(thesis_id, db)
    await _authorize_view(t, user, db)
    ev = next((e for e in t.evaluations if e.id == evaluation_id), None)
    if not ev or not ev.report_stored_filename:
        raise HTTPException(404, "Report not found.")
    role = user.active_role
    if role == UserRole.EXTERNAL_EXAMINER:
        mine = await _my_examiner_evaluation(t, user)
        if not mine or mine.id != ev.id:
            raise HTTPException(404, "Report not found.")
    elif role not in (UserRole.DPGS, UserRole.SUPER_ADMIN):
        if ev.status != "approved":
            raise HTTPException(404, "Report not found.")
    data = _read_stored(t.id, ev.report_stored_filename)
    return Response(
        content=data, media_type=ev.report_content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(ev.report_original_filename or 'evaluation_report.docx', safe='')}"},
    )
