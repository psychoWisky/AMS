"""Admission application endpoints."""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db.base import get_db
from app.core.config import settings
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Program, Department
from app.models.admission import AdmissionApplication

router = APIRouter(prefix="/admission", tags=["Admission"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024


async def _save_file(upload: UploadFile, dest_dir: str, field_name: str) -> str:
    """Read upload, enforce size limit, write to dest_dir, return relative path."""
    content = await upload.read()
    if len(content) > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{field_name} exceeds {settings.MAX_FILE_SIZE_MB} MB limit.",
        )
    ext = os.path.splitext(upload.filename or "")[1] or ""
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(dest_dir, filename)
    with open(filepath, "wb") as f:
        f.write(content)
    # Return relative path from UPLOAD_DIR
    return os.path.relpath(filepath, settings.UPLOAD_DIR).replace("\\", "/")


async def _generate_application_number(db: AsyncSession) -> str:
    year = datetime.now(timezone.utc).year
    result = await db.execute(select(func.count()).select_from(AdmissionApplication))
    count = result.scalar_one()
    return f"AVFU-{year}-{count + 1:05d}"


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

@router.get("/programs")
async def list_programs_for_apply(db: AsyncSession = Depends(get_db)):
    """List active programs for the admission apply dropdown."""
    result = await db.execute(
        select(Program, Department)
        .outerjoin(Department, Program.department_id == Department.id)
        .where(Program.is_active == True)
        .order_by(Program.name)
    )
    rows = result.all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "code": p.code,
            "level": p.level,
            "department_name": d.name if d else None,
        }
        for p, d in rows
    ]


