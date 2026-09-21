"""Synopsis of Thesis/Dissertation Problem — First Synopsis (BUSINESS_LOGIC.md section AB).

Workflow (per submission cycle):

    Student submits -> Major Advisor -> ALL other Advisory Committee members (any
    order) -> HOD -> Incharge Academic Cell -> DPGS -> APPROVED

* Every approver may APPROVE or REVERT. A revert REQUIRES a remark, returns the
  Synopsis to the student (Rule 37), and is shown to the student with who/role/
  when/remark. Resubmission starts a NEW cycle from the Major Advisor; earlier
  cycles are kept as history and never count toward a later one.
* Signatories (Major Advisor, committee members, HOD, DPGS) sign with an email OTP
  exactly like PPW; the Incharge Academic Cell is a workflow approver only — no OTP,
  no signature.
* Authorization is derived on the server from the caller's ACTIVE session role /
  department and the student's real Advisory Committee rows — never from an id in
  the request (see `_find_my_stage`, `_authorize_view`, `_authorize_edit`).
* At DPGS approval the final document is FROZEN: a JSON snapshot of everything the
  document shows plus the rendered A4 PDF (AMS-rendered pages + the student's
  uploaded PDF) are stored on the approved cycle in the same transaction. An
  approved Synopsis is served from that frozen copy only. Only a Super Admin may
  edit an approved Synopsis's live record (title / uploaded PDF, with a mandatory
  reason, written to `ams_audit_logs`); that never alters the frozen document or
  any approval/signature history.
* Only ONE First Synopsis per student, enforced by a partial unique index in the
  database. "Revise" Synopsis (a later feature) is a separate `synopsis_type`.
"""
import hashlib
import logging
import os
import re
import secrets
import string
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.api.v1.endpoints.ppw import _PPW_LOAD_OPTIONS, _ppw_dict
from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.core.email import send_email
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.audit import AuditLog
from app.models.ppw import Ppw
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.synopsis import (
    Synopsis, SynopsisApprovalCycle, SynopsisApprovalStage, SynopsisFile, SynopsisSignature,
)
from app.models.user import Department, Program, User, UserRole
from app.utils.pdf import ChromiumRenderFailed, ChromiumUnavailable, get_logo_data_uri, render_html_documents
from app.utils.pdf_merge import InvalidPdf, inspect_pdf, merge_synopsis_package

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/synopsis", tags=["Synopsis"])

UNIVERSITY_NAME = "Assam Veterinary And Fishery University"
_PG_LEVELS = ("PG", "PhD")
_EDITABLE_STATUSES = ("draft", "reverted")
_APPROVER_ROLES = (UserRole.FACULTY, UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)
# A committee stage belongs to a PERSON (their Advisory Committee membership); they may act on it
# while their active session role is a faculty-type role. Global roles / Student / Super Admin may not.
_COMMITTEE_ACTING_ROLES = (UserRole.FACULTY, UserRole.HOD)
_SIGNATORY_STAGES = ("major_advisor", "committee_member", "hod", "dpgs")  # OTP-verified signatures

_PHASE_FOR_STAGE = {
    "major_advisor": "major_advisor_pending",
    "committee_member": "committee_pending",
    "hod": "hod_pending",
    "incharge_academic_cell": "incharge_pending",
    "dpgs": "dpgs_pending",
}
_STATUS_LABELS = {
    "draft": "Draft",
    "major_advisor_pending": "Major Advisor Approval Pending",
    "committee_pending": "Advisory Committee Approval Pending",
    "hod_pending": "HOD Approval Pending",
    "incharge_pending": "Incharge Academic Cell Approval Pending",
    "dpgs_pending": "DPGS Approval Pending",
    "approved": "DPGS Approved (Final)",
    "reverted": "Reverted",
}
_ADVISORY_LABELS = {
    "major_advisor": "Major Advisor",
    "co_major_advisor": "Co-Major Advisor",
    "member_major": "Member Major",
    "member_minor": "Member Minor",
    "supporting": "Supporting",
    "member_of_others": "Members from Others",
    "member": "Member",
}
_ADVISORY_ORDER = ["co_major_advisor", "member_major", "member_minor", "supporting", "member_of_others", "member"]
_ROLE_DISPLAY = {"hod": "HOD", "faculty": "Faculty", "incharge_academic_cell": "Incharge Academic Cell", "dpgs": "DPGS", "super_admin": "Super Admin"}
_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.pdf$|^final-[0-9a-f]{32}\.pdf$")
_ALLOWED_CONTENT_TYPES = {"", "application/pdf", "application/x-pdf", "application/octet-stream"}
_OTP_TTL_MINUTES = 10
_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
_jinja_env = None


# ── Schemas ──────────────────────────────────────────────────────────────────

class SynopsisCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = None


class SynopsisUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = None
    reason: Optional[str] = None   # required only for the Super Admin override on an approved Synopsis


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp: Optional[str] = None


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    remark: str


# ── Small helpers ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _pretty(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%d-%b-%Y %H:%M") if dt else None


def _not_found() -> HTTPException:
    return HTTPException(404, "Synopsis not found.")


def _advisory_label(role: str) -> str:
    return _ADVISORY_LABELS.get(role, role.replace("_", " ").title())


def _person_name(u: Optional[User]) -> Optional[str]:
    if not u:
        return None
    return " ".join(p for p in (u.title, u.full_name) if p)


def _name_designation(u: Optional[User]) -> str:
    if not u:
        return "—"
    return f"{_person_name(u)} ({u.designation})" if u.designation else _person_name(u)


def _is_dev_environment() -> bool:
    return settings.ENVIRONMENT.strip().lower() == "development"


