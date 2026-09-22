import uuid
from datetime import datetime, date, timezone
from enum import Enum
from sqlalchemy import String, Boolean, DateTime, Date, ForeignKey, Text, Enum as SAEnum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class UserRole(str, Enum):
    """AMS's current, intentional role set (business decision — see
    BUSINESS_LOGIC.md's role-architecture section). `ACADEMIC_ADMIN`,
    `REGISTRAR`, `EXAMINER`, and `RESEARCH_SUPERVISOR` were early-development
    dummy/testing roles and were removed entirely (migration
    `0015_remove_legacy_roles`) — not hidden, not kept as legacy-compatible
    values. Add a future role here only once AVFU defines an actual business
    requirement for it; the multi-role assignment/switching system (see
    UserRoleAssignment, RefreshToken.active_role) already supports any
    number of roles generically, so no other code needs to change to add one.

    Incharge Academic Cell / DPGS task (this revision) — two new GLOBAL,
    departmentless roles sitting above HOD in the confirmed hierarchy
    (SUPER_ADMIN > DPGS > INCHARGE_ACADEMIC_CELL > HOD > FACULTY > STUDENT).
    Neither is seeded/assigned by any code in this repository — a Super
    Admin must manually assign each one exactly once via the existing
    role-assignment endpoints (see auth.py's `add_user_role`, which enforces
    "at most one active holder" for both — see `0016_incharge_dpgs_roles`).

    External Examiner Selection task (this revision) — two more roles:

    `VICE_CHANCELLOR` — a third GLOBAL, departmentless, single-holder role,
    sitting above DPGS (SUPER_ADMIN > VICE_CHANCELLOR > DPGS >
    INCHARGE_ACADEMIC_CELL > HOD > FACULTY > STUDENT for this workflow's
    approval chain). Assigned the same way as DPGS/Incharge — manually, by a
    Super Admin, via the existing role-assignment endpoints — and enforced
    single-holder by the same partial-unique-index mechanism (migration
    `0026_vc_examiner_roles`).

    `EXTERNAL_EXAMINER` — an ordinary AMS user role, never assigned by a Super
    Admin through the role-assignment endpoints: an account is created
    automatically (`external_examiner.py`, on VC confirmation) with this as
    its legacy `User.role`, exactly like an Orientation-created STUDENT
    account — no `UserRoleAssignment` row is created at issuance either;
    `get_current_user`'s existing self-heal path creates one (with no
    department, since this role is never department-scoped) the first time
    the examiner logs in. Distinguishes the account by role, not by an
    additional boolean flag, consistent with every other account type."""
    SUPER_ADMIN            = "super_admin"
    VICE_CHANCELLOR        = "vice_chancellor"
    DPGS                   = "dpgs"
    INCHARGE_ACADEMIC_CELL = "incharge_academic_cell"
    HOD                    = "hod"
    FACULTY                = "faculty"
    STUDENT                = "student"
    EXTERNAL_EXAMINER      = "external_examiner"


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
    # Multi-role/role-switching task — `role` is retained ONLY as a legacy/
    # display "primary role" column. It is kept synchronized so it always
    # equals one of the user's rows in `UserRoleAssignment` (never a role the
    # user doesn't actually hold) and is used for: the Users list's `role=`
    # filter, account seeding/display, and the legacy single-role Edit User
    # dropdown. It is NEVER read for authorization decisions about the
    # CALLER — every `require_roles(...)` check and every inline
    # `if user.role == ...`/`if user.active_role == ...` authorization branch
    # reads the caller's active role instead (see
    # app.core.dependencies.get_current_user's `active_role`/`assigned_roles`
    # attributes, backed by `RefreshToken.active_role_assignment_id`).
    # "Who is the HOD of department X" business lookups (enrollment.py/
    # ppw.py notification routing) no longer use `User.role`/`User.department_id`
    # either (multi-role/multi-department task, this revision) — a HOD's
    # assignment department can now differ from this scalar column, so those
    # lookups query `UserRoleAssignment` directly (see
    # app.core.dependencies.find_role_holder_in_department).
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"))
    # Multi-role/multi-department task (this revision) — `department_id`
    # above is STILL the authoritative field for: (a) a STUDENT's own
    # department (unchanged — students are out of scope for this task's
    # multi-department model, see core/student_scope.py), and (b) legacy
    # display/profile/reporting purposes for staff. It is NEVER read for
    # STAFF (HOD/FACULTY) authorization decisions about the CALLER anymore —
    # a HOD/FACULTY user's actual department-scoped permissions come
    # entirely from their active `UserRoleAssignment.department_id` (see
    # app.core.dependencies.get_current_user's `active_department_id`
    # attribute), since one person can now hold the same staff role in more
    # than one department simultaneously, which this single column cannot
    # represent. Kept, not dropped: still needed for the reasons above, and
    # as the source the multi-department migration's backfill copied FROM
    # for each user's initial HOD/FACULTY assignment(s).
    # Bulk User/Faculty upload task (this revision) — College is part of the
    # user's own profile, deliberately INDEPENDENT of `department_id` (no
    # College<->Department relationship exists or is inferred anywhere in
    # AMS; both are resolved separately from their own master-data tables —
    # see the bulk-upload validation in auth.py). Nullable: every existing
    # user predates this column and has no College recorded.
    college_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_colleges.id"))
    program_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_programs.id", use_alter=True))
    admission_year: Mapped[int | None] = mapped_column()
    # Student's explicit Academic Year — a reference to the Super Admin-managed
    # AcademicCalendar (`ams_academic_calendars`). Deliberately a SEPARATE field
    # from `admission_year` (the admitted-year integer other modules read) and
    # from the Orientation candidate's free-text `academic_year` label; none of
    # the three is derived from another. Nullable (an unassigned student is
    # valid); no ON DELETE action, like the other transactional references to a
    # calendar — `delete_calendar` refuses to delete a year students are assigned to.
    academic_year_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_academic_calendars.id"), index=True)
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
    college: Mapped["College | None"] = relationship("College", foreign_keys=[college_id])
    academic_calendar: Mapped["AcademicCalendar | None"] = relationship("AcademicCalendar", foreign_keys=[academic_year_id])  # type: ignore[name-defined]
    role_assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        "UserRoleAssignment", back_populates="user", foreign_keys="UserRoleAssignment.user_id", cascade="all, delete-orphan",
    )

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


