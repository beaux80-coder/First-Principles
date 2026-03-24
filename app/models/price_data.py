"""Stores public pricing data from transparency files, Medicare schedules, etc.

This is the core of the Phase 1 data pipeline (Function 8, public layer).
"""

import uuid
from datetime import datetime

import enum

from sqlalchemy import String, Numeric, DateTime, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class PriceSource(str, enum.Enum):
    hospital_transparency = "hospital_transparency"
    insurer_transparency = "insurer_transparency"
    medicare_physician_fee = "medicare_physician_fee"
    medicare_outpatient = "medicare_outpatient"
    medicare_inpatient = "medicare_inpatient"
    nadac_pharmacy = "nadac_pharmacy"
    cash_price = "cash_price"
    samhsa_mental_health = "samhsa_mental_health"
    state_medicaid = "state_medicaid"
    dental_fee_schedule = "dental_fee_schedule"
    all_payer_claims = "all_payer_claims"
    dmepos_fee_schedule = "dmepos_fee_schedule"
    asp_drug_pricing = "asp_drug_pricing"
    va_fee_schedule = "va_fee_schedule"
    other = "other"


class PriceData(Base):
    __tablename__ = "price_data"

    price_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    provider_name: Mapped[str] = mapped_column(String(500), index=True)
    provider_npi: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    service_code: Mapped[str] = mapped_column(String(20), index=True)
    service_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    channel: Mapped[str] = mapped_column(String(100))  # e.g., "cash", "negotiated_aetna"
    source: Mapped[PriceSource] = mapped_column(SAEnum(PriceSource))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    zip_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    file_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
