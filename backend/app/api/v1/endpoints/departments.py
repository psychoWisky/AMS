"""Departments, Programs, Colleges & Roles — includes Super Admin Master Data
management (BUSINESS_LOGIC.md Section N.5). Department/Program list+create
endpoints are unchanged from before this revision; edit/deactivate and the
College/Roles surfaces are new, added to this file rather than a duplicate
module since Department/Program already lived here."""
import re
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Department, Program, ProgramDepartment, College, Designation, Role

router = APIRouter(prefix="/departments", tags=["Departments"])
admin_router = APIRouter(prefix="/admin", tags=["Admin — Master Data"])

_MASTER_DATA_ROLES = (UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)
# Designation management is deliberately narrower than the general master-data
# roles above — Section 5 of the designation-management task explicitly scopes
# create/edit/deactivate to SUPER_ADMIN only (ACADEMIC_ADMIN is NOT included,
# unlike Department/Program/College). Reading (for HOD's Add Faculty dropdown)
# uses the same unrestricted get_current_user pattern as College/Department.
_DESIGNATION_MANAGE_ROLES = (UserRole.SUPER_ADMIN,)
# Role master-data management (role-management task) — Super Admin only, same
# reasoning as Designations: not broadened to Academic Admin.
_ROLE_MANAGE_ROLES = (UserRole.SUPER_ADMIN,)


class DeptIn(BaseModel):
    name: str; code: str
    stream: Optional[str] = None

class DeptUpdate(BaseModel):
    name: Optional[str] = None
    stream: Optional[str] = None
    is_active: Optional[bool] = None

# Programme<->Department many-to-many redesign — a Programme is created
# independently now (no single Department at creation time; a Programme can
# have several, or none yet). `department_id` deliberately removed from both
# schemas below — Program.department_id itself is a dormant legacy column
# (see models/user.py), never read or written by application code anymore;
# associations are managed via the dedicated endpoints further down.
class ProgramIn(BaseModel):
    name: str; code: str; level: str = "UG"
    duration_years: int = 4

class ProgramUpdate(BaseModel):
    name: Optional[str] = None
    level: Optional[str] = None
    duration_years: Optional[int] = None
    is_active: Optional[bool] = None

class ProgramDepartmentIn(BaseModel):
    department_id: UUID

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

class RoleIn(BaseModel):
    code: str
    name: str

class RoleUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("")
async def list_departments(
    stream: Optional[str] = None, include_inactive: bool = False,
    program_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
):
    """Existing stream/include_inactive filters unchanged. `program_id`
    (Programme<->Department many-to-many redesign) restricts the result to
    Departments actually associated with that Programme via
    ams_program_departments — this is what drives the "Programme -> filtered
    Department" selector direction (Orientation, Users page) everywhere else
    in AMS. Omitting it preserves the exact prior unfiltered-by-programme
    behavior for existing consumers."""
    q = select(Department)
    if not include_inactive: q = q.where(Department.is_active == True)
    if stream: q = q.where(Department.stream == stream)
    if program_id:
        q = q.where(Department.id.in_(
            select(ProgramDepartment.department_id).where(ProgramDepartment.program_id == program_id)
        ))
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
    """Programme<->Department many-to-many redesign — no longer returns a
    single `department_id` (Program.department_id is a dormant legacy column,
    never authoritative for a Programme's Departments anymore; a Programme's
    associated Departments are fetched via GET /departments?program_id=... or
    GET /departments/programs/{id}/departments)."""
    q = select(Program)
    if not include_inactive: q = q.where(Program.is_active == True)
    result = await db.execute(q.order_by(Program.name))
    return [{"id": str(p.id), "name": p.name, "code": p.code, "level": p.level,
             "duration_years": p.duration_years, "is_active": p.is_active}
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


# ── Programme <-> Department associations (many-to-many redesign) ──────────────
# The same ams_program_departments row is manageable from either side — from a
# Programme ("Associated Departments: CSE, Mechanical, ...") or from a
# Department ("Associated Programmes: B.Tech, M.Tech, ..."). Removing an
# association only removes the join row — it never deletes the Programme,
# Department, or any User/Student/Course/history that references either.

async def validate_program_department_pair(
    program_id: Optional[UUID], department_id: Optional[UUID], db: AsyncSession,
) -> None:
    """The one new validation rule this redesign introduces: whenever BOTH a
    Programme and a Department are supplied together, that exact pair must
    exist in ams_program_departments. Neither field is made mandatory by this
    check — if either is None, it is skipped entirely (Programme stays
    non-mandatory for Faculty/HOD, and a Department may be set alone, as
    today). Raises the project's normal HTTPException(400, ...) on an invalid
    pair, for callers to surface via their existing error-response handling."""
    if not program_id or not department_id:
        return
    result = await db.execute(
        select(ProgramDepartment.id).where(
            ProgramDepartment.program_id == program_id,
            ProgramDepartment.department_id == department_id,
        ).limit(1)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(400, "This Department is not associated with the selected Programme.")


@router.get("/programs/{program_id}/departments")
async def list_program_departments(
    program_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
):
    if not await db.get(Program, program_id):
        raise HTTPException(404, "Program not found.")
    result = await db.execute(
        select(ProgramDepartment, Department)
        .join(Department, Department.id == ProgramDepartment.department_id)
        .where(ProgramDepartment.program_id == program_id)
        .order_by(Department.name)
    )
    return [{"association_id": str(link.id), "department_id": str(d.id), "department_name": d.name, "department_code": d.code}
            for link, d in result.all()]


@router.post("/programs/{program_id}/departments", status_code=201)
async def add_program_department(
    program_id: UUID, body: ProgramDepartmentIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    if not await db.get(Program, program_id):
        raise HTTPException(404, "Program not found.")
    if not await db.get(Department, body.department_id):
        raise HTTPException(404, "Department not found.")
    existing = await db.execute(
        select(ProgramDepartment).where(
            ProgramDepartment.program_id == program_id, ProgramDepartment.department_id == body.department_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "This Department is already associated with this Programme.")
    link = ProgramDepartment(program_id=program_id, department_id=body.department_id)
    db.add(link)
    await db.commit()
    return {"id": str(link.id), "message": "Department associated with Programme."}


@router.delete("/programs/{program_id}/departments/{department_id}", status_code=204)
async def remove_program_department(
    program_id: UUID, department_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MASTER_DATA_ROLES)),
):
    """Removes ONLY the association row — the Programme and Department
    themselves, and everything referencing either, are untouched. Idempotent:
    a no-op 204 if the association doesn't exist."""
    result = await db.execute(
        select(ProgramDepartment).where(
            ProgramDepartment.program_id == program_id, ProgramDepartment.department_id == department_id,
        )
    )
    link = result.scalar_one_or_none()
    if link:
        await db.delete(link)
        await db.commit()


@router.get("/{department_id}/programs")
async def list_department_programs(
    department_id: UUID, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
):
    """Reverse direction of GET /programs/{id}/departments — lets the
    Department-side admin panel manage the same association rows."""
    if not await db.get(Department, department_id):
        raise HTTPException(404, "Department not found.")
    result = await db.execute(
        select(ProgramDepartment, Program)
        .join(Program, Program.id == ProgramDepartment.program_id)
        .where(ProgramDepartment.department_id == department_id)
        .order_by(Program.name)
    )
    return [{"association_id": str(link.id), "program_id": str(p.id), "program_name": p.name, "program_code": p.code, "program_level": p.level}
            for link, p in result.all()]


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


# ── Roles (master data — role-management task) ─────────────────────────────────
# CRITICAL: this is MASTER DATA ONLY. UserRole (the Python/PostgreSQL enum),
# User.role, require_roles(), and every existing authorization check are
# completely untouched by this section — ams_roles has NO foreign key from
# User.role and a "custom" (is_system=False) role can never be selected as an
# actual User.role or grant any system access. See Role's model docstring.

_ROLE_CODE_RE = re.compile(r"[^A-Z0-9]+")


def _normalize_role_code(code: str) -> str:
    normalized = _ROLE_CODE_RE.sub("_", code.strip().upper()).strip("_")
    if not normalized:
        raise HTTPException(400, "Role code is required.")
    return normalized


def _normalize_role_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise HTTPException(400, "Role name is required.")
    return normalized


async def _role_user_counts(db: AsyncSession) -> dict:
    """Maps UserRole enum VALUE (lowercase, e.g. 'super_admin') -> live active
    user count. Role.code (e.g. 'SUPER_ADMIN') is matched against this by enum
    member NAME lookup in the caller — a code with no matching UserRole member
    (i.e. every custom role) simply never appears here, so its count is 0."""
    counts_result = await db.execute(
        select(User.role, func.count()).where(User.is_active == True).group_by(User.role)
    )
    return {row[0].value: row[1] for row in counts_result.all()}


def _role_dict(r: Role, counts: dict) -> dict:
    try:
        user_count = counts.get(UserRole[r.code].value, 0)
    except KeyError:
        user_count = 0  # custom role code has no corresponding UserRole member
    return {
        "id": str(r.id), "code": r.code, "name": r.name,
        "is_system": r.is_system, "is_active": r.is_active,
        "user_count": user_count,
        "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat(),
    }


@admin_router.get("/roles")
async def list_roles(active: Optional[bool] = None, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    """Any authenticated user may read this (mirrors Designations) — the
    Administration UI and any future role-aware selector both depend on it.
    `active=true`/`active=false` filter; omitted -> all rows."""
    q = select(Role)
    if active is not None:
        q = q.where(Role.is_active == active)
    result = await db.execute(q.order_by(Role.is_system.desc(), Role.name))
    counts = await _role_user_counts(db)
    return [_role_dict(r, counts) for r in result.scalars().all()]


@admin_router.post("/roles", status_code=201)
async def create_role(
    body: RoleIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_ROLE_MANAGE_ROLES)),
):
    """Creates a CUSTOM (is_system=False) master-data row only — never
    system=True, never selectable as a real User.role, never grants access."""
    code = _normalize_role_code(body.code)
    name = _normalize_role_name(body.name)

    dup_code = await db.execute(select(Role).where(func.upper(Role.code) == code))
    if dup_code.scalar_one_or_none():
        raise HTTPException(400, "A role with this code already exists.")
    dup_name = await db.execute(select(Role).where(func.lower(Role.name) == name.lower()))
    if dup_name.scalar_one_or_none():
        raise HTTPException(400, "A role with this name already exists.")

    r = Role(code=code, name=name, is_system=False, is_active=True)
    db.add(r); await db.commit(); await db.refresh(r)
    return {"id": str(r.id), "message": "Role created.", "is_system": False}


@admin_router.patch("/roles/{role_id}")
async def update_role(
    role_id: UUID, body: RoleUpdate, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_ROLE_MANAGE_ROLES)),
):
    r = await db.get(Role, role_id)
    if not r: raise HTTPException(404, "Role not found.")

    updates = body.model_dump(exclude_none=True)
    if "code" in updates:
        if r.is_system:
            raise HTTPException(400, "The code of a system role cannot be changed.")
        code = _normalize_role_code(updates["code"])
        dup_code = await db.execute(select(Role).where(func.upper(Role.code) == code, Role.id != role_id))
        if dup_code.scalar_one_or_none():
            raise HTTPException(400, "A role with this code already exists.")
        updates["code"] = code
    if "name" in updates:
        name = _normalize_role_name(updates["name"])
        dup_name = await db.execute(select(Role).where(func.lower(Role.name) == name.lower(), Role.id != role_id))
        if dup_name.scalar_one_or_none():
            raise HTTPException(400, "A role with this name already exists.")
        updates["name"] = name

    for k, v in updates.items():
        setattr(r, k, v)
    await db.commit()
    return {"message": "Role updated."}


@admin_router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: UUID, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_ROLE_MANAGE_ROLES)),
):
    """System roles are NEVER deletable (hard or soft) — this is the one rule
    protecting the ability to authorize Super Admin itself. Custom roles are
    hard-deleted (no ams_users row can reference a custom code, since User.role
    only ever holds a real UserRole enum value), but only once their computed
    user_count is 0, exactly as specified."""
    r = await db.get(Role, role_id)
    if not r: raise HTTPException(404, "Role not found.")
    if r.is_system:
        raise HTTPException(400, "System roles cannot be deleted.")

    counts = await _role_user_counts(db)
    try:
        user_count = counts.get(UserRole[r.code].value, 0)
    except KeyError:
        user_count = 0
    if user_count > 0:
        raise HTTPException(400, "This role has users assigned and cannot be deleted.")

    await db.delete(r)
    await db.commit()
