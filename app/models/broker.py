"""Broker model (Function 10).

Brokers are distribution partners who advise employers. Their advisory fee
is paid from value-share revenue (not employer pass-through) and is fully
disclosed. No exclusive arrangements.
"""

import uuid
from datetime import datetime

import enum

from sqlalchemy import String, Integer, Numeric, DateTime, ForeignKey
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class BrokerStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    suspended = "suspended"


class Broker(Base):
    __tablename__ = "brokers"

    broker_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255))
    firm: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[BrokerStatus] = mapped_column(
        SAEnum(BrokerStatus), default=BrokerStatus.active
    )
    # Advisory fee as percentage of value-share revenue (NOT employer pass-through)
    advisory_fee_pct: Mapped[float] = mapped_column(Numeric(5, 4), default=0.10)
    # Lifetime activations (employers brought onto platform)
    activations: Mapped[int] = mapped_column(Integer, default=0)
    # Aggregate outcome score across broker's clients
    outcomes_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class BrokerActivation(Base):
    """Tracks which broker activated which employer."""
    __tablename__ = "broker_activations"

    activation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    broker_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("brokers.broker_id"), index=True
    )
    employer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employers.employer_id"), index=True
    )
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
