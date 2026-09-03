"""AMS Authentication — login, refresh, me, user management."""
import hashlib, random, string, smtplib
from email.mime.text import MIMEText
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel, EmailStr, field_validator

from app.db.base import get_db
from app.core.security import verify_password, hash_password, create_access_token, create_refresh_token, verify_token
from app.core.dependencies import get_current_user, require_roles, is_profile_complete, get_missing_profile_fields
from app.core.config import settings
from app.core.email import send_email
from app.models.user import User, UserRole, RefreshToken, Designation

router = APIRouter(prefix="/auth", tags=["Auth"])

# BUSINESS_LOGIC.md Section N.2 — implementation assumption: staff/faculty AVFU
# email domain, matching the convention already used by every seeded staff
# account (seed.py). Not a secret; not sourced from .env.
_AVFU_STAFF_EMAIL_DOMAIN = "avfu.ac.in"
_FACULTY_TITLES = ("Dr.", "Mr", "Mrs", "Miss")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict

class RefreshRequest(BaseModel):
    refresh_token: str

class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    role: UserRole
    designation: Optional[str] = None
    mobile: Optional[str] = None
    department_id: Optional[UUID] = None
    program_id: Optional[UUID] = None
    employee_id: Optional[str] = None
    student_roll: Optional[str] = None
    admission_year: Optional[int] = None

class UpdateUserRequest(BaseModel):
    role: Optional[UserRole] = None
    designation: Optional[str] = None
    mobile: Optional[str] = None
    department_id: Optional[UUID] = None
    program_id: Optional[UUID] = None
    employee_id: Optional[str] = None
    student_roll: Optional[str] = None
    admission_year: Optional[int] = None
    is_active: Optional[bool] = None

# Self-service profile edit (BUSINESS_LOGIC.md K.1). Deliberately a SEPARATE,
# narrower schema from UpdateUserRequest — that endpoint is admin-only and
# accepts any field via exclude_none; this one is field-restricted by design
# so a student can never write Batch Year/Degree/Roll No./Email through it.
class UpdateMyProfileRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    blood_group: Optional[str] = None
    mobile: Optional[str] = None
    father_name: Optional[str] = None
    abc_id: Optional[str] = None
    address: Optional[str] = None

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# HOD "Add Faculty" (BUSINESS_LOGIC.md Section N.2/N.3). Deliberately a
# separate, narrower schema from CreateUserRequest: role is always FACULTY,
# department is never client-supplied (forced to the caller's own department
# in the endpoint, not merely defaulted here), designation/title are
# closed-choice, and no password field exists — the initial password is
# always the AVFU email itself (Section N.2's confirmed demo rule).
class CreateFacultyRequest(BaseModel):
    title: Literal["Dr.", "Mr", "Mrs", "Miss"]
    first_name: str
    middle_name: Optional[str] = None
    last_name: str
    date_of_birth: date
    gender: str
    email: EmailStr
    mobile: str
    # Validated against active app.models.user.Designation rows at request
    # time (create_faculty), not a static Literal — designation-management
    # task, replaces the previously hardcoded 3-value list.
    designation: str
    address: str

    @field_validator("email")
    @classmethod
    def _must_be_avfu_email(cls, v: str) -> str:
        if not v.lower().endswith("@" + _AVFU_STAFF_EMAIL_DOMAIN):
            raise ValueError(f"Email must be an AVFU account (@{_AVFU_STAFF_EMAIL_DOMAIN}).")
        return v.lower()


def _user_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "title": u.title,
        "first_name": u.first_name,
        "middle_name": u.middle_name,
        "last_name": u.last_name,
        "mobile": u.mobile,
        "role": u.role.value,
        "designation": u.designation,
        "department_id": str(u.department_id) if u.department_id else None,
        "program_id": str(u.program_id) if u.program_id else None,
        "student_roll": u.student_roll,
        "date_of_birth": u.date_of_birth.isoformat() if u.date_of_birth else None,
        "gender": u.gender,
        "blood_group": u.blood_group,
        "father_name": u.father_name,
        "abc_id": u.abc_id,
        "address": u.address,
        "must_change_password": u.must_change_password,
        "profile_complete": is_profile_complete(u),
        "missing_profile_fields": get_missing_profile_fields(u),
    }


async def _issue_tokens(user: User, request: Request, db: AsyncSession) -> TokenResponse:
    access_token = create_access_token(str(user.id), {"role": user.role.value})
    refresh_token = create_refresh_token(str(user.id))
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    rt = RefreshToken(user_id=user.id, token_hash=token_hash, device_info=request.headers.get("user-agent", ""))
    db.add(rt)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token, user=_user_dict(user))


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated.")
    resp = await _issue_tokens(user, request, db)
    await db.commit()
    return resp


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = verify_token(payload.refresh_token, "refresh")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")
    token_hash = hashlib.sha256(payload.refresh_token.encode()).hexdigest()
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash, RefreshToken.is_revoked == False))
    stored = result.scalar_one_or_none()
    if not stored:
        raise HTTPException(status_code=401, detail="Refresh token revoked.")
    stored.is_revoked = True
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    resp = await _issue_tokens(user, request, db)
    await db.commit()
    return resp


