import uuid
from datetime import datetime, date, timezone
from enum import Enum
from sqlalchemy import String, Boolean, DateTime, Date, ForeignKey, Text, Enum as SAEnum, UniqueConstraint
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
    # Programme<->Department many-to-many redesign (confirmed business rule:
    # one Department, e.g. CSE, may belong to multiple Programmes, e.g. both
    # B.Tech and M.Tech) — see ProgramDepartment below. A Department with zero
    # rows here is valid and expected for administrative/support departments
    # (e.g. Finance & Accounts, Administration) that have no Programme at all.
    program_links: Mapped[list["ProgramDepartment"]] = relationship("ProgramDepartment", back_populates="department", cascade="all, delete-orphan")


class College(Base):
    """Master data entity for Super Admin (BUSINESS_LOGIC.md Section N.5). Flat,
    no relationship to Department/Program yet — none was confirmed (Open Question 49)."""
    __tablename__ = "ams_colleges"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Designation(Base):
    """Master data entity for Super Admin (BUSINESS_LOGIC.md Section N.5 /
    designation-management investigation). `User.designation` remains a plain
    string column (NOT a foreign key to this table) — existing values such as
    "University Registrar"/"Controller of Exams" must keep working untouched;
    this table is only the controlled source offered to HOD's Add Faculty form
    for NEW faculty designation selection, mirroring College's flat, FK-less
    master-data pattern exactly."""
    __tablename__ = "ams_designations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Role(Base):
    """Role MASTER DATA for the Super Admin Administration UI (role-management
    task). This is deliberately NOT the authorization mechanism — `UserRole`
    (the Python/PostgreSQL enum above) and `User.role` remain the sole source
    of truth for every `require_roles(...)` check in this codebase, byte-for-
    byte unchanged. `ams_roles` has no foreign key from `User.role` and never
    will in this step; `code` for the 8 seeded rows matches the `UserRole`
    enum member names purely so `user_count` can be computed by joining on
    `User.role`'s string value, not because the two are structurally linked.
    A "custom" (`is_system=False`) role created here grants NO system access —
    it cannot be selected as an actual User.role until a real permission
    system is built (out of scope here); the Admin UI must disclose this."""
    __tablename__ = "ams_roles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class User(Base):
    __tablename__ = "ams_users"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str]          = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    # Salutation (Dr./Mr/Mrs/Miss) and middle name — added for HOD Add Faculty
    # (BUSINESS_LOGIC.md Section N.3). Nullable: pre-existing accounts have neither.
    title: Mapped[str | None]       = mapped_column(String(10))
    first_name: Mapped[str | None]  = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
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
    # Student Self-Service profile fields (BUSINESS_LOGIC.md K.1) — student-editable
    # via PATCH /auth/me. Nullable: most existing users (created before this field
    # set existed) will have these unset; profile-completion status is derived from
    # whether they are set, not assumed from account existence (see auth.py).
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None]         = mapped_column(String(20))
    blood_group: Mapped[str | None]    = mapped_column(String(10))
    father_name: Mapped[str | None]    = mapped_column(String(200))
    abc_id: Mapped[str | None]         = mapped_column(String(50))
    address: Mapped[str | None]        = mapped_column(Text)
    # Set True for accounts created with a system-generated temporary password
    # (Orientation credential generation); a distinct concept from profile
    # completion — see core/dependencies.py's require_complete_profile, which
    # does NOT check this flag.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    department: Mapped["Department | None"] = relationship("Department", back_populates="users", foreign_keys=[department_id])
    program: Mapped["Program | None"] = relationship("Program", foreign_keys=[program_id])

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p) or self.email


class Program(Base):
    __tablename__ = "ams_programs"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str]           = mapped_column(String(200), nullable=False)
    code: Mapped[str]           = mapped_column(String(20), unique=True, nullable=False)
    level: Mapped[str]          = mapped_column(String(20), nullable=False)  # UG / PG / PhD
    # LEGACY — Programme<->Department many-to-many redesign (confirmed business
    # rule: one Programme may have multiple Departments, e.g. B.Tech has CSE/
    # Mechanical/Electrical, and one Department may belong to multiple
    # Programmes, e.g. CSE belongs to both B.Tech and M.Tech — the opposite
    # cardinality from this single-FK column). Application code MUST NOT read
    # this column anymore — use `department_links`/`ams_program_departments`
    # instead. Column intentionally NOT dropped yet (see migration
    # 0009_program_department_m2m's docstring) so a "migration 2" can drop it
    # only after every read is confirmed gone; kept here only as a dormant,
    # unused column during that transition window.
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    duration_years: Mapped[int] = mapped_column(default=4)
    is_active: Mapped[bool]     = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    department_links: Mapped[list["ProgramDepartment"]] = relationship("ProgramDepartment", back_populates="program", cascade="all, delete-orphan")


class ProgramDepartment(Base):
    """Programme<->Department many-to-many association (confirmed business
    rule — see Program.department_id's docstring for the full reasoning).
    Modeled directly on the existing ams_course_availability convention: a
    synthetic `id` primary key (not a composite PK), a UniqueConstraint on the
    pair, and one supporting index on the reverse-lookup column
    (department_id — program_id is already the unique constraint's leading
    column). A Department with zero rows here is valid (administrative/
    support departments); a Programme with zero rows here is unusual but not
    forbidden at the model level."""
    __tablename__ = "ams_program_departments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_programs.id", ondelete="CASCADE"))
    department_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("program_id", "department_id", name="uq_program_department"),)

    program: Mapped["Program"] = relationship("Program", back_populates="department_links", foreign_keys=[program_id])
    department: Mapped["Department"] = relationship("Department", back_populates="program_links", foreign_keys=[department_id])


class RefreshToken(Base):
    __tablename__ = "ams_refresh_tokens"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="CASCADE"))
    token_hash: Mapped[str]     = mapped_column(String(64), unique=True, index=True)
    is_revoked: Mapped[bool]    = mapped_column(Boolean, default=False)
    device_info: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
