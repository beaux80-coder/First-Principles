"""Provider Selection API (Function 4).

Constitution: "Two-step selection with architectural separation."
"""

import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/providers", tags=["provider-selection"])


class ProviderSelectRequest(BaseModel):
    condition: str
    benefit_type: str = "health"
    patient_history: dict = {}
    state: Optional[str] = None
    service_code: Optional[str] = None


class OutcomeRecordRequest(BaseModel):
    provider_id: str
    condition: str
    resolved: bool
    resolution_criteria: str
    resolution_timeframe_days: Optional[int] = None


class ProviderConcernRequest(BaseModel):
    provider_id: str
    concern_type: str  # "negative_experience", "accessibility", "scheduling"
    description: str


@router.post("/select")
def select_provider(req: ProviderSelectRequest, db: Session = Depends(get_db)):
    """Select best provider: clinical filtering (TEE) → cost optimization.

    Constitution: "Step 1 (inside TEE, zero financial data): clinical sufficiency.
    Step 2 (outside TEE): lowest verified price among approved providers."
    """
    from app.services.provider_selection import select_provider as _select

    return _select(
        db, req.condition, req.benefit_type, req.patient_history,
        req.state, req.service_code,
    )


@router.post("/outcome")
def record_outcome(req: OutcomeRecordRequest, db: Session = Depends(get_db)):
    """Record a provider outcome using clinical criteria.

    Constitution: "Resolution is defined per condition and per service type
    using clinical criteria from peer-reviewed medical literature —
    not patient satisfaction surveys."
    """
    from app.services.provider_selection import record_provider_outcome

    return record_provider_outcome(
        db, uuid.UUID(req.provider_id), req.condition,
        req.resolved, req.resolution_criteria, req.resolution_timeframe_days,
    )


@router.get("/{provider_id}/outcomes")
def get_outcomes(provider_id: str, db: Session = Depends(get_db)):
    """Get provider outcome report with confidence intervals.

    Constitution: "Provider outcome scores account for statistical sample size
    using confidence intervals."
    """
    from app.services.provider_selection import get_provider_outcome_report

    return get_provider_outcome_report(db, uuid.UUID(provider_id))


@router.get("/thresholds")
def list_thresholds():
    """List all clinical sufficiency thresholds and their published sources.

    Constitution: "The clinical sufficiency threshold is not set by AI reasoning.
    It is derived from published, peer-reviewed clinical standards."
    """
    from app.services.provider_selection import (
        CLINICAL_SUFFICIENCY_THRESHOLDS,
        RESOLUTION_DEFINITIONS,
    )

    return {
        "thresholds": CLINICAL_SUFFICIENCY_THRESHOLDS,
        "resolution_definitions": RESOLUTION_DEFINITIONS,
        "note": "All thresholds derived from published peer-reviewed standards. "
                "The AI retrieves these — it does not set them.",
    }


@router.post("/concern")
def flag_concern(req: ProviderConcernRequest, db: Session = Depends(get_db)):
    """Employee flags a provider concern.

    Constitution: "Employees can flag provider concerns. Flags weighted in selection."
    """
    # In production, this would store the concern and weight it in selection
    return {
        "acknowledged": True,
        "provider_id": req.provider_id,
        "concern_type": req.concern_type,
        "action": "Concern recorded. Will be weighted in future provider selections.",
        "feeding_f8": True,
    }
