"""Major/Minor/Supporting discipline validation (approved business rules,
this task) — shared by PPW (`app/api/v1/endpoints/ppw.py`) and Course
Registration (`app/api/v1/endpoints/enrollment.py`). Each feature keeps its
own independent selection storage (`PpwCourse` vs `StudentEnrollment` +
`CourseOffering`) — this module holds ONLY the pure department-comparison
rules, which are identical for both, so they are never independently
re-implemented (and therefore never able to silently drift apart) in two
places.

Approved rules (confirmed, not assumptions):
- Major: selection's department must equal the student's own department.
- Minor: selection's department must differ from the student's own
  department; every Minor selection for the same PPW/registration must
  belong to the SAME department (the first Minor selection establishes the
  Minor Discipline; every later one must match it).
- Supporting: at most two selections. Any selection whose department IS LPM
  (`Department.code == "LPM"`) is always allowed, regardless of the
  student's Major/Minor department (LPM is the unconditionally-permitted
  slot). Any selection whose department is NOT LPM must differ from both the
  student's Major and Minor department, and by the time the second (final)
  Supporting selection is made, at least one of the two must be LPM — the
  structural "one LPM + one other-or-LPM" requirement. Supporting Discipline
  is the department of whichever Supporting selection is NOT LPM (or LPM
  itself, only when both selections are LPM) — an ORDER-INDEPENDENT
  derivation by department identity, deliberately NOT "whichever was
  inserted/re-inserted second" (`sl_no`/`enrolled_at`): a bug found during
  verification testing showed that chronological-order derivation gives the
  wrong answer whenever the non-LPM course happens to be added FIRST (e.g.
  FISH then LPM), and can even become ambiguous after a remove+re-add cycle,
  since `sl_no`/insertion-order is reassigned by counting current rows, not
  a stable, gap-free sequence. See `enrollment.py`'s and `ppw.py`'s own
  discipline-derivation functions for the fixed, order-independent logic.
- Research/Seminar/Compulsory: unchanged — no department restriction is
  documented anywhere in this repository's business logic, so none is
  invented here.

Deliberately NOT implemented here (explicit instruction): which two specific
LPM courses the university designates as the compulsory pair — that is an
offline, non-technical decision communicated to students directly; nothing
in this module or its callers hard-codes a course ID list or adds
configuration for it. Only the structural "at least one Supporting selection
must be from LPM" rule is enforced.
"""
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Department
# Reused, not duplicated — PPW's existing controlled vocabulary already
# covers exactly the six classifications this task's business rules apply
# to; Course Registration's new classification field (see enrollment.py)
# uses this same tuple rather than inventing a second, parallel one.
from app.models.ppw import PPW_CLASSIFICATIONS, PPW_CLASSIFICATION_LABELS  # noqa: F401

LPM_DEPARTMENT_CODE = "LPM"


class ClassificationError(Exception):
    """Raised for any Major/Minor/Supporting department-rule violation.
    Callers (PPW, Course Registration) catch this and convert it to an
    HTTP 400 with the message as-is, mirroring the existing
    `_EnrollmentDecisionError` pattern already used in enrollment.py."""


async def get_lpm_department_id(db: AsyncSession) -> Optional[UUID]:
    """Resolve the LPM department by its `code`, never by name (names can be
    edited by an admin; `code` is the stable identifier) and never by a
    hard-coded UUID (per explicit instruction — this must keep working
    correctly even if the department were recreated with a new id)."""
    result = await db.execute(select(Department.id).where(Department.code == LPM_DEPARTMENT_CODE))
    return result.scalar_one_or_none()


def validate_major(course_department_id: Optional[UUID], student_department_id: Optional[UUID]) -> None:
    if not student_department_id:
        raise ClassificationError("Your department is not configured; contact administration.")
    if not course_department_id or course_department_id != student_department_id:
        raise ClassificationError("A Major selection must be from your own department.")


def validate_minor(
    course_department_id: Optional[UUID],
    student_department_id: Optional[UUID],
    existing_minor_department_id: Optional[UUID],
) -> None:
    if not course_department_id:
        raise ClassificationError("This course has no department and cannot be selected as Minor.")
    if course_department_id == student_department_id:
        raise ClassificationError("A Minor selection must be from a department other than your own.")
    if existing_minor_department_id and course_department_id != existing_minor_department_id:
        raise ClassificationError(
            "All Minor selections must be from the same department as your existing Minor selection."
        )


def validate_supporting(
    course_department_id: Optional[UUID],
    lpm_department_id: Optional[UUID],
    major_department_id: Optional[UUID],
    minor_department_id: Optional[UUID],
    existing_supporting_department_ids: Sequence[Optional[UUID]],
) -> None:
    """`existing_supporting_department_ids` = the department of every
    Supporting selection already made for this PPW/registration (0 or 1 at
    call time — a 2nd existing one means the structure is already complete,
    handled below)."""
    if not course_department_id:
        raise ClassificationError("This course has no department and cannot be selected as Supporting.")
    if len(existing_supporting_department_ids) >= 2:
        raise ClassificationError("Only two Supporting selections are allowed.")

    is_lpm = bool(lpm_department_id) and course_department_id == lpm_department_id
    if not is_lpm:
        if major_department_id and course_department_id == major_department_id:
            raise ClassificationError("A non-LPM Supporting selection cannot be from your Major department.")
        if minor_department_id and course_department_id == minor_department_id:
            raise ClassificationError("A non-LPM Supporting selection cannot be from your Minor department.")

    # This would be the SECOND (final) Supporting selection — the pair must
    # then include at least one LPM department (the structural requirement;
    # WHICH course the university designates within LPM is an offline
    # decision, never enforced here).
    if len(existing_supporting_department_ids) == 1:
        already_has_lpm = lpm_department_id is not None and lpm_department_id in existing_supporting_department_ids
        if not already_has_lpm and not is_lpm:
            raise ClassificationError("At least one of your two Supporting selections must be from LPM.")
