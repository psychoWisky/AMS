import uuid
from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Text, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class UserRole(str, Enum):
    SUPER_ADMIN        = "super_admin"
    ACADEMIC_ADMIN     = "academic_admin"
    HOD                = "hod"
    FACULTY            = "faculty"
    STUDENT            = "student"
    REGISTRAR          = "registrar"
    EXAMINER           = "examiner"
    RESEARCH_SUPERVISOR = "research_supervisor"


class Department(Base):
    __tablename__ = "ams_departments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    stream: Mapped[str | None] = mapped_column(String(20))  # fisheries / veterinary
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    users: Mapped[list["User"]] = relationship("User", back_populates="department", foreign_keys="User.department_id")


class User(Base):
    __tablename__ = "ams_users"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str]          = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None]  = mapped_column(String(100))
    last_name: Mapped[str | None]   = mapped_column(String(100))
    mobile: Mapped[str | None]      = mapped_column(String(20))
    role: Mapped[UserRole]          = mapped_column(SAEnum(UserRole, name="ams_user_role"), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(200))
    employee_id: Mapped[str | None] = mapped_column(String(50), unique=True)  # faculty/staff
    student_roll: Mapped[str | None] = mapped_column(String(50), unique=True)  # students
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    program_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_programs.id", use_alter=True))
    admission_year: Mapped[int | None] = mapped_column()
    is_active: Mapped[bool]    = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool]  = mapped_column(Boolean, default=False)
    profile_photo: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    department: Mapped["Department | None"] = relationship("Department", back_populates="users", foreign_keys=[department_id])
    program: Mapped["Program | None"] = relationship("Program", foreign_keys=[program_id])

    @property
    def full_name(self) -> str:
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or self.email


class Program(Base):
    __tablename__ = "ams_programs"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str]           = mapped_column(String(200), nullable=False)
    code: Mapped[str]           = mapped_column(String(20), unique=True, nullable=False)
    level: Mapped[str]          = mapped_column(String(20), nullable=False)  # UG / PG / PhD
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    duration_years: Mapped[int] = mapped_column(default=4)
    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[department_id])


class RefreshToken(Base):
    __tablename__ = "ams_refresh_tokens"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="CASCADE"))
    token_hash: Mapped[str]     = mapped_column(String(64), unique=True, index=True)
    is_revoked: Mapped[bool]    = mapped_column(Boolean, default=False)
    device_info: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
