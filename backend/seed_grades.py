"""
Seed dummy grading data:
- Enroll 4 students in 3 published offerings (2025-26 Sem 1)
- Approve enrollments
- Create grade sheets with marks
- Mark sheets as published (bypass OTP approval pipeline)
"""
import asyncio
import uuid
from datetime import datetime, timezone, date
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, update

DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/ams_db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

OFFERING_IDS = [
    "8edf55e8-b2ae-487a-8324-ebd51363610b",  # AGR101
    "482b8379-c9ea-4f2d-ac86-15149488864a",  # AGR102
    "c7143104-a14a-4fac-b59f-d40a2ac6660a",  # EXT101
]

STUDENT_IDS = [
    ("c4a74e2c-6bba-4702-b36b-6eacf72cce5b", "Amit Joshi"),
    ("bff5e804-bc09-46ae-885e-63b656ff8d07", "Priya Sharma"),
    ("6bdc1d1c-2c30-42de-af43-6f26db74b84c", "Rahul Gupta"),
    ("0e4dc5d9-5d5e-449f-8de9-9472b32a40af", "Sunita Devi"),
]

# Marks per student per offering: (internal, external)
MARKS = {
    "8edf55e8-b2ae-487a-8324-ebd51363610b": [  # AGR101 theory (external only matters)
        (None, 78.0),
        (None, 85.0),
        (None, 62.0),
        (None, 91.0),
    ],
    "482b8379-c9ea-4f2d-ac86-15149488864a": [  # AGR102 theory
        (None, 71.0),
        (None, 88.0),
        (None, 55.0),
        (None, 76.0),
    ],
    "c7143104-a14a-4fac-b59f-d40a2ac6660a": [  # EXT101 theory
        (None, 83.0),
        (None, 79.0),
        (None, 68.0),
        (None, 94.0),
    ],
}

GRADE_TABLE = [
    (90, "O",  10.0),
    (80, "A+",  9.0),
    (70, "A",   8.0),
    (60, "B+",  7.0),
    (50, "B",   6.0),
    (40, "C",   5.0),
    (0,  "F",   0.0),
]

def compute_grade(total):
    for threshold, letter, points in GRADE_TABLE:
        if total >= threshold:
            return letter, points
    return "F", 0.0

now = datetime.now(timezone.utc)

async def run():
    async with AsyncSessionLocal() as db:
        from app.models.enrollment import StudentEnrollment
        from app.models.grading import GradeSheet, GradeEntry, ApprovalStage

        enrollment_map = {}  # (offering_id, student_id) -> enrollment_id

        # 1. Create approved enrollments
        print("Creating enrollments...")
        for oid in OFFERING_IDS:
            for sid, name in STUDENT_IDS:
                # Check if already exists
                existing = await db.execute(
                    select(StudentEnrollment).where(
                        StudentEnrollment.offering_id == uuid.UUID(oid),
                        StudentEnrollment.student_id == uuid.UUID(sid),
                    )
                )
                enr = existing.scalar_one_or_none()
                if not enr:
                    enr = StudentEnrollment(
                        id=uuid.uuid4(),
                        offering_id=uuid.UUID(oid),
                        student_id=uuid.UUID(sid),
                        status="approved",
                        enrolled_at=now,
                        remarks=None,
                    )
                    db.add(enr)
                    await db.flush()
                    print(f"  Enrolled {name} in {oid[:8]}...")
                else:
                    enr.status = "approved"
                    print(f"  Approved existing enrollment: {name} in {oid[:8]}...")
                enrollment_map[(oid, sid)] = str(enr.id)

        await db.commit()

        # 2. Create grade sheets and entries
        print("\nCreating grade sheets...")
        APPROVAL_PIPELINE = [
            (1, "faculty"), (2, "hod"), (3, "registrar"),
            (4, "examiner"), (5, "academic_admin"),
        ]

        for oid in OFFERING_IDS:
            # Check if sheet already exists
            existing_sheet = await db.execute(
                select(GradeSheet).where(
                    GradeSheet.offering_id == uuid.UUID(oid),
                    GradeSheet.sheet_type == "final",
                )
            )
            sheet = existing_sheet.scalar_one_or_none()
            if not sheet:
                sheet = GradeSheet(
                    id=uuid.uuid4(),
                    offering_id=uuid.UUID(oid),
                    sheet_type="final",
                    status="published",
                    is_locked=True,
                    published_at=now,
                )
                db.add(sheet)
                await db.flush()
                print(f"  Created grade sheet for offering {oid[:8]}...")

                # Create approval stages (all approved for dummy data)
                for stage_num, role in APPROVAL_PIPELINE:
                    db.add(ApprovalStage(
                        id=uuid.uuid4(),
                        sheet_id=sheet.id,
                        stage=stage_num,
                        role_required=role,
                        status="approved",
                        signed_at=now,
                    ))
            else:
                sheet.status = "published"
                sheet.is_locked = True
                sheet.published_at = now
                print(f"  Updated existing sheet for offering {oid[:8]}...")

            await db.flush()

            # 3. Add grade entries
            marks_list = MARKS[oid]
            for i, (sid, name) in enumerate(STUDENT_IDS):
                internal, external = marks_list[i]

                existing_entry = await db.execute(
                    select(GradeEntry).where(
                        GradeEntry.sheet_id == sheet.id,
                        GradeEntry.student_id == uuid.UUID(sid),
                    )
                )
                entry = existing_entry.scalar_one_or_none()

                # For theory-only: total = external
                total = external if external else (internal or 0)
                grade_letter, grade_points = compute_grade(total)

                if not entry:
                    enr_id = enrollment_map.get((oid, sid))
                    entry = GradeEntry(
                        id=uuid.uuid4(),
                        sheet_id=sheet.id,
                        student_id=uuid.UUID(sid),
                        enrollment_id=uuid.UUID(enr_id) if enr_id else None,
                        internal_marks=internal,
                        external_marks=external,
                        total_marks=total,
                        grade_letter=grade_letter,
                        grade_points=grade_points,
                        is_absent=False,
                    )
                    db.add(entry)
                    print(f"    {name}: {total} = {grade_letter} ({grade_points})")
                else:
                    entry.internal_marks = internal
                    entry.external_marks = external
                    entry.total_marks = total
                    entry.grade_letter = grade_letter
                    entry.grade_points = grade_points
                    print(f"    Updated {name}: {total} = {grade_letter} ({grade_points})")

        await db.commit()
        print("\nDone! Grading dummy data seeded successfully.")

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    asyncio.run(run())
