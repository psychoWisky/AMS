"""Student-wise semester results: CoE compilation -> publication -> student
Result Tracking / Result Management / Marksheet / Grade Card.

Flow: course-wise Gradesheets are finalized independently (CoE approval, see
`grading.py`). The Controller of Examination then compiles a student's semester
result from the LATEST finalized gradesheet entry of each of the student's
registered courses in that semester, and manually chooses the result status —
Pass or Pass with Backlogs. AMS never infers that status (not from GPA, grades,
attendance or credit counts), and never promotes a student, changes their current
semester, or applies backlog/repeat/completion rules: AVFU has not confirmed any
of those (see BUSINESS_LOGIC.md's Gradesheet section for the open questions).

Visibility: a student sees only their OWN PUBLISHED results; an unpublished result
is invisible to everyone but the CoE (and Super Admin, read-only).
"""
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import get_current_user, require_roles, student_user_clause
from app.core.document_render import pdf_response, render_template
from app.core.grading_calc import compute_gpa, format_gpa
from app.core.result_service import cgpa_for, ordered_results
from app.core.student_scope import resolve_student_department_id
from app.db.base import get_db
from app.models.academic import Semester
from app.models.course import Course, CourseOffering
from app.models.enrollment import StudentEnrollment
from app.models.grading import (
    RESULT_STATUSES, GradeEntry, GradeSheet, StudentSemesterResult, StudentSemesterResultCourse,
)
from app.models.research import AdvisoryCommittee, CommitteeMember
from app.models.user import User, UserRole

router = APIRouter(prefix="/results", tags=["Results"])

_COE = UserRole.CONTROLLER_OF_EXAMINATION
_RESULT_LABEL = {"pass": "Pass", "pass_with_backlogs": "Pass with Backlogs"}
_PROGRESSION_NOTE = "Not yet available — pending AVFU confirmation of the academic progression rules."


# ── Schemas ──────────────────────────────────────────────────────────────────

class CompileItem(BaseModel):
    student_id: UUID
    result_status: str

    @field_validator("result_status")
    @classmethod
    def _valid(cls, v: str) -> str:
        if v not in RESULT_STATUSES:
            raise ValueError("result_status must be 'pass' or 'pass_with_backlogs'.")
        return v


class CompileIn(BaseModel):
    semester_id: UUID
    items: List[CompileItem]

    @field_validator("items")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("Select at least one student.")
        return v


class PublishIn(BaseModel):
    result_ids: List[UUID]

    @field_validator("result_ids")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("Select at least one result.")
        return v


# ── Helpers ──────────────────────────────────────────────────────────────────

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _f(v) -> Optional[float]:
    return float(v) if v is not None else None


def _credit_display(course: Course) -> str:
    return f"{course.total_credits}({course.credit_theory}+{course.credit_practical})"


async def _finalized_entries(db: AsyncSession, offering_ids: list, student_ids: Optional[list] = None) -> dict:
    """(offering_id, student_id) -> (GradeEntry, GradeSheet) using the LATEST
    finalized (CoE-approved) gradesheet per offering (ASSUMPTION — which sheet
    wins when a Revised/Repeat/Make-up sheet exists is an open AVFU question)."""
    if not offering_ids:
        return {}
    q = (
        select(GradeEntry, GradeSheet).join(GradeSheet, GradeSheet.id == GradeEntry.sheet_id)
        .where(GradeSheet.offering_id.in_(offering_ids), GradeSheet.status == "approved", GradeEntry.grade_letter.is_not(None))
        .order_by(GradeSheet.finalized_at.asc())
    )
    if student_ids:
        q = q.where(GradeEntry.student_id.in_(student_ids))
    found: dict = {}
    for entry, sheet in (await db.execute(q)).all():
        found[(sheet.offering_id, entry.student_id)] = (entry, sheet)
    return found


async def _has_major_advisor(db: AsyncSession, faculty_id: UUID, student_id: UUID) -> bool:
    row = await db.execute(
        select(CommitteeMember.id).join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .where(AdvisoryCommittee.student_id == student_id, CommitteeMember.faculty_id == faculty_id,
               CommitteeMember.role == "major_advisor", CommitteeMember.accepted == True).limit(1)  # noqa: E712
    )
    return row.scalar_one_or_none() is not None


