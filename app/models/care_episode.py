import uuid
from datetime import datetime

import enum

from sqlalchemy import String, Integer, Text, ForeignKey, DateTime, JSON, Float, Boolean
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base
from app.models.service import BenefitType


class EpisodeStatus(str, enum.Enum):
    open = "open"
    scheduled = "scheduled"
    in_progress = "in_progress"
    resolved = "resolved"
    abandoned = "abandoned"


class StepType(str, enum.Enum):
    intake = "intake"
    provider_selection = "provider_selection"
    scheduling = "scheduling"
    referral = "referral"
    imaging = "imaging"
    lab = "lab"
    prescription = "prescription"
    follow_up = "follow_up"
    payment = "payment"
    departure_recommendation = "departure_recommendation"


class StepStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class CareEpisode(Base):
    __tablename__ = "care_episodes"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employees.employee_id"), index=True
    )
    benefit_type: Mapped[BenefitType] = mapped_column(SAEnum(BenefitType))
    status: Mapped[EpisodeStatus] = mapped_column(
        SAEnum(EpisodeStatus), default=EpisodeStatus.open
    )
    issue_description: Mapped[str] = mapped_column(Text)
    interpreted_condition: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Inferred benefit type from NLP (may differ from employee-supplied)
    interpreted_benefit_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # NLP confidence score for the interpretation
    nlp_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # NLP matched guideline IDs
    matched_guidelines: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Array of {type, status, timestamp, details}
    steps: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    employee_actions_required: Mapped[int] = mapped_column(Integer, default=1)
    # Resolution tracking
    resolution_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provider selected via F4
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("providers.provider_id"), nullable=True
    )
    # Appointment tracking
    appointment_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    appointment_missed: Mapped[bool] = mapped_column(Boolean, default=False)
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0)
    last_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Prescription routing
    prescription_routed: Mapped[bool] = mapped_column(Boolean, default=False)
    prescription_channel: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prescription_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Provider concern flag
    provider_concern_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_concern_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee", back_populates="care_episodes")
    provider = relationship("Provider", foreign_keys=[provider_id])