_LOAD_OPTIONS = (
    selectinload(Synopsis.student).selectinload(User.program),
    selectinload(Synopsis.student).selectinload(User.department),
    selectinload(Synopsis.student).selectinload(User.college),
    selectinload(Synopsis.files),
    selectinload(Synopsis.cycles).selectinload(SynopsisApprovalCycle.file),
    selectinload(Synopsis.cycles).selectinload(SynopsisApprovalCycle.stages).selectinload(SynopsisApprovalStage.committee_member),
    selectinload(Synopsis.cycles).selectinload(SynopsisApprovalCycle.stages).selectinload(SynopsisApprovalStage.assignee),
    selectinload(Synopsis.cycles).selectinload(SynopsisApprovalCycle.stages).selectinload(SynopsisApprovalStage.approver),
)


async def _get_synopsis(synopsis_id: UUID, db: AsyncSession) -> Synopsis:
    """Load a Synopsis with everything the serializers need. `populate_existing` so a re-read
    after an edit/approval never returns stale relationship state."""
    result = await db.execute(
        select(Synopsis).options(*_LOAD_OPTIONS).where(Synopsis.id == synopsis_id).execution_options(populate_existing=True)
    )
    s = result.scalar_one_or_none()
    if not s:
        raise _not_found()
    return s


def _latest_cycle(s: Synopsis) -> Optional[SynopsisApprovalCycle]:
    return s.cycles[-1] if s.cycles else None


def _active_cycle(s: Synopsis) -> Optional[SynopsisApprovalCycle]:
    c = _latest_cycle(s)
    return c if c and c.status == "active" else None


def _latest_file(s: Synopsis) -> Optional[SynopsisFile]:
    return s.files[-1] if s.files else None


def _document_file(s: Synopsis) -> Optional[SynopsisFile]:
    """The uploaded PDF currently in effect: the file pinned by the ACTIVE cycle (no upload can happen during
    approval, so it is also the newest), otherwise the newest upload. For an approved Synopsis this is the live
    record's current file — the frozen document always embeds `cycle.file`, the exact file that was approved."""
    c = _latest_cycle(s)
    if c and c.status == "active":
        return c.file
    return _latest_file(s)


# ── File storage (server-generated names; the client never sees a path) ──────

def _synopsis_dir(synopsis_id: UUID) -> Path:
    return Path(settings.UPLOAD_DIR) / "synopsis" / str(synopsis_id)


def _stored_path(synopsis_id: UUID, stored_filename: str) -> Path:
    if not _STORED_NAME_RE.match(stored_filename or ""):
        raise HTTPException(500, "Invalid stored file reference.")
    return _synopsis_dir(synopsis_id) / stored_filename


def _read_stored(synopsis_id: UUID, stored_filename: str) -> bytes:
    path = _stored_path(synopsis_id, stored_filename)
    if not path.is_file():
        raise HTTPException(404, "The stored file is missing on the server.")
    return path.read_bytes()


def _write_stored(synopsis_id: UUID, stored_filename: str, data: bytes) -> None:
    directory = _synopsis_dir(synopsis_id)
    directory.mkdir(parents=True, exist_ok=True)
    with open(_stored_path(synopsis_id, stored_filename), "xb") as f:
        f.write(data)


def _unlink_quietly(synopsis_id: UUID, stored_filename: Optional[str]) -> None:
    if not stored_filename:
        return
    try:
        _stored_path(synopsis_id, stored_filename).unlink(missing_ok=True)
    except (OSError, HTTPException):  # noqa: BLE001 - best-effort cleanup only
        logger.warning("Could not remove stored Synopsis file %s", stored_filename)


async def _validate_pdf_upload(upload: UploadFile) -> tuple[bytes, int, str]:
    """PDF ONLY: extension, declared content type, real `%PDF-` signature, size limit and a real
    parse. Returns (bytes, page_count, sha256)."""
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
        pages = inspect_pdf(data)
    except InvalidPdf as exc:
        raise HTTPException(400, str(exc)) from exc
    return data, pages, hashlib.sha256(data).hexdigest()


def _clean_filename(name: Optional[str]) -> str:
    base = os.path.basename((name or "synopsis.pdf").replace("\\", "/"))
    base = "".join(ch for ch in base if ch.isprintable())[:255].strip()
    return base or "synopsis.pdf"


# ── Authorization ────────────────────────────────────────────────────────────

