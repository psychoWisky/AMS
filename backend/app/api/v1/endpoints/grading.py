"""Course-wise Gradesheet workflow (Gradesheet -> Approval -> CoE finalization).

Replaces the previous single-sheet Internal/External + email-OTP grading module
(its legacy tables/columns are retained, unused). Student-wise compilation,
Result Tracking/Management, Marksheet and Grade Card live in `results.py`.

Workflow: the instructor creates a gradesheet for an assigned offering (type,
theory/practical structure, components), enters marks + attendance, submits;
creator signs on submit -> every other assigned instructor approves (or is
deemed-approved after the server-side 24h window) -> HOD -> Incharge Academic
Cell -> DPGS -> Controller of Examination (finalizes/locks). "Approve = Sign":
each action records the signer + signed-at on the stage row (no OTP/eSign).
Revert (mandatory remark) returns the sheet to its creator; resubmission opens a
new approval cycle. See `app.core.gradesheet_flow`.

Authorization is enforced here on every endpoint from the caller's ACTIVE role/
department and the persisted assignment records — no client-supplied
faculty/department/student/offering id is ever trusted as authority; a record
the caller may not see is reported as 404 (never confirming it exists).
"""
import logging
import re
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import exists, func, or_, select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import gradesheet_flow as flow
from app.core.document_render import pdf_response, render_template
from app.core.dependencies import get_current_user, require_roles
from app.core.grading_calc import attendance_band, compute_entry_outcome, format_gpa, round2
from app.core.result_service import cgpa_for, ordered_results
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.academic import Semester
from app.models.course import CourseOffering, OfferingFaculty
from app.models.enrollment import StudentEnrollment
from app.models.grading import (
    COMPONENT_CATALOG, GRADESHEET_EDITABLE_STATUSES, GRADESHEET_TYPES,
    GradeEntry, GradeEntryMark, GradeSheet, GradesheetComponent, GradesheetCycle, GradesheetStage,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import College, Program, User, UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/grading", tags=["Grading"])

# Read-only viewing of a student's published academic results (GPA endpoint).
_ADMIN_ROLES = (UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.CONTROLLER_OF_EXAMINATION)

_STAGE_ROLE = {
    "hod": UserRole.HOD, "incharge": UserRole.INCHARGE_ACADEMIC_CELL,
    "dpgs": UserRole.DPGS, "coe": UserRole.CONTROLLER_OF_EXAMINATION,
}
_CREDIT_TYPE_LABEL = {"credit": "Credit", "non_credit": "Non-credit"}


# ── Schemas ──────────────────────────────────────────────────────────────────

class ComponentIn(BaseModel):
    code: str
    max_marks: Decimal


class StructureIn(BaseModel):
    total_theory_marks: Decimal = Decimal("0")
    theory_pass_marks: Decimal = Decimal("0")
    theory_components: List[ComponentIn] = []
    total_practical_marks: Decimal = Decimal("0")
    practical_pass_marks: Decimal = Decimal("0")


class SheetCreateIn(StructureIn):
    offering_id: UUID
    gradesheet_type: str = "new"
    related_sheet_id: Optional[UUID] = None
    student_ids: Optional[List[UUID]] = None


class EntryIn(BaseModel):
    student_id: UUID
    component_marks: Dict[str, Optional[Decimal]] = {}
    attendance_percent: Optional[Decimal] = None
    remark: Optional[str] = None
    is_absent: bool = False


class EntriesIn(BaseModel):
    entries: List[EntryIn]


class RevertIn(BaseModel):
    remark: str

    @field_validator("remark")
    @classmethod
    def _remark_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("A remark is required when reverting.")
        return v.strip()


# ── Small helpers ────────────────────────────────────────────────────────────

def _not_found() -> HTTPException:
    return HTTPException(404, "Gradesheet not found.")


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


_IST = timezone(timedelta(hours=5, minutes=30))


def _ist(dt) -> Optional[str]:
    """Human-readable IST timestamp for signatory blocks / PDFs."""
    return dt.astimezone(_IST).strftime("%d/%m/%Y %H:%M IST") if dt else None


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _credit_display(course) -> str:
    return f"{course.total_credits}({course.credit_theory}+{course.credit_practical})"


def _dept_matches(user: User, offering: CourseOffering) -> bool:
    return bool(user.active_department_id and offering.department_id and user.active_department_id == offering.department_id)


def _is_research(offering: CourseOffering) -> bool:
    return bool(offering.course and offering.course.category == "research")


# ── Structure validation (Generate Gradesheet modal rules) ───────────────────

def _validate_structure(body: StructureIn) -> list[dict]:
    """Returns the component specs to persist, or raises 400. Rules: the selected
    theory components' maxima must sum EXACTLY to Total Theory Marks; theory
    components require a Total Theory Marks > 0 and vice-versa; pass marks may
    not exceed their totals; at least theory or practical must carry marks."""
    theory_total = round2(body.total_theory_marks)
    practical_total = round2(body.total_practical_marks)
    theory_pass = round2(body.theory_pass_marks)
    practical_pass = round2(body.practical_pass_marks)
    if min(theory_total, practical_total, theory_pass, practical_pass) < 0:
        raise HTTPException(400, "Marks cannot be negative.")
    if theory_total == 0 and practical_total == 0:
        raise HTTPException(400, "Configure Total Theory Marks and/or Total Practical Marks.")

    specs: list[dict] = []
    seen = set()
    for i, c in enumerate(body.theory_components):
        meta = COMPONENT_CATALOG.get(c.code)
        if not meta or meta[1] != "theory":
            raise HTTPException(400, f"Unknown theory component '{c.code}'. Allowed: first_test, mid_term, end_term.")
        if c.code in seen:
            raise HTTPException(400, f"Theory component '{c.code}' is selected more than once.")
        seen.add(c.code)
        mx = round2(c.max_marks)
        if mx <= 0:
            raise HTTPException(400, f"{meta[0]}: total marks must be greater than zero.")
        specs.append({"code": c.code, "name": meta[0], "component_type": "theory", "max_marks": mx})
    order = list(COMPONENT_CATALOG)
    specs.sort(key=lambda s: order.index(s["code"]))

    if theory_total > 0:
        if not specs:
            raise HTTPException(400, "Select at least one theory component (First Test / Mid Term / End Term).")
        if sum((s["max_marks"] for s in specs), Decimal("0")) != theory_total:
            raise HTTPException(400, "The selected theory components' total marks must equal Total Theory Marks.")
        if theory_pass > theory_total:
            raise HTTPException(400, "Theory Pass Marks cannot exceed Total Theory Marks.")
    else:
        if specs:
            raise HTTPException(400, "Theory components were selected but Total Theory Marks is zero.")
        if theory_pass > 0:
            raise HTTPException(400, "Theory Pass Marks requires Total Theory Marks.")

    if practical_total > 0:
        if practical_pass > practical_total:
            raise HTTPException(400, "Practical Pass Marks cannot exceed Total Practical Marks.")
        specs.append({"code": "practical", "name": "Practical", "component_type": "practical", "max_marks": practical_total})
    elif practical_pass > 0:
        raise HTTPException(400, "Practical Pass Marks requires Total Practical Marks.")

    for n, s in enumerate(specs):
        s["sort_order"] = n
    return specs


# ── Loading & authorization ──────────────────────────────────────────────────

_SHEET_OPTIONS = (
    selectinload(GradeSheet.offering).selectinload(CourseOffering.course),
    selectinload(GradeSheet.offering).selectinload(CourseOffering.department),
    selectinload(GradeSheet.offering).selectinload(CourseOffering.semester),
    selectinload(GradeSheet.offering).selectinload(CourseOffering.calendar),
    selectinload(GradeSheet.components),
    selectinload(GradeSheet.entries).selectinload(GradeEntry.student),
    selectinload(GradeSheet.entries).selectinload(GradeEntry.marks),
    selectinload(GradeSheet.cycles).selectinload(GradesheetCycle.stages).selectinload(GradesheetStage.approver),
    selectinload(GradeSheet.cycles).selectinload(GradesheetCycle.stages).selectinload(GradesheetStage.assigned_user),
    selectinload(GradeSheet.creator),
)


async def _load_sheet(db: AsyncSession, sheet_id: UUID) -> Optional[GradeSheet]:
    return (await db.execute(
        select(GradeSheet).options(*_SHEET_OPTIONS).where(GradeSheet.id == sheet_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _load_offering(db: AsyncSession, offering_id: UUID) -> Optional[CourseOffering]:
    return (await db.execute(
        select(CourseOffering).options(
            selectinload(CourseOffering.course), selectinload(CourseOffering.department),
            selectinload(CourseOffering.semester), selectinload(CourseOffering.calendar),
        ).where(CourseOffering.id == offering_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _is_offering_instructor(db: AsyncSession, offering_id: UUID, user_id: UUID) -> bool:
    """A user is an instructor of an offering when they have an OfferingFaculty
    row (primary or secondary alike) or — Research Course — are the per-student
    instructor of at least one enrollment in it."""
    assigned = await db.execute(select(OfferingFaculty.id).where(
        OfferingFaculty.offering_id == offering_id, OfferingFaculty.faculty_id == user_id).limit(1))
    if assigned.scalar_one_or_none():
        return True
    research = await db.execute(select(StudentEnrollment.id).where(
        StudentEnrollment.offering_id == offering_id, StudentEnrollment.instructor_id == user_id).limit(1))
    return research.scalar_one_or_none() is not None


async def _authorize_offering_view(db: AsyncSession, offering: CourseOffering, user: User) -> str:
    """Who may see an offering's gradesheet list. Returns the caller's access
    level: "instructor" (sees every sheet) or "reviewer" (sees submitted sheets
    only). Anyone else gets 404."""
    role = user.active_role
    if role == UserRole.SUPER_ADMIN:
        return "reviewer"
    if role in (UserRole.FACULTY, UserRole.HOD) and await _is_offering_instructor(db, offering.id, user.id):
        return "instructor"
    if role == UserRole.HOD and _dept_matches(user, offering):
        return "reviewer"
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.CONTROLLER_OF_EXAMINATION):
        return "reviewer"
    raise HTTPException(404, "Course offering not found.")


async def _authorize_view(db: AsyncSession, sheet: GradeSheet, user: User) -> str:
    """Read authorization for one gradesheet — 404 for everyone else. Instructors
    of the offering see it from creation; department HOD and the institution-wide
    approvers (Incharge/DPGS/CoE) only once it has been submitted; Super Admin is
    read-only (never an actor in this workflow)."""
    role = user.active_role
    offering = sheet.offering
    submitted = bool(sheet.cycles)
    if role == UserRole.SUPER_ADMIN:
        return "reviewer"
    if role in (UserRole.FACULTY, UserRole.HOD) and await _is_offering_instructor(db, offering.id, user.id):
        return "instructor"
    if role == UserRole.HOD and _dept_matches(user, offering) and submitted:
        return "reviewer"
    if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.CONTROLLER_OF_EXAMINATION) and submitted:
        return "reviewer"
    raise _not_found()


async def _authorize_entry(db: AsyncSession, offering: CourseOffering, student_id: UUID, user: User) -> None:
    """Per-entry rule (unchanged Research Course semantics): on a Research Course
    offering an instructor may only enter marks for the students they are the
    per-student instructor of — never another instructor's student."""
    if not _is_research(offering):
        return
    linked = await db.execute(select(StudentEnrollment.id).where(
        StudentEnrollment.offering_id == offering.id, StudentEnrollment.student_id == student_id,
        StudentEnrollment.instructor_id == user.id).limit(1))
    if not linked.scalar_one_or_none():
        raise HTTPException(403, "You can only enter grades for students you are the Research Course instructor for.")


def _can_edit_data(sheet: GradeSheet, user: User) -> bool:
    """Marks/attendance/remark entry: the creating instructor — plus, on a
    Research Course, any per-student instructor of the offering (restricted
    per-entry). Only while the sheet is draft/reverted."""
    if user.active_role != UserRole.FACULTY or sheet.status not in GRADESHEET_EDITABLE_STATUSES:
        return False
    if sheet.created_by == user.id:
        return True
    return _is_research(sheet.offering)  # instructor-ship of the offering is checked by the caller


def _may_act(user: User, stage: GradesheetStage, offering: CourseOffering) -> bool:
    if stage.stage_type in ("creator", "instructor"):
        return stage.assigned_user_id == user.id and user.active_role in (UserRole.FACULTY, UserRole.HOD)
    role = _STAGE_ROLE.get(stage.stage_type)
    if user.active_role != role:
        return False
    if stage.stage_type == "hod":
        return _dept_matches(user, offering)
    return True


def _latest_cycle(sheet: GradeSheet) -> Optional[GradesheetCycle]:
    return max(sheet.cycles, key=lambda c: c.cycle_number) if sheet.cycles else None


def _open_cycle(sheet: GradeSheet) -> Optional[GradesheetCycle]:
    c = _latest_cycle(sheet)
    return c if c and c.status == "open" else None


def _actor_stage(cycle: GradesheetCycle, user: User, offering: CourseOffering) -> Optional[GradesheetStage]:
    seq = flow.current_sequence(cycle)
    if seq is None:
        return None
    for s in cycle.stages:
        if s.status == "pending" and s.sequence == seq and _may_act(user, s, offering):
            return s
    return None


# ── Data derivation ──────────────────────────────────────────────────────────

async def _college_degree(db: AsyncSession, offering: CourseOffering) -> tuple[Optional[str], Optional[str]]:
    """College and degree shown for an offering: derived from the approved
    enrolled students' own College/Programme (the only real data link — AMS has no
    Department->College relation); falls back to the department's stream / the
    course's programme level when no student carries them."""
    rows = (await db.execute(
        select(College.name, Program.name)
        .select_from(StudentEnrollment)
        .join(User, User.id == StudentEnrollment.student_id)
        .outerjoin(College, College.id == User.college_id)
        .outerjoin(Program, Program.id == User.program_id)
        .where(StudentEnrollment.offering_id == offering.id, StudentEnrollment.status == "approved")
        .distinct()
    )).all()
    colleges = sorted({r[0] for r in rows if r[0]})
    degrees = sorted({r[1] for r in rows if r[1]})
    college = ", ".join(colleges) or (offering.department.stream if offering.department else None)
    degree = ", ".join(degrees) or (offering.course.program_level if offering.course else None)
    return college, degree


async def _required_instructor_ids(db: AsyncSession, sheet: GradeSheet) -> set:
    ids = set((await db.execute(select(OfferingFaculty.faculty_id).where(OfferingFaculty.offering_id == sheet.offering_id))).scalars().all())
    enrollment_ids = [e.enrollment_id for e in sheet.entries if e.enrollment_id]
    if enrollment_ids:
        ids |= set((await db.execute(select(StudentEnrollment.instructor_id).where(
            StudentEnrollment.id.in_(enrollment_ids), StudentEnrollment.instructor_id.is_not(None)))).scalars().all())
    return ids


def _outcome_for(sheet: GradeSheet, entry: GradeEntry) -> dict:
    comps = [{"code": c.code, "component_type": c.component_type, "max_marks": c.max_marks} for c in sheet.components]
    by_id = {c.id: c.code for c in sheet.components}
    marks = {by_id[m.component_id]: m.marks for m in entry.marks if m.component_id in by_id}
    return compute_entry_outcome(
        comps, marks, theory_pass_marks=sheet.theory_pass_marks, practical_pass_marks=sheet.practical_pass_marks,
        is_absent=entry.is_absent,
    )


def _recompute_entry(sheet: GradeSheet, entry: GradeEntry) -> dict:
    out = _outcome_for(sheet, entry)
    entry.theory_total = out["theory_total"]
    entry.practical_total = out["practical_total"]
    entry.total_marks = float(out["grand_total"])
    entry.marks_percent = out["marks_percent"]
    entry.grade_letter = out["grade_letter"]
    entry.grade_points = out["grade_points"]
    return out


# ── Serialization ────────────────────────────────────────────────────────────

def _stage_dict(s: GradesheetStage) -> dict:
    name = None
    if s.approver:
        name = s.approver.full_name
    elif s.assigned_user:
        name = s.assigned_user.full_name
    return {
        "id": str(s.id), "sequence": s.sequence, "stage_type": s.stage_type, "label": flow.STAGE_LABELS[s.stage_type],
        "status": s.status, "name": name, "is_deemed": s.is_deemed,
        "signed_at": _iso(s.acted_at) if s.status == "approved" else None,
        "acted_at": _iso(s.acted_at), "signed_at_display": _ist(s.acted_at), "remark": s.remark,
        "acted_role": s.acted_role,
    }


def _cycle_dict(c: GradesheetCycle) -> dict:
    return {
        "id": str(c.id), "cycle_number": c.cycle_number, "status": c.status,
        "submitted_at": _iso(c.submitted_at), "instructor_deadline_at": _iso(c.instructor_deadline_at),
        "closed_at": _iso(c.closed_at), "stages": [_stage_dict(s) for s in c.stages],
    }


def _signatories(sheet: GradeSheet) -> Optional[dict]:
    """Latest approval cycle grouped for display/PDF: course instructors (with the
    creator first), then HOD, Incharge Academic Cell, DPGS, CoE. Only `approved`
    stages carry a signature timestamp; a deemed approval is shown as such."""
    cycle = _latest_cycle(sheet)
    if not cycle:
        return None
    stages = [_stage_dict(s) for s in cycle.stages]
    return {
        "cycle_number": cycle.cycle_number, "cycle_status": cycle.status,
        "instructors": [s for s in stages if s["stage_type"] in ("creator", "instructor")],
        "hod": next((s for s in stages if s["stage_type"] == "hod"), None),
        "incharge": next((s for s in stages if s["stage_type"] == "incharge"), None),
        "dpgs": next((s for s in stages if s["stage_type"] == "dpgs"), None),
        "coe": next((s for s in stages if s["stage_type"] == "coe"), None),
    }


def _entry_dict(sheet: GradeSheet, e: GradeEntry) -> dict:
    by_id = {c.id: c.code for c in sheet.components}
    marks = {code: None for code in by_id.values()}
    for m in e.marks:
        if m.component_id in by_id:
            marks[by_id[m.component_id]] = _num(m.marks)
    out = _outcome_for(sheet, e)
    return {
        "id": str(e.id), "student_id": str(e.student_id),
        "student_name": e.student.full_name if e.student else None,
        "student_roll": e.student.student_roll if e.student else None,
        "component_marks": marks,
        "theory_total": _num(out["theory_total"]), "practical_total": _num(out["practical_total"]),
        "grand_total": _num(out["grand_total"]), "marks_percent": _num(out["marks_percent"]),
        "grade_letter": out["grade_letter"], "grade_points": out["grade_points"], "complete": out["complete"],
        "attendance_percent": _num(e.attendance_percent), "attendance_band": attendance_band(e.attendance_percent),
        "is_absent": e.is_absent, "remark": e.remarks,
    }


def _component_dict(c: GradesheetComponent) -> dict:
    return {"code": c.code, "name": c.name, "component_type": c.component_type, "max_marks": _num(c.max_marks), "sort_order": c.sort_order}


async def _course_info(db: AsyncSession, offering: CourseOffering) -> dict:
    course = offering.course
    college, degree = await _college_degree(db, offering)
    return {
        "college": college, "degree": degree,
        "department": offering.department.name if offering.department else None,
        "course_number": course.course_number, "course_title": course.title,
        "credit": _credit_display(course),
        "credit_type": _CREDIT_TYPE_LABEL.get(course.credit_type or "", "—"),
        "semester": offering.semester.name if offering.semester else None,
        "session": offering.calendar.academic_year if offering.calendar else None,
    }


def _student_summary(sheet: GradeSheet) -> dict:
    genders = [(e.student.gender or "").strip().lower() if e.student else "" for e in sheet.entries]
    return {"total": len(genders), "male": genders.count("male"), "female": genders.count("female")}


def _sheet_row(sheet: GradeSheet) -> dict:
    off = sheet.offering
    return {
        "id": str(sheet.id), "offering_id": str(sheet.offering_id),
        "teacher": sheet.creator.full_name if sheet.creator else None,
        "gradesheet_type": sheet.gradesheet_type, "status": sheet.status,
        "created_at": _iso(sheet.created_at), "submitted_at": _iso(sheet.submitted_at),
        "course_number": off.course.course_number, "course_title": off.course.title,
        "department": off.department.name if off.department else None,
        "semester": off.semester.name if off.semester else None,
        "academic_year": off.calendar.academic_year if off.calendar else None,
    }


async def _permissions(db: AsyncSession, sheet: GradeSheet, user: User, access: str) -> dict:
    is_instructor = access == "instructor"
    editable = sheet.status in GRADESHEET_EDITABLE_STATUSES
    is_creator = sheet.created_by == user.id and user.active_role == UserRole.FACULTY
    cycle = _open_cycle(sheet)
    stage = _actor_stage(cycle, user, sheet.offering) if cycle else None
    awaiting = None
    if cycle:
        seq = flow.current_sequence(cycle)
        if seq is not None:
            awaiting = flow.STAGE_LABELS[next(s.stage_type for s in cycle.stages if s.sequence == seq and s.status == "pending")]
    return {
        "can_edit_data": bool(is_instructor and _can_edit_data(sheet, user)),
        "can_edit_structure": bool(is_creator and editable),
        "can_submit": bool(is_creator and editable),
        "can_approve": stage is not None,
        "can_revert": stage is not None,
        "acting_as": flow.STAGE_LABELS[stage.stage_type] if stage else None,
        "awaiting": awaiting,
    }


async def _sheet_detail(db: AsyncSession, sheet: GradeSheet, user: User, access: str) -> dict:
    info = await _course_info(db, sheet.offering)
    entries = sorted(sheet.entries, key=lambda e: ((e.student.student_roll or "") if e.student else "", (e.student.full_name if e.student else "")))
    latest = _latest_cycle(sheet)
    return {
        "id": str(sheet.id), "offering_id": str(sheet.offering_id),
        "gradesheet_type": sheet.gradesheet_type, "related_sheet_id": str(sheet.related_sheet_id) if sheet.related_sheet_id else None,
        "status": sheet.status, "is_locked": sheet.is_locked,
        "teacher": sheet.creator.full_name if sheet.creator else None,
        "created_at": _iso(sheet.created_at), "submitted_at": _iso(sheet.submitted_at), "finalized_at": _iso(sheet.finalized_at),
        "course": info, "students": _student_summary(sheet),
        "structure": {
            "total_theory_marks": _num(sheet.total_theory_marks), "theory_pass_marks": _num(sheet.theory_pass_marks),
            "total_practical_marks": _num(sheet.total_practical_marks), "practical_pass_marks": _num(sheet.practical_pass_marks),
            "components": [_component_dict(c) for c in sheet.components],
        },
        "entries": [_entry_dict(sheet, e) for e in entries],
        "signatories": _signatories(sheet),
        "instructor_deadline_at": _iso(latest.instructor_deadline_at) if latest and latest.status == "open" else None,
        "permissions": await _permissions(db, sheet, user, access),
    }


# ── Endpoints: courses & gradesheet lifecycle ────────────────────────────────

@router.get("/assigned-courses")
async def assigned_courses(
    calendar_id: Optional[UUID] = None, semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY)),
):
    """Course Gradesheet Management — ONLY the offerings the authenticated instructor
    is actually assigned to (OfferingFaculty row, or Research Course per-student
    instructor). The filter is applied in the query, not the UI."""
    q = (
        select(CourseOffering)
        .options(selectinload(CourseOffering.course), selectinload(CourseOffering.department),
                 selectinload(CourseOffering.semester), selectinload(CourseOffering.calendar))
        .where(or_(
            exists(select(OfferingFaculty.id).where(OfferingFaculty.offering_id == CourseOffering.id, OfferingFaculty.faculty_id == user.id)),
            exists(select(StudentEnrollment.id).where(StudentEnrollment.offering_id == CourseOffering.id, StudentEnrollment.instructor_id == user.id)),
        ))
    )
    if calendar_id:
        q = q.where(CourseOffering.calendar_id == calendar_id)
    if semester_id:
        q = q.where(CourseOffering.semester_id == semester_id)
    offerings = (await db.execute(q.order_by(CourseOffering.created_at.desc()))).scalars().all()
    ids = [o.id for o in offerings]
    students = dict((await db.execute(
        select(StudentEnrollment.offering_id, func.count()).where(StudentEnrollment.offering_id.in_(ids), StudentEnrollment.status == "approved")
        .group_by(StudentEnrollment.offering_id))).all()) if ids else {}
    sheets = dict((await db.execute(
        select(GradeSheet.offering_id, func.count()).where(GradeSheet.offering_id.in_(ids)).group_by(GradeSheet.offering_id))).all()) if ids else {}
    rows = []
    for o in offerings:
        college, degree = await _college_degree(db, o)
        rows.append({
            "offering_id": str(o.id), "college_degree": " / ".join(x for x in (college, degree) if x) or None,
            "department": o.department.name if o.department else None,
            "course_number": o.course.course_number, "course_title": o.course.title, "course_credit": _credit_display(o.course),
            "semester": o.semester.name if o.semester else None, "semester_id": str(o.semester_id),
            "calendar_id": str(o.calendar_id), "academic_year": o.calendar.academic_year if o.calendar else None,
            "total_students": students.get(o.id, 0), "gradesheet_count": sheets.get(o.id, 0),
        })
    return rows


@router.get("/offering/{offering_id}/sheets")
async def sheets_for_offering(offering_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    offering = await _load_offering(db, offering_id)
    if not offering:
        raise HTTPException(404, "Course offering not found.")
    access = await _authorize_offering_view(db, offering, user)
    total = (await db.execute(select(func.count()).select_from(StudentEnrollment).where(
        StudentEnrollment.offering_id == offering_id, StudentEnrollment.status == "approved"))).scalar_one()
    sheets = (await db.execute(
        select(GradeSheet).options(
            selectinload(GradeSheet.creator), selectinload(GradeSheet.cycles),
            selectinload(GradeSheet.offering).selectinload(CourseOffering.course),
            selectinload(GradeSheet.offering).selectinload(CourseOffering.department),
            selectinload(GradeSheet.offering).selectinload(CourseOffering.semester),
            selectinload(GradeSheet.offering).selectinload(CourseOffering.calendar),
        ).where(GradeSheet.offering_id == offering_id).order_by(GradeSheet.created_at)
    )).scalars().all()
    if access != "instructor":
        sheets = [s for s in sheets if s.cycles]
    students = []
    if access == "instructor":
        students = [{"id": str(u.id), "name": u.full_name, "roll_no": u.student_roll} for u in (await db.execute(
            select(User).join(StudentEnrollment, StudentEnrollment.student_id == User.id)
            .where(StudentEnrollment.offering_id == offering_id, StudentEnrollment.status == "approved").order_by(User.student_roll, User.first_name)
        )).scalars().all()]
    return {
        "offering": {"id": str(offering.id), **(await _course_info(db, offering)), "total_students": total},
        "students": students,
        "can_create": user.active_role == UserRole.FACULTY and access == "instructor",
        "sheets": [_sheet_row(s) for s in sheets],
    }


@router.post("/sheets", status_code=201)
async def create_sheet(body: SheetCreateIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY))):
    offering = await _load_offering(db, body.offering_id)
    # The caller must be a genuine instructor of THIS offering — an id alone never authorizes.
    if not offering or not await _is_offering_instructor(db, offering.id, user.id):
        raise HTTPException(404, "Course offering not found.")
    if body.gradesheet_type not in GRADESHEET_TYPES:
        raise HTTPException(400, f"Gradesheet type must be one of: {', '.join(GRADESHEET_TYPES)}.")
    specs = _validate_structure(body)

    related_id = None
    if body.related_sheet_id:
        if body.gradesheet_type == "new":
            raise HTTPException(400, "A New gradesheet cannot reference a related gradesheet.")
        related = (await db.execute(select(GradeSheet).where(GradeSheet.id == body.related_sheet_id))).scalar_one_or_none()
        if not related or related.offering_id != offering.id:
            raise HTTPException(400, "The related gradesheet must belong to the same course offering.")
        related_id = related.id

    if body.gradesheet_type == "new":
        dup = await db.execute(select(GradeSheet.id).where(GradeSheet.offering_id == offering.id, GradeSheet.gradesheet_type == "new"))
        if dup.scalar_one_or_none():
            raise HTTPException(409, "A New gradesheet already exists for this course. Use Repeat / Revised / Make up for further sheets.")

    enrollments = (await db.execute(select(StudentEnrollment).where(
        StudentEnrollment.offering_id == offering.id, StudentEnrollment.status == "approved"))).scalars().all()
    if body.student_ids is not None:
        allowed = {e.student_id for e in enrollments}
        if not body.student_ids or any(sid not in allowed for sid in body.student_ids):
            raise HTTPException(400, "Every selected student must have an approved registration for this course.")
        wanted = set(body.student_ids)
        enrollments = [e for e in enrollments if e.student_id in wanted]
    if not enrollments:
        raise HTTPException(400, "No students have an approved registration for this course.")

    sheet = GradeSheet(
        offering_id=offering.id, sheet_type="final", gradesheet_type=body.gradesheet_type, related_sheet_id=related_id,
        total_theory_marks=round2(body.total_theory_marks), theory_pass_marks=round2(body.theory_pass_marks),
        total_practical_marks=round2(body.total_practical_marks), practical_pass_marks=round2(body.practical_pass_marks),
        status="draft", created_by=user.id,
    )
    db.add(sheet)
    try:
        await db.flush()
        for s in specs:
            db.add(GradesheetComponent(sheet_id=sheet.id, **s))
        for e in enrollments:
            db.add(GradeEntry(sheet_id=sheet.id, student_id=e.student_id, enrollment_id=e.id))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A New gradesheet already exists for this course.")
    return {"id": str(sheet.id), "message": "Gradesheet created. Enter the marks and attendance, then submit it for approval."}


@router.get("/sheets/{sheet_id}")
async def get_sheet(sheet_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    access = await _authorize_view(db, sheet, user)
    cycle = _open_cycle(sheet)
    if (
        cycle and cycle.instructor_deadline_at and flow.now() >= cycle.instructor_deadline_at
        and any(s.stage_type == "instructor" and s.status == "pending" for s in cycle.stages)
    ):
        # Server-authoritative 24h rule, applied lazily under the cycle's row lock.
        locked = await flow.lock_open_cycle(db, sheet.id)
        if locked:
            flow.apply_deadline(sheet, locked)
        await db.commit()  # persists the forward (if any) and releases the row lock
        sheet = await _load_sheet(db, sheet_id)
    return await _sheet_detail(db, sheet, user, access)


@router.put("/sheets/{sheet_id}/structure")
async def update_structure(sheet_id: UUID, body: StructureIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY))):
    """Reconfigure the components/totals while the sheet is still editable (draft
    or reverted). Only the creating instructor. Marks of removed components are
    discarded; a component whose new maximum is below an already-entered mark is
    refused."""
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    if await _authorize_view(db, sheet, user) != "instructor" or sheet.created_by != user.id:
        raise _not_found()
    if sheet.status not in GRADESHEET_EDITABLE_STATUSES:
        raise HTTPException(409, "The gradesheet structure can only be changed while it is in draft or reverted state.")
    specs = _validate_structure(body)
    existing = {c.code: c for c in sheet.components}
    new_codes = {s["code"] for s in specs}
    for s in specs:
        c = existing.get(s["code"])
        if c:
            over = (await db.execute(select(func.count()).select_from(GradeEntryMark).where(
                GradeEntryMark.component_id == c.id, GradeEntryMark.marks > s["max_marks"]))).scalar_one()
            if over:
                raise HTTPException(400, f"{c.name}: entered marks exceed the new maximum ({s['max_marks']}). Correct the marks first.")
    for code, c in existing.items():
        if code not in new_codes:
            await db.execute(delete(GradeEntryMark).where(GradeEntryMark.component_id == c.id))
            await db.delete(c)
    for s in specs:
        c = existing.get(s["code"])
        if c:
            c.max_marks = s["max_marks"]; c.sort_order = s["sort_order"]
        else:
            db.add(GradesheetComponent(sheet_id=sheet.id, **s))
    sheet.total_theory_marks = round2(body.total_theory_marks); sheet.theory_pass_marks = round2(body.theory_pass_marks)
    sheet.total_practical_marks = round2(body.total_practical_marks); sheet.practical_pass_marks = round2(body.practical_pass_marks)
    await db.commit()
    sheet = await _load_sheet(db, sheet_id)
    for e in sheet.entries:
        _recompute_entry(sheet, e)
    await db.commit()
    return {"message": "Gradesheet structure updated."}


@router.put("/sheets/{sheet_id}/entries")
async def save_entries(sheet_id: UUID, body: EntriesIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY))):
    """Save marks / attendance / remarks. Every item is validated BEFORE anything
    is written, so a rejected batch never partially saves."""
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    if await _authorize_view(db, sheet, user) != "instructor":
        raise _not_found()
    if not _can_edit_data(sheet, user):
        if sheet.status not in GRADESHEET_EDITABLE_STATUSES:
            raise HTTPException(409, "This gradesheet is locked for approval; marks can only be edited in draft or reverted state.")
        raise HTTPException(403, "Only the instructor who created this gradesheet can enter its data.")

    comps = {c.code: c for c in sheet.components}
    entries = {e.student_id: e for e in sheet.entries}
    for item in body.entries:
        entry = entries.get(item.student_id)
        if entry is None:
            raise HTTPException(400, "A submitted student is not part of this gradesheet.")
        await _authorize_entry(db, sheet.offering, item.student_id, user)
        for code, value in item.component_marks.items():
            comp = comps.get(code)
            if comp is None:
                raise HTTPException(400, f"'{code}' is not a component of this gradesheet.")
            if value is not None and not (0 <= round2(value) <= comp.max_marks):
                raise HTTPException(400, f"{comp.name}: marks must be between 0 and {comp.max_marks}.")
        if item.attendance_percent is not None and not (0 <= item.attendance_percent <= 100):
            raise HTTPException(400, "Attendance % must be between 0 and 100.")

    for item in body.entries:
        entry = entries[item.student_id]
        provided = item.model_fields_set
        by_comp = {m.component_id: m for m in entry.marks}
        for code, value in item.component_marks.items():
            comp = comps[code]
            row = by_comp.get(comp.id)
            if row is None:
                row = GradeEntryMark(entry_id=entry.id, component_id=comp.id)
                entry.marks.append(row)
            row.marks = round2(value) if value is not None else None
        if "attendance_percent" in provided:
            entry.attendance_percent = round2(item.attendance_percent) if item.attendance_percent is not None else None
        if "remark" in provided:
            entry.remarks = (item.remark or "").strip() or None
        if "is_absent" in provided:
            entry.is_absent = item.is_absent
        entry.updated_by = user.id
        _recompute_entry(sheet, entry)
    await db.commit()
    return {"message": "Gradesheet saved."}