class CollegeProgram(Base):
    """College<->Programme many-to-many association, explicitly managed by Super
    Admin (BUSINESS_LOGIC.md section X). Modeled on ProgramDepartment above
    and fully INDEPENDENT of it: no College->Department->Programme chain is
    derived or enforced, and no existing Department<->Programme row is read or
    written. A Programme may belong to several Colleges and a College may
    offer several Programmes (the docs fix no single-college rule, and a
    many-to-many table can express a single-college setup, not vice versa).
    Configuration-only: no admission/student/course behaviour reads this yet.
    Removing a row removes only the association — never the College or
    Programme (the FKs' ON DELETE CASCADE only ever removes this join row
    when a parent is hard-deleted, which AMS does not do — both are
    soft-deleted via is_active)."""
    __tablename__ = "ams_college_programs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    college_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_colleges.id", ondelete="CASCADE"))
    program_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_programs.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("college_id", "program_id", name="uq_college_program"),)

    college: Mapped["College"] = relationship("College", foreign_keys=[college_id])
    program: Mapped["Program"] = relationship("Program", foreign_keys=[program_id])


class RefreshToken(Base):
    __tablename__ = "ams_refresh_tokens"
    id: Mapped[uuid.UUID]       = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="CASCADE"))
    token_hash: Mapped[str]     = mapped_column(String(64), unique=True, index=True)
    is_revoked: Mapped[bool]    = mapped_column(Boolean, default=False)
    device_info: Mapped[str | None] = mapped_column(Text)
    # Multi-role/multi-department task (this revision) — REPLACES the
    # previous bare `active_role: UserRole | None` column. A bare role name
    # is no longer sufficient: the same role can now exist for more than one
    # department on the same user (e.g. HOD @ A and HOD @ B), so "the active
    # role" must identify WHICH persisted assignment is in effect, not just
    # a role name — a role+department pair stored as two independent
    # columns could be edited out of sync with each other and produce a
    # combination the user was never actually granted (e.g. HOD @ C, if C
    # were ever written to one column without the other). Pointing at a
    # single `UserRoleAssignment.id` makes an invalid combination
    # structurally impossible: whatever this points to IS a real, persisted
    # grant, atomically. ON DELETE SET NULL: if the assignment is later
    # removed by a Super Admin, the session simply loses its active context
    # and self-heals to a new default on the next request (see
    # app.core.dependencies.get_current_user) rather than referencing a
    # dangling row or blocking the assignment's deletion.
    active_role_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ams_user_role_assignments.id", ondelete="SET NULL"), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    active_role_assignment: Mapped["UserRoleAssignment | None"] = relationship("UserRoleAssignment", foreign_keys=[active_role_assignment_id])


