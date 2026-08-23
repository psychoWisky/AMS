import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Integer, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class Course(Base):
    __tablename__ = "ams_courses"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    course_number: Mapped[str]  = mapped_column(String(50), unique=True, nullable=False)
    title: Mapped[str]          = mapped_column(String(300), nullable=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    # Credit structure: theory + practical  (e.g. 2+0, 1+1, 0+2)
    credit_theory: Mapped[int]     = mapped_column(Integer, default=0)
    credit_practical: Mapped[int]  = mapped_column(Integer, default=0)
    # course_type derived from credits but stored for quick filter
    course_type: Mapped[str]    = mapped_column(String(20), default="theory")  # theory / practical / both
    program_level: Mapped[str]  = mapped_column(String(20), default="UG")      # UG / PG / PhD
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str]         = mapped_column(String(20), default="active")  # active / inactive / archived
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[department_id])
    offerings: Mapped[list["CourseOffering"]] = relationship("CourseOffering", back_populates="course")

    @property
    def total_credits(self) -> int:
        return self.credit_theory + self.credit_practical

    @property
    def credit_structure(self) -> str:
        return f"{self.credit_theory}+{self.credit_practical}"


class CourseOffering(Base):
    __tablename__ = "ams_course_offerings"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    calendar_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_academic_calendars.id"))
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_semesters.id"))
    course_id: Mapped[uuid.UUID]   = mapped_column(UUID(as_uuid=True), ForeignKey("ams_courses.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    max_enrollment: Mapped[int]    = mapped_column(Integer, default=60)
    section: Mapped[str | None]    = mapped_column(String(20))   # A / B / C
    practical_group: Mapped[str | None] = mapped_column(String(20))  # G1 / G2
    status: Mapped[str]            = mapped_column(String(20), default="draft")  # draft / published / closed
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("semester_id", "course_id", "department_id", "section", name="uq_offering_section"),)

    calendar: Mapped["AcademicCalendar"] = relationship("AcademicCalendar", foreign_keys=[calendar_id])
    semester: Mapped["Semester"] = relationship("Semester", back_populates="offerings")
    course: Mapped["Course"] = relationship("Course", back_populates="offerings")
    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[department_id])
    faculty_assignments: Mapped[list["OfferingFaculty"]] = relationship("OfferingFaculty", back_populates="offering", cascade="all, delete-orphan")
    enrollments: Mapped[list["StudentEnrollment"]] = relationship("StudentEnrollment", back_populates="offering")
    grade_sheets: Mapped[list["GradeSheet"]] = relationship("GradeSheet", back_populates="offering")


class OfferingFaculty(Base):
    __tablename__ = "ams_offering_faculty"
    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offering_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_course_offerings.id", ondelete="CASCADE"))
    faculty_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"))
    role: Mapped[str]              = mapped_column(String(20), default="primary")  # primary / secondary
    assigned_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("offering_id", "faculty_id", name="uq_offering_faculty"),)

    offering: Mapped["CourseOffering"] = relationship("CourseOffering", back_populates="faculty_assignments")
    faculty: Mapped["User"] = relationship("User", foreign_keys=[faculty_id])


# Avoid circular imports
from app.models.user import Department, User  # noqa: E402
from app.models.academic import AcademicCalendar, Semester  # noqa: E402
from app.models.enrollment import StudentEnrollment  # noqa: E402
from app.models.grading import GradeSheet  # noqa: E402
