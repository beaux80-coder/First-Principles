import uuid
from datetime import datetime

from sqlalchemy import String, Numeric, ForeignKey, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class PriceComparison(Base):
    __tablename__ = "price_comparisons"

    comparison_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("services.service_id"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("providers.provider_id"), index=True
    )
    # Array of {channel, price, verified, source}
    channels_compared: Mapped[dict] = mapped_column(JSON)
    lowest_price: Mapped[float] = mapped_column(Numeric(12, 2))
    lowest_channel: Mapped[str] = mapped_column(String(100))
    comparison_timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
