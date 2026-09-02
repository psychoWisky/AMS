from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.base import get_db
from app.core.security import verify_token
from app.models.user import User, UserRole

bearer = HTTPBearer()


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    user_id = verify_token(creds.credentials)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")
    return user


def require_roles(*roles: UserRole):
    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions.")
        return user
    return _check


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
