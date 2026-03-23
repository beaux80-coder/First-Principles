import uuid
from datetime import datetime

import enum

from sqlalchemy import String, Integer, Text, ForeignKey, DateTime, JSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base
from app.models.service import BenefitType


class EpisodeStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"
    abandoned = "abandoned"


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
    # Array of {type, status, timestamp, details}
    steps: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    employee_actions_required: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee", back_populates="care_episodes")
