import uuid
from datetime import datetime

import enum

from sqlalchemy import String, Numeric, ForeignKey, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base
from app.models.service import BenefitType


class ClaimMode(str, enum.Enum):
    shadow = "shadow"
    live = "live"


class ClaimStatus(str, enum.Enum):
    submitted = "submitted"
    adjudicating = "adjudicating"
    approved = "approved"
    denied = "denied"
    paid = "paid"
    appealed = "appealed"


class Claim(Base):
    __tablename__ = "claims"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    employer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employers.employer_id"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employees.employee_id"), index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("providers.provider_id"), nullable=True
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("services.service_id"), nullable=True
    )
    benefit_type: Mapped[BenefitType] = mapped_column(SAEnum(BenefitType))
    mode: Mapped[ClaimMode] = mapped_column(SAEnum(ClaimMode))
    status: Mapped[ClaimStatus] = mapped_column(
        SAEnum(ClaimStatus), default=ClaimStatus.submitted
    )
    clinical_determination_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("clinical_determinations.determination_id"), nullable=True
    )
    price_comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("price_comparisons.comparison_id"), nullable=True
    )
    amount_billed: Mapped[float] = mapped_column(Numeric(12, 2))
    amount_paid: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    amount_employee_oop: Mapped[float] = mapped_column(Numeric(12, 2), default=0.00)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    adjudicated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employer = relationship("Employer", back_populates="claims")
    employee = relationship("Employee", back_populates="claims")
