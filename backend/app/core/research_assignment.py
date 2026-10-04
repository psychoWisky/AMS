"""Research Course Assignment Strategy — the student-specific resolution half.

`CourseOffering.research_assignment_type` (`app/models/course.py`) stores only
the STRATEGY ("major_advisor" / "external_examiner") — never a specific
faculty/examiner id, since the actual party is always resolved per student,
per enrollment (mirrors `app.core.major_advisor`'s existing reasoning for why
Major Advisor resolution lives here and not on the offering).

This module is the "external_examiner" strategy's counterpart to
`app.core.major_advisor.resolve_accepted_major_advisor` — reused by BOTH the
enrollment endpoints (`enrollment.py`) and the thesis External Examiner VC-
selection endpoint (`external_examiner.py`), per the explicit instruction not
to create a second, parallel examiner-assignment system. It reads the SAME
`ExternalExaminerAssignment`/`ExternalExaminer` tables that module already
writes — nothing new is stored about "who the examiner is."

Key difference from `resolve_accepted_major_advisor`: that helper RAISES if
no accepted Major Advisor exists (enrollment in a Major-Advisor-strategy
Research offering is blocked without one — unchanged, pre-existing rule).
`resolve_selected_external_examiner` below never raises — enrollment in an
External-Examiner-strategy Research offering must succeed even before the
student's examiner has been selected (confirmed Case B requirement); it
simply returns `None`, leaving `StudentEnrollment.instructor_id` NULL
("pending"), which `backfill_pending_external_examiner_assignments` later
fills in once a selection completes.

PhD multi-examiner note (flagged assumption, not a confirmed AVFU rule): a
PhD student's thesis External Examiner Selection has TWO active assignments
(`REQUIRED_SELECTION_COUNT["PhD"] == 2`), but `StudentEnrollment.instructor_id`
is a single FK. AVFU has not specified which of the two should be assigned to
a Research course offering. This module picks the one whose
`ExternalExaminerAssignment` row was created first (deterministic, stable),
never silently averaging/guessing a "default" examiner beyond that explicit,
documented tie-break.
"""
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_examiner import ExternalExaminer, ExternalExaminerAssignment
from app.models.course import Course, CourseOffering
from app.models.enrollment import StudentEnrollment

RESEARCH_ASSIGNMENT_TYPES = ("major_advisor", "external_examiner")


async def resolve_selected_external_examiner(student_id: UUID, db: AsyncSession) -> Optional[UUID]:
    """The `User.id` of the student's currently selected thesis External
    Examiner, or `None` if none has been selected yet. Never raises.

    Every examiner selected through the existing thesis External Examiner VC-
    selection flow (`external_examiner.py`'s `vc_select_examiners`) is
    guaranteed to have a real AMS `User` account by that flow itself
    (`_resolve_or_create_examiner_account`) — so this never needs to handle
    an accountless examiner; `ExternalExaminer.user_id` is always set for any
    row this query can reach via an `active` assignment."""
    result = await db.execute(
        select(ExternalExaminer.user_id)
        .join(ExternalExaminerAssignment, ExternalExaminerAssignment.examiner_id == ExternalExaminer.id)
        .where(ExternalExaminerAssignment.student_id == student_id, ExternalExaminerAssignment.status == "active")
        .order_by(ExternalExaminerAssignment.created_at)
    )
    examiner_user_ids = [row[0] for row in result.all() if row[0] is not None]
    return examiner_user_ids[0] if examiner_user_ids else None


async def backfill_pending_external_examiner_assignments(student_id: UUID, db: AsyncSession) -> int:
    """Called immediately after a student's thesis External Examiner
    selection is finalized (`vc_select_examiners`, same transaction, before
    its own commit — Section 24's transactional-safety requirement). Finds
    this student's `StudentEnrollment` rows in Research-category,
    "external_examiner"-strategy offerings that are still unassigned
    (`instructor_id IS NULL`) and fills them in with the just-selected
    examiner.

    Idempotent by construction: only rows CURRENTLY NULL are touched, so a
    retried/duplicate call (e.g. the VC-selection endpoint's own existing
    idempotent-replay path) updates zero rows the second time, never
    double-assigns, and never overwrites an already-set value — including a
    Major-Advisor-strategy enrollment's `instructor_id`, which this query
    structurally cannot reach (scoped to `research_assignment_type ==
    "external_examiner"` offerings only).

    Returns the number of enrollment rows updated (0 is a normal, expected
    result on a replay or when the student has no pending Research
    enrollment of this kind)."""
    examiner_user_id = await resolve_selected_external_examiner(student_id, db)
    if not examiner_user_id:
        return 0
    result = await db.execute(
        select(StudentEnrollment)
        .join(CourseOffering, CourseOffering.id == StudentEnrollment.offering_id)
        .join(Course, Course.id == CourseOffering.course_id)
        .where(
            StudentEnrollment.student_id == student_id,
            StudentEnrollment.instructor_id.is_(None),
            Course.category == "research",
            CourseOffering.research_assignment_type == "external_examiner",
        )
    )
    pending = result.scalars().all()
    for enrollment in pending:
        enrollment.instructor_id = examiner_user_id
    return len(pending)
