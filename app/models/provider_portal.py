"""Provider Portal Models (Function 2, Questions 6-9).

Constitution: "The provider portal is web-based, requiring no software
installation. The provider receives a pre-service notification with the
confirmed amount and payment method. Payment is executed at the earliest
moment a verified charge is presented."

Constitution: "Provider's own published price is the first reference point
in any dispute. Pre-service confirmation is the second. Service actually
rendered is the third. Every dispute resolution feeds F8."

Models:
- ProviderAuthorization: Pre-service notification, charge submission, validation
- ProviderDispute: Dispute filing, resolution, documented price trail
"""

import uuid
from datetime import datetime

import enum

from sqlalchemy import (
    String,
    Numeric,
    ForeignKey,
    DateTime,
    Text,
    Boolean,
    JSON,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


# ---- Authorization enums ----

class AuthPaymentMethod(str, enum.Enum):
    ach_direct = "ach_direct"
    virtual_card = "virtual_card"


class AuthorizationStatus(str, enum.Enum):
    pending = "pending"
    notified = "notified"
    charge_received = "charge_received"
    validated = "validated"
    paid = "paid"
    disputed = "disputed"


# ---- Dispute enums ----

class DisputeResolutionMethod(str, enum.Enum):
    programmatic = "programmatic"
    human_review = "human_review"


class DisputeStatus(str, enum.Enum):
    filed = "filed"
    under_review = "under_review"
    resolved_programmatic = "resolved_programmatic"
    resolved_human = "resolved_human"
    provider_declined_future = "provider_declined_future"


class ProviderAuthorization(Base):
    """Pre-service notification and charge validation for a provider.

    Constitution: "The provider receives a pre-service notification with
    the confirmed amount and payment method... Payment is executed at the
    earliest moment a verified charge is presented."

    Lifecycle:
      pending -> notified -> charge_received -> validated -> paid
      At any point: -> disputed
    """

    __tablename__ = "provider_authorizations"

    auth_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("providers.provider_id"), index=True
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("claims.claim_id"), nullable=True, index=True
    )
    service_code: Mapped[str] = mapped_column(String(20))
    service_description: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    benefit_type: Mapped[str] = mapped_column(String(50))
    confirmed_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[AuthPaymentMethod] = mapped_column(
        SAEnum(AuthPaymentMethod)
    )
    estimated_payment_timeline: Mapped[str] = mapped_column(String(100))

    # Notification tracking
    pre_service_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    notification_channel: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )

    # Charge submission
    charge_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    charge_amount: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    charge_validated: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )

    # Discrepancy tracking
    discrepancy_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    discrepancy_details: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    # Status
    status: Mapped[AuthorizationStatus] = mapped_column(
        SAEnum(AuthorizationStatus), default=AuthorizationStatus.pending
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


class ProviderDispute(Base):
    """Provider dispute with documented price trail and resolution.

    Constitution: "If the provider disagrees, the documented price trail —
    the provider's own published price, the pre-service confirmation, and
    the payment — constitutes the factual record. If the provider cannot
    accept these terms, they may decline future patients from this plan."

    Resolution hierarchy:
    1. Factual error (wrong code, wrong service) -> correct automatically
    2. Price disagreement -> present documented price trail
    3. If provider cannot accept -> decline future patients option

    Every dispute resolution feeds F8 (transparency reporting).
    """

    __tablename__ = "provider_disputes"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("claims.claim_id"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("providers.provider_id"), index=True
    )
    authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("provider_authorizations.auth_id"),
        nullable=True,
    )

    # Dispute details
    dispute_reason: Mapped[str] = mapped_column(Text)
    disputed_amount: Mapped[float] = mapped_column(Numeric(12, 2))

    # Documented price trail (Constitution: three reference points)
    published_price_reference: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    pre_service_confirmation_reference: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    service_rendered_reference: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )

    # Resolution
    resolution_method: Mapped[DisputeResolutionMethod | None] = mapped_column(
        SAEnum(DisputeResolutionMethod), nullable=True
    )
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_amount: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    provider_accepted: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )

    # Timestamps
    filed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )

    # Status
    status: Mapped[DisputeStatus] = mapped_column(
        SAEnum(DisputeStatus), default=DisputeStatus.filed
    )

    # F8 transparency reporting
    feeding_f8: Mapped[bool] = mapped_column(Boolean, default=True)
