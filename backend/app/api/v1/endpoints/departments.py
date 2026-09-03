"""Departments, Programs, Colleges & Roles — includes Super Admin Master Data
management (BUSINESS_LOGIC.md Section N.5). Department/Program list+create
endpoints are unchanged from before this revision; edit/deactivate and the
College/Roles surfaces are new, added to this file rather than a duplicate
module since Department/Program already lived here."""
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Department, Program, College, Designation

router = APIRouter(prefix="/departments", tags=["Departments"])
admin_router = APIRouter(prefix="/admin", tags=["Admin — Master Data"])

_MASTER_DATA_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)
# Designation management is deliberately narrower than the general master-data
# roles above — Section 5 of the designation-management task explicitly scopes
# create/edit/deactivate to SUPER_ADMIN only (ACADEMIC_ADMIN is NOT included,
# unlike Department/Program/College). Reading (for HOD's Add Faculty dropdown)
# uses the same unrestricted get_current_user pattern as College/Department.
_DESIGNATION_MANAGE_ROLES = (UserRole.SUPER_ADMIN,)


class DeptIn(BaseModel):
    name: str; code: str
    stream: Optional[str] = None

class DeptUpdate(BaseModel):
    name: Optional[str] = None
    stream: Optional[str] = None
    is_active: Optional[bool] = None

class ProgramIn(BaseModel):
    name: str; code: str; level: str = "UG"
    department_id: UUID; duration_years: int = 4

class ProgramUpdate(BaseModel):
    name: Optional[str] = None
    level: Optional[str] = None
    department_id: Optional[UUID] = None
    duration_years: Optional[int] = None
    is_active: Optional[bool] = None

class CollegeIn(BaseModel):
    name: str; code: str

class CollegeUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None

class DesignationIn(BaseModel):
    name: str

class DesignationUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_departments(
    stream: Optional[str] = None, include_inactive: bool = False,
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
):
    q = select(Department)
    if not include_inactive: q = q.where(Department.is_active == True)
    if stream: q = q.where(Department.stream == stream)
    result = await db.execute(q.order_by(Department.name))
    return [{"id": str(d.id), "name": d.name, "code": d.code, "stream": d.stream, "is_active": d.is_active} for d in result.scalars().all()]


