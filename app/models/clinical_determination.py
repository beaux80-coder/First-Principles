import uuid
from datetime import datetime, UTC

import enum

from sqlalchemy import String, Text, Float, DateTime, JSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID, StringArray
from app.database import Base
from app.encryption import EncryptedString


class DeterminationDecision(str, enum.Enum):
    approved = "approved"
    denied = "denied"
    modified = "modified"


class ClinicalDetermination(Base):
    __tablename__ = "clinical_determinations"

    determination_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    # claim_id is a reference, not a FK — the clinical engine runs in TEE
    # isolation and must not depend on the claims table (which is financial)
    claim_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    # Benefit type for per-type rate publication (F1 completion test 7)
    benefit_type: Mapped[str | None] = mapped_column(
        String(50), index=True, nullable=True
    )
    # Inputs encrypted at rest (symptoms, history)
    inputs_encrypted: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    decision: Mapped[DeterminationDecision] = mapped_column(SAEnum(DeterminationDecision))
    reasoning: Mapped[str] = mapped_column(Text)
    guidelines_referenced: Mapped[list[str] | None] = mapped_column(StringArray(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    audit_hash: Mapped[str] = mapped_column(String(128))
    # Gray-area risk assessment fields (F1 completion test 8)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_factors: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Latency measurement (F1 completion test 3)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Determination authority (Build Manifest items 3, 8, 17)
    # "standing_protocol" = auto-approved via Medical Director standing orders
    # "individual_review" = sent to Medical Director/UR for individual review
    determination_authority: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_recommendation: Mapped[bool | None] = mapped_column(nullable=True, default=True)
    # Outcome feedback loop for accuracy measurement (F1 completion test 4)
    outcome_feedback: Mapped[str | None] = mapped_column(String(50), nullable=True)
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
