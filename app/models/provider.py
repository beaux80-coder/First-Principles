import uuid

import enum

from sqlalchemy import String, Integer, Numeric, Float, JSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID, StringArray
from app.database import Base


class ProviderType(str, enum.Enum):
    physician = "physician"
    hospital = "hospital"
    lab = "lab"
    pharmacy = "pharmacy"
    dental = "dental"
    vision = "vision"
    mental_health = "mental_health"
    other = "other"


class Provider(Base):
    __tablename__ = "providers"

    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    npi: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    provider_type: Mapped[ProviderType] = mapped_column(SAEnum(ProviderType))
    specialties: Mapped[list[str] | None] = mapped_column(StringArray(), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    outcome_data_points: Mapped[int] = mapped_column(Integer, default=0)
    # Provider availability windows — JSON list of
    # {"day_of_week": 0-6, "start_hour": 8, "end_hour": 17, "slot_minutes": 30}
    # Supports Q3: scheduling at earliest provider availability
    availability_schedule: Mapped[dict | None] = mapped_column(JSON, nullable=True)
