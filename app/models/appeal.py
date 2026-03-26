"""Appeal model — Constitution F1 Q9-Q11.

Implements the appeals process required by ERISA, ACA, and state mandates.
Every determination recorded in an immutable, append-only, cryptographically
secured log — same standard as ClinicalDetermination.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, Text, JSON, ForeignKey, Boolean
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base
from app.models.service import BenefitType


class AppealType(str, enum.Enum):
    internal_level_1 = "internal_level_1"
    internal_level_2 = "internal_level_2"
    external_iro = "external_iro"
    expedited = "expedited"


class AppealStage(str, enum.Enum):
    internal_review = "internal_review"
    external_review = "external_review"
    resolved = "resolved"


class AppealStatus(str, enum.Enum):
    pending = "pending"
    filed = "filed"
    under_review = "under_review"
    decided = "decided"
    escalated = "escalated"
    upheld = "upheld"
    overturned = "overturned"
    partially_overturned = "partially_overturned"


class AppealOutcome(str, enum.Enum):
    upheld = "upheld"
    overturned = "overturned"
    partial_reversal = "partial_reversal"
    partially_overturned = "partially_overturned"
    remanded = "remanded"


class ReviewerType(str, enum.Enum):
    clinical_professional = "clinical_professional"
    medical_director = "medical_director"
    iro_reviewer = "iro_reviewer"
    independent_iro = "independent_iro"
    peer_specialist = "peer_specialist"


class Appeal(Base):
    __tablename__ = "appeals"

    appeal_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    determination_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("claims.claim_id"), index=True
    )
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employees.employee_id"), index=True, nullable=True
    )
    benefit_type: Mapped[BenefitType | None] = mapped_column(
        SAEnum(BenefitType), nullable=True
    )

    appeal_type: Mapped[AppealType] = mapped_column(
        SAEnum(AppealType), default=AppealType.internal_level_1
    )
    stage: Mapped[AppealStage | None] = mapped_column(
        SAEnum(AppealStage), default=AppealStage.internal_review, nullable=True
    )
    appeal_status: Mapped[AppealStatus] = mapped_column(
        SAEnum(AppealStatus), default=AppealStatus.pending
    )

    filed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=True
    )
    review_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    is_expedited: Mapped[bool] = mapped_column(Boolean, default=False)
    expedited_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    reviewer_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)

    original_denial_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    denial_notice_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_rights_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    guidelines_referenced: Mapped[list | None] = mapped_column(JSON, nullable=True)

    iro_organization: Mapped[str | None] = mapped_column(String(200), nullable=True)
    iro_assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    audit_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AppealTimeline(Base):
    __tablename__ = "appeal_timeline"

    timeline_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    appeal_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("appeals.appeal_id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
