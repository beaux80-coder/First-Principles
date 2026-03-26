"""Appeal model — Constitution F1 Q9-Q11.

Implements the appeals process required by ERISA, ACA, and state mandates.
Every determination recorded in an immutable, append-only, cryptographically
secured log — same standard as ClinicalDetermination.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base
from app.models.service import BenefitType


class AppealType(str, enum.Enum):
    standard = "standard"
    expedited = "expedited"
    external_iro = "external_iro"


class AppealStage(str, enum.Enum):
    internal_review = "internal_review"
    external_review = "external_review"
    resolved = "resolved"


class AppealStatus(str, enum.Enum):
    pending = "pending"
    under_review = "under_review"
    upheld = "upheld"
    overturned = "overturned"
    partially_overturned = "partially_overturned"


class Appeal(Base):
    __tablename__ = "appeals"

    appeal_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("claims.claim_id"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employees.employee_id"), index=True
    )
    benefit_type: Mapped[BenefitType] = mapped_column(SAEnum(BenefitType))

    appeal_type: Mapped[AppealType] = mapped_column(
        SAEnum(AppealType), default=AppealType.standard
    )
    stage: Mapped[AppealStage] = mapped_column(
        SAEnum(AppealStage), default=AppealStage.internal_review
    )
    status: Mapped[AppealStatus] = mapped_column(
        SAEnum(AppealStatus), default=AppealStatus.pending
    )

    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)

    reviewer_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    original_denial_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    guidelines_referenced: Mapped[list | None] = mapped_column(JSON, nullable=True)

    audit_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