@router.post("/apply", status_code=201)
async def apply_for_admission(
    db: AsyncSession = Depends(get_db),
    # Program & year
    program_id: Optional[str] = Form(None),
    academic_year: str = Form(...),
    category: str = Form(...),
    # Personal
    first_name: str = Form(...),
    middle_name: Optional[str] = Form(None),
    last_name: str = Form(...),
    date_of_birth: str = Form(...),          # ISO date string YYYY-MM-DD
    gender: str = Form(...),
    nationality: str = Form("Indian"),
    religion: Optional[str] = Form(None),
    mother_tongue: Optional[str] = Form(None),
    aadhar_number: str = Form(...),
    personal_email: str = Form(...),
    mobile: str = Form(...),
    alt_mobile: Optional[str] = Form(None),
    # Current address
    current_address: str = Form(...),
    current_city: str = Form(...),
    current_state: str = Form(...),
    current_pincode: str = Form(...),
    # Permanent address
    permanent_address: str = Form(...),
    permanent_city: str = Form(...),
    permanent_state: str = Form(...),
    permanent_pincode: str = Form(...),
    # Guardian
    father_name: str = Form(...),
    father_occupation: Optional[str] = Form(None),
    father_mobile: Optional[str] = Form(None),
    father_income: Optional[str] = Form(None),
    mother_name: str = Form(...),
    mother_occupation: Optional[str] = Form(None),
    mother_mobile: Optional[str] = Form(None),
    # Academics – 10th
    tenth_board: str = Form(...),
    tenth_school: str = Form(...),
    tenth_year: int = Form(...),
    tenth_percentage: float = Form(...),
    tenth_roll: Optional[str] = Form(None),
    # Academics – 12th
    twelfth_board: str = Form(...),
    twelfth_school: str = Form(...),
    twelfth_year: int = Form(...),
    twelfth_percentage: float = Form(...),
    twelfth_roll: Optional[str] = Form(None),
    twelfth_stream: str = Form(...),
    entrance_exam: Optional[str] = Form(None),
    entrance_score: Optional[float] = Form(None),
    # Documents
    doc_photo: Optional[UploadFile] = File(None),
    doc_signature: Optional[UploadFile] = File(None),
    doc_tenth_marksheet: Optional[UploadFile] = File(None),
    doc_twelfth_marksheet: Optional[UploadFile] = File(None),
    doc_aadhar: Optional[UploadFile] = File(None),
    doc_category_cert: Optional[UploadFile] = File(None),
    doc_transfer_cert: Optional[UploadFile] = File(None),
):
    """Public endpoint — submit an admission application."""
    # Parse date_of_birth
    from datetime import date as date_type
    try:
        dob = date_type.fromisoformat(date_of_birth)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_of_birth must be in YYYY-MM-DD format.",
        )

    # Reserve a new application ID so we can create the upload folder
    app_id = uuid.uuid4()
    application_number = await _generate_application_number(db)

    # Create upload directory
    dest_dir = os.path.join(settings.UPLOAD_DIR, "admissions", str(app_id))
    os.makedirs(dest_dir, exist_ok=True)

    # Save documents
    async def maybe_save(upload: Optional[UploadFile], field: str) -> Optional[str]:
        if upload and upload.filename:
            return await _save_file(upload, dest_dir, field)
        return None

    doc_paths = {
        "doc_photo":            await maybe_save(doc_photo, "doc_photo"),
        "doc_signature":        await maybe_save(doc_signature, "doc_signature"),
        "doc_tenth_marksheet":  await maybe_save(doc_tenth_marksheet, "doc_tenth_marksheet"),
        "doc_twelfth_marksheet": await maybe_save(doc_twelfth_marksheet, "doc_twelfth_marksheet"),
        "doc_aadhar":           await maybe_save(doc_aadhar, "doc_aadhar"),
        "doc_category_cert":    await maybe_save(doc_category_cert, "doc_category_cert"),
        "doc_transfer_cert":    await maybe_save(doc_transfer_cert, "doc_transfer_cert"),
    }

    application = AdmissionApplication(
        id=app_id,
        application_number=application_number,
        program_id=uuid.UUID(program_id) if program_id else None,
        academic_year=academic_year,
        category=category,
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        date_of_birth=dob,
        gender=gender,
        nationality=nationality,
        religion=religion,
        mother_tongue=mother_tongue,
        aadhar_number=aadhar_number,
        personal_email=personal_email,
        mobile=mobile,
        alt_mobile=alt_mobile,
        current_address=current_address,
        current_city=current_city,
        current_state=current_state,
        current_pincode=current_pincode,
        permanent_address=permanent_address,
        permanent_city=permanent_city,
        permanent_state=permanent_state,
        permanent_pincode=permanent_pincode,
        father_name=father_name,
        father_occupation=father_occupation,
        father_mobile=father_mobile,
        father_income=father_income,
        mother_name=mother_name,
        mother_occupation=mother_occupation,
        mother_mobile=mother_mobile,
        tenth_board=tenth_board,
        tenth_school=tenth_school,
        tenth_year=tenth_year,
        tenth_percentage=tenth_percentage,
        tenth_roll=tenth_roll,
        twelfth_board=twelfth_board,
        twelfth_school=twelfth_school,
        twelfth_year=twelfth_year,
        twelfth_percentage=twelfth_percentage,
        twelfth_roll=twelfth_roll,
        twelfth_stream=twelfth_stream,
        entrance_exam=entrance_exam,
        entrance_score=entrance_score,
        **doc_paths,
    )

    db.add(application)
    await db.commit()

    return {
        "application_number": application_number,
        "message": "Application submitted successfully. Please note your application number for future reference.",
    }


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

_admin_dep = Depends(
    require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)
)


@router.get("/applications")
async def list_applications(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = _admin_dep,
):
    """List all applications, optionally filtered by status."""
    query = select(AdmissionApplication).order_by(AdmissionApplication.submitted_at.desc())
    if status:
        query = query.where(AdmissionApplication.status == status)
    result = await db.execute(query)
    apps = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "application_number": a.application_number,
            "status": a.status,
            "first_name": a.first_name,
            "last_name": a.last_name,
            "personal_email": a.personal_email,
            "mobile": a.mobile,
            "program_id": str(a.program_id) if a.program_id else None,
            "academic_year": a.academic_year,
            "category": a.category,
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        }
        for a in apps
    ]