class UserRoleAssignment(Base):
    """Multi-role/multi-department task (this revision, extending the
    original multi-role/role-switching task). The authoritative record of
    which (role, department) combinations a user has been GRANTED by a
    Super Admin — distinct from `RefreshToken.active_role_assignment_id`
    (which ONE of these rows a given session is currently USING).
    `User.role`/`User.department_id` are retained only as legacy/display
    "primary" columns (see their own docstrings) and are never read for
    STAFF authorization after this task; every `_authorize_*` helper reads
    the caller's active assignment instead (see app.core.dependencies).

    `department_id` is nullable and department-scoping is role-dependent,
    not a blanket rule:
      * HOD / FACULTY — REQUIRE a real department. One row per department
        the person actually holds that role in — HOD @ A and HOD @ B are
        two separate rows, never one row with two departments. Holding
        HOD @ A never implies FACULTY @ A; that would need its own,
        separately-granted row.
      * DPGS / INCHARGE_ACADEMIC_CELL / SUPER_ADMIN — institution-wide,
        ALWAYS NULL. Never given a department, invented or otherwise.
      * STUDENT — also NULL here; a student's department continues to come
        from `User.department_id` directly (unchanged, out of scope for
        this multi-department staff model — see `core/student_scope.py`).

    Reuses the existing `UserRole` enum/Postgres type rather than inventing a
    new vocabulary or repurposing the unrelated `ams_roles` master-data table
    (that table remains cosmetic label data only, per its own docstring)."""
    __tablename__ = "ams_user_role_assignments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="CASCADE"), index=True)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="ams_user_role"), nullable=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_departments.id"), nullable=True)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # (user_id, role, department_id) allows the SAME role to repeat for
    # DIFFERENT departments (HOD @ A + HOD @ B). It does NOT, by itself,
    # prevent two NULL-department rows for the same (user_id, role) —
    # Postgres treats every NULL as distinct in a UNIQUE constraint — so a
    # SEPARATE partial unique index (`uq_user_role_assignment_global`,
    # migration 0021) additionally enforces (user_id, role) uniqueness
    # specifically WHERE department_id IS NULL, closing that gap for the
    # global roles. The pre-existing single-institution-wide-holder partial
    # index for DPGS/INCHARGE_ACADEMIC_CELL (migration 0016,
    # `uq_user_role_assignment_single_holder`, keyed on `role` alone) is
    # unchanged by this task and continues to apply on top of this.
    __table_args__ = (UniqueConstraint("user_id", "role", "department_id", name="uq_user_role_assignment"),)

    user: Mapped["User"] = relationship("User", back_populates="role_assignments", foreign_keys=[user_id])
    department: Mapped["Department | None"] = relationship("Department", foreign_keys=[department_id])


from app.models.academic import AcademicCalendar  # noqa: E402,F401
