"""Provider-initiated price offer model — Constitution F2 Q16-Q17.

Providers can proactively submit price offers for platform patients.
Offers operate OUTSIDE the TEE — no financial data enters clinical filtering.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import String, Numeric, Integer, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class OfferStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    withdrawn = "withdrawn"


class ProviderOffer(Base):
    __tablename__ = "provider_offers"

    offer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    provider_npi: Mapped[str] = mapped_column(String(10), index=True)
    service_code: Mapped[str] = mapped_column(String(20), index=True)
    offered_price: Mapped[float] = mapped_column(Numeric(12, 2))
    volume_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[OfferStatus] = mapped_column(
        SAEnum(OfferStatus), default=OfferStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