async def _authorize_student_scope(db: AsyncSession, student_id: UUID, user: User, *, published: bool) -> None:
    """Read authorization over ONE student's results. 404 (never 403) for anyone
    outside scope so a foreign student's existence/result is never confirmed.
    `published` — the record being read is published (staff other than the CoE and
    Super Admin only ever see published results)."""
    role = user.active_role
    if role in (_COE, UserRole.SUPER_ADMIN):
        return
    if role == UserRole.STUDENT:
        if student_id == user.id and published:
            return
    elif published:
        if role in (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS):
            return
        if role == UserRole.HOD:
            dept = await resolve_student_department_id(student_id, db)
            if dept and user.active_department_id and dept == user.active_department_id:
                return
        if role == UserRole.FACULTY and await _has_major_advisor(db, user.id, student_id):
            return
    raise HTTPException(404, "Result not found.")


async def _load_result(db: AsyncSession, result_id: UUID) -> Optional[StudentSemesterResult]:
    return (await db.execute(
        select(StudentSemesterResult).options(selectinload(StudentSemesterResult.courses))
        .where(StudentSemesterResult.id == result_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _authorize_result(db: AsyncSession, result: Optional[StudentSemesterResult], user: User) -> StudentSemesterResult:
    if result is None:
        raise HTTPException(404, "Result not found.")
    await _authorize_student_scope(db, result.student_id, user, published=result.status == "published")
    return result


def _course_dict(c: StudentSemesterResultCourse) -> dict:
    return {
        "offering_id": str(c.offering_id), "course_number": c.course_number, "course_title": c.course_title,
        "department": c.department_name, "credit": c.credit_structure, "credits": _f(c.credits),
        "grade_letter": c.grade_letter, "grade_points": _f(c.grade_points), "credit_points": _f(c.credit_points),
        "marks_percent": _f(c.marks_percent),
    }


async def _result_detail(db: AsyncSession, r: StudentSemesterResult) -> dict:
    """Result + Marksheet payload. `promoted_class`/`admission_status`/`class_label`
    are deliberately None: no confirmed source or rule exists yet, and inventing
    them would be fabricating academic progression data."""
    ordered = await ordered_results(db, r.student_id, include_result_id=r.id)
    cgpa = cgpa_for(ordered, r.id)
    courses = sorted(r.courses, key=lambda c: c.sort_order)
    return {
        "id": str(r.id), "status": r.status, "version": r.version,
        "student": {
            "id": str(r.student_id), "name": r.student_name, "roll_no": r.student_roll, "college": r.college_name,
            "department": r.department_name, "degree": r.degree_name,
        },
        "semester_id": str(r.semester_id), "semester": r.semester_name, "academic_year": r.academic_year,
        "exam_label": r.exam_label,
        "result_status": r.result_status, "result_status_label": _RESULT_LABEL.get(r.result_status, r.result_status),
        "promoted_class": None, "admission_status": None, "class_label": None,
        "progression_note": _PROGRESSION_NOTE,
        "courses": [_course_dict(c) for c in courses],
        "totals": {"credits": _f(r.total_credits), "grade_points": _f(r.total_grade_points), "credit_points": _f(r.total_credit_points)},
        "gpa": format_gpa(r.gpa), "cgpa": format_gpa(cgpa), "cgpa_applicable": cgpa is not None,
        "compiled_at": _iso(r.compiled_at), "published_at": _iso(r.published_at),
    }


def _list_row(r: StudentSemesterResult) -> dict:
    return {
        "id": str(r.id), "degree": r.degree_name, "semester": r.semester_name, "semester_id": str(r.semester_id),
        "academic_year": r.academic_year, "result_status": r.result_status,
        "result_status_label": _RESULT_LABEL.get(r.result_status, r.result_status), "status": r.status,
        "gpa": format_gpa(r.gpa), "published_at": _iso(r.published_at),
    }


# ── Student: Result Tracking / Result Management ─────────────────────────────

@router.get("/tracking")
async def result_tracking(
    calendar_id: Optional[UUID] = None, semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """The student's OWN registered courses with a per-course status: `compiled`
    once a PUBLISHED result contains the course, otherwise `pending`. Nothing about
    unpublished results (or grades) is exposed here."""
    q = (
        select(StudentEnrollment).options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course).selectinload(Course.department),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.department),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.semester),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.calendar),
        ).join(CourseOffering, CourseOffering.id == StudentEnrollment.offering_id)
        .where(StudentEnrollment.student_id == user.id, StudentEnrollment.status == "approved")
    )
    enrollments = (await db.execute(q)).scalars().all()
    published = set((await db.execute(
        select(StudentSemesterResultCourse.offering_id)
        .join(StudentSemesterResult, StudentSemesterResult.id == StudentSemesterResultCourse.result_id)
        .where(StudentSemesterResult.student_id == user.id, StudentSemesterResult.status == "published")
    )).scalars().all())

    filters_years, filters_sems, rows = {}, {}, []
    for e in enrollments:
        o = e.offering
        filters_years[str(o.calendar_id)] = o.calendar.academic_year if o.calendar else None
        filters_sems[str(o.semester_id)] = {"semester_id": str(o.semester_id), "name": o.semester.name if o.semester else None, "calendar_id": str(o.calendar_id)}
        if (calendar_id and o.calendar_id != calendar_id) or (semester_id and o.semester_id != semester_id):
            continue
        rows.append({
            "offering_id": str(o.id), "course_code": o.course.course_number, "course_title": o.course.title,
            "course_department": (o.course.department.name if o.course.department else (o.department.name if o.department else None)),
            "semester": o.semester.name if o.semester else None, "academic_year": o.calendar.academic_year if o.calendar else None,
            "status": "compiled" if o.id in published else "pending",
        })
    rows.sort(key=lambda r: (r["academic_year"] or "", r["semester"] or "", r["course_code"]))
    return {
        "filters": {"academic_years": [{"calendar_id": k, "academic_year": v} for k, v in filters_years.items()], "semesters": list(filters_sems.values())},
        "rows": rows,
    }