@router.post("", status_code=201)
async def create_department(
    body: DeptIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    d = Department(name=body.name, code=body.code.upper(), stream=body.stream)
    db.add(d); await db.commit(); await db.refresh(d)
    return {"id": str(d.id), "message": "Department created."}


@router.put("/{department_id}")
async def update_department(
    department_id: UUID, body: DeptUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    d = await db.get(Department, department_id)
    if not d: raise HTTPException(404, "Department not found.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(d, k, v)
    await db.commit()
    return {"message": "Department updated."}


@router.get("/programs")
async def list_programs(include_inactive: bool = False, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    q = select(Program)
    if not include_inactive: q = q.where(Program.is_active == True)
    result = await db.execute(q.order_by(Program.name))
    return [{"id": str(p.id), "name": p.name, "code": p.code, "level": p.level,
             "department_id": str(p.department_id), "duration_years": p.duration_years, "is_active": p.is_active}
            for p in result.scalars().all()]


@router.post("/programs", status_code=201)
async def create_program(
    body: ProgramIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    p = Program(**body.model_dump())
    db.add(p); await db.commit(); await db.refresh(p)
    return {"id": str(p.id), "message": "Program created."}


@router.put("/programs/{program_id}")
async def update_program(
    program_id: UUID, body: ProgramUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    p = await db.get(Program, program_id)
    if not p: raise HTTPException(404, "Program not found.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(p, k, v)
    await db.commit()
    return {"message": "Program updated."}


# ── Colleges (BUSINESS_LOGIC.md Section N.5) ───────────────────────────────────

@admin_router.get("/colleges")
async def list_colleges(include_inactive: bool = False, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    q = select(College)
    if not include_inactive: q = q.where(College.is_active == True)
    result = await db.execute(q.order_by(College.name))
    return [{"id": str(c.id), "name": c.name, "code": c.code, "is_active": c.is_active} for c in result.scalars().all()]


@admin_router.post("/colleges", status_code=201)
async def create_college(
    body: CollegeIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    existing = await db.execute(select(College).where(College.code == body.code.upper()))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "College code already exists.")
    c = College(name=body.name, code=body.code.upper())
    db.add(c); await db.commit(); await db.refresh(c)
    return {"id": str(c.id), "message": "College created."}


@admin_router.put("/colleges/{college_id}")
async def update_college(
    college_id: UUID, body: CollegeUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    c = await db.get(College, college_id)
    if not c: raise HTTPException(404, "College not found.")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    await db.commit()
    return {"message": "College updated."}


@admin_router.delete("/colleges/{college_id}", status_code=204)
async def deactivate_college(
    college_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    """Soft-delete only (is_active=False) — hard delete is intentionally not
    offered; nothing currently references College, but the same pattern as
    Department/Program is used for consistency and future-safety."""
    c = await db.get(College, college_id)
    if not c: raise HTTPException(404, "College not found.")
    c.is_active = False
    await db.commit()


# ── Designations (Super Admin master data — designation-management task) ──────
# Replaces the previously hardcoded Professor/Associate Professor/Assistant
# Professor Literal in auth.py's CreateFacultyRequest. ams_users.designation
# stays a plain string (see Designation model docstring) — this table is only
# the controlled source HOD's Add Faculty form selects from, not a FK target.

def _normalize_designation_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise HTTPException(400, "Designation name is required.")
    return normalized


@admin_router.get("/designations")
async def list_designations(active: Optional[bool] = None, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    """Any authenticated user may read this (mirrors Department/Program/College) —
    HOD's Add Faculty form depends on being able to fetch active designations.
    `active=true` -> only active rows; `active=false` -> only inactive rows;
    omitted -> all rows (this is the Super Admin Administration view's default)."""
    q = select(Designation)
    if active is not None:
        q = q.where(Designation.is_active == active)
    result = await db.execute(q.order_by(Designation.name))
    return [{"id": str(d.id), "name": d.name, "is_active": d.is_active, "created_at": d.created_at.isoformat()} for d in result.scalars().all()]


@admin_router.post("/designations", status_code=201)
async def create_designation(
    body: DesignationIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_DESIGNATION_MANAGE_ROLES)),
):
    name = _normalize_designation_name(body.name)
    existing = await db.execute(select(Designation).where(func.lower(Designation.name) == name.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "A designation with this name already exists.")
    d = Designation(name=name)
    db.add(d); await db.commit(); await db.refresh(d)
    return {"id": str(d.id), "message": "Designation created."}


@admin_router.patch("/designations/{designation_id}")
async def update_designation(
    designation_id: UUID, body: DesignationUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_DESIGNATION_MANAGE_ROLES)),
):
    d = await db.get(Designation, designation_id)
    if not d: raise HTTPException(404, "Designation not found.")
    updates = body.model_dump(exclude_none=True)
    if "name" in updates:
        name = _normalize_designation_name(updates["name"])
        dup = await db.execute(select(Designation).where(func.lower(Designation.name) == name.lower(), Designation.id != designation_id))
        if dup.scalar_one_or_none():
            raise HTTPException(400, "A designation with this name already exists.")
        updates["name"] = name
    for k, v in updates.items():
        setattr(d, k, v)
    await db.commit()
    return {"message": "Designation updated."}


@admin_router.delete("/designations/{designation_id}", status_code=204)
async def deactivate_designation(
    designation_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_DESIGNATION_MANAGE_ROLES)),
):
    """Soft-delete only (is_active=False) — ams_users.designation has no FK to
    this table, so existing faculty records are never affected; the row simply
    stops appearing in the active list HOD's Add Faculty form offers."""
    d = await db.get(Designation, designation_id)
    if not d: raise HTTPException(404, "Designation not found.")
    d.is_active = False
    await db.commit()


# ── Roles (read-only — BUSINESS_LOGIC.md Section N.5 "Role management" note) ──

@admin_router.get("/roles")
async def list_roles(db: AsyncSession = Depends(get_db), _: User = Depends(require_roles(*_MASTER_DATA_ROLES))):
    """UserRole is a fixed Python/PostgreSQL enum, not a database-managed RBAC
    table — every require_roles(...) check in this codebase depends on it being
    a closed, known set. This endpoint is deliberately READ-ONLY: no create/
    edit/delete, so the UI cannot imply a capability the architecture doesn't
    safely support. See BUSINESS_LOGIC.md Section N.5 for the full rationale."""
    counts_result = await db.execute(
        select(User.role, func.count()).where(User.is_active == True).group_by(User.role)
    )
    counts = {row[0].value: row[1] for row in counts_result.all()}
    return [{"value": r.value, "label": r.value.replace("_", " ").title(), "user_count": counts.get(r.value, 0)} for r in UserRole]
