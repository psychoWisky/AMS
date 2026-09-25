"""Authoritative "accepted Major Advisor" resolution.

Before this module, the "student's Major Advisor" query (AdvisoryCommittee ->
CommitteeMember filtered by role) was re-implemented ad hoc in several places
(thesis.py, progress_report.py, synopsis.py, external_examiner.py,
enrollment.py) with inconsistent filtering — some require
`CommitteeMember.accepted is True`, some don't — and none of them raise a
clear error if zero or more than one row matches; a couple even use
`scalar_one_or_none()`, which raises an unhandled `MultipleResultsFound`
(500) rather than a clean validation error if the data ever has two.

This module is used specifically where an automatic, security-relevant
assignment is being made from "the" Major Advisor (Research Course instructor
assignment) — a case where silently picking one of several candidates, or
silently proceeding with none, would be a real correctness/security problem,
not just a display quirk. It does not replace the existing ad hoc lookups
used elsewhere for read-only display/authorization purposes — those are
out of scope for this task and are left exactly as they are.

The authoritative rule (confirmed): `CommitteeMember.role == "major_advisor"
AND CommitteeMember.accepted is True`. Exactly one such row must exist.
"""
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research import AdvisoryCommittee, CommitteeMember


async def resolve_accepted_major_advisor(student_id: UUID, db: AsyncSession) -> UUID:
    """Return the single `User.id` of the student's accepted Major Advisor.

    Raises HTTPException(400) with a clear, actionable message if there is
    zero or more than one accepted Major Advisor — never arbitrarily picks
    one, and never returns None for the caller to silently treat as "no
    instructor"."""
    result = await db.execute(
        select(CommitteeMember.faculty_id)
        .join(AdvisoryCommittee, AdvisoryCommittee.id == CommitteeMember.committee_id)
        .where(
            AdvisoryCommittee.student_id == student_id,
            CommitteeMember.role == "major_advisor",
            CommitteeMember.accepted == True,  # noqa: E712 — SQLAlchemy column comparison
        )
    )
    advisor_ids = [row[0] for row in result.all()]
    if len(advisor_ids) == 0:
        raise HTTPException(
            400,
            "You must have an accepted Major Advisor before proceeding. "
            "Please contact your department if you believe this is in error.",
        )
    if len(advisor_ids) > 1:
        raise HTTPException(
            400,
            "Your Advisory Committee has more than one accepted Major Advisor, which is a data "
            "configuration problem. Please contact your department to resolve this before "
            "proceeding.",
        )
    return advisor_ids[0]
