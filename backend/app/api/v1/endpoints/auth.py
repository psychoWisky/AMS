"""AMS Authentication — login, refresh, me, user management."""
import hashlib, random, string, smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel, EmailStr

from app.db.base import get_db
from app.core.security import verify_password, hash_password, create_access_token, create_refresh_token, verify_token
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, RefreshToken

router = APIRouter(prefix="/auth", tags=["Auth"])


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


def _user_dict(u: User) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role.value,
        "designation": u.designation,
        "department_id": str(u.department_id) if u.department_id else None,
        "program_id": str(u.program_id) if u.program_id else None,
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

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(target, field, value)

    await db.commit()
    return {"message": "User updated."}


@router.get("/users")
async def list_users(
    role: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN, UserRole.REGISTRAR, UserRole.HOD, UserRole.EXAMINER)),
):
    q = select(User).where(User.is_active == True)
    if role:
        q = q.where(User.role == role)
    result = await db.execute(q.order_by(User.first_name))
    return [_user_dict(u) for u in result.scalars().all()]