@router.post("/sheets/{sheet_id}/submit")
async def submit_sheet(sheet_id: UUID, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.FACULTY))):
    """Creator submits: signs (approve = sign) and opens a NEW approval cycle."""
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    if await _authorize_view(db, sheet, user) != "instructor" or sheet.created_by != user.id:
        raise _not_found()
    # Serialise double-submits on the sheet row, then re-check the state.
    await db.execute(select(GradeSheet.id).where(GradeSheet.id == sheet_id).with_for_update())
    sheet = await _load_sheet(db, sheet_id)
    if sheet.status not in GRADESHEET_EDITABLE_STATUSES:
        raise HTTPException(409, "This gradesheet has already been submitted.")
    if not sheet.components:
        raise HTTPException(400, "Configure the gradesheet components before submitting.")
    if not sheet.entries:
        raise HTTPException(400, "The gradesheet has no students.")
    incomplete = [(e.student.student_roll or e.student.full_name) for e in sheet.entries if not _outcome_for(sheet, e)["complete"]]
    if incomplete:
        raise HTTPException(400, f"Marks are missing for {len(incomplete)} student(s) (or mark them absent): {', '.join(incomplete[:8])}{'…' if len(incomplete) > 8 else ''}.")

    at = flow.now()
    number = max((c.cycle_number for c in sheet.cycles), default=0) + 1
    instructors = await _required_instructor_ids(db, sheet)
    cycle = flow.create_cycle(sheet, cycle_number=number, creator_id=user.id, other_instructor_ids=instructors, at=at, ip=_client_ip(request))
    db.add(cycle)
    flow.refresh_sheet_status(sheet, cycle, at)
    sheet.submitted_at = at
    await db.commit()
    return {"message": "Gradesheet submitted and signed.", "status": sheet.status, "cycle_number": number,
            "instructor_deadline_at": _iso(cycle.instructor_deadline_at)}


