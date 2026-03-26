"""Pre-service provider notification model — Constitution F2 Q6.

The system notifies providers in advance that the service is approved,
payment is guaranteed, with the confirmed amount and payment method.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import String, Numeric, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class NotificationStatus(str, enum.Enum):
    sent = "sent"
    confirmed = "confirmed"
    failed = "failed"


class ProviderNotification(Base):
    __tablename__ = "provider_notifications"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    provider_npi: Mapped[str] = mapped_column(String(10), index=True)
    claim_id: Mapped[str] = mapped_column(String(100), index=True)
    notification_type: Mapped[str] = mapped_column(
        String(50), default="pre_service_auth"
    )
    confirmed_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[str] = mapped_column(String(50))
    expected_payment_timeline: Mapped[str] = mapped_column(String(100))
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus), default=NotificationStatus.sent
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