@router.get("/mine")
async def my_results(
    calendar_id: Optional[UUID] = None, semester_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(UserRole.STUDENT)),
):
    """Result Management list — the student's own PUBLISHED semester results."""
    q = select(StudentSemesterResult).where(StudentSemesterResult.student_id == user.id, StudentSemesterResult.status == "published")
    if calendar_id:
        q = q.where(StudentSemesterResult.calendar_id == calendar_id)
    if semester_id:
        q = q.where(StudentSemesterResult.semester_id == semester_id)
    rows = (await db.execute(q.join(Semester, Semester.id == StudentSemesterResult.semester_id).order_by(Semester.start_date.desc()))).scalars().all()
    return [_list_row(r) for r in rows]


@router.get("/student/{student_id}")
async def student_results(student_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """A student's results for authorized viewers (the student, their HOD/Major
    Advisor, Incharge/DPGS — published only; CoE/Super Admin — all)."""
    await _authorize_student_scope(db, student_id, user, published=True)
    q = select(StudentSemesterResult).join(Semester, Semester.id == StudentSemesterResult.semester_id).where(StudentSemesterResult.student_id == student_id)
    if user.active_role not in (_COE, UserRole.SUPER_ADMIN):
        q = q.where(StudentSemesterResult.status == "published")
    rows = (await db.execute(q.order_by(Semester.start_date.desc()))).scalars().all()
    return [_list_row(r) for r in rows]


# ── CoE: compile & publish ───────────────────────────────────────────────────

@router.get("/coe/semesters")
async def coe_semesters(db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(_COE))):
    rows = (await db.execute(
        select(Semester).options(selectinload(Semester.calendar)).where(Semester.id.in_(
            select(CourseOffering.semester_id).join(StudentEnrollment, StudentEnrollment.offering_id == CourseOffering.id)
            .where(StudentEnrollment.status == "approved")
        )).order_by(Semester.start_date.desc())
    )).scalars().all()
    return [{"semester_id": str(s.id), "name": s.name, "calendar_id": str(s.calendar_id), "academic_year": s.calendar.academic_year if s.calendar else None} for s in rows]


