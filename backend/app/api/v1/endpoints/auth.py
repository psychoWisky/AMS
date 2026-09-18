"""AMS Authentication — login, refresh, me, user management."""
import hashlib, io, random, string, smtplib, uuid as uuid_lib
from email.mime.text import MIMEText
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr, field_validator, model_validator
import openpyxl

from app.db.base import get_db
from app.core.security import verify_password, hash_password, create_access_token, create_refresh_token, decode_token
from app.core.dependencies import (
    get_current_user, require_roles, is_profile_complete, get_missing_profile_fields,
    get_assigned_roles, pick_default_role,
)
from app.core.config import settings
from app.core.email import enqueue_email
from app.core.bulk_upload import bulk_cell_to_str, parse_bulk_upload_file
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment, Designation, Department, College
# Programme<->Department many-to-many redesign — shared validation, whenever
# both fields are supplied together they must form a real association.
# Neither field becomes mandatory by importing this (Faculty/HOD keep working
# with Department-only or neither, exactly as today).
from app.api.v1.endpoints.departments import validate_program_department_pair

router = APIRouter(prefix="/auth", tags=["Auth"])

# BUSINESS_LOGIC.md Section N.2 — implementation assumption: staff/faculty AVFU
# email domain, matching the convention already used by every seeded staff
# account (seed.py). Not a secret; not sourced from .env.
_AVFU_STAFF_EMAIL_DOMAIN = "avfu.ac.in"
_FACULTY_TITLES = ("Dr.", "Mr", "Mrs", "Miss")

# Incharge Academic Cell / DPGS task (this revision) — Section 3.1/8's
# confirmed business rule: exactly one active holder of EACH of these two
# roles at a time (the same user MAY hold both simultaneously — that is a
# different constraint, not enforced here). See migration
# `0016_incharge_dpgs_roles` for the database-level partial unique index
# that is the actual source of truth; the check in add_user_role() below is
# a friendly pre-flight only.
_SINGLE_HOLDER_ROLES = (UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)
_ROLE_DISPLAY_NAMES = {
    UserRole.INCHARGE_ACADEMIC_CELL: "Incharge Academic Cell",
    UserRole.DPGS: "DPGS",
}


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
    middle_name: Optional[str] = None
    last_name: str
    role: UserRole
    designation: Optional[str] = None
    mobile: Optional[str] = None
    department_id: Optional[UUID] = None
    program_id: Optional[UUID] = None
    employee_id: Optional[str] = None
    student_roll: Optional[str] = None
    admission_year: Optional[int] = None