async def _authorize_view(s: Synopsis, user: User, db: AsyncSession) -> None:
    """Who may READ a Synopsis (its data, uploaded file and document). Anything else is a 404 so the
    existence of another student's Synopsis is never revealed. Drafts are private to the student
    (and Super Admin); everyone else sees a Synopsis only once it has been submitted."""
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return
    if role == UserRole.STUDENT:
        if s.student_id == user.id:
            return
        raise _not_found()
    if s.status == "draft":
        raise _not_found()
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
        return
    if role in (UserRole.FACULTY, UserRole.HOD):
        # Anyone who was ever assigned a stage on this Synopsis (past-cycle approvers keep read access).
        assigned = await db.execute(
            select(SynopsisApprovalStage.id)
            .join(SynopsisApprovalCycle, SynopsisApprovalCycle.id == SynopsisApprovalStage.cycle_id)
            .where(SynopsisApprovalCycle.synopsis_id == s.id, SynopsisApprovalStage.assignee_id == user.id)
            .limit(1)
        )
        if assigned.scalar_one_or_none():
            return
    if role == UserRole.HOD:
        dept_id = await resolve_student_department_id(s.student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
    raise _not_found()


def _authorize_edit(s: Synopsis, user: User) -> bool:
    """May `user` change this Synopsis's title / uploaded PDF? Returns True when it is the audited
    Super Admin override (approved Synopsis), False for the student's own edit. Raises otherwise."""
    if user.active_role == UserRole.STUDENT:
        if s.student_id != user.id:
            raise _not_found()
        if s.status not in _EDITABLE_STATUSES:
            raise HTTPException(400, "This Synopsis is under approval or approved and can no longer be edited.")
        return False
    if user.active_role == UserRole.SUPER_ADMIN:
        if s.status == "approved":
            return True
        raise HTTPException(400, "A Super Admin may edit a Synopsis only after it has been approved; until then only the student can.")
    raise HTTPException(403, "Insufficient permissions.")


async def _committee_id(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    return (await db.execute(select(AdvisoryCommittee.id).where(AdvisoryCommittee.student_id == student_id))).scalar_one_or_none()


async def _find_my_stage(s: Synopsis, user: User, db: AsyncSession) -> Optional[SynopsisApprovalStage]:
    """The SOLE authorization check for every approval action: the caller's own currently-actionable
    stage in the ACTIVE cycle, or None. Never a bare role match and never an id from the request:

    * Major Advisor / committee stage — the caller's ACTIVE session role is faculty-type AND
      the stage's `CommitteeMember` row belongs to THIS student's Advisory Committee, has
      `faculty_id == caller`, and has the right kind (major advisor vs. other member), re-checked live.
    * HOD — active role HOD AND the caller's ACTIVE assignment department == the student's department.
    * Incharge / DPGS — the caller's active role.
    A stage is only actionable while the Synopsis is in that stage's phase, so none can be skipped."""
    cycle = _active_cycle(s)
    if not cycle:
        return None
    committee_id = None
    for stage in cycle.stages:
        if stage.status != "pending" or _PHASE_FOR_STAGE.get(stage.stage_type) != s.status:
            continue
        if stage.stage_type in ("major_advisor", "committee_member"):
            if user.active_role not in _COMMITTEE_ACTING_ROLES:
                continue
            member = stage.committee_member
            if not member or member.faculty_id != user.id:
                continue
            is_major = member.role == "major_advisor"
            if is_major != (stage.stage_type == "major_advisor"):
                continue
            if committee_id is None:
                committee_id = await _committee_id(s.student_id, db)
            if committee_id and member.committee_id == committee_id:
                return stage
        elif stage.stage_type == "hod":
            if user.active_role == UserRole.HOD and user.active_department_id:
                dept_id = await resolve_student_department_id(s.student_id, db)
                if dept_id and dept_id == user.active_department_id:
                    return stage
        elif stage.stage_type == "incharge_academic_cell":
            if user.active_role == UserRole.INCHARGE_ACADEMIC_CELL:
                return stage
        elif stage.stage_type == "dpgs":
            if user.active_role == UserRole.DPGS:
                return stage
    return None


async def _require_my_stage(s: Synopsis, user: User, db: AsyncSession) -> SynopsisApprovalStage:
    stage = await _find_my_stage(s, user, db)
    if not stage:
        raise HTTPException(403, "You have no pending Synopsis approval action.")
    return stage


# ── Serialization / document context ─────────────────────────────────────────

async def _student_block(student: User, db: AsyncSession) -> dict:
    """Everything the Synopsis shows about the student, read from the canonical sources and never
    typed by the student: name/roll from `User`, programme from `User.program`, Major discipline
    from the student's own department, Minor/Supporting discipline from the student's PPW (the
    existing PPW derivation), College from `User.college_id` (no Orientation fallback)."""
    ppw = (await db.execute(select(Ppw).options(*_PPW_LOAD_OPTIONS).where(Ppw.student_id == student.id))).scalar_one_or_none()
    minor = supporting = None
    if ppw:
        derived = _ppw_dict(ppw)
        minor, supporting = derived["minor_discipline_name"], derived["supporting_discipline_name"]
    return {
        "student_name": student.full_name,
        "student_roll": student.student_roll,
        "program_name": student.program.name if student.program else None,
        "program_level": student.program.level if student.program else None,
        "major_discipline": student.department.name if student.department else None,
        "minor_discipline": minor,
        "supporting_discipline": supporting,
        "college_name": student.college.name if student.college else None,
    }


def _stage_dict(stage: SynopsisApprovalStage, dept_names: dict) -> dict:
    return {
        "sequence": stage.sequence,
        "stage_type": stage.stage_type,
        "role_label": stage.role_label,
        "assigned_to": _name_designation(stage.assignee) if stage.assignee else None,
        "status": stage.status,
        "acted_by": _name_designation(stage.approver) if stage.approver else None,
        "acted_role": _ROLE_DISPLAY.get(stage.acted_role or "", stage.acted_role),
        "acted_department": dept_names.get(stage.acted_department_id),
        "acted_at": _iso(stage.acted_at),
        "remark": stage.remark,
        "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
    }


def _file_dict(f: Optional[SynopsisFile]) -> Optional[dict]:
    if not f:
        return None
    return {
        "id": str(f.id), "version": f.version_number, "original_filename": f.original_filename,
        "size_bytes": f.size_bytes, "page_count": f.page_count, "uploaded_at": _iso(f.uploaded_at),
    }


def _single_stage_view(cycle: Optional[SynopsisApprovalCycle], stage_type: str, dept_names: dict) -> dict:
    stage = next((st for st in cycle.stages if st.stage_type == stage_type), None) if cycle else None
    if not stage:
        return {"status": "not_submitted", "approver_name": None, "department_name": None, "acted_at": None, "remark": None}
    return {
        "status": stage.status,
        "approver_name": _person_name(stage.approver),
        "department_name": dept_names.get(stage.acted_department_id),
        "acted_at": _pretty(stage.acted_at),
        "remark": stage.remark,
    }


async def _live_committee_rows(student_id: UUID, db: AsyncSession) -> list[dict]:
    """Preview rows from the student's actual committee (used before any submission)."""
    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == student_id)
    )).scalar_one_or_none()
    if not committee:
        return []
    members = sorted(committee.members, key=lambda m: (m.role != "major_advisor", _ADVISORY_ORDER.index(m.role) if m.role in _ADVISORY_ORDER else 99, m.invited_at))
    return [{
        "sl_no": i, "name_designation": _name_designation(m.faculty), "advisory": _advisory_label(m.role),
        "status": "not_submitted", "acted_at": None, "remark": None,
    } for i, m in enumerate(members, start=1)]