@router.post("/logout")
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    token_hash = hashlib.sha256(payload.refresh_token.encode()).hexdigest()
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    token = result.scalar_one_or_none()
    if token:
        token.is_revoked = True
    await db.commit()
    return {"message": "Signed out."}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return _user_dict(user)


@router.patch("/me")
async def update_my_profile(
    body: UpdateMyProfileRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Self-service profile edit — any authenticated user may edit their own
    permitted fields. Field set is restricted by UpdateMyProfileRequest itself
    (see its docstring) rather than by role, so this is safe for any role to call."""
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    await db.commit()
    return _user_dict(user)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not user.hashed_password or not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    user.hashed_password = hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()
    return {"message": "Password changed."}


# ── Admin: create users ───────────────────────────────────────────────────────

@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered.")
    user = User(
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        first_name=body.first_name,
        last_name=body.last_name,
        role=body.role,
        designation=body.designation,
        mobile=body.mobile,
        department_id=body.department_id,
        program_id=body.program_id,
        employee_id=body.employee_id,
        student_roll=body.student_roll,
        admission_year=body.admission_year,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    await db.commit()
    return {"message": "User created.", "id": str(user.id)}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    demoting = body.role is not None and body.role != UserRole.SUPER_ADMIN
    deactivating = body.is_active is False
    if target.role == UserRole.SUPER_ADMIN and (demoting or deactivating):
        count_result = await db.execute(
            select(func.count()).select_from(User).where(
                User.role == UserRole.SUPER_ADMIN, User.is_active == True,
            )
        )
        if count_result.scalar() <= 1:
            raise HTTPException(status_code=400, detail="Cannot change role/status: at least one active Super Admin must remain.")

    # BUSINESS_LOGIC.md Section N (role transition): promoting a user to HOD
    # must never create a departmentless HOD — the target's EXISTING
    # department_id (not a client-supplied one) becomes their HOD scope, so if
    # they have none, the transition is rejected rather than silently
    # assigning a default/random department.
    new_role = body.role if body.role is not None else target.role
    effective_department_id = body.department_id if body.department_id is not None else target.department_id
    if new_role == UserRole.HOD and not effective_department_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot set role to HOD: this user has no department assigned. Assign a department first.",
        )

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(target, field, value)

    await db.commit()
    return {"message": "User updated."}


@router.get("/users")
async def list_users(
    role: Optional[str] = None,
    department_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR, UserRole.HOD, UserRole.EXAMINER)),
):
    q = select(User).where(User.is_active == True)
    if role:
        q = q.where(User.role == role)
    # BUSINESS_LOGIC.md Section N.4 — HOD is always scoped to their own
    # department, regardless of any client-supplied department_id. Other
    # roles keep their existing unrestricted behavior (unchanged).
    if user.role == UserRole.HOD:
        if not user.department_id:
            return []
        q = q.where(User.department_id == user.department_id)
    elif department_id:
        q = q.where(User.department_id == department_id)
    result = await db.execute(q.order_by(User.first_name))
    return [_user_dict(u) for u in result.scalars().all()]


# ── HOD: Add Faculty (BUSINESS_LOGIC.md Section N.2/N.3) ──────────────────────

@router.post("/faculty", status_code=201)
async def create_faculty(
    body: CreateFacultyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.HOD)),
):
    if not user.department_id:
        raise HTTPException(400, "Your account has no department assigned; contact an administrator.")

    # Designation-management task — backend-authoritative check against active
    # Designation master-data rows, replacing the previous static Literal.
    designation_check = await db.execute(
        select(Designation).where(func.lower(Designation.name) == body.designation.strip().lower(), Designation.is_active == True)
    )
    designation_row = designation_check.scalar_one_or_none()
    if not designation_row:
        raise HTTPException(400, "Designation is not a currently active option. Please select a valid designation.")

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "This email is already registered.")

    faculty = User(
        email=body.email,
        hashed_password=hash_password(body.email),  # initial password = AVFU email (Section N.2)
        title=body.title,
        first_name=body.first_name,
        middle_name=body.middle_name,
        last_name=body.last_name,
        date_of_birth=body.date_of_birth,
        gender=body.gender,
        mobile=body.mobile,
        designation=designation_row.name,
        address=body.address,
        role=UserRole.FACULTY,
        department_id=user.department_id,  # never client-supplied — always the HOD's own department
        is_active=True,
        is_verified=True,
        must_change_password=True,
    )
    db.add(faculty)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "This email is already registered.")
    await db.refresh(faculty)

    ams_link = settings.origins[0] if settings.origins else ""
    sent = send_email(
        faculty.email,
        "Your AVFU AMS Faculty Account",
        f"Dear {faculty.full_name},\n\nAn AVFU AMS account has been created for you.\n\n"
        f"AMS link: {ams_link}\nUsername (email): {faculty.email}\nInitial password: {faculty.email}\n\n"
        f"Please log in and change your password immediately.\n\nAVFU Academic Office",
    )
    return {
        "id": str(faculty.id),
        "message": "Faculty account created." + ("" if sent else " Credential email could not be sent — SMTP is not configured for this environment; share the login details manually."),
        "email_sent": sent,
    }
