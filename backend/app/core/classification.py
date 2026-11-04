"""Major/Minor/Supporting discipline validation (approved business rules) —
shared by PPW (`app/api/v1/endpoints/ppw.py`) and Course Registration
(`app/api/v1/endpoints/enrollment.py`). Each feature keeps its own
independent selection storage (`PpwCourse` vs `StudentEnrollment` +
`CourseOffering`) — this module holds ONLY the pure department/course-identity
comparison rules, which are identical for both, so they are never
independently re-implemented (and therefore never able to silently drift
apart) in two places.

Approved rules (confirmed, not assumptions):
- Major: selection's department must equal the student's own department.
- Minor: selection's department must differ from the student's own
  department; every Minor selection for the same PPW/registration must
  belong to the SAME department (the first Minor selection establishes the
  Minor Discipline; every later one must match it). **No count limit** — a
  student may select any number of Minor courses (expanded-selection task,
  this revision; was never count-limited to begin with).
- Supporting: **no count limit** (expanded-selection task, this revision —
  supersedes the original "at most two" cap). Any selection whose department
  IS LPM (`Department.code == "LPM"`) is always allowed, regardless of the
  student's Major/Minor department (LPM is the unconditionally-permitted
  slot). Any selection whose department is NOT LPM must differ from both the
  student's Major and Minor department. **At least one Supporting selection
  must be from LPM, regardless of the total count** — AVFU-confirmed, this
  revision. This can only be known once the student's FULL intended
  Supporting set is in hand, so — unlike the department-exclusion checks
  above, which still run on every individual add — this specific condition
  is deliberately NOT checked by `validate_supporting` itself (a student
  building up their selection incrementally, one non-LPM course at a time,
  must not be blocked mid-way through). It is checked once, by
  `validate_supporting_has_lpm` below, at the point each feature considers
  its Supporting selection set COMPLETE: PPW submission (`submit_ppw`) and
  Course Registration Card submission (`submit_registration_card`) — see
  those functions' own call sites for why those are the correct checkpoints.
  Supporting Discipline derivation (confirmed rule, unchanged by this
  revision): a Supporting course from LPM is compulsory; if the OTHER
  Supporting course is also LPM, Supporting Discipline is LPM; if the other
  belongs to a different department, that department becomes the Supporting
  Discipline. This is well-defined for exactly one LPM + one non-LPM
  selection (the case this rule's own wording describes: "the OTHER
  Supporting course", singular). **With three or more Supporting selections
  spanning more than one NON-LPM department, this derivation is genuinely
  ambiguous** — AVFU's confirmed rule does not say which of several non-LPM
  departments should be shown, and nothing in this module enforces that all
  non-LPM Supporting selections share one department (unlike Minor, which
  does enforce single-department consistency) — deliberately NOT invented
  here; see `enrollment.py`'s/`ppw.py`'s own discipline-derivation functions'
  docstrings for this same flagged ambiguity, preserved as a documented open
  question, not silently resolved.
- Minor/Supporting mutual exclusion (AVFU-confirmed, this revision): the SAME
  underlying `Course.id` may never be selected as both Minor and Supporting
  for the same student+PPW or student+semester — compared by `Course.id`
  (via each selection's `CourseOffering.course_id` for Course Registration,
  or `PpwCourse.course_id` directly for PPW), never by `CourseOffering.id`/
  offering id, department, or course code/title. Two different offerings (or
  sections) of the identical course are still the same course for this rule.
- Research/Seminar/Compulsory: unchanged — no department restriction is
  documented anywhere in this repository's business logic, so none is
  invented here.

Deliberately NOT implemented here (explicit instruction): which specific LPM
course(s) the university designates as compulsory — that is an offline,
non-technical decision communicated to students directly; nothing in this
module or its callers hard-codes a course ID list or adds configuration for
it. Only the structural "at least one Supporting selection must be from LPM"
rule is enforced, and only at the completion checkpoints described above.
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
) -> None:
    """Per-selection department check ONLY — no count limit, and deliberately
    NOT the "at least one LPM" rule (that is checked once, at completion, by
    `validate_supporting_has_lpm` below — see this module's docstring for
    why). Called on every individual Supporting add, in both PPW and Course
    Registration, exactly as before this revision."""
    if not course_department_id:
        raise ClassificationError("This course has no department and cannot be selected as Supporting.")

    is_lpm = bool(lpm_department_id) and course_department_id == lpm_department_id
    if not is_lpm:
        if major_department_id and course_department_id == major_department_id:
            raise ClassificationError("A non-LPM Supporting selection cannot be from your Major department.")
        if minor_department_id and course_department_id == minor_department_id:
            raise ClassificationError("A non-LPM Supporting selection cannot be from your Minor department.")


def validate_supporting_has_lpm(
    supporting_department_ids: Sequence[Optional[UUID]],
    lpm_department_id: Optional[UUID],
) -> None:
    """Completion-time check (expanded-selection task, this revision) — call
    once, when the Supporting selection set is considered COMPLETE: PPW
    submission (`submit_ppw`) or Course Registration Card submission
    (`submit_registration_card`). Never called per-add — a student building
    up Supporting selections one non-LPM course at a time must not be
    blocked mid-way through; only the FINAL, submitted set must satisfy this.
    A student with ZERO Supporting selections is not affected (Supporting
    remains optional overall, unchanged from before this revision) — this
    only fires once at least one Supporting selection exists."""
    if not supporting_department_ids:
        return
    if lpm_department_id is None or lpm_department_id not in supporting_department_ids:
        raise ClassificationError("At least one Supporting selection must be from LPM.")


def validate_minor_supporting_exclusion(
    course_id: UUID,
    classification: str,
    existing_minor_course_ids: Sequence[UUID],
    existing_supporting_course_ids: Sequence[UUID],
) -> None:
    """AVFU-confirmed rule (this revision): the SAME underlying `Course.id`
    may never be classified as both Minor and Supporting for the same
    PPW/registration — compared by `Course.id` specifically (never
    `CourseOffering.id`, department, or course code/title), so two different
    offerings/sections of the identical course are still caught. Callers
    pass the course ids of every EXISTING Minor/Supporting selection
    (PPW: `PpwCourse.course_id`; Course Registration:
    `CourseOffering.course_id` via each `StudentEnrollment.offering_id`) —
    including, for Course Registration's batch endpoint, any already-
    processed selection from earlier in the SAME request, so a single batch
    containing the same course as both classifications is rejected too."""
    if classification == "minor" and course_id in existing_supporting_course_ids:
        raise ClassificationError(
            "This course is already selected as a Supporting course and cannot also be selected as Minor."
        )
    if classification == "supporting" and course_id in existing_minor_course_ids:
        raise ClassificationError(
            "This course is already selected as a Minor course and cannot also be selected as Supporting."
        )