async def _build_document_context(s: Synopsis, db: AsyncSession) -> dict:
    """The complete, JSON-serializable content of the generated document. The same dict is (a) the live
    render input while a Synopsis is in progress and (b) the frozen snapshot stored at final approval."""
    dept_names = await _department_names(db)
    cycle = _latest_cycle(s)
    block = await _student_block(s.student, db)
    if cycle:
        rows = [{
            "sl_no": i,
            "name_designation": _name_designation(st.assignee),
            "advisory": st.role_label,
            "status": st.status,
            "acted_at": _pretty(st.acted_at),
            "remark": st.remark,
        } for i, st in enumerate((x for x in cycle.stages if x.stage_type in ("major_advisor", "committee_member")), start=1)]
    else:
        rows = await _live_committee_rows(s.student_id, db)
    file = _document_file(s)
    title = cycle.title_snapshot if (s.status == "approved" and cycle) else s.title
    return {
        "university": UNIVERSITY_NAME,
        **block,
        "title": title,
        "status": s.status,
        "status_label": _STATUS_LABELS.get(s.status, s.status),
        "cycle_number": cycle.cycle_number if cycle else None,
        "committee_rows": rows,
        "hod": _single_stage_view(cycle, "hod", dept_names),
        "incharge": _single_stage_view(cycle, "incharge_academic_cell", dept_names),
        "dpgs": _single_stage_view(cycle, "dpgs", dept_names),
        "approved_at": _pretty(s.approved_at),
        "file": {"page_count": file.page_count, "sha256": file.sha256, "original_filename": file.original_filename} if file else None,
    }


async def _department_names(db: AsyncSession) -> dict:
    return {i: n for i, n in (await db.execute(select(Department.id, Department.name))).all()}


def _jinja():
    global _jinja_env
    if _jinja_env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        _jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))
    return _jinja_env


def _render_html(part: str, context: dict, generated_at: datetime) -> str:
    return _jinja().get_template("synopsis_document.html").render(
        d=_DotDict(context), part=part, logo_data_uri=get_logo_data_uri(), generated_at=generated_at,
    )


class _DotDict(dict):
    """Attribute access for the template (`d.hod.status`) over the plain JSON-serializable context."""
    def __getattr__(self, name):
        value = self.get(name)
        return _DotDict(value) if isinstance(value, dict) else value


def _render_package(context: dict, uploaded_pdf: bytes) -> bytes:
    """AMS-rendered front + approval pages (one Chromium launch, A4) with the student's uploaded PDF
    merged between them (fitted to A4). Synchronous — call from a threadpool."""
    generated_at = _now()
    front, approval = render_html_documents([
        _render_html("front", context, generated_at), _render_html("approval", context, generated_at),
    ])
    return merge_synopsis_package(front, uploaded_pdf, approval, title=f"Synopsis - {context.get('student_name', '')}")


async def _render_or_503(context: dict, uploaded_pdf: bytes) -> bytes:
    try:
        return await run_in_threadpool(_render_package, context, uploaded_pdf)
    except ChromiumUnavailable as exc:
        logger.error("Synopsis document generation unavailable: %s", exc)
        raise HTTPException(503, "PDF generation service is currently unavailable.")
    except ChromiumRenderFailed as exc:
        logger.error("Synopsis document render failed: %s", exc)
        raise HTTPException(422, "Unable to generate the Synopsis document.")


