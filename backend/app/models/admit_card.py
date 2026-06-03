import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base


class AdmitCard(Base):
    __tablename__ = "ams_admit_cards"

    id: Mapped[uuid.UUID]          = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID]  = mapped_column(UUID(as_uuid=True), ForeignKey("ams_users.id"), nullable=False)
    semester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ams_semesters.id"), nullable=False)
    uid: Mapped[str]               = mapped_column(String(100), nullable=False)  # UID entered by student
    otp_code: Mapped[str | None]   = mapped_column(String(6))
    otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    otp_verified: Mapped[bool]     = mapped_column(Boolean, default=False)
    is_downloaded: Mapped[bool]    = mapped_column(Boolean, default=False)  # one-time download flag
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    student: Mapped["User"]   = relationship("User", foreign_keys=[student_id])
    semester: Mapped["Semester"] = relationship("Semester")

    __table_args__ = (
        UniqueConstraint("student_id", "semester_id", name="uq_admit_card_student_semester"),
    )


from app.models.user import User          # noqa: E402
from app.models.academic import Semester  # noqa: E402