# Widened (this task's confirmed requirement) to also cover the fields Super
# Admin actually enters on the Add User form — first/middle/last name and
# email — so Edit User can correct the same fields Create User captured.
# Password is deliberately NOT here: password changes go through the
# dedicated change-password/admin-reset endpoints, never this generic
# field-patch endpoint. `email` is handled specially in update_user() (must
# stay lowercase + unique), so it is excluded from the generic setattr loop
# there even though it's listed here.
class UpdateUserRequest(BaseModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
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

# This task's confirmed requirement: the self-service change-password UI no
# longer collects the current password — `get_current_user` (JWT session)
# already authorizes the caller to change their own password, so a redundant
# current-password check is not required for authorization. `confirm_password`
# exists purely for API/UI-boundary validation (must match new_password) and
# is deliberately never persisted anywhere.
class ChangePasswordRequest(BaseModel):
    new_password: str
    confirm_password: str

    @model_validator(mode="after")
    def check_passwords_match(self):
        if len(self.new_password) < 8:
            raise ValueError("New password must be at least 8 characters.")
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirm password do not match.")
        return self


# Super Admin / Academic Admin administrative reset of another user's
# password (Issue 6). No old-password field — the admin does not know and is
# never shown the target's existing password. `confirm_password` is
# validated the same way as above and never stored.
class AdminResetPasswordRequest(BaseModel):
    new_password: str
    confirm_password: str

    @model_validator(mode="after")
    def check_passwords_match(self):
        if len(self.new_password) < 8:
            raise ValueError("New password must be at least 8 characters.")
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirm password do not match.")
        return self


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
    # Multi-role/role-switching task — `role` remains for legacy/display
    # compatibility (see User.role's docstring); `assigned_roles`/
    # `active_role` are the real authorization-relevant fields, populated by
    # get_current_user/_issue_tokens onto the transient `assigned_roles`/
    # `active_role` attributes. Falls back to `[u.role]`/`u.role` only for a
    # caller that built a `User` without going through either of those paths.
    assigned = getattr(u, "assigned_roles", None) or [u.role]
    active = getattr(u, "active_role", None) or u.role
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
        "assigned_roles": [r.value for r in assigned],
        "active_role": active.value,
        "designation": u.designation,
        "department_id": str(u.department_id) if u.department_id else None,
        # Bulk Faculty/User Excel Upload task (this revision) — College is
        # part of the user's own profile now (User.college_id).
        "college_id": str(u.college_id) if u.college_id else None,
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
    """Multi-role/role-switching task. The access token's old `role` claim
    was write-only dead data (confirmed by investigation: nothing ever read
    it back — `require_roles` always re-checked the live DB row). It is
    replaced by a `sid` claim pointing at the RefreshToken row created here,
    which is this app's natural per-login/per-device "session" unit; that
    row (not the token) is where the active role actually lives, re-read
    fresh on every request (app.core.dependencies.get_current_user) — the
    token still proves nothing about role by itself, exactly like `sub`
    already didn't prove account validity by itself (a revoked/deactivated
    user's token is still rejected by that same fresh DB lookup)."""
    assigned = await get_assigned_roles(user.id, db)
    if not assigned:
        # Defensive self-heal for a pre-migration/legacy account (see
        # get_current_user's identical fallback) — persist it so it's no
        # longer missing on the next login.
        assigned = [user.role]
        db.add(UserRoleAssignment(user_id=user.id, role=user.role))

    initial_active = pick_default_role(assigned, user.role)
    rt_id = uuid_lib.uuid4()
    access_token = create_access_token(str(user.id), {"sid": str(rt_id)})
    refresh_token = create_refresh_token(str(user.id))
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    rt = RefreshToken(
        id=rt_id, user_id=user.id, token_hash=token_hash,
        device_info=request.headers.get("user-agent", ""), active_role=initial_active,
    )
    db.add(rt)
    user.active_role = initial_active
    user.assigned_roles = assigned
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
    # Authorization for this action comes from the caller's authenticated
    # session (get_current_user / JWT) — this task's confirmed product
    # decision is that the caller is not additionally required to re-prove
    # knowledge of their current password. Match/length validation already
    # happened in ChangePasswordRequest; confirm_password is never stored.
    user.hashed_password = hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()
    return {"message": "Password changed."}


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: UUID,
    body: AdminResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Administrative password reset (Issue 6): Super Admin / Academic Admin
    sets a new password for another user without knowing or being shown the
    existing one. Reuses the same require_roles gate as create_user/update_user
    and the existing must_change_password convention (forces the target to
    pick their own password on next login), rather than introducing a new
    authorization mechanism."""
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.hashed_password = hash_password(body.new_password)
    target.must_change_password = True
    await db.commit()
    return {"message": "Password reset."}


# ── Admin: create users ───────────────────────────────────────────────────────

@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered.")
    await validate_program_department_pair(body.program_id, body.department_id, db)

    # Incharge Academic Cell / DPGS task (Section 3.1/8) — this legacy
    # create-with-a-single-role path must honor the SAME "exactly one active
    # holder" invariant as the dedicated add_user_role endpoint; checked
    # BEFORE any row is created so a rejection never leaves a half-created
    # user behind. The database's own partial unique index (migration 0016)
    # remains the race-condition-safe backstop for both paths.
    if body.role in _SINGLE_HOLDER_ROLES:
        other = (await db.execute(
            select(UserRoleAssignment).where(UserRoleAssignment.role == body.role)
        )).scalar_one_or_none()
        if other:
            holder = await db.get(User, other.user_id)
            raise HTTPException(
                status_code=409,
                detail=f"{_ROLE_DISPLAY_NAMES.get(body.role, body.role.value)} is already assigned to "
                       f"{holder.full_name if holder else 'another user'}"
                       f"{f' ({holder.email})' if holder else ''}. Remove that assignment first.",
            )

    user = User(
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        first_name=body.first_name,
        middle_name=body.middle_name,
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
    await db.flush()
    # Multi-role/role-switching task — every user must have at least one
    # UserRoleAssignment row from the moment they exist (Section 6/41's
    # "every existing user has at least one assigned role" invariant applies
    # to every NEW user too, not only the migration backfill).
    db.add(UserRoleAssignment(user_id=user.id, role=user.role, assigned_by=admin.id))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{_ROLE_DISPLAY_NAMES.get(body.role, body.role.value)} was just assigned to another user. Please try again."
            if body.role in _SINGLE_HOLDER_ROLES else "Could not create user (a conflicting record already exists).",
        )
    return {"message": "User created.", "id": str(user.id)}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
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

    # Programme<->Department many-to-many redesign — validate the EFFECTIVE
    # pair (after this patch is applied), not just whatever the request body
    # happens to include. Editing only Programme, or only Department, must
    # still be checked against whichever value the OTHER field already has —
    # e.g. changing Programme away from one that matches the student's
    # existing Department must be rejected, not silently saved.
    #
    # Only run this check when the request actually touches one of the two
    # fields — otherwise an update that doesn't mention program_id/
    # department_id at all (e.g. {"mobile": "..."}) would re-validate
    # whatever pair the target ALREADY has, and reject the request even for
    # unrelated fields on a user whose pre-existing pair is already
    # inconsistent (pre-commit review finding A1).
    if body.program_id is not None or body.department_id is not None:
        effective_program_id = body.program_id if body.program_id is not None else target.program_id
        await validate_program_department_pair(effective_program_id, effective_department_id, db)

    # email is handled explicitly (must stay lowercase + unique against other
    # users) rather than through the generic setattr loop below, since the DB
    # enforces a UNIQUE constraint on email and EmailStr does not lowercase.
    if body.email is not None:
        new_email = body.email.lower()
        if new_email != target.email:
            dupe = await db.execute(select(User).where(User.email == new_email, User.id != target.id))
            if dupe.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Email already registered.")
            target.email = new_email

    updates = body.model_dump(exclude_none=True)
    updates.pop("email", None)
    for field, value in updates.items():
        setattr(target, field, value)

    # Multi-role/role-switching task — this legacy single-role field remains
    # supported (Section 32: existing single-role admin flows keep working
    # unchanged), but setting it must never leave `User.role` pointing at a
    # role the user isn't actually assigned (Section 7's "must not remain an
    # independent authorization authority that can contradict assigned
    # roles"). This is purely ADDITIVE — it never removes another role a
    # Super Admin already granted via the new role-assignment endpoints.
    if body.role is not None:
        existing = await db.execute(
            select(UserRoleAssignment).where(UserRoleAssignment.user_id == target.id, UserRoleAssignment.role == body.role)
        )
        if not existing.scalar_one_or_none():
            # Incharge Academic Cell / DPGS task (Section 3.1/8) — same
            # "exactly one active holder" invariant as add_user_role, applied
            # here too since this legacy single-role field can also grant a
            # NEW assignment (see the comment above).
            if body.role in _SINGLE_HOLDER_ROLES:
                other = (await db.execute(
                    select(UserRoleAssignment).where(UserRoleAssignment.role == body.role)
                )).scalar_one_or_none()
                if other and other.user_id != target.id:
                    holder = await db.get(User, other.user_id)
                    raise HTTPException(
                        status_code=409,
                        detail=f"{_ROLE_DISPLAY_NAMES.get(body.role, body.role.value)} is already assigned to "
                               f"{holder.full_name if holder else 'another user'}"
                               f"{f' ({holder.email})' if holder else ''}. Remove that assignment first.",
                    )
            db.add(UserRoleAssignment(user_id=target.id, role=body.role, assigned_by=admin.id))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{_ROLE_DISPLAY_NAMES.get(body.role, body.role.value)} was just assigned to another user. Please try again."
            if body.role in _SINGLE_HOLDER_ROLES else "Could not update user (a conflicting record already exists).",
        )
    return {"message": "User updated."}


@router.get("/users")
async def list_users(
    role: Optional[str] = None,
    department_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    # Incharge Academic Cell / DPGS task (Section 10/32) — both are global,
    # read-only here (view users across every department, same as Super
    # Admin's existing unrestricted access below); they gain NO user-
    # management capability from this alone (create/update/role-assignment
    # endpoints remain SUPER_ADMIN-only, untouched by this task).
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.HOD, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS)),
):
    # .options(selectinload(User.department)) eager-loads the department in
    # the same query (one extra batched SELECT, not one per row) so the
    # Administration > Users "Department" column can display a name without
    # N+1 queries. Scoped to this endpoint only — _user_dict() itself is not
    # touched, so its other call sites (login/me, unaffected by this task)
    # don't need to eager-load anything they don't already use.
    q = select(User).where(User.is_active == True).options(selectinload(User.department))
    if role:
        q = q.where(User.role == role)
    # BUSINESS_LOGIC.md Section N.4 — HOD is always scoped to their own
    # department, regardless of any client-supplied department_id. Other
    # roles keep their existing unrestricted behavior (unchanged).
    if user.active_role == UserRole.HOD:
        if not user.department_id:
            return []
        q = q.where(User.department_id == user.department_id)
    elif department_id:
        q = q.where(User.department_id == department_id)
    result = await db.execute(q.order_by(User.first_name))
    users_out = []
    for u in result.scalars().all():
        d = _user_dict(u)
        d["department_name"] = u.department.name if u.department else None
        users_out.append(d)
    return users_out


# ── Multi-role / role-switching (this revision) ─────────────────────────────
#
# Assigned roles (UserRoleAssignment, DB-authoritative, Super Admin/Academic
# Admin managed) vs. active role (RefreshToken.active_role, session-scoped —
# see app.core.dependencies.get_current_user). `require_roles(...)` and
# every inline role branch across the backend already read `user.active_role`
# (never `user.role`, never anything client-supplied), so a role switch here
# takes effect on the very next request with no new token needed.

class SwitchRoleRequest(BaseModel):
    role: UserRole


class RoleAssignmentRequest(BaseModel):
    role: UserRole


@router.post("/switch-role")
async def switch_role(
    body: SwitchRoleRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Backend-authoritative role switch (Section 9/10). Verifies the
    requested role is actually assigned to THIS user (re-read fresh by
    get_current_user above, never trusted from the request) before touching
    anything — an unassigned role (including `super_admin`) is always
    rejected with 403, regardless of what the client sends."""
    if body.role not in user.assigned_roles:
        raise HTTPException(status_code=403, detail="This role is not assigned to your account.")

    session = getattr(user, "_active_session", None)
    if session is None:
        # Access token predates this feature (no `sid` claim) or its session
        # was revoked — role switching needs a live, identifiable session to
        # scope the change to. Short-lived access tokens make this a
        # narrow, self-resolving window; logging in again issues one.
        raise HTTPException(status_code=401, detail="Your session does not support role switching. Please log in again.")

    session.active_role = body.role
    user.active_role = body.role
    await db.commit()
    return _user_dict(user)


@router.get("/users/{user_id}/roles")
async def get_user_roles(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    assigned = await get_assigned_roles(user_id, db)
    return {"user_id": str(user_id), "assigned_roles": [r.value for r in assigned]}


@router.post("/users/{user_id}/roles", status_code=201)
async def add_user_role(
    user_id: UUID,
    body: RoleAssignmentRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Role assignment is Super Admin/Academic Admin-only (Section 12) — the
    same authorization already used for create_user/update_user above, never
    self-service and never reachable by a user for their own account through
    any other endpoint."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    # Preserve the existing HOD-requires-a-department business rule
    # (Section 29/30) — never silently assign a default department.
    if body.role == UserRole.HOD and not target.department_id:
        raise HTTPException(status_code=400, detail="Cannot assign HOD: this user has no department assigned. Assign a department first.")

    existing = await db.execute(
        select(UserRoleAssignment).where(UserRoleAssignment.user_id == user_id, UserRoleAssignment.role == body.role)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="This role is already assigned to the user.")

    # Incharge Academic Cell / DPGS task (Section 3.1/8) — exactly one active
    # holder each, enforced here as a friendly, pre-flight check AND at the
    # database level (partial unique index, migration 0016) as the
    # race-condition-safe backstop caught below. The SAME user may hold both
    # roles (this check only looks for a DIFFERENT existing holder of the
    # SAME role) — that is a completely separate, still-permitted case,
    # already handled by the plain duplicate check just above.
    if body.role in _SINGLE_HOLDER_ROLES:
        other = (await db.execute(
            select(UserRoleAssignment).where(UserRoleAssignment.role == body.role)
        )).scalar_one_or_none()
        if other:
            holder = await db.get(User, other.user_id)
            raise HTTPException(
                status_code=409,
                detail=f"{_ROLE_DISPLAY_NAMES.get(body.role, body.role.value)} is already assigned to "
                       f"{holder.full_name if holder else 'another user'}"
                       f"{f' ({holder.email})' if holder else ''}. Remove that assignment first before assigning it to someone else.",
            )

    db.add(UserRoleAssignment(user_id=user_id, role=body.role, assigned_by=admin.id))
    try:
        await db.commit()
    except IntegrityError:
        # Race-condition backstop (Section 8's explicit requirement) — two
        # concurrent assignment requests for the same single-holder role can
        # both pass the pre-flight check above; the database's own partial
        # unique index is the actual source of truth and rejects the loser.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{_ROLE_DISPLAY_NAMES.get(body.role, body.role.value)} was just assigned to another user. Please refresh and try again.",
        )
    assigned = await get_assigned_roles(user_id, db)
    return {"message": "Role assigned.", "assigned_roles": [r.value for r in assigned]}


@router.delete("/users/{user_id}/roles/{role}")
async def remove_user_role(
    user_id: UUID,
    role: UserRole,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Role removal takes effect immediately (Section 10/28): any session
    currently active in this role self-heals to a deterministic fallback on
    its very next request (app.core.dependencies.get_current_user — the
    session's `active_role` is re-validated against assignments on every
    call), never remaining authorized as the removed role merely because an
    old token/frontend cache still names it."""
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    assigned = await get_assigned_roles(user_id, db)
    if role not in assigned:
        raise HTTPException(status_code=404, detail="This role is not assigned to the user.")
    if len(assigned) <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove a user's only assigned role. Assign another role first.")

    if role == UserRole.SUPER_ADMIN:
        # Mirrors update_user's existing "at least one active Super Admin
        # must remain" invariant, but counts by ASSIGNMENT (not the legacy
        # scalar column) so it stays correct once a Super Admin can also
        # hold other roles.
        count_result = await db.execute(
            select(func.count(func.distinct(UserRoleAssignment.user_id)))
            .select_from(UserRoleAssignment)
            .join(User, User.id == UserRoleAssignment.user_id)
            .where(UserRoleAssignment.role == UserRole.SUPER_ADMIN, User.is_active == True)
        )
        if count_result.scalar() <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove: at least one active Super Admin must remain.")

    row = (await db.execute(
        select(UserRoleAssignment).where(UserRoleAssignment.user_id == user_id, UserRoleAssignment.role == role)
    )).scalar_one()
    await db.delete(row)

    remaining = [r for r in assigned if r != role]
    if target.role == role:
        # Keep the legacy display field from contradicting the assignment
        # set it's supposed to be consistent with (Section 7).
        target.role = pick_default_role(remaining, None)

    await db.commit()
    return {"message": "Role removed.", "assigned_roles": [r.value for r in remaining]}


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
        await db.flush()
        db.add(UserRoleAssignment(user_id=faculty.id, role=UserRole.FACULTY, assigned_by=user.id))
        # Bulk Upload SMTP timeout fix (this revision) — the credential email
        # is now enqueued into the SAME transaction/commit as the user and
        # role-assignment rows above (transactional outbox pattern), instead
        # of being sent synchronously with a blocking smtplib call after
        # commit. `faculty.full_name` reads plain in-memory attributes set
        # above — no DB round-trip needed before building the message.
        ams_link = settings.AMS_FRONTEND_URL
        enqueue_email(
            db, faculty.email,
            "Your AVFU AMS Faculty Account",
            f"Dear {faculty.full_name},\n\nAn AVFU AMS account has been created for you.\n\n"
            f"AMS link: {ams_link}\nUsername (email): {faculty.email}\nInitial password: {faculty.email}\n\n"
            f"Please log in and change your password immediately and complete your profile.\n\nAVFU Academic Office",
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "This email is already registered.")
    await db.refresh(faculty)

    return {
        "id": str(faculty.id),
        "message": "Faculty account created. Credential email queued for delivery.",
        "email_queued": True,
    }


# ── Bulk Faculty/User Excel Upload (this revision) ──────────────────────────
#
# Common Excel format, used identically at BOTH upload locations (HOD
# Faculties -> Bulk Upload, Super Admin User Management -> Bulk Upload):
#     First name | Middle name | Last name | AVFU email | Designation |
#     Role | College | Department | Gender | Mobile
# Middle name/Gender/Mobile are optional; every other column is required.
#
# Two separate, authorization-specific endpoints (never a single generic
# unrestricted upload endpoint) share one row-validation helper
# (`_validate_user_bulk_rows`) parameterized by `force_role`/
# `force_department_id` — the HOD endpoint passes both (Role/Department are
# NEVER taken from the file for that path, only checked against them, see
# that function's docstring); the Super Admin endpoint passes neither
# (Role/Department/College come from the file, subject to validation,
# exactly like Super Admin's existing individual `create_user`).
_USER_BULK_COLUMNS = [
    "First name", "Middle name", "Last name", "AVFU email", "Designation",
    "Role", "College", "Department", "Gender", "Mobile",
]
_USER_BULK_REQUIRED_COLUMNS = [c for c in _USER_BULK_COLUMNS if c not in ("Middle name", "Gender", "Mobile")]
_MAX_USER_BULK_BYTES = settings.MAX_FILE_SIZE_MB * 1024 * 1024


def _role_from_label(raw: str) -> Optional[UserRole]:
    """Case-insensitive role resolution against the REAL `UserRole` enum
    ("Faculty"/"FACULTY"/"faculty" all resolve to `UserRole.FACULTY`) —
    never a hard-coded string list, and never accepts a value that isn't an
    actual enum member."""
    key = raw.strip().lower()
    for r in UserRole:
        if r.value == key:
            return r
    return None


async def _validate_user_bulk_rows(
    rows: list[dict], db: AsyncSession, *,
    force_role: Optional[UserRole], force_department_id: Optional[UUID],
) -> tuple[list[dict], list[dict]]:
    """Phase 1 — validates the ENTIRE file against the database with no
    writes of its own. Returns (findings, valid_row_data); if `findings` is
    non-empty the caller must create nothing at all (all-or-nothing, mirrors
    orientation.py's bulk-upload philosophy exactly).

    Each finding is `{"row", "column", "value", "error"}` — a flat,
    row+column-addressable structure (Section 23's explicit requirement),
    not a single bundled message per row; a row with several problems
    produces several findings, since nothing here short-circuits on the
    first failure within a row.

    `force_role`/`force_department_id` (the HOD path): when set, the Excel's
    OWN Role/Department values are still read and validated AGAINST these —
    a mismatch is reported with the exact expected/received values — but the
    value actually used to create the user always comes from these
    parameters, never from the file itself. This is the load-bearing
    security property: a HOD can never use the Excel file to create a
    non-Faculty account or a colleague in another department, exactly
    mirroring `create_faculty`'s own department_id/role handling above.
    When both are `None` (the Super Admin path), Role/Department/College are
    taken from the file, subject to validation — `super_admin` is always
    rejected regardless (see `allowed_roles` below), matching Section 10's
    explicit requirement without inventing a further restriction beyond it.
    """
    designation_names = (await db.execute(select(Designation.name).where(Designation.is_active == True))).scalars().all()
    designation_idx = {d.strip().lower(): d for d in designation_names}

    department_rows = (await db.execute(select(Department.id, Department.code, Department.name))).all()
    department_by_code = {code.strip().lower(): did for did, code, name in department_rows}
    department_names = {did: name for did, code, name in department_rows}

    college_rows = (await db.execute(select(College.id, College.code))).all()
    college_by_code = {code.strip().lower(): cid for cid, code in college_rows}

    existing_emails = {e.lower() for e in (await db.execute(select(User.email))).scalars().all()}

    # Never SUPER_ADMIN through bulk upload, for either path (Section 10) —
    # for the HOD path this is moot (force_role already pins FACULTY), but
    # applying it unconditionally means the same rule can never accidentally
    # diverge between the two call sites. Incharge Academic Cell / DPGS task
    # (this revision) — these two are also excluded from bulk upload: the
    # "exactly one active holder" invariant has no per-row conflict handling
    # in this all-or-nothing bulk path, and Section 53's "no automatic
    # office-holder seeding" is safest served by requiring these two specific
    # roles to always go through the single-user assignment endpoints.
    allowed_roles = set(UserRole) - {UserRole.SUPER_ADMIN, UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS}

    hod_department_label = department_names.get(force_department_id) if force_department_id else None

    findings: list[dict] = []
    seen_emails: dict[str, int] = {}
    row_payload: dict[int, dict] = {}

    for entry in rows:
        row_no = entry["row"]
        v = entry["values"]
        row_findings: list[dict] = []

        def add(column: str, value: str, error: str) -> None:
            row_findings.append({"row": row_no, "column": column, "value": value, "error": error})

        for col in _USER_BULK_REQUIRED_COLUMNS:
            if not v.get(col):
                add(col, v.get(col, ""), f"{col} is required.")

        email_norm: Optional[str] = None
        raw_email = v.get("AVFU email", "")
        if raw_email:
            if not raw_email.lower().endswith("@" + _AVFU_STAFF_EMAIL_DOMAIN):
                add("AVFU email", raw_email, f"Invalid AVFU email address. Must be an @{_AVFU_STAFF_EMAIL_DOMAIN} address.")
            else:
                email_norm = raw_email.lower()

        role_value: Optional[UserRole] = None
        raw_role = v.get("Role", "")
        if raw_role:
            resolved_role = _role_from_label(raw_role)
            if force_role is not None:
                if resolved_role != force_role:
                    add("Role", raw_role, f'Expected "{force_role.value}", received "{raw_role.strip()}".')
                else:
                    role_value = force_role
            elif resolved_role is None:
                add("Role", raw_role, f"'{raw_role}' is not a recognized role.")
            elif resolved_role not in allowed_roles:
                add("Role", raw_role, f'Role "{resolved_role.value}" cannot be created through bulk upload.')
            else:
                role_value = resolved_role

        department_id_value: Optional[UUID] = None
        raw_dept = v.get("Department", "")
        if raw_dept:
            resolved_dept = department_by_code.get(raw_dept.strip().lower())
            if not resolved_dept:
                add("Department", raw_dept, f"Department '{raw_dept}' was not found.")
            elif force_department_id is not None and resolved_dept != force_department_id:
                suffix = f" (expected: {hod_department_label})" if hod_department_label else ""
                add("Department", raw_dept, f"Department does not match the authenticated HOD's department{suffix}.")
            else:
                department_id_value = resolved_dept

        college_id_value: Optional[UUID] = None
        raw_college = v.get("College", "")
        if raw_college:
            resolved_college = college_by_code.get(raw_college.strip().lower())
            if not resolved_college:
                add("College", raw_college, f"College '{raw_college}' was not found.")
            else:
                college_id_value = resolved_college

        designation_value: Optional[str] = None
        raw_desig = v.get("Designation", "")
        if raw_desig:
            resolved_desig = designation_idx.get(raw_desig.strip().lower())
            if not resolved_desig:
                add("Designation", raw_desig, f"Designation \"{raw_desig}\" is not an active designation.")
            else:
                designation_value = resolved_desig

        if email_norm:
            if email_norm in existing_emails:
                add("AVFU email", raw_email, "This email is already registered to an existing AMS user.")
            elif email_norm in seen_emails:
                other = seen_emails[email_norm]
                add("AVFU email", raw_email, f"Duplicate email within the uploaded file (also row {other}).")
                findings.append({
                    "row": other, "column": "AVFU email", "value": raw_email,
                    "error": f"Duplicate email within the uploaded file (also row {row_no}).",
                })
            else:
                seen_emails[email_norm] = row_no

        if row_findings:
            findings.extend(row_findings)
        elif email_norm and role_value and department_id_value and college_id_value and designation_value:
            row_payload[row_no] = {
                "first_name": v.get("First name", "").strip() or None,
                "middle_name": v.get("Middle name", "").strip() or None,
                "last_name": v.get("Last name", "").strip() or None,
                "email": email_norm,
                "designation": designation_value,
                "role": role_value,
                "college_id": college_id_value,
                "department_id": department_id_value,
                "gender": v.get("Gender", "").strip() or None,
                "mobile": v.get("Mobile", "").strip() or None,
            }

    # A row that looked valid in isolation may have been retroactively
    # invalidated once a LATER duplicate-email row was found above — drop it
    # from the create list (mirrors orientation.py's identical handling).
    invalidated_rows = {f["row"] for f in findings}
    for r in list(row_payload):
        if r in invalidated_rows:
            del row_payload[r]

    findings.sort(key=lambda f: (f["row"], f["column"]))
    valid = [row_payload[r] for r in sorted(row_payload)]
    return findings, valid


async def _create_bulk_users(valid_rows: list[dict], db: AsyncSession) -> list[User]:
    """Phase 2 — every row already passed validation; create them, their
    role assignments, AND their credential-email outbox rows all in ONE
    transaction (Section 30's explicit transactional-safety requirement,
    extended by the Bulk Upload SMTP timeout fix's transactional-outbox
    requirement: an EmailOutbox row must never exist without its user, or
    vice versa — both commit together or both roll back together).
    Password/must_change_password mirror `create_faculty` exactly (Section 7
    — the SAME mechanism, not a new one): initial password = the user's own
    AVFU email, hashed; `must_change_password=True` forces a reset on first
    login.

    Credential emails are no longer sent synchronously here (that was the
    root cause of the bulk-upload 504: a single Uvicorn worker blocked for
    the duration of N sequential blocking SMTP sends). Each user's email is
    now enqueued via `enqueue_email` in this same transaction; the
    standalone `app.core.email_worker` process delivers it afterwards,
    entirely outside this request."""
    created: list[User] = []
    for r in valid_rows:
        u = User(
            email=r["email"],
            hashed_password=hash_password(r["email"]),
            first_name=r["first_name"], middle_name=r["middle_name"], last_name=r["last_name"],
            designation=r["designation"], role=r["role"],
            college_id=r["college_id"], department_id=r["department_id"],
            gender=r["gender"], mobile=r["mobile"],
            is_active=True, is_verified=True, must_change_password=True,
        )
        db.add(u)
        created.append(u)
    try:
        await db.flush()
        ams_link = settings.AMS_FRONTEND_URL
        for u in created:
            db.add(UserRoleAssignment(user_id=u.id, role=u.role))
            enqueue_email(
                db, u.email,
                "Your AVFU AMS Account",
                f"Dear {u.full_name},\n\nAn AVFU AMS account has been created for you.\n\n"
                f"AMS link: {ams_link}\nUsername (email): {u.email}\nInitial password: {u.email}\n\n"
                f"Please log in and change your password immediately and complete your profile.\n\nAVFU Academic Office",
            )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            "One or more rows conflict with existing data (e.g. a duplicate AVFU Email or Employee ID). "
            "No users were created.",
        )
    for u in created:
        await db.refresh(u)
    return created


def _build_user_bulk_template_workbook() -> io.BytesIO:
    """Shared template-generation logic (Section 26's explicit "avoid
    duplicating the exact same template-generation logic in two unrelated
    places" instruction) — called by BOTH the HOD and Super Admin template
    endpoints below, so the two locations can never silently drift apart
    into two different column sets. Mirrors orientation.py's own
    template-download pattern (one clearly-marked, unimportable example
    row) without sharing code with it — that feature's own template
    generator is left untouched."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "AMS Users"
    ws.append(_USER_BULK_COLUMNS)
    # Marked example row — "EXAMPLE" is never a valid Role/Department/
    # College code, so even if left in by mistake it fails validation
    # loudly ("not a recognized role" / "was not found") rather than ever
    # being silently imported as a real person.
    ws.append([
        "EXAMPLE — DELETE THIS ROW", "", "Do Not Import",
        "example@avfu.ac.in", "<exact active Designation name>", "faculty",
        "<exact existing College code>", "<exact existing Department code>", "", "",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@router.post("/faculty/bulk-upload", status_code=201)
async def bulk_upload_faculty(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.HOD)),
):
    """HOD bulk upload (Section 8/9's explicit security rules). The
    authenticated HOD's OWN department is authoritative for EVERY row —
    never the Excel file's Department column, and role is always forced to
    FACULTY — never the Excel file's Role column. Both are still validated
    against the file's values so a mismatched row is reported clearly
    (row/column/value/expected), but a mismatch REJECTS THE ENTIRE upload
    rather than silently substituting the correct value, per the explicit
    "AVFU wants the uploaded file itself to be correct" instruction — this
    endpoint never silently corrects a wrong Department/Role."""
    if not user.department_id:
        raise HTTPException(400, "Your account has no department assigned; contact an administrator.")

    content = await file.read()
    if len(content) > _MAX_USER_BULK_BYTES:
        raise HTTPException(413, f"File exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")

    rows = parse_bulk_upload_file(file.filename or "", content, _USER_BULK_COLUMNS)
    findings, valid = await _validate_user_bulk_rows(
        rows, db, force_role=UserRole.FACULTY, force_department_id=user.department_id,
    )
    if findings:
        return JSONResponse(status_code=400, content={"success": False, "imported_count": 0, "errors": findings})

    created = await _create_bulk_users(valid, db)
    # Bulk Upload SMTP timeout fix — emails are queued (durably, in the same
    # transaction as the users above), never sent synchronously here, so
    # `emails_queued` is reported instead of a since-removed `emails_sent`/
    # `emails_total` pair that implied delivery had already happened.
    return {
        "success": True, "imported_count": len(created), "filename": file.filename,
        "emails_queued": len(created),
    }


@router.get("/faculty/bulk-upload/template")
async def download_faculty_bulk_upload_template(_: User = Depends(require_roles(UserRole.HOD))):
    buf = _build_user_bulk_template_workbook()
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ams_users_bulk_upload_template.xlsx"},
    )


@router.post("/users/bulk-upload", status_code=201)
async def bulk_upload_users(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Super Admin / Academic Admin bulk upload — the same roles already
    authorized for individual `create_user` above, never broadened. Unlike
    the HOD endpoint, Role/Department/College ARE taken from the file
    (subject to validation) — consistent with `create_user`'s own existing,
    unrestricted cross-department/cross-role authority for this role — with
    exactly one Excel-specific carve-out: `super_admin` can never be created
    through bulk upload (Section 10), regardless of what already exists for
    individual creation."""
    content = await file.read()
    if len(content) > _MAX_USER_BULK_BYTES:
        raise HTTPException(413, f"File exceeds the {settings.MAX_FILE_SIZE_MB} MB limit.")

    rows = parse_bulk_upload_file(file.filename or "", content, _USER_BULK_COLUMNS)
    findings, valid = await _validate_user_bulk_rows(rows, db, force_role=None, force_department_id=None)
    if findings:
        return JSONResponse(status_code=400, content={"success": False, "imported_count": 0, "errors": findings})

    created = await _create_bulk_users(valid, db)
    return {
        "success": True, "imported_count": len(created), "filename": file.filename,
        "emails_queued": len(created),
    }


@router.get("/users/bulk-upload/template")
async def download_users_bulk_upload_template(
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    buf = _build_user_bulk_template_workbook()
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=ams_users_bulk_upload_template.xlsx"},
    )