async def _synopsis_dict(s: Synopsis, viewer: User, db: AsyncSession) -> dict:
    dept_names = await _department_names(db)
    latest = _latest_cycle(s)
    is_owner = viewer.active_role == UserRole.STUDENT and s.student_id == viewer.id
    current_file = _document_file(s)
    my_stage = await _find_my_stage(s, viewer, db) if viewer.active_role in _APPROVER_ROLES else None

    revert_info = None
    if s.status == "reverted" and latest and latest.status == "reverted":
        stage = next((st for st in latest.stages if st.status == "reverted"), None)
        revert_info = {
            "cycle_number": latest.cycle_number,
            "reverted_by": _name_designation(stage.approver) if stage and stage.approver else None,
            "role": stage.role_label if stage else None,
            "acting_as": _ROLE_DISPLAY.get(stage.acted_role or "", stage.acted_role) if stage else None,
            "department": dept_names.get(stage.acted_department_id) if stage else None,
            "reverted_at": _iso(latest.reverted_at),
            "remark": latest.revert_remark,
        }

    if latest:
        committee_rows = [_stage_dict(st, dept_names) for st in latest.stages if st.stage_type in ("major_advisor", "committee_member")]
        approvals = [_stage_dict(st, dept_names) for st in latest.stages if st.stage_type not in ("major_advisor", "committee_member")]
    else:
        committee_rows = [{"sequence": r["sl_no"], "stage_type": "preview", "role_label": r["advisory"], "assigned_to": r["name_designation"],
                           "status": "not_submitted", "acted_by": None, "acted_role": None, "acted_department": None, "acted_at": None,
                           "remark": None, "requires_otp": True} for r in await _live_committee_rows(s.student_id, db)]
        approvals = []

    return {
        "id": str(s.id),
        "synopsis_type": s.synopsis_type,
        "status": s.status,
        "status_label": _STATUS_LABELS.get(s.status, s.status),
        "title": s.title,
        "submitted_at": _iso(s.submitted_at),
        "approved_at": _iso(s.approved_at),
        "student": await _student_block(s.student, db),
        "file": _file_dict(current_file),
        "is_owner": is_owner,
        "can_edit": is_owner and s.status in _EDITABLE_STATUSES,
        "can_admin_edit": viewer.active_role == UserRole.SUPER_ADMIN and s.status == "approved",
        "current_cycle_number": latest.cycle_number if latest else None,
        "committee": committee_rows,
        "approvals": approvals,
        "revert_info": revert_info,
        "frozen_document": bool(s.status == "approved" and latest and latest.frozen_pdf_filename),
        "my_pending_stage": (
            {"stage_type": my_stage.stage_type, "role_label": my_stage.role_label, "requires_otp": my_stage.stage_type in _SIGNATORY_STAGES}
            if my_stage else None
        ),
        "history": [{
            "cycle_number": c.cycle_number,
            "status": c.status,
            "submitted_at": _iso(c.submitted_at),
            "completed_at": _iso(c.completed_at),
            "reverted_at": _iso(c.reverted_at),
            "revert_remark": c.revert_remark,
            "title": c.title_snapshot,
            "file": _file_dict(c.file),
            "stages": [_stage_dict(st, dept_names) for st in c.stages],
        } for c in s.cycles],
    }


# ── Create / read ────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_first_synopsis(
    body: SynopsisCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """A student creates their ONE First Synopsis. The student is always the caller — no student id is
    accepted. The database's partial unique index is the final guarantee; the pre-check below only gives
    a friendly message."""
    program = await db.get(Program, user.program_id) if user.program_id else None
    if not program or program.level not in _PG_LEVELS:
        raise HTTPException(403, "The Synopsis is available to postgraduate students only.")
    existing = (await db.execute(
        select(Synopsis.id).where(Synopsis.student_id == user.id, Synopsis.synopsis_type == "first")
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "You already have a First Synopsis. A student can have only one.")
    title = (body.title or "").strip() or None
    s = Synopsis(student_id=user.id, synopsis_type="first", title=title, status="draft")
    db.add(s)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "You already have a First Synopsis. A student can have only one.")
    return {"id": str(s.id), "message": "Synopsis draft created."}


@router.get("/me")
async def get_my_synopsis(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT))):
    s = (await db.execute(
        select(Synopsis.id).where(Synopsis.student_id == user.id, Synopsis.synopsis_type == "first")
    )).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "You have not created a Synopsis yet.")
    return await _synopsis_dict(await _get_synopsis(s, db), user, db)


@router.get("/all")
async def list_all_synopses(db: AsyncSession = Depends(get_db), _: User = Depends(require_roles(UserRole.SUPER_ADMIN))):
    """Super Admin oversight list (needed to reach an approved Synopsis for the audited override)."""
    rows = (await db.execute(
        select(Synopsis).options(selectinload(Synopsis.student).selectinload(User.program)).order_by(Synopsis.created_at.desc()).limit(500)
    )).scalars().all()
    return [{
        "id": str(s.id), "student_name": s.student.full_name if s.student else None,
        "student_roll": s.student.student_roll if s.student else None, "title": s.title,
        "status": s.status, "status_label": _STATUS_LABELS.get(s.status, s.status), "synopsis_type": s.synopsis_type,
        "submitted_at": _iso(s.submitted_at), "approved_at": _iso(s.approved_at),
    } for s in rows]


@router.get("/pending-approvals")
async def list_pending_approvals(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES))):
    """"My Synopsis approvals" inbox — every Synopsis whose CURRENT stage the caller (in their active
    session role) can act on. Candidates are pre-filtered in SQL and each is confirmed with the same
    `_find_my_stage` check the approve/revert endpoints use."""
    ids: set[UUID] = set()
    if user.active_role in _COMMITTEE_ACTING_ROLES:
        ids.update((await db.execute(
            select(Synopsis.id)
            .join(SynopsisApprovalCycle, SynopsisApprovalCycle.synopsis_id == Synopsis.id)
            .join(SynopsisApprovalStage, SynopsisApprovalStage.cycle_id == SynopsisApprovalCycle.id)
            .where(SynopsisApprovalCycle.status == "active", SynopsisApprovalStage.status == "pending",
                   SynopsisApprovalStage.assignee_id == user.id)
        )).scalars().all())
    phase = {UserRole.HOD: "hod_pending", UserRole.INCHARGE_ACADEMIC_CELL: "incharge_pending", UserRole.DPGS: "dpgs_pending"}.get(user.active_role)
    if phase:
        q = select(Synopsis.id).where(Synopsis.status == phase)
        if user.active_role == UserRole.HOD:
            if not user.active_department_id:
                return []
            q = q.join(User, User.id == Synopsis.student_id).where(User.department_id == user.active_department_id)
        ids.update((await db.execute(q)).scalars().all())

    rows = []
    for sid in ids:
        s = await _get_synopsis(sid, db)
        stage = await _find_my_stage(s, user, db)
        if not stage:
            continue
        cycle = _active_cycle(s)
        rows.append({
            "synopsis_id": str(s.id),
            "student_name": s.student.full_name,
            "student_roll": s.student.student_roll,
            "program_name": s.student.program.name if s.student.program else None,
            "department_name": s.student.department.name if s.student.department else None,
            "title": s.title,
            "status": s.status,
            "status_label": _STATUS_LABELS.get(s.status, s.status),
            "acting_as": stage.role_label,
            "requires_otp": stage.stage_type in _SIGNATORY_STAGES,
            "cycle_number": cycle.cycle_number if cycle else None,
            "submitted_at": _iso(cycle.submitted_at) if cycle else None,
        })
    rows.sort(key=lambda r: r["submitted_at"] or "")
    return rows


