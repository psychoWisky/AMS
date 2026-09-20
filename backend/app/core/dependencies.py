from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import selectinload
from uuid import UUID
from typing import Optional

from app.db.base import get_db
from app.core.security import decode_token
from app.models.user import User, UserRole, RefreshToken, UserRoleAssignment

bearer = HTTPBearer()

# Multi-role/role-switching task — deterministic fallback order when a
# session has no valid active assignment recorded yet (fresh login default,
# or the previously-active assignment having just been removed). Ordered
# least-to-most-privileged so a multi-role account is never silently dropped
# into a MORE privileged mode than the one it was last known to use
# (Section 26/28's "do not silently choose a privileged role in a surprising
# way"). `user.role` (the legacy/primary field) is always preferred first
# when it is still one of the user's assigned roles.
_ROLE_PRIORITY: list[UserRole] = [
    UserRole.STUDENT, UserRole.FACULTY, UserRole.HOD,
    UserRole.INCHARGE_ACADEMIC_CELL, UserRole.DPGS, UserRole.SUPER_ADMIN,
]


async def get_assigned_role_assignments(user_id, db: AsyncSession) -> list[UserRoleAssignment]:
    """Multi-role/multi-department task (this revision) — returns the FULL
    persisted assignment rows (role AND department together), not just role
    names, since the same role can now repeat for different departments.
    Ordered by `assigned_at` for a stable, deterministic tie-break wherever
    a default must be chosen among several assignments of the same role
    (see `pick_default_assignment`)."""
    rows = (await db.execute(
        select(UserRoleAssignment)
        .options(selectinload(UserRoleAssignment.department))
        .where(UserRoleAssignment.user_id == user_id)
        .order_by(UserRoleAssignment.assigned_at)
    )).scalars().all()
    return list(rows)


async def get_assigned_roles(user_id, db: AsyncSession) -> list[UserRole]:
    """Back-compat convenience: distinct role NAMES only, for callers that
    only ever needed "which roles does this person hold" (e.g. the Users
    list's role filter) and have no use for department detail. Prefer
    `get_assigned_role_assignments` for anything authorization-related."""
    assignments = await get_assigned_role_assignments(user_id, db)
    seen: list[UserRole] = []
    for a in assignments:
        if a.role not in seen:
            seen.append(a.role)
    return seen


def pick_default_assignment(
    assignments: list[UserRoleAssignment], preferred_id: Optional[UUID], preferred_role: Optional[UserRole],
) -> UserRoleAssignment:
    """Deterministic, never-privileged-by-surprise selection among
    `assignments`. `assignments` must be non-empty. `preferred_id` (a
    specific assignment row id — e.g. the session's previously-active one,
    if it still exists) wins outright; otherwise falls back to the
    least-to-most-privileged `_ROLE_PRIORITY` order, preferring
    `preferred_role` (the legacy `User.role` field) among same-priority
    candidates, and finally the earliest-assigned row as a stable
    tie-break among multiple departments of the SAME role — e.g. a fresh
    session for a `HOD @ A` + `HOD @ B` user deterministically lands on
    whichever of the two was granted first, never an arbitrary one."""
    if preferred_id is not None:
        for a in assignments:
            if a.id == preferred_id:
                return a
    if preferred_role is not None:
        candidates = [a for a in assignments if a.role == preferred_role]
        if candidates:
            return candidates[0]
    for role in _ROLE_PRIORITY:
        candidates = [a for a in assignments if a.role == role]
        if candidates:
            return candidates[0]
    return assignments[0]


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = decode_token(creds.credentials, "access")
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    # Multi-role/multi-department task. `assignments` and the active
    # assignment are re-derived from the database on EVERY request — never
    # trusted from the token, localStorage, or any client-supplied value
    # (Sections 2-4/40). This is the ONLY place a session's active
    # (role, department) pair is resolved.
    assignments = await get_assigned_role_assignments(user.id, db)
    if not assignments:
        # Defensive self-heal only: every real account is backfilled by the
        # 0014_multi_role/0021_multi_dept_role_assign migrations, so this
        # should never trigger in practice — but if it ever does (e.g. a
        # user created by code that bypasses the assignment-creating
        # helpers), treat their legacy scalar role/department as their sole
        # assignment rather than locking them out.
        fallback = UserRoleAssignment(
            user_id=user.id, role=user.role,
            department_id=user.department_id if user.role in (UserRole.HOD, UserRole.FACULTY) else None,
        )
        db.add(fallback)
        await db.flush()
        assignments = [fallback]

    session: RefreshToken | None = None
    sid = payload.get("sid") if payload else None
    if sid:
        session = (await db.execute(
            select(RefreshToken).where(
                RefreshToken.id == sid, RefreshToken.user_id == user.id, RefreshToken.is_revoked == False,
            )
        )).scalar_one_or_none()

    valid_ids = {a.id for a in assignments}
    active = None
    if session is not None and session.active_role_assignment_id in valid_ids:
        active = next(a for a in assignments if a.id == session.active_role_assignment_id)

    if active is None:
        # Either a brand-new/legacy session with no recorded active
        # assignment, or the previously-active assignment was just removed
        # by an admin — in both cases fall back deterministically, NEVER
        # silently keep a removed assignment active (Section 8/28's
        # invariant: the active assignment must always be one of this
        # user's CURRENT rows).
        active = pick_default_assignment(assignments, None, user.role)
        if session is not None and session.active_role_assignment_id != active.id:
            session.active_role_assignment_id = active.id
            await db.commit()

    user.active_role_assignment = active
    user.active_role = active.role
    user.active_department_id = active.department_id
    user.assigned_roles = [a.role for a in assignments]
    user.assigned_role_assignments = assignments
    user._active_session = session
    return user


