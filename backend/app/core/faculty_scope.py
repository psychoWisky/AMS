"""Canonical faculty department-membership resolution (Advisory Committee
department-eligibility task).

CONFIRMED AVFU rule: a faculty member can hold a current `FACULTY`
`UserRoleAssignment` in more than one department simultaneously. For any
"is this faculty member eligible for department X" decision, `User.department_id`
(the legacy single-department scalar column, retained only for display/reporting —
see its own docstring in `app.models.user.User`) is NOT authoritative. The only
authoritative source is `UserRoleAssignment` rows with `role == UserRole.FACULTY`,
mirroring the exact pattern `app.core.dependencies.find_role_holder_in_department`
already uses for "who holds ROLE in DEPARTMENT" lookups elsewhere in AMS.

This module is the single place Advisory Committee (and any future caller) asks
these two questions, so the join/filter logic is never duplicated:
  - `faculty_department_ids`      -> every department a user currently holds
                                      FACULTY in (empty set if none)
  - `faculty_has_department`      -> does this user currently hold FACULTY in
                                      this ONE department
  - `faculty_has_other_department`-> does this user currently hold FACULTY in
                                      ANY department other than this one

Deliberately scoped to `role == FACULTY` only (not HOD) because the confirmed
rule is specifically about "internal faculty member" committee eligibility —
Advisory Committee members are proposed/added as faculty, never as HOD-only
accounts. `User.is_active` is also required, matching the existing convention
in `find_role_holder_in_department` and `list_eligible_faculty`.
"""
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Department, User, UserRole, UserRoleAssignment


async def faculty_department_ids(user_id: UUID, db: AsyncSession) -> set[UUID]:
    """Every department `user_id` currently holds a FACULTY UserRoleAssignment
    in. Returns an empty set if the user is inactive, has no FACULTY
    assignment at all, or does not exist — never raises, since callers use
    this purely for eligibility checks (an empty set simply means "not
    eligible anywhere")."""
    rows = await db.execute(
        select(UserRoleAssignment.department_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .where(
            UserRoleAssignment.user_id == user_id,
            UserRoleAssignment.role == UserRole.FACULTY,
            UserRoleAssignment.department_id.is_not(None),
            User.is_active == True,  # noqa: E712 — SQLAlchemy column comparison
        )
    )
    return {r for (r,) in rows.all()}


async def faculty_has_department(user_id: UUID, department_id: Optional[UUID], db: AsyncSession) -> bool:
    """Does `user_id` currently hold a FACULTY assignment in exactly this
    department? False (fail-closed) if `department_id` is None."""
    if department_id is None:
        return False
    departments = await faculty_department_ids(user_id, db)
    return department_id in departments


async def faculty_has_other_department(user_id: UUID, department_id: Optional[UUID], db: AsyncSession) -> bool:
    """Does `user_id` currently hold a FACULTY assignment in ANY department
    OTHER than `department_id`? A multi-department faculty member (A + B) is
    correctly eligible here for `department_id = A` because they also hold B —
    their own membership in A does not disqualify them (Member Minor rule)."""
    departments = await faculty_department_ids(user_id, db)
    return bool(departments - ({department_id} if department_id is not None else set()))


async def faculty_department_names(user_id: UUID, db: AsyncSession) -> list[str]:
    """Sorted department NAMES `user_id` currently holds FACULTY in — for display
    purposes (e.g. an Advisory Committee member listing showing every department a
    multi-department faculty member belongs to, not just their single legacy
    `User.department_id`). Empty list if none."""
    department_ids = await faculty_department_ids(user_id, db)
    if not department_ids:
        return []
    rows = await db.execute(select(Department.name).where(Department.id.in_(department_ids)))
    return sorted(rows.scalars().all())


async def list_faculty_with_departments(db: AsyncSession) -> list[dict]:
    """Every active internal faculty candidate for Advisory Committee purposes,
    with the FULL set of departments each currently holds a FACULTY assignment
    in (never just their single legacy `User.department_id`). One row per
    USER — a multi-department faculty member appears exactly once here, with
    `department_ids` containing every department they hold FACULTY in, which
    is the single place de-duplication happens for every caller (dropdown
    endpoints, eligibility checks) rather than each caller re-implementing its
    own DISTINCT/group-by. HOD-only accounts (no FACULTY assignment at all)
    are correctly excluded, matching the confirmed "internal faculty member"
    rule — an HOD who is ALSO FACULTY somewhere still appears, keyed by their
    FACULTY assignment(s) only.

    Candidates are ordered by name so callers that don't re-sort still get a
    stable, predictable dropdown order."""
    rows = await db.execute(
        select(User, UserRoleAssignment.department_id)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(
            UserRoleAssignment.role == UserRole.FACULTY,
            UserRoleAssignment.department_id.is_not(None),
            User.is_active == True,  # noqa: E712
        )
        .order_by(User.first_name, User.last_name)
    )
    by_user: dict[UUID, dict] = {}
    order: list[UUID] = []
    for user, dept_id in rows.all():
        entry = by_user.get(user.id)
        if entry is None:
            entry = {"user": user, "department_ids": set()}
            by_user[user.id] = entry
            order.append(user.id)
        entry["department_ids"].add(dept_id)

    if not by_user:
        return []
    all_dept_ids = {d for e in by_user.values() for d in e["department_ids"]}
    dept_names = {d.id: d.name for d in (await db.execute(select(Department).where(Department.id.in_(all_dept_ids)))).scalars().all()}

    return [{
        "id": str(uid),
        "full_name": by_user[uid]["user"].full_name,
        "designation": by_user[uid]["user"].designation,
        "department_ids": [str(d) for d in by_user[uid]["department_ids"]],
        "department_names": sorted(dept_names.get(d, "—") for d in by_user[uid]["department_ids"]),
    } for uid in order]