@router.get("/{synopsis_id}")
async def get_synopsis(synopsis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    s = await _get_synopsis(synopsis_id, db)
    await _authorize_view(s, user, db)
    return await _synopsis_dict(s, user, db)


# ── Edit (title / PDF) ───────────────────────────────────────────────────────

def _audit(db: AsyncSession, request: Request, user: User, s: Synopsis, action: str, old: dict, new: dict, reason: str) -> None:
    db.add(AuditLog(
        user_id=user.id, action=action, entity_type="synopsis", entity_id=str(s.id), old_value=old, new_value=new,
        metadata_={"reason": reason, "frozen_document_unchanged": True, "student_id": str(s.student_id)},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"), role_context=user.active_role.value,
    ))


def _require_reason(reason: Optional[str]) -> str:
    if not reason or not reason.strip():
        raise HTTPException(400, "A reason is required to edit an approved Synopsis.")
    return reason.strip()


@router.patch("/{synopsis_id}")
async def update_synopsis(
    synopsis_id: UUID, body: SynopsisUpdate, request: Request, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.STUDENT, UserRole.SUPER_ADMIN)),
):
    s = await _get_synopsis(synopsis_id, db)
    override = _authorize_edit(s, user)
    if body.title is None:
        raise HTTPException(400, "Nothing to update.")
    new_title = body.title.strip() or None
    if override:
        reason = _require_reason(body.reason)
        if not new_title:
            raise HTTPException(400, "The title of an approved Synopsis cannot be blank.")
        _audit(db, request, user, s, "synopsis.admin_edit_title", {"title": s.title}, {"title": new_title}, reason)
    s.title = new_title
    await db.commit()
    return await _synopsis_dict(await _get_synopsis(synopsis_id, db), user, db)


@router.post("/{synopsis_id}/file", status_code=201)
async def upload_synopsis_file(
    synopsis_id: UUID, request: Request, file: UploadFile = File(...), reason: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT, UserRole.SUPER_ADMIN)),
):
    """Upload (or replace) the Synopsis PDF. PDF only — see `_validate_pdf_upload`. Each upload is a new
    immutable version; a superseded version that no approval cycle references is removed."""
    s = await _get_synopsis(synopsis_id, db)
    override = _authorize_edit(s, user)
    reason_text = _require_reason(reason) if override else None
    data, pages, digest = await _validate_pdf_upload(file)

    version = (max((f.version_number for f in s.files), default=0)) + 1
    stored_name = f"{uuid.uuid4().hex}.pdf"
    referenced = {c.file_id for c in s.cycles}
    obsolete = [f for f in s.files if f.id not in referenced]
    previous = _latest_file(s)

    _write_stored(s.id, stored_name, data)
    new_file = SynopsisFile(
        synopsis_id=s.id, version_number=version, original_filename=_clean_filename(file.filename), stored_filename=stored_name,
        content_type="application/pdf", size_bytes=len(data), sha256=digest, page_count=pages, uploaded_by=user.id,
    )
    db.add(new_file)
    for old in obsolete:
        await db.delete(old)
    if override:
        _audit(db, request, user, s, "synopsis.admin_replace_file",
               {"file": _file_dict(previous)}, {"file": {"version": version, "original_filename": new_file.original_filename, "sha256": digest}}, reason_text)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        _unlink_quietly(s.id, stored_name)
        raise HTTPException(409, "Another upload was in progress. Please try again.")
    for old in obsolete:
        _unlink_quietly(s.id, old.stored_filename)
    return await _synopsis_dict(await _get_synopsis(synopsis_id, db), user, db)


@router.get("/{synopsis_id}/file")
async def download_synopsis_file(
    synopsis_id: UUID, file_id: Optional[UUID] = None, inline: bool = False,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """The uploaded PDF, streamed through this authorized endpoint (no path is ever exposed). `file_id`,
    if given, must belong to THIS Synopsis."""
    s = await _get_synopsis(synopsis_id, db)
    await _authorize_view(s, user, db)
    chosen = _document_file(s)
    if file_id is not None:
        chosen = next((f for f in s.files if f.id == file_id), None)
        if not chosen:
            raise HTTPException(404, "File not found.")
    if not chosen:
        raise HTTPException(404, "No PDF has been uploaded yet.")
    data = _read_stored(s.id, chosen.stored_filename)
    filename = urllib.parse.quote(chosen.original_filename, safe="")
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{filename}"},
    )


# ── Submit (starts a new approval cycle) ─────────────────────────────────────

_COMMITTEE_ORDER = lambda m: (_ADVISORY_ORDER.index(m.role) if m.role in _ADVISORY_ORDER else 99, m.invited_at)  # noqa: E731