@router.get("/coe/semesters/{semester_id}/students")
async def coe_semester_students(semester_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(_COE))):
    """Every student with an approved registration in the semester, how many of their
    courses have a finalized gradesheet entry, and their compiled result (if any)."""
    if not await db.get(Semester, semester_id):
        raise HTTPException(404, "Semester not found.")
    enrollments = (await db.execute(
        select(StudentEnrollment).options(
            selectinload(StudentEnrollment.student), selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
        ).join(CourseOffering, CourseOffering.id == StudentEnrollment.offering_id)
        .where(CourseOffering.semester_id == semester_id, StudentEnrollment.status == "approved")
    )).scalars().all()
    finalized = await _finalized_entries(db, list({e.offering_id for e in enrollments}))
    results = {r.student_id: r for r in (await db.execute(select(StudentSemesterResult).where(StudentSemesterResult.semester_id == semester_id))).scalars().all()}

    by_student: dict = {}
    for e in enrollments:
        s = by_student.setdefault(e.student_id, {"student": e.student, "total": 0, "done": 0, "pending": []})
        s["total"] += 1
        if (e.offering_id, e.student_id) in finalized:
            s["done"] += 1
        else:
            s["pending"].append(e.offering.course.course_number)
    out = []
    for sid, s in by_student.items():
        r = results.get(sid)
        out.append({
            "student_id": str(sid), "name": s["student"].full_name, "roll_no": s["student"].student_roll,
            "courses_total": s["total"], "courses_finalized": s["done"], "pending_courses": sorted(s["pending"]),
            "ready": s["total"] > 0 and s["done"] == s["total"],
            "result": {"id": str(r.id), "status": r.status, "result_status": r.result_status, "gpa": format_gpa(r.gpa)} if r else None,
        })
    out.sort(key=lambda x: (x["roll_no"] or "", x["name"] or ""))
    return out


