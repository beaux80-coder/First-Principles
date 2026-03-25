"""Appeals Process models (Function 1, Questions 9-11).

Constitution: "If a determination results in a denial, a clear, plain-language
explanation of the denial reason is provided to the employee, along with a
complete description of their appeal rights."

"The appeal process includes: internal review by a qualified clinical
professional not involved in the original determination, external independent
review by a qualified IRO, expedited review for urgent/emergent situations."

"All appeal proceedings, decisions, and outcomes are recorded in the same
immutable, cryptographically secured audit log as the original determination."

Models:
- Appeal: tracks a single appeal through its lifecycle (filed -> under_review -> decided/escalated)
- AppealTimeline: append-only event log for every action taken on an appeal
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    JSON,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base


class AppealType(str, enum.Enum):
    internal_level_1 = "internal_level_1"
    internal_level_2 = "internal_level_2"
    external_iro = "external_iro"
    expedited = "expedited"


class AppealStatus(str, enum.Enum):
    filed = "filed"
    under_review = "under_review"
    decided = "decided"
    escalated = "escalated"


class ReviewerType(str, enum.Enum):
    clinical_professional = "clinical_professional"
    medical_director = "medical_director"
    independent_iro = "independent_iro"


class AppealOutcome(str, enum.Enum):
    upheld = "upheld"
    overturned = "overturned"
    partial_reversal = "partial_reversal"


class Appeal(Base):
    __tablename__ = "appeals"

    appeal_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    determination_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("clinical_determinations.determination_id"),
        nullable=True,
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("claims.claim_id"),
        nullable=True,
        index=True,
    )
    appeal_type: Mapped[AppealType] = mapped_column(SAEnum(AppealType))
    appeal_status: Mapped[AppealStatus] = mapped_column(
        SAEnum(AppealStatus), default=AppealStatus.filed
    )
    appeal_reason: Mapped[str] = mapped_column(Text)

    # Reviewer fields
    reviewer_type: Mapped[ReviewerType | None] = mapped_column(
        SAEnum(ReviewerType), nullable=True
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timing
    filed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    review_deadline: Mapped[datetime] = mapped_column(DateTime)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Outcome
    outcome: Mapped[AppealOutcome | None] = mapped_column(
        SAEnum(AppealOutcome), nullable=True
    )

    # Plain-language notices (ACA Section 2719 compliance)
    denial_notice_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_rights_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # External IRO fields
    iro_organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    iro_assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Expedited review
    is_expedited: Mapped[bool] = mapped_column(Boolean, default=False)
    expedited_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit integrity
    audit_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    timeline_entries = relationship(
        "AppealTimeline", back_populates="appeal", order_by="AppealTimeline.occurred_at"
    )


class AppealTimeline(Base):
    __tablename__ = "appeal_timeline"

    timeline_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    appeal_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("appeals.appeal_id"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actor: Mapped[str] = mapped_column(String(255))

    # Relationship
    appeal = relationship("Appeal", back_populates="timeline_entries")