@router.patch("/{synopsis_id}/submit")
async def submit_synopsis(
    synopsis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Submit (or RE-submit after a revert): validates the Synopsis and the student's real Advisory
    Committee, then seeds a brand-new approval cycle — one stage per person — starting at the Major
    Advisor. Nothing from any earlier cycle carries over."""
    s = await _get_synopsis(synopsis_id, db)
    if s.student_id != user.id:
        raise _not_found()
    if s.status not in _EDITABLE_STATUSES:
        raise HTTPException(400, "This Synopsis is already under approval or approved.")
    if not (s.title and s.title.strip()):
        raise HTTPException(400, "Enter the Title of the Research Problem before submitting.")
    file = _latest_file(s)
    if not file:
        raise HTTPException(400, "Upload your Synopsis PDF before submitting.")
    if not _stored_path(s.id, file.stored_filename).is_file():
        raise HTTPException(400, "Your uploaded PDF is missing on the server; please upload it again.")

    committee = (await db.execute(
        select(AdvisoryCommittee).options(selectinload(AdvisoryCommittee.members).selectinload(CommitteeMember.faculty))
        .where(AdvisoryCommittee.student_id == user.id)
    )).scalar_one_or_none()
    if not committee:
        raise HTTPException(400, "You need an Advisory Committee before you can submit your Synopsis.")
    major = next((m for m in committee.members if m.role == "major_advisor"), None)
    if not major or major.accepted is not True:
        raise HTTPException(400, "Your Major Advisor must be assigned and must have accepted before you can submit your Synopsis.")
    others = sorted((m for m in committee.members if m.role != "major_advisor"), key=_COMMITTEE_ORDER)
    waiting = [_person_name(m.faculty) for m in others if m.accepted is not True]
    if waiting:
        raise HTTPException(400, "Every Advisory Committee member must have accepted before you can submit. Waiting on: " + ", ".join(waiting) + ".")
    inactive = [_person_name(m.faculty) for m in [major, *others] if not m.faculty or not m.faculty.is_active]
    if inactive:
        raise HTTPException(400, "These committee members are inactive and cannot approve: " + ", ".join(inactive) + ". Ask your HOD/Major Advisor to replace them.")

    next_number = (max((c.cycle_number for c in s.cycles), default=0)) + 1
    now = _now()
    cycle = SynopsisApprovalCycle(
        synopsis_id=s.id, cycle_number=next_number, file_id=file.id, title_snapshot=s.title.strip(), status="active", submitted_at=now,
    )
    db.add(cycle)
    await db.flush()
    seq = 1
    db.add(SynopsisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type="major_advisor", role_label=_advisory_label("major_advisor"),
                                 committee_member_id=major.id, assignee_id=major.faculty_id))
    for m in others:
        seq += 1
        db.add(SynopsisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type="committee_member", role_label=_advisory_label(m.role),
                                     committee_member_id=m.id, assignee_id=m.faculty_id))
    for stage_type, label in (("hod", "Head of the Department"), ("incharge_academic_cell", "Incharge Academic Cell"), ("dpgs", "DPGS")):
        seq += 1
        db.add(SynopsisApprovalStage(cycle_id=cycle.id, sequence=seq, stage_type=stage_type, role_label=label))
    s.status = "major_advisor_pending"
    s.submitted_at = now
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "This Synopsis already has an active approval cycle.")
    return {"message": "Synopsis submitted for approval.", "status": s.status, "cycle_number": next_number}


# ── OTP / approve / revert ───────────────────────────────────────────────────

def _send_otp_email(to: str, code: str) -> None:
    send_email(
        to, "AVFU AMS — Synopsis Approval OTP",
        f"Your OTP for Synopsis approval is: {code}\n\nThis code is valid for {_OTP_TTL_MINUTES} minutes. "
        "If you did not request this, you can safely ignore this email.",
    )


@router.get("/{synopsis_id}/approval/otp")
async def request_approval_otp(
    synopsis_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    s = await _get_synopsis(synopsis_id, db)
    stage = await _require_my_stage(s, user, db)
    if stage.stage_type not in _SIGNATORY_STAGES:
        raise HTTPException(400, "The Incharge Academic Cell approval is a workflow action and does not need an OTP.")
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    db.add(SynopsisSignature(approval_stage_id=stage.id, user_id=user.id, otp_code=code, otp_expires_at=_now() + timedelta(minutes=_OTP_TTL_MINUTES)))
    await db.commit()
    _send_otp_email(user.email, code)
    response = {"message": "OTP sent to your email."}
    if _is_dev_environment():
        response["dev_otp"] = code   # development convenience only; never returned unless ENVIRONMENT == "development"
    return response


async def _consume_otp(stage: SynopsisApprovalStage, user: User, otp: str, request: Request, db: AsyncSession) -> None:
    sig = (await db.execute(
        select(SynopsisSignature).where(
            SynopsisSignature.approval_stage_id == stage.id, SynopsisSignature.user_id == user.id,
            SynopsisSignature.otp_used == False, SynopsisSignature.otp_expires_at > _now(),  # noqa: E712
        ).order_by(SynopsisSignature.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sig:
        raise HTTPException(400, "No valid OTP found. Please request a new OTP.")
    if not otp or not secrets.compare_digest(otp, sig.otp_code or ""):
        raise HTTPException(400, "Invalid OTP.")
    sig.otp_used = True
    sig.verified_at = _now()
    sig.ip_address = request.client.host if request.client else None
    sig.user_agent = request.headers.get("user-agent")


async def _lock_and_reload(s: Synopsis, stage: SynopsisApprovalStage, cycle: SynopsisApprovalCycle, db: AsyncSession) -> list[SynopsisApprovalStage]:
    """Serialize concurrent actions on one Synopsis: take a row lock, then re-read the Synopsis status and each
    stage's status from the database (never the copies loaded before the lock) and re-validate that the caller's
    stage is still the current pending one. Only the `status` column is refreshed, so loaded relationships stay
    usable."""
    await db.execute(select(Synopsis.id).where(Synopsis.id == s.id).with_for_update())
    current_status = (await db.execute(select(Synopsis.status).where(Synopsis.id == s.id))).scalar_one()
    stages = list(cycle.stages)
    for st in stages:
        await db.refresh(st, attribute_names=["status"])
    mine = next((x for x in stages if x.id == stage.id), None)
    if not mine or mine.status != "pending" or _PHASE_FOR_STAGE.get(mine.stage_type) != current_status:
        raise HTTPException(409, "This Synopsis has already moved on; please reload.")
    s.status = current_status
    return stages


def _record_action(stage: SynopsisApprovalStage, user: User, status: str, now: datetime, remark: Optional[str] = None) -> None:
    stage.status = status
    stage.approver = user
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id
    stage.acted_at = now
    if remark is not None:
        stage.remark = remark


@router.post("/{synopsis_id}/approval/approve")
async def approve_synopsis_stage(
    synopsis_id: UUID, body: ApproveIn, request: Request, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    s = await _get_synopsis(synopsis_id, db)
    stage = await _require_my_stage(s, user, db)
    cycle = _active_cycle(s)
    stages = await _lock_and_reload(s, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()

    if stage.stage_type in _SIGNATORY_STAGES:
        await _consume_otp(stage, user, body.otp or "", request, db)
    _record_action(stage, user, "approved", now)

    if stage.stage_type == "major_advisor":
        s.status = "committee_pending" if any(x.stage_type == "committee_member" for x in stages) else "hod_pending"
    elif stage.stage_type == "committee_member":
        if all(x.status == "approved" for x in stages if x.stage_type == "committee_member"):
            s.status = "hod_pending"
    elif stage.stage_type == "hod":
        s.status = "incharge_pending"
    elif stage.stage_type == "incharge_academic_cell":
        s.status = "dpgs_pending"
    elif stage.stage_type == "dpgs":
        s.status = "approved"
        s.approved_at = now
        cycle.status = "approved"
        cycle.completed_at = now
        await _freeze_final_document(s, cycle, db)

    frozen_name = cycle.frozen_pdf_filename if s.status == "approved" else None
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        _unlink_quietly(s.id, frozen_name)
        raise
    return {"message": f"{stage.role_label} approved.", "status": s.status}


@router.post("/{synopsis_id}/approval/revert")
async def revert_synopsis_stage(
    synopsis_id: UUID, body: RevertIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(*_APPROVER_ROLES)),
):
    """Revert to the student. A remark is REQUIRED and is stored on the reverting stage and the cycle; the
    cycle's other still-pending stages are marked `cancelled`. The student sees who/role/when/remark."""
    remark = (body.remark or "").strip()
    if not remark:
        raise HTTPException(400, "A remark is required when reverting.")
    s = await _get_synopsis(synopsis_id, db)
    stage = await _require_my_stage(s, user, db)
    cycle = _active_cycle(s)
    stages = await _lock_and_reload(s, stage, cycle, db)
    stage = next(x for x in stages if x.id == stage.id)
    now = _now()
    _record_action(stage, user, "reverted", now, remark)
    for other in stages:
        if other.id != stage.id and other.status == "pending":
            other.status = "cancelled"
    cycle.status = "reverted"
    cycle.reverted_at = now
    cycle.revert_remark = remark
    s.status = "reverted"
    await db.commit()
    return {"message": "Synopsis reverted to the student for correction.", "status": s.status}


# ── Frozen final document ────────────────────────────────────────────────────

async def _freeze_final_document(s: Synopsis, cycle: SynopsisApprovalCycle, db: AsyncSession) -> None:
    """Called inside the DPGS-approval transaction. Builds the document snapshot from the just-approved
    state, renders the A4 package (AMS pages + the submitted PDF) and stores both on the approved cycle. If
    anything fails the caller rolls the approval back, so an approved Synopsis ALWAYS has a frozen document."""
    context = await _build_document_context(s, db)
    uploaded = _read_stored(s.id, cycle.file.stored_filename)
    pdf = await _render_or_503(context, uploaded)
    name = f"final-{uuid.uuid4().hex}.pdf"
    _write_stored(s.id, name, pdf)
    cycle.snapshot = context
    cycle.frozen_pdf_filename = name
    cycle.frozen_pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    cycle.frozen_pdf_size = len(pdf)
    cycle.frozen_at = _now()


@router.get("/{synopsis_id}/document")
async def get_synopsis_document(
    synopsis_id: UUID, inline: bool = False, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    """The AMS-generated Synopsis as an A4 PDF. In progress: rendered LIVE from current data, so signatures
    appear as they happen. Approved: the FROZEN final document (integrity-checked against its stored hash);
    if the stored copy is missing it is rebuilt from the approved snapshot, never from mutable live data."""
    s = await _get_synopsis(synopsis_id, db)
    await _authorize_view(s, user, db)
    cycle = _latest_cycle(s)
    if s.status == "approved" and cycle and cycle.frozen_pdf_filename:
        path = _stored_path(s.id, cycle.frozen_pdf_filename)
        if path.is_file():
            pdf = path.read_bytes()
            if hashlib.sha256(pdf).hexdigest() != cycle.frozen_pdf_sha256:
                logger.error("Frozen Synopsis document failed its integrity check (synopsis_id=%s)", s.id)
                raise HTTPException(500, "The stored final document failed its integrity check.")
        else:
            pdf = await _render_or_503(cycle.snapshot, _read_stored(s.id, cycle.file.stored_filename))
    else:
        file = _document_file(s)
        if not file:
            raise HTTPException(400, "Upload the Synopsis PDF before the document can be generated.")
        pdf = await _render_or_503(await _build_document_context(s, db), _read_stored(s.id, file.stored_filename))
    roll = s.student.student_roll or str(s.student_id)
    safe_roll = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in roll)
    filename = urllib.parse.quote(f"Synopsis-{safe_roll}-{s.status}.pdf", safe="")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{filename}"},
    )