async def _resolve_actor(db: AsyncSession, sheet_id: UUID, user: User) -> tuple[GradeSheet, GradesheetCycle, GradesheetStage]:
    """Shared by approve/revert: load, authorize (404), lock the open cycle, apply
    the 24h rule, and return the stage this caller may act on RIGHT NOW — or raise:
    409 when it is not yet their turn, 403 when they have no standing at all."""
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    await _authorize_view(db, sheet, user)
    cycle = await flow.lock_open_cycle(db, sheet_id)
    if cycle is None:
        raise HTTPException(409, "This gradesheet is not awaiting approval.")
    changed = flow.apply_deadline(sheet, cycle)
    stage = _actor_stage(cycle, user, sheet.offering)
    if stage is None:
        # Work out the error BEFORE releasing the row lock (commit/rollback expire loaded state).
        pending = [s for s in cycle.stages if s.status == "pending"]
        seq = flow.current_sequence(cycle)
        waiting = next((flow.STAGE_LABELS[s.stage_type] for s in pending if s.sequence == seq), "an earlier stage")
        has_future_stage = any(_may_act(user, s, sheet.offering) for s in pending)
        await db.commit()  # persists a lazy forward (if any) and releases the row lock
        if has_future_stage:
            raise HTTPException(409, f"It is not your turn yet: this gradesheet is awaiting {waiting}.")
        raise HTTPException(403, "You are not authorized to act on this gradesheet at this stage.")
    return sheet, cycle, stage


