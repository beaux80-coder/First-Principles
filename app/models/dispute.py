"""Provider dispute model — Constitution F2 Q8-Q9.

When a provider disputes a payment amount, the system resolves it by
referencing the provider's own published price and the pre-service
confirmation. Every dispute is recorded as structured data feeding F8.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import String, Numeric, DateTime, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class DisputeType(str, enum.Enum):
    factual_error = "factual_error"
    price_disagreement = "price_disagreement"


class DisputeStatus(str, enum.Enum):
    open = "open"
    auto_resolved = "auto_resolved"
    escalated = "escalated"
    resolved = "resolved"


class Dispute(Base):
    __tablename__ = "disputes"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    claim_id: Mapped[str] = mapped_column(String(100), index=True)
    provider_npi: Mapped[str] = mapped_column(String(10), index=True)
    dispute_type: Mapped[DisputeType] = mapped_column(SAEnum(DisputeType))
    status: Mapped[DisputeStatus] = mapped_column(
        SAEnum(DisputeStatus), default=DisputeStatus.open
    )
    provider_stated_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    system_verified_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    published_price_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