async def _compile_one(db: AsyncSession, coe_id: UUID, semester: Semester, student_id: UUID, result_status: str) -> StudentSemesterResult:
    student = (await db.execute(
        select(User).options(selectinload(User.college), selectinload(User.program), selectinload(User.department))
        .where(User.id == student_id, student_user_clause()).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not student:
        raise HTTPException(404, "Student not found.")
    enrollments = (await db.execute(
        select(StudentEnrollment).options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course),
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.department),
        ).join(CourseOffering, CourseOffering.id == StudentEnrollment.offering_id)
        .where(StudentEnrollment.student_id == student_id, StudentEnrollment.status == "approved", CourseOffering.semester_id == semester.id)
    )).scalars().all()
    if not enrollments:
        raise HTTPException(400, "The student has no approved course registration in this semester.")
    finalized = await _finalized_entries(db, [e.offering_id for e in enrollments], [student_id])
    missing = sorted(e.offering.course.course_number for e in enrollments if (e.offering_id, student_id) not in finalized)
    if missing:
        raise HTTPException(409, f"Finalized (CoE-approved) gradesheets are still pending for: {', '.join(missing)}.")

    existing = (await db.execute(
        select(StudentSemesterResult).options(selectinload(StudentSemesterResult.courses))
        .where(StudentSemesterResult.student_id == student_id, StudentSemesterResult.semester_id == semester.id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if existing and existing.status == "published":
        raise HTTPException(409, "This result is already published and cannot be recompiled (reopening rules are pending AVFU confirmation).")

    lines, total_credits, total_gp, total_cp = [], Decimal("0"), Decimal("0"), Decimal("0")
    for n, e in enumerate(sorted(enrollments, key=lambda x: x.offering.course.course_number)):
        entry, sheet = finalized[(e.offering_id, student_id)]
        course = e.offering.course
        credits = Decimal(course.total_credits)
        gp = Decimal(str(entry.grade_points))
        cp = gp * credits
        total_credits += credits; total_gp += gp; total_cp += cp
        lines.append(StudentSemesterResultCourse(
            offering_id=e.offering_id, course_id=course.id, gradesheet_id=sheet.id, entry_id=entry.id,
            course_number=course.course_number, course_title=course.title,
            department_name=e.offering.department.name if e.offering.department else None,
            credit_structure=_credit_display(course), credits=credits, grade_letter=entry.grade_letter,
            grade_points=gp, credit_points=cp, marks_percent=entry.marks_percent, sort_order=n,
        ))

    cal = semester.calendar
    exam_date = semester.exam_end or semester.exam_start
    snapshot = dict(
        student_name=student.full_name, student_roll=student.student_roll,
        college_name=(student.college.name if student.college else (student.department.stream if student.department else None)),
        department_name=student.department.name if student.department else None,
        degree_name=student.program.name if student.program else None,
        semester_name=semester.name, academic_year=cal.academic_year if cal else None, calendar_id=semester.calendar_id,
        exam_label=f"{exam_date.strftime('%B %Y')} Examination" if exam_date else None,
        result_status=result_status, status="compiled", gpa=compute_gpa(total_cp, total_credits),
        total_credits=total_credits, total_grade_points=total_gp, total_credit_points=total_cp,
        compiled_by=coe_id, compiled_at=datetime.now(timezone.utc),
    )
    if existing:
        existing.courses.clear()
        await db.flush()
        for k, v in snapshot.items():
            setattr(existing, k, v)
        existing.version = (existing.version or 1) + 1
        existing.courses.extend(lines)
        result = existing
    else:
        result = StudentSemesterResult(student_id=student_id, semester_id=semester.id, **snapshot)
        result.courses = lines
        db.add(result)
    await db.flush()
    return result


@router.post("/coe/compile")
async def coe_compile(body: CompileIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(_COE))):
    """Compile student-wise semester results from finalized gradesheets and record
    the CoE's manual Pass / Pass with Backlogs decision. Each student is processed
    independently: failures are reported per student and never block the rest."""
    coe_id = user.id  # captured up front: a per-student rollback below expires every loaded object
    async def _semester():
        return (await db.execute(
            select(Semester).options(selectinload(Semester.calendar)).where(Semester.id == body.semester_id).execution_options(populate_existing=True)
        )).scalar_one_or_none()
    if not await _semester():
        raise HTTPException(404, "Semester not found.")
    compiled, errors = [], []
    for item in body.items:
        try:
            r = await _compile_one(db, coe_id, await _semester(), item.student_id, item.result_status)
            await db.commit()
            compiled.append({"student_id": str(item.student_id), "result_id": str(r.id), "result_status": r.result_status, "gpa": format_gpa(r.gpa), "version": r.version})
        except HTTPException as exc:
            await db.rollback()
            errors.append({"student_id": str(item.student_id), "detail": exc.detail})
    return {"compiled": compiled, "errors": errors}


@router.post("/coe/publish")
async def coe_publish(body: PublishIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(_COE))):
    """Publish compiled results (makes them visible to the student). Publishing
    changes visibility only — no promotion or progression side effects."""
    published, errors = [], []
    coe_id = user.id
    for rid in body.result_ids:
        r = (await db.execute(select(StudentSemesterResult).where(StudentSemesterResult.id == rid).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if not r:
            errors.append({"result_id": str(rid), "detail": "Result not found."})
        elif r.status == "published":
            errors.append({"result_id": str(rid), "detail": "Already published."})
        else:
            r.status = "published"
            r.published_by = coe_id
            r.published_at = datetime.now(timezone.utc)
            published.append(str(rid))
    await db.commit()
    return {"published": published, "errors": errors}


# ── Result / Marksheet / Grade Card (declared last: `{result_id}` is a wildcard) ─

@router.get("/{result_id}")
async def result_details(result_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Student Result and Admission Details + Semester Marksheet (courses/grades, GPA,
    CGPA). Authorized per student (own published result for students)."""
    r = await _authorize_result(db, await _load_result(db, result_id), user)
    return await _result_detail(db, r)


@router.get("/{result_id}/grade-card")
async def grade_card(result_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Statement of Marks/Grades PDF for the semester result (on demand)."""
    r = await _authorize_result(db, await _load_result(db, result_id), user)
    from app.utils.pdf import get_logo_data_uri
    detail = await _result_detail(db, r)
    detail["logo_data_uri"] = get_logo_data_uri()
    detail["dated"] = r.published_at or datetime.now(timezone.utc)
    html = render_template("grade_card.html", detail)
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", r.student_roll or str(r.student_id))
    return await pdf_response(html, f"GradeCard-{safe}-{re.sub(r'[^A-Za-z0-9_-]', '-', r.semester_name or 'semester')}.pdf", "Grade Card")