@router.post("/sheets/{sheet_id}/approve")
async def approve_sheet(sheet_id: UUID, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Approve = Sign: records the caller's signature (signer, role, department, IP,
    signed-at) on their stage. Strict order enforced server-side."""
    sheet, cycle, stage = await _resolve_actor(db, sheet_id, user)
    at = flow.now()
    stage.status = "approved"
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id if user.active_role == UserRole.HOD else None
    stage.acted_at = at
    stage.ip_address = _client_ip(request)
    flow.refresh_sheet_status(sheet, cycle, at)
    await db.commit()
    return {"message": f"Signed as {flow.STAGE_LABELS[stage.stage_type]}.", "status": sheet.status}


@router.post("/sheets/{sheet_id}/revert")
async def revert_sheet(sheet_id: UUID, body: RevertIn, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Any acting approver may revert (remark mandatory). The cycle is closed with its
    signatures preserved as history; other pending stages are cancelled; the sheet
    returns to its creating instructor for correction and resubmission (new cycle)."""
    sheet, cycle, stage = await _resolve_actor(db, sheet_id, user)
    at = flow.now()
    stage.status = "reverted"
    stage.approver_id = user.id
    stage.acted_role = user.active_role.value
    stage.acted_department_id = user.active_department_id if user.active_role == UserRole.HOD else None
    stage.acted_at = at
    stage.remark = body.remark
    stage.ip_address = _client_ip(request)
    for s in cycle.stages:
        if s.status == "pending":
            s.status = "cancelled"
    cycle.status = "reverted"
    cycle.closed_at = at
    sheet.status = "reverted"
    sheet.is_locked = False
    await db.commit()
    return {"message": "Gradesheet reverted to the creating instructor.", "status": sheet.status}


