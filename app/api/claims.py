"""Automated Claims Processing API (Function 5).

Constitution: "Every function legally performable by software must be automated.
Human review only where law mandates it."

Endpoints:
- POST /claims/submit — Submit a claim for automated adjudication
- GET /claims/{claim_id} — Get claim status and full audit trail
- GET /claims/metrics — Processing metrics (latency, accuracy, automation rate)
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/claims", tags=["claims-processing"])


class ClaimSubmitRequest(BaseModel):
    employer_id: str
    employee_id: str
    benefit_type: str  # health, dental, vision, life, std, ltd, mental_health
    amount_billed: float
    provider_id: Optional[str] = None
    service_id: Optional[str] = None
    service_code: Optional[str] = None
    mode: str = "live"  # "shadow" or "live"
    clinical_determination_id: Optional[str] = None
    price_comparison_id: Optional[str] = None


@router.post("/submit")
def submit_claim(req: ClaimSubmitRequest, db: Session = Depends(get_db)):
    """Submit a claim for automated adjudication.

    Constitution: "Every function legally performable by software must be
    automated. Human review only where law mandates it."

    Pipeline: submitted -> adjudicating -> approved/denied -> paid
    Integrates F1 (clinical determination) and F2 (price verification).
    Auto-detects duplicates, coding errors, and eligibility issues.
    """
    from app.models.claim import Claim, ClaimStatus, ClaimMode
    from app.models.service import BenefitType
    from app.services.claims_adjudication import adjudicate_claim

    # Validate benefit type
    try:
        benefit_type = BenefitType(req.benefit_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid benefit_type: {req.benefit_type}. "
                   f"Must be one of: {[bt.value for bt in BenefitType]}",
        )

    # Validate mode
    try:
        mode = ClaimMode(req.mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {req.mode}. Must be 'shadow' or 'live'.",
        )

    # Create the claim record
    claim = Claim(
        employer_id=uuid.UUID(req.employer_id),
        employee_id=uuid.UUID(req.employee_id),
        provider_id=uuid.UUID(req.provider_id) if req.provider_id else None,
        service_id=uuid.UUID(req.service_id) if req.service_id else None,
        benefit_type=benefit_type,
        mode=mode,
        status=ClaimStatus.submitted,
        amount_billed=req.amount_billed,
        clinical_determination_id=(
            uuid.UUID(req.clinical_determination_id)
            if req.clinical_determination_id else None
        ),
        price_comparison_id=(
            uuid.UUID(req.price_comparison_id)
            if req.price_comparison_id else None
        ),
    )
    db.add(claim)
    db.flush()  # Get the claim_id assigned

    logger.info(
        "Claim %s submitted: benefit_type=%s, amount=%.2f, mode=%s",
        claim.claim_id, benefit_type.value, req.amount_billed, mode.value,
    )

    # Run automated adjudication pipeline
    # Constitution: minimum latency from submission to payment
    result = adjudicate_claim(db, claim.claim_id)

    return result


@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db)):
    """Get F5 processing metrics.

    Constitution metrics:
    - Latency: submission to payment (minimum physically possible)
    - Accuracy: % correctly processed without correction
    - Automation rate: % fully auto-adjudicated

    Completion test: "Is there any claim processing step that could legally
    be automated but currently requires human intervention?"
    """
    from app.services.claims_adjudication import get_processing_metrics

    return get_processing_metrics(db)


@router.get("/{claim_id}")
def get_claim(claim_id: str, db: Session = Depends(get_db)):
    """Get claim status and full audit trail.

    Returns the complete adjudication record including:
    - Current status in state machine
    - Duplicate check result
    - Eligibility verification
    - Coding validation
    - Clinical determination reference (F1)
    - Price comparison reference (F2)
    - Error flags
    - Processing latency

    Every claim recorded feeding F8 (transparency reporting).
    """
    from app.services.claims_adjudication import get_claim_status

    try:
        cid = uuid.UUID(claim_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid claim_id format.")

    result = get_claim_status(db, cid)
    if not result:
        raise HTTPException(status_code=404, detail="Claim not found.")

    return result
