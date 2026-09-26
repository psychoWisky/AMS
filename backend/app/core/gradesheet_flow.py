"""Gradesheet approval state machine (no HTTP, no authorization — callers do that).

Workflow (one cycle per submission):

    seq 1  creating instructor      auto-signed at submission
    seq 2  every OTHER instructor   each must approve, OR is deemed-approved once the
                                    server-side 24h deadline passes
    seq 3  HOD                      (department of the offering)
    seq 4  Incharge Academic Cell
    seq 5  DPGS
    seq 6  Controller of Examination -> finalizes the course-wise gradesheet

The next stage only opens when every stage of the current sequence is no longer
pending. A revert closes the cycle (history kept intact); resubmission opens a
NEW cycle with fresh stages, so a signature can never carry over into a new round.

24-hour timer (ASSUMPTION — AVFU has not confirmed the exact start): the window
starts when the creator submits (`cycle.submitted_at`) and applies to every other
instructor identically. The deadline is stored on the cycle and evaluated ONLY on
the server, either lazily (every read/act on the sheet, the approver inboxes) or
by the optional sweeper worker (`gradesheet_worker.py`) — never from a client value.
"""
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.grading import GradeSheet, GradesheetCycle, GradesheetStage

INSTRUCTOR_APPROVAL_WINDOW = timedelta(hours=24)

SEQ_CREATOR, SEQ_INSTRUCTOR, SEQ_HOD, SEQ_INCHARGE, SEQ_DPGS, SEQ_COE = 1, 2, 3, 4, 5, 6

STAGE_SEQUENCE = {
    "creator": SEQ_CREATOR, "instructor": SEQ_INSTRUCTOR, "hod": SEQ_HOD,
    "incharge": SEQ_INCHARGE, "dpgs": SEQ_DPGS, "coe": SEQ_COE,
}
SHEET_STATUS_BY_SEQUENCE = {
    SEQ_CREATOR: "instructor_pending", SEQ_INSTRUCTOR: "instructor_pending",
    SEQ_HOD: "hod_pending", SEQ_INCHARGE: "incharge_pending",
    SEQ_DPGS: "dpgs_pending", SEQ_COE: "coe_pending",
}
STAGE_LABELS = {
    "creator": "Course Instructor", "instructor": "Course Instructor", "hod": "HOD",
    "incharge": "Incharge Academic Cell", "dpgs": "DPGS", "coe": "Controller of Examination",
}
DEEMED_REMARK = "Auto-forwarded to HOD: no response within 24 hours of submission."


def now() -> datetime:
    """Single clock for the whole workflow. Tests replace this attribute to move
    time deterministically instead of waiting 24 real hours."""
    return datetime.now(timezone.utc)


def current_sequence(cycle: GradesheetCycle) -> Optional[int]:
    pending = [s.sequence for s in cycle.stages if s.status == "pending"]
    return min(pending) if pending else None


def refresh_sheet_status(sheet: GradeSheet, cycle: GradesheetCycle, at: datetime) -> None:
    """Derive the sheet/cycle status from the stages. Called after every stage change."""
    seq = current_sequence(cycle)
    if seq is None:
        cycle.status = "completed"
        cycle.closed_at = at
        sheet.status = "approved"
        sheet.is_locked = True
        sheet.finalized_at = at
    else:
        sheet.status = SHEET_STATUS_BY_SEQUENCE[seq]


def create_cycle(
    sheet: GradeSheet, *, cycle_number: int, creator_id: UUID, other_instructor_ids: Iterable[UUID],
    at: datetime, ip: Optional[str] = None,
) -> GradesheetCycle:
    """Open a fresh approval cycle: the creator is signed immediately, every
    other instructor gets an individual pending stage, then HOD -> Incharge ->
    DPGS -> CoE."""
    others = sorted(set(other_instructor_ids) - {creator_id}, key=str)
    cycle = GradesheetCycle(
        sheet_id=sheet.id, cycle_number=cycle_number, status="open", submitted_by=creator_id, submitted_at=at,
        instructor_deadline_at=(at + INSTRUCTOR_APPROVAL_WINDOW) if others else None,
    )
    stages = [GradesheetStage(
        sequence=SEQ_CREATOR, stage_type="creator", assigned_user_id=creator_id, status="approved",
        approver_id=creator_id, acted_role="faculty", acted_at=at, ip_address=ip,
    )]
    stages += [GradesheetStage(sequence=SEQ_INSTRUCTOR, stage_type="instructor", assigned_user_id=uid, status="pending") for uid in others]
    stages += [GradesheetStage(sequence=STAGE_SEQUENCE[t], stage_type=t, status="pending") for t in ("hod", "incharge", "dpgs", "coe")]
    cycle.stages = stages
    return cycle


def apply_deadline(sheet: GradeSheet, cycle: GradesheetCycle) -> bool:
    """If the server-side deadline has passed, deem every still-pending OTHER
    instructor as approved (audit distinction: `deemed_approved` / `is_deemed`,
    no approver, no signature) and advance to HOD. Idempotent."""
    if cycle.status != "open" or cycle.instructor_deadline_at is None:
        return False
    t = now()
    if t < cycle.instructor_deadline_at:
        return False
    changed = False
    for s in cycle.stages:
        if s.stage_type == "instructor" and s.status == "pending":
            s.status = "deemed_approved"
            s.is_deemed = True
            s.acted_at = cycle.instructor_deadline_at
            s.remark = DEEMED_REMARK
            changed = True
    if changed:
        refresh_sheet_status(sheet, cycle, t)
    return changed


async def lock_open_cycle(db: AsyncSession, sheet_id: UUID) -> Optional[GradesheetCycle]:
    """Row-lock the sheet's open cycle and re-read its stages fresh
    (`populate_existing`), so two concurrent actors can never both act on a
    stage that the first one already closed."""
    return (await db.execute(
        select(GradesheetCycle)
        .options(selectinload(GradesheetCycle.stages))
        .where(GradesheetCycle.sheet_id == sheet_id, GradesheetCycle.status == "open")
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def sweep_overdue(db: AsyncSession, only_sheet_ids: Optional[list] = None) -> int:
    """Apply the 24h rule to every overdue open cycle (or, when `only_sheet_ids` is
    given, just those sheets). Safe to call from any request or from the worker;
    each cycle is processed under its own row lock."""
    q = (
        select(GradesheetCycle.id)
        .join(GradesheetStage, GradesheetStage.cycle_id == GradesheetCycle.id)
        .where(
            GradesheetCycle.status == "open", GradesheetCycle.instructor_deadline_at <= now(),
            GradesheetStage.stage_type == "instructor", GradesheetStage.status == "pending",
        ).distinct()
    )
    if only_sheet_ids is not None:
        q = q.where(GradesheetCycle.sheet_id.in_(only_sheet_ids))
    overdue_ids = (await db.execute(q)).scalars().all()
    forwarded = 0
    for cycle_id in overdue_ids:
        cycle = (await db.execute(
            select(GradesheetCycle).options(selectinload(GradesheetCycle.stages))
            .where(GradesheetCycle.id == cycle_id).with_for_update().execution_options(populate_existing=True)
        )).scalar_one()
        sheet = (await db.execute(
            select(GradeSheet).where(GradeSheet.id == cycle.sheet_id).execution_options(populate_existing=True)
        )).scalar_one()
        if apply_deadline(sheet, cycle):
            forwarded += 1
    await db.commit()
    return forwarded