@router.get("/sheets/{sheet_id}/approvals")
async def approval_history(sheet_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Every approval cycle with its stages — older cycles keep their own
    signatures/reverts, so history stays meaningful after resubmission."""
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    await _authorize_view(db, sheet, user)
    return {"sheet_id": str(sheet.id), "status": sheet.status, "cycles": [_cycle_dict(c) for c in sheet.cycles]}


# ── Approver inbox ───────────────────────────────────────────────────────────

@router.get("/inbox")
async def inbox(
    scope: str = "pending", calendar_id: Optional[UUID] = None, semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.FACULTY, UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.CONTROLLER_OF_EXAMINATION)),
):
    """Sheets awaiting THIS caller's action (scope=pending), or — CoE only —
    finalized course-wise gradesheets (scope=finalized)."""
    await flow.sweep_overdue(db)  # server-authoritative 24h rule, even with no worker running
    role = user.active_role
    base = select(GradeSheet).options(
        selectinload(GradeSheet.creator),
        selectinload(GradeSheet.offering).selectinload(CourseOffering.course),
        selectinload(GradeSheet.offering).selectinload(CourseOffering.department),
        selectinload(GradeSheet.offering).selectinload(CourseOffering.semester),
        selectinload(GradeSheet.offering).selectinload(CourseOffering.calendar),
    ).join(CourseOffering, CourseOffering.id == GradeSheet.offering_id)
    if calendar_id:
        base = base.where(CourseOffering.calendar_id == calendar_id)
    if semester_id:
        base = base.where(CourseOffering.semester_id == semester_id)

    found: dict = {}
    if scope == "finalized":
        if role != UserRole.CONTROLLER_OF_EXAMINATION:
            raise HTTPException(403, "Only the Controller of Examination can list finalized gradesheets.")
        for s in (await db.execute(base.where(GradeSheet.status == "approved").order_by(GradeSheet.finalized_at.desc()))).scalars().all():
            found[s.id] = s
    elif scope == "pending":
        if role in (UserRole.FACULTY, UserRole.HOD):
            instr = base.join(GradesheetCycle, GradesheetCycle.sheet_id == GradeSheet.id).join(
                GradesheetStage, GradesheetStage.cycle_id == GradesheetCycle.id).where(
                GradesheetCycle.status == "open", GradesheetStage.stage_type == "instructor",
                GradesheetStage.status == "pending", GradesheetStage.assigned_user_id == user.id)
            for s in (await db.execute(instr)).scalars().all():
                found[s.id] = s
        if role == UserRole.HOD:
            if user.active_department_id:
                for s in (await db.execute(base.where(GradeSheet.status == "hod_pending", CourseOffering.department_id == user.active_department_id))).scalars().all():
                    found[s.id] = s
        status_by_role = {UserRole.INCHARGE_ACADEMIC_CELL: "incharge_pending", UserRole.DPGS: "dpgs_pending", UserRole.CONTROLLER_OF_EXAMINATION: "coe_pending"}
        if role in status_by_role:
            for s in (await db.execute(base.where(GradeSheet.status == status_by_role[role]))).scalars().all():
                found[s.id] = s
    else:
        raise HTTPException(400, "scope must be 'pending' or 'finalized'.")
    rows = sorted((_sheet_row(s) for s in found.values()), key=lambda r: r["submitted_at"] or "")
    return rows


# ── PDF ──────────────────────────────────────────────────────────────────────

@router.get("/sheets/{sheet_id}/document")
async def gradesheet_document(sheet_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Official course-wise Gradesheet PDF (on-demand, never stored): course
    information, the full component table, and every signatory with its signed-at
    timestamp. Same read authorization as the details endpoint."""
    sheet = await _load_sheet(db, sheet_id)
    if not sheet:
        raise _not_found()
    await _authorize_view(db, sheet, user)
    from app.utils.pdf import get_logo_data_uri
    detail = await _sheet_detail(db, sheet, user, "reviewer")
    detail["logo_data_uri"] = get_logo_data_uri()
    detail["generated_at"] = flow.now()
    html = render_template("gradesheet_document.html", detail)
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", f"{sheet.offering.course.course_number}-{sheet.gradesheet_type}")
    return await pdf_response(html, f"Gradesheet-{safe}.pdf", "Gradesheet")


# ── Student GPA/CGPA (published results only) ────────────────────────────────

async def _authorize_student_academic_view(student_id: UUID, user: User, db: AsyncSession) -> None:
    """Who may view a student's academic-progress data. Unchanged rules: admins
    (now including the Controller of Examination) unrestricted; a student only
    themself; HOD via the student's own department; faculty only through an
    established relationship (shared course assignment or advisory committee)."""
    if user.active_role in _ADMIN_ROLES:
        return
    if user.active_role == UserRole.STUDENT:
        if student_id == user.id:
            return
        raise HTTPException(403, "You can only view your own academic progress.")
    if user.active_role == UserRole.HOD:
        dept_id = await resolve_student_department_id(student_id, db)
        if dept_id and user.active_department_id and dept_id == user.active_department_id:
            return
        raise HTTPException(403, "You can only view students within your own department.")
    if user.active_role == UserRole.FACULTY:
        course_link = await db.execute(
            select(OfferingFaculty.id)
            .join(StudentEnrollment, StudentEnrollment.offering_id == OfferingFaculty.offering_id)
            .where(OfferingFaculty.faculty_id == user.id, StudentEnrollment.student_id == student_id).limit(1))
        if course_link.scalar_one_or_none():
            return
        committee_link = await db.execute(
            select(CommitteeMember.id).join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
            .where(CommitteeMember.faculty_id == user.id, AdvisoryCommittee.student_id == student_id).limit(1))
        if committee_link.scalar_one_or_none():
            return
        raise HTTPException(403, "You can only view academic progress for students you teach or advise.")
    raise HTTPException(403, "Insufficient permissions.")


@router.get("/student/{student_id}/gpa")
async def student_gpa(student_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """SGPA per PUBLISHED semester result and CGPA (average of semester GPAs; none
    for the first semester), 3-decimal truncated display. Reads only what the
    Controller of Examination has compiled AND published."""
    await _authorize_student_academic_view(student_id, user, db)
    results = await ordered_results(db, student_id)
    sems = {s.id: s for s in (await db.execute(select(Semester).where(Semester.id.in_([r.semester_id for r in results])))).scalars().all()} if results else {}
    rows = []
    for r in results:
        rows.append({
            "result_id": str(r.id), "semester_id": str(r.semester_id),
            "semester_name": r.semester_name or (sems[r.semester_id].name if r.semester_id in sems else None), "academic_year": r.academic_year,
            "sgpa": format_gpa(r.gpa), "credits": float(r.total_credits),
            "cgpa": format_gpa(cgpa_for(results, r.id)), "result_status": r.result_status,
        })
    latest = rows[-1] if rows else None
    return {"student_id": str(student_id), "cgpa": latest["cgpa"] if latest else None, "cgpa_applicable": bool(latest and latest["cgpa"]), "semesters": rows}