@router.get("/applications/{app_id}")
async def get_application(
    app_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = _admin_dep,
):
    """Full detail of a single application."""
    result = await db.execute(
        select(AdmissionApplication).where(AdmissionApplication.id == app_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    def doc_url(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return f"/api/v1/admission/documents/{app_id}/{os.path.basename(path)}"

    return {
        "id": str(app.id),
        "application_number": app.application_number,
        "status": app.status,
        "program_id": str(app.program_id) if app.program_id else None,
        "academic_year": app.academic_year,
        "category": app.category,
        # Personal
        "first_name": app.first_name,
        "middle_name": app.middle_name,
        "last_name": app.last_name,
        "date_of_birth": app.date_of_birth.isoformat() if app.date_of_birth else None,
        "gender": app.gender,
        "nationality": app.nationality,
        "religion": app.religion,
        "mother_tongue": app.mother_tongue,
        "aadhar_number": app.aadhar_number,
        "personal_email": app.personal_email,
        "mobile": app.mobile,
        "alt_mobile": app.alt_mobile,
        # Current address
        "current_address": app.current_address,
        "current_city": app.current_city,
        "current_state": app.current_state,
        "current_pincode": app.current_pincode,
        # Permanent address
        "permanent_address": app.permanent_address,
        "permanent_city": app.permanent_city,
        "permanent_state": app.permanent_state,
        "permanent_pincode": app.permanent_pincode,
        # Guardian
        "father_name": app.father_name,
        "father_occupation": app.father_occupation,
        "father_mobile": app.father_mobile,
        "father_income": app.father_income,
        "mother_name": app.mother_name,
        "mother_occupation": app.mother_occupation,
        "mother_mobile": app.mother_mobile,
        # Academics
        "tenth_board": app.tenth_board,
        "tenth_school": app.tenth_school,
        "tenth_year": app.tenth_year,
        "tenth_percentage": app.tenth_percentage,
        "tenth_roll": app.tenth_roll,
        "twelfth_board": app.twelfth_board,
        "twelfth_school": app.twelfth_school,
        "twelfth_year": app.twelfth_year,
        "twelfth_percentage": app.twelfth_percentage,
        "twelfth_roll": app.twelfth_roll,
        "twelfth_stream": app.twelfth_stream,
        "entrance_exam": app.entrance_exam,
        "entrance_score": app.entrance_score,
        # Documents (served via admin URL)
        "documents": {
            "photo":              doc_url(app.doc_photo),
            "signature":          doc_url(app.doc_signature),
            "tenth_marksheet":    doc_url(app.doc_tenth_marksheet),
            "twelfth_marksheet":  doc_url(app.doc_twelfth_marksheet),
            "aadhar":             doc_url(app.doc_aadhar),
            "category_cert":      doc_url(app.doc_category_cert),
            "transfer_cert":      doc_url(app.doc_transfer_cert),
        },
        # Review
        "reviewed_by": str(app.reviewed_by) if app.reviewed_by else None,
        "reviewed_at": app.reviewed_at.isoformat() if app.reviewed_at else None,
        "remarks": app.remarks,
        # Timestamps
        "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
        "created_at": app.created_at.isoformat() if app.created_at else None,
        "updated_at": app.updated_at.isoformat() if app.updated_at else None,
    }


class StatusUpdate:
    def __init__(self, status: str, remarks: Optional[str] = None):
        self.status = status
        self.remarks = remarks


from pydantic import BaseModel as _BM

class StatusUpdateBody(_BM):
    status: str
    remarks: Optional[str] = None


@router.patch("/applications/{app_id}/status")
async def update_application_status(
    app_id: uuid.UUID,
    body: StatusUpdateBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR)
    ),
):
    """Update status and optional remarks on an application."""
    valid_statuses = {"pending", "under_review", "accepted", "rejected", "waitlisted"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    result = await db.execute(
        select(AdmissionApplication).where(AdmissionApplication.id == app_id)
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found.")

    app.status = body.status
    app.remarks = body.remarks
    app.reviewed_by = current_user.id
    app.reviewed_at = datetime.now(timezone.utc)
    app.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {
        "id": str(app.id),
        "application_number": app.application_number,
        "status": app.status,
        "reviewed_by": str(app.reviewed_by),
        "reviewed_at": app.reviewed_at.isoformat(),
        "message": "Application status updated.",
    }


@router.get("/documents/{app_id}/{filename}")
async def serve_document(
    app_id: uuid.UUID,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: User = _admin_dep,
):
    """Serve an uploaded admission document file (admin only)."""
    # Safety: prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    file_path = os.path.join(settings.UPLOAD_DIR, "admissions", str(app_id), filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Document not found.")

    return FileResponse(file_path)