def require_roles(*roles: UserRole):
    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.active_role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions.")
        return user
    return _check


def student_user_clause():
    """SQL clause over `User`: this account is a STUDENT — it holds a STUDENT
    role assignment, or (an account created by Orientation before its first
    login, which is when `get_current_user` self-heals the missing row) it has
    no assignment rows at all and its legacy role is STUDENT. There is no
    separate Student table; students are `User` rows."""
    ra = UserRoleAssignment
    return or_(
        User.id.in_(select(ra.user_id).where(ra.role == UserRole.STUDENT)),
        and_(~User.id.in_(select(ra.user_id)), User.role == UserRole.STUDENT),
    )


def staff_user_clause():
    """SQL clause over `User`: a non-student ("system"/staff) account — it holds
    at least one non-STUDENT assignment (or, with no assignment rows, a
    non-student legacy role). A person who is both staff and a student (e.g.
    Faculty who also holds STUDENT) is staff here AND is listed as a student.
    This is the population of the Super Admin User Management directory."""
    ra = UserRoleAssignment
    return or_(
        User.id.in_(select(ra.user_id).where(ra.role != UserRole.STUDENT)),
        and_(~User.id.in_(select(ra.user_id)), User.role != UserRole.STUDENT),
    )


async def find_role_holder_in_department(role: UserRole, department_id, db: AsyncSession) -> Optional[UUID]:
    """Multi-role/multi-department task (this revision) — "who holds `role`
    for `department_id`" business lookups (e.g. routing a notification to
    "the HOD of the student's department") must go through
    `UserRoleAssignment` now, never `User.role`/`User.department_id`
    directly: a HOD's assignment department can now differ from their
    single legacy `User.department_id` column (e.g. a person who is
    HOD @ A but whose profile's home department is B). Returns the first
    active holder's user id, or None if nobody currently holds that
    (role, department) combination. Only meaningful for department-scoped
    roles (HOD/FACULTY) — callers should not use this for global roles."""
    result = await db.execute(
        select(UserRoleAssignment.user_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .where(UserRoleAssignment.role == role, UserRoleAssignment.department_id == department_id, User.is_active == True)
        .limit(1)
    )
    return result.scalar_one_or_none()


# Required-for-completion fields (BUSINESS_LOGIC.md K.1/Section N.2 — ABC ID and
# Blood Group are CONFIRMED optional, not part of this list). Computed on read,
# not a stored flag, so it can never drift out of sync with the actual field values.
_MANDATORY_PROFILE_FIELDS = [
    ("date_of_birth", "Date of Birth"),
    ("gender", "Gender"),
    ("father_name", "Father's Name"),
    ("address", "Address"),
]


def get_missing_profile_fields(user: User) -> list[str]:
    """Returns only the genuinely mandatory fields that are currently empty
    (BUSINESS_LOGIC.md Section N — 'do not list optional fields as missing')."""
    return [label for attr, label in _MANDATORY_PROFILE_FIELDS if not getattr(user, attr)]


def is_profile_complete(user: User) -> bool:
    return not get_missing_profile_fields(user)


async def require_complete_profile(user: User = Depends(require_roles(UserRole.STUDENT))) -> User:
    """Backend-authoritative gate for restricted student actions (Section 28.14/28.20).
    Only ever applied to STUDENT-role endpoints — never affects faculty/HOD/admin."""
    if not is_profile_complete(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please complete your student profile before continuing.",
        )
    return user


async def require_advisory_committee_established(
    user: User = Depends(require_complete_profile),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Backend-authoritative Course Registration gate (BUSINESS_LOGIC.md Section L.7/
    M.10 confirmed dependency ordering: Student Intake -> Profile Completion ->
    Advisory Committee -> Course Registration; STUDENT_SIDE_IMPLEMENTATION_PLAN.md
    Section 30.2/31.13/32 Phase F-4).

    ASSUMPTION, not a confirmed rule (Open Question 31 — exact eligibility rule is
    unconfirmed): "Advisory Committee established" is interpreted here as the
    student's Major Advisor having accepted (committee stage past
    'major_advisor_pending' and not 'reverted') — a mere HOD proposal is not treated
    as sufficient. Record any correction to this interpretation in
    BUSINESS_LOGIC.md's Open Questions, not silently in code.
    """
    from app.models.research import AdvisoryCommittee  # local import: avoids a
    # models/research.py <-> core/dependencies.py import-order dependency at module
    # load time, consistent with how model files themselves lazily import siblings.

    result = await db.execute(select(AdvisoryCommittee).where(AdvisoryCommittee.student_id == user.id))
    committee = result.scalar_one_or_none()
    if not committee or committee.status in ("major_advisor_pending", "reverted"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Course Registration requires an established Advisory Committee (Major Advisor accepted). "
                   "Please contact your department if you believe this is in error.",
        )
    return user
