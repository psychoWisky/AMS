"""Academic Calendar & Semester management (Module 4)."""
from typing import Optional, List
from uuid import UUID
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole
from app.models.academic import AcademicCalendar, Semester
from app.models.course import CourseOffering
from app.models.enrollment import CourseRegistration
from app.models.admit_card import AdmitCard

router = APIRouter(prefix="/academic", tags=["Academic Calendar"])


class CalendarIn(BaseModel):
    name: str
    academic_year: str
    start_date: date
    end_date: date
    description: Optional[str] = None

class CalendarOut(BaseModel):
    id: UUID; name: str; academic_year: str
    start_date: date; end_date: date
    status: str; description: Optional[str]
    model_config = {"from_attributes": True}

class SemesterIn(BaseModel):
    calendar_id: UUID
    name: str
    sem_type: str = "odd"
    start_date: date; end_date: date
    registration_start: Optional[date] = None
    registration_end: Optional[date] = None
    exam_start: Optional[date] = None
    exam_end: Optional[date] = None
    result_declaration: Optional[date] = None
    holidays: Optional[list] = None

class SemesterOut(BaseModel):
    id: UUID; calendar_id: UUID; name: str; sem_type: str
    start_date: date; end_date: date; status: str
    registration_start: Optional[date]; registration_end: Optional[date]
    exam_start: Optional[date]; exam_end: Optional[date]
    result_declaration: Optional[date]
    holidays: Optional[list]
    model_config = {"from_attributes": True}


# ── Calendars ─────────────────────────────────────────────────────────────────

@router.get("/calendars", response_model=List[CalendarOut])
async def list_calendars(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(AcademicCalendar).order_by(AcademicCalendar.start_date.desc()))
    return result.scalars().all()


@router.post("/calendars", response_model=CalendarOut, status_code=201)
async def create_calendar(
    body: CalendarIn, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    cal = AcademicCalendar(**body.model_dump(), created_by=user.id)
    db.add(cal); await db.commit(); await db.refresh(cal)
    return cal


@router.get("/calendars/{cal_id}", response_model=CalendarOut)
async def get_calendar(cal_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    cal = await db.get(AcademicCalendar, cal_id)
    if not cal: raise HTTPException(404, "Calendar not found.")
    return cal


@router.patch("/calendars/{cal_id}/status")
async def update_calendar_status(
    cal_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    cal = await db.get(AcademicCalendar, cal_id)
    if not cal: raise HTTPException(404, "Calendar not found.")
    cal.status = status; await db.commit()
    return {"message": f"Status updated to {status}."}


@router.put("/calendars/{cal_id}")
async def update_calendar(
    cal_id: UUID, body: CalendarIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    cal = await db.get(AcademicCalendar, cal_id)
    if not cal: raise HTTPException(404, "Calendar not found.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(cal, k, v)
    await db.commit(); return {"message": "Updated."}


@router.delete("/calendars/{cal_id}", status_code=204)
async def delete_calendar(
    cal_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    """Delete an Academic Year (Calendar) — only while it is still DRAFT
    (this task's confirmed requirement). Enforced here regardless of what the
    frontend shows/hides. Semesters belonging to this calendar cascade away
    automatically (ON DELETE CASCADE, pre-existing schema behavior — a
    Semester is a structural child of its own Calendar, not independent
    transactional data). Anything that could represent real transactional
    activity — course offerings, course registrations, or admit cards, either
    linked directly to the calendar or to one of its semesters — is checked
    explicitly first and blocks the delete with a clear error, mirroring
    courses.py's delete_course pattern. Nothing else references
    ams_academic_calendars (confirmed by inspecting every FK in the schema) —
    Orientation/Admission/PPW records key off a plain academic_year string,
    never this calendar row, so they can never be affected by this."""
    cal = await db.get(AcademicCalendar, cal_id)
    if not cal: raise HTTPException(404, "Calendar not found.")
    if cal.status != "draft":
        raise HTTPException(400, "Only a DRAFT academic year can be deleted.")

    semester_ids = (await db.execute(select(Semester.id).where(Semester.calendar_id == cal_id))).scalars().all()

    offering_exists = await db.execute(select(CourseOffering.id).where(
        (CourseOffering.calendar_id == cal_id) | (CourseOffering.semester_id.in_(semester_ids))
    ).limit(1))
    if offering_exists.scalar_one_or_none():
        raise HTTPException(400, "This academic year has course offerings and cannot be deleted. Remove them first.")

    registration_exists = await db.execute(select(CourseRegistration.id).where(
        (CourseRegistration.calendar_id == cal_id) | (CourseRegistration.semester_id.in_(semester_ids))
    ).limit(1))
    if registration_exists.scalar_one_or_none():
        raise HTTPException(400, "This academic year has course registrations and cannot be deleted. Remove them first.")

    if semester_ids:
        admit_card_exists = await db.execute(select(AdmitCard.id).where(AdmitCard.semester_id.in_(semester_ids)).limit(1))
        if admit_card_exists.scalar_one_or_none():
            raise HTTPException(400, "This academic year has admit cards issued against it and cannot be deleted. Remove them first.")

    await db.delete(cal)
    await db.commit()


# ── Semesters ─────────────────────────────────────────────────────────────────

@router.get("/calendars/{cal_id}/semesters", response_model=List[SemesterOut])
async def list_semesters(cal_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Semester).where(Semester.calendar_id == cal_id).order_by(Semester.start_date))
    return result.scalars().all()


@router.post("/semesters", response_model=SemesterOut, status_code=201)
async def create_semester(
    body: SemesterIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    sem = Semester(**body.model_dump())
    db.add(sem); await db.commit(); await db.refresh(sem)
    return sem


@router.get("/semesters/{sem_id}", response_model=SemesterOut)
async def get_semester(sem_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    sem = await db.get(Semester, sem_id)
    if not sem: raise HTTPException(404, "Semester not found.")
    return sem


@router.put("/semesters/{sem_id}")
async def update_semester(
    sem_id: UUID, body: SemesterIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    sem = await db.get(Semester, sem_id)
    if not sem: raise HTTPException(404, "Semester not found.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(sem, k, v)
    await db.commit(); return {"message": "Updated."}


@router.patch("/semesters/{sem_id}/status")
async def update_semester_status(
    sem_id: UUID, status: str, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    sem = await db.get(Semester, sem_id)
    if not sem: raise HTTPException(404, "Semester not found.")
    sem.status = status; await db.commit()
    return {"message": f"Semester status updated to {status}."}


@router.get("/semesters")
async def all_semesters(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Semester).order_by(Semester.start_date.desc()))
    return result.scalars().all()
