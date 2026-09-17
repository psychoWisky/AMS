"""Notifications (Module 9, 12)."""
from uuid import UUID
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel

from app.db.base import get_db
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User, UserRole
from app.models.audit import Notification, AuditLog

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
async def my_notifications(
    unread_only: bool = False, db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only: q = q.where(Notification.is_read == False)
    result = await db.execute(q.order_by(Notification.created_at.desc()).limit(50))
    return [{
        "id": str(n.id), "type": n.type, "title": n.title,
        "message": n.message, "is_read": n.is_read,
        "entity_type": n.entity_type, "entity_id": n.entity_id,
        "created_at": n.created_at.isoformat(),
    } for n in result.scalars().all()]


@router.patch("/{notif_id}/read")
async def mark_read(notif_id: UUID, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await db.execute(update(Notification).where(Notification.id == notif_id, Notification.user_id == user.id).values(is_read=True))
    await db.commit()
    return {"message": "Marked read."}


@router.patch("/read-all")
async def mark_all_read(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await db.execute(update(Notification).where(Notification.user_id == user.id).values(is_read=True))
    await db.commit()
    return {"message": "All notifications marked read."}


# ── Audit logs ────────────────────────────────────────────────────────────────

@router.get("/audit")
async def audit_logs(
    entity_type: Optional[str] = None, user_id: Optional[UUID] = None, limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    q = select(AuditLog).order_by(AuditLog.created_at.desc())
    if entity_type: q = q.where(AuditLog.entity_type == entity_type)
    if user_id: q = q.where(AuditLog.user_id == user_id)
    result = await db.execute(q.limit(limit))
    return [{
        "id": str(log.id), "user_id": str(log.user_id) if log.user_id else None,
        "action": log.action, "entity_type": log.entity_type, "entity_id": log.entity_id,
        "ip_address": log.ip_address, "role_context": log.role_context,
        "created_at": log.created_at.isoformat(),
    } for log in result.scalars().all()]
