"""Departments & Programs management."""
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole, Department, Program

router = APIRouter(prefix="/departments", tags=["Departments"])


class DeptIn(BaseModel):
    name: str; code: str
    stream: Optional[str] = None

class ProgramIn(BaseModel):
    name: str; code: str; level: str = "UG"
    department_id: UUID; duration_years: int = 4


@router.get("")
async def list_departments(
    stream: Optional[str] = None,
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
):
    q = select(Department).where(Department.is_active == True)
    if stream: q = q.where(Department.stream == stream)
    result = await db.execute(q.order_by(Department.name))
    return [{"id": str(d.id), "name": d.name, "code": d.code, "stream": d.stream} for d in result.scalars().all()]


@router.post("", status_code=201)
async def create_department(
    body: DeptIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    d = Department(name=body.name, code=body.code.upper(), stream=body.stream)
    db.add(d); await db.commit(); await db.refresh(d)
    return {"id": str(d.id), "message": "Department created."}


@router.get("/programs")
async def list_programs(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Program).where(Program.is_active == True).order_by(Program.name))
    return [{"id": str(p.id), "name": p.name, "code": p.code, "level": p.level,
             "department_id": str(p.department_id), "duration_years": p.duration_years}
            for p in result.scalars().all()]


@router.post("/programs", status_code=201)
async def create_program(
    body: ProgramIn, db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ACADEMIC_ADMIN)),
):
    p = Program(**body.model_dump())
    db.add(p); await db.commit(); await db.refresh(p)
    return {"id": str(p.id), "message": "Program created."}
