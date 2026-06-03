"""Admit Card generation with UID + email OTP verification (one-time download)."""
import random, string, smtplib
from datetime import datetime, timezone, timedelta
from typing import List
from uuid import UUID

from email.mime.text import MIMEText
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.core.config import settings
from app.models.user import User, UserRole
from app.models.academic import Semester, AcademicCalendar
from app.models.course import CourseOffering
from app.models.enrollment import StudentEnrollment
from app.models.admit_card import AdmitCard

router = APIRouter(prefix="/admit-card", tags=["Admit Card"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send_otp_email(to: str, student_name: str, otp: str, semester_name: str) -> None:
    if not settings.SMTP_USER:
        return
    body = (
        f"Dear {student_name},\n\n"
        f"Your OTP for Admit Card generation (Semester: {semester_name}) is:\n\n"
        f"    {otp}\n\n"
        f"This OTP is valid for 10 minutes. Do not share it with anyone.\n\n"
        f"AVFU Academic Management System"
    )
    msg = MIMEText(body, "plain")
    msg["Subject"] = f"Admit Card OTP — {semester_name}"
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as s:
            s.starttls()
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.sendmail(settings.SMTP_FROM, [to], msg.as_string())
    except Exception:
        pass


async def _build_admit_card_data(card: AdmitCard, db: AsyncSession) -> dict:
    """Assemble full admit card payload including enrolled courses."""
    student = card.student
    semester = card.semester

    # Get approved enrollments for this student in this semester
    q = (
        select(StudentEnrollment)
        .where(
            StudentEnrollment.student_id == card.student_id,
            StudentEnrollment.status == "approved",
        )
        .options(
            selectinload(StudentEnrollment.offering).selectinload(CourseOffering.course)
        )
    )
    result = await db.execute(q)
    enrollments = result.scalars().all()

    # Filter to offerings in this semester
    courses = []
    for e in enrollments:
        if e.offering and str(e.offering.semester_id) == str(semester.id):
            o = e.offering
            c = o.course
            courses.append({
                "course_number": c.course_number if c else "—",
                "course_title": c.title if c else "—",
                "credit_structure": c.credit_structure if c else "—",
                "section": o.section,
            })

    # Calendar info
    cal = await db.get(AcademicCalendar, semester.calendar_id)

    return {
        "admit_card_id": str(card.id),
        "student": {
            "id": str(student.id),
            "full_name": student.full_name,
            "roll_number": student.student_roll or "—",
            "uid": card.uid,
            "email": student.email,
        },
        "exam": {
            "semester_name": semester.name,
            "academic_year": cal.academic_year if cal else "—",
            "exam_start": semester.exam_start.isoformat() if semester.exam_start else None,
            "exam_end": semester.exam_end.isoformat() if semester.exam_end else None,
        },
        "courses": courses,
        "generated_at": card.generated_at.isoformat(),
        "downloaded_at": card.downloaded_at.isoformat() if card.downloaded_at else None,
        "is_downloaded": card.is_downloaded,
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class OtpRequest(BaseModel):
    semester_id: UUID
    uid: str

class GenerateRequest(BaseModel):
    semester_id: UUID
    uid: str
    otp: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/request-otp")
async def request_otp(
    body: OtpRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Student requests OTP for admit card generation. Validates UID against their profile."""
    if user.role != UserRole.STUDENT:
        raise HTTPException(403, "Only students can request admit card OTPs.")

    semester = await db.get(Semester, body.semester_id)
    if not semester:
        raise HTTPException(404, "Semester not found.")

    # Validate UID — must match student's roll or employee_id
    provided_uid = body.uid.strip().upper()
    roll = (user.student_roll or "").upper()
    emp = (user.employee_id or "").upper()
    if provided_uid not in (roll, emp) and provided_uid != str(user.id).upper():
        raise HTTPException(400, "UID does not match your registered student ID. Please check and try again.")

    # Check if already downloaded
    existing = await db.execute(
        select(AdmitCard).where(
            AdmitCard.student_id == user.id,
            AdmitCard.semester_id == body.semester_id,
        )
    )
    card = existing.scalar_one_or_none()
    if card and card.is_downloaded:
        raise HTTPException(400, "You have already downloaded your admit card for this semester. Each student can download it only once.")

    # Generate OTP
    otp_code = "".join(random.choices(string.digits, k=6))
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=10)

    if card:
        card.otp_code = otp_code
        card.otp_expires_at = expires
        card.otp_verified = False
        card.uid = provided_uid
    else:
        # Check that student has at least one approved enrollment in this semester
        enr_q = (
            select(StudentEnrollment)
            .join(CourseOffering, StudentEnrollment.offering_id == CourseOffering.id)
            .where(
                StudentEnrollment.student_id == user.id,
                StudentEnrollment.status == "approved",
                CourseOffering.semester_id == body.semester_id,
            )
        )
        enr_result = await db.execute(enr_q)
        if not enr_result.scalars().first():
            raise HTTPException(400, "You have no approved enrollments in this semester. Admit card cannot be generated.")

        card = AdmitCard(
            student_id=user.id,
            semester_id=body.semester_id,
            uid=provided_uid,
            otp_code=otp_code,
            otp_expires_at=expires,
        )
        db.add(card)

    await db.commit()

    _send_otp_email(user.email, user.full_name, otp_code, semester.name)

    return {
        "message": f"OTP sent to {user.email}. Valid for 10 minutes.",
        "dev_otp": otp_code,  # Remove in production
    }


@router.post("/generate")
async def generate_admit_card(
    body: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Verify OTP and return admit card data. Marks as downloaded — one-time only."""
    if user.role != UserRole.STUDENT:
        raise HTTPException(403, "Only students can generate admit cards.")

    result = await db.execute(
        select(AdmitCard)
        .where(AdmitCard.student_id == user.id, AdmitCard.semester_id == body.semester_id)
        .options(
            selectinload(AdmitCard.student),
            selectinload(AdmitCard.semester),
        )
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(404, "No OTP request found. Please request an OTP first.")

    if card.is_downloaded:
        raise HTTPException(400, "Admit card already downloaded. Each student can download it only once.")

    # Verify OTP
    now = datetime.now(timezone.utc)
    if not card.otp_expires_at or card.otp_expires_at < now:
        raise HTTPException(400, "OTP has expired. Please request a new one.")
    if card.otp_code != body.otp.strip():
        raise HTTPException(400, "Incorrect OTP. Please check and try again.")

    # Mark as downloaded
    card.otp_verified = True
    card.is_downloaded = True
    card.downloaded_at = now
    await db.commit()

    # Reload with relationships
    result2 = await db.execute(
        select(AdmitCard)
        .where(AdmitCard.id == card.id)
        .options(selectinload(AdmitCard.student), selectinload(AdmitCard.semester))
    )
    card = result2.scalar_one()
    return await _build_admit_card_data(card, db)


@router.get("/my")
async def my_admit_cards(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Student views their own admit card records."""
    result = await db.execute(
        select(AdmitCard)
        .where(AdmitCard.student_id == user.id)
        .options(selectinload(AdmitCard.student), selectinload(AdmitCard.semester))
        .order_by(AdmitCard.generated_at.desc())
    )
    cards = result.scalars().all()
    return [await _build_admit_card_data(c, db) for c in cards]


@router.get("/all")
async def all_admit_cards(
    semester_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.EXAMINER, UserRole.REGISTRAR)),
):
    """Admin/Examiner views all generated admit cards."""
    q = (
        select(AdmitCard)
        .options(selectinload(AdmitCard.student), selectinload(AdmitCard.semester))
        .order_by(AdmitCard.generated_at.desc())
    )
    if semester_id:
        q = q.where(AdmitCard.semester_id == semester_id)
    result = await db.execute(q)
    cards = result.scalars().all()
    return [await _build_admit_card_data(c, db) for c in cards]


@router.get("/{card_id}")
async def get_admit_card(
    card_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """View a specific admit card (admin) or your own (student)."""
    result = await db.execute(
        select(AdmitCard)
        .where(AdmitCard.id == card_id)
        .options(selectinload(AdmitCard.student), selectinload(AdmitCard.semester))
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(404, "Admit card not found.")

    is_admin = user.role in (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.EXAMINER, UserRole.REGISTRAR)
    if not is_admin and card.student_id != user.id:
        raise HTTPException(403, "Access denied.")

    return await _build_admit_card_data(card, db)
