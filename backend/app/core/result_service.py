"""Shared helpers for student semester results (used by grading.py's GPA endpoint
and results.py).

CGPA sequencing (ASSUMPTION — AVFU has not defined semester numbering): a
student's semester results are ordered by their semester's start date (then
creation time). The earliest such result is the "first semester" — GPA only, no
CGPA; every later one shows CGPA = average of the semester GPAs from the first
up to and including it. Only PUBLISHED results (plus, for a CoE preview, the
result being viewed) take part, so an unpublished result never leaks into a
student's CGPA.
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.grading_calc import compute_cgpa
from app.models.academic import Semester
from app.models.grading import StudentSemesterResult


async def ordered_results(db: AsyncSession, student_id: UUID, *, include_result_id: Optional[UUID] = None) -> list[StudentSemesterResult]:
    cond = StudentSemesterResult.status == "published"
    if include_result_id is not None:
        cond = or_(cond, StudentSemesterResult.id == include_result_id)
    rows = await db.execute(
        select(StudentSemesterResult)
        .join(Semester, Semester.id == StudentSemesterResult.semester_id)
        .where(StudentSemesterResult.student_id == student_id, cond)
        .order_by(Semester.start_date, Semester.created_at, StudentSemesterResult.created_at)
    )
    return list(rows.scalars().all())


def cgpa_for(results: list[StudentSemesterResult], result_id: UUID) -> Optional[Decimal]:
    """CGPA of `result_id` within an already-ordered list; None for the first semester."""
    ids = [r.id for r in results]
    if result_id not in ids:
        return None
    upto = results[: ids.index(result_id) + 1]
    return compute_cgpa([r.gpa for r in upto])
