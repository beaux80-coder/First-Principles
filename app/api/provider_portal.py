"""Provider Portal API (Function 2, Questions 6-9).

Constitution: "The provider portal is web-based, requiring no software
installation. The provider receives a pre-service notification with the
confirmed amount and payment method."

Endpoints:
- POST /provider-portal/authorize — create pre-service notification
- POST /provider-portal/charge — submit a charge against authorization
- POST /provider-portal/dispute — file a dispute
- POST /provider-portal/dispute/{dispute_id}/resolve — resolve a dispute
- POST /provider-portal/dispute/{dispute_id}/decline — provider declines future patients
- GET  /provider-portal/{provider_id}/dashboard — full provider dashboard
- GET  /provider-portal/{provider_id}/payments — payment history
- GET  /provider-portal/{provider_id}/authorizations — authorizations (filterable)
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/provider-portal", tags=["provider-portal"])


# ---- Request schemas ----


class AuthorizeRequest(BaseModel):
    provider_id: str
    claim_id: Optional[str] = None
    service_code: str
    service_description: Optional[str] = None
    benefit_type: str
    confirmed_amount: float
    payment_method: str  # ach_direct or virtual_card


class ChargeRequest(BaseModel):
    auth_id: str
    provider_id: str
    charge_amount: float


class DisputeRequest(BaseModel):
    claim_id: str
    provider_id: str
    dispute_reason: str
    disputed_amount: float


class ResolveDisputeRequest(BaseModel):
    resolution: str
    resolution_amount: Optional[float] = None
    resolution_method: str  # programmatic or human_review


# ---- Endpoints ----


@router.post("/authorize")
def authorize(req: AuthorizeRequest, db: Session = Depends(get_db)):
    """Create a pre-service notification for a provider.

    Constitution: "The provider receives a pre-service notification with
    the confirmed amount and payment method."

    The provider portal is web-based — no software installation needed.
    The notification is served via this API to the provider's web portal.
    """
    from app.services.provider_portal import create_pre_service_notification

    try:
        provider_uuid = uuid.UUID(req.provider_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider_id format. Must be a valid UUID.",
        )

    claim_uuid = None
    if req.claim_id:
        try:
            claim_uuid = uuid.UUID(req.claim_id)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid claim_id format. Must be a valid UUID.",
            )

    result = create_pre_service_notification(
        db=db,
        provider_id=provider_uuid,
        claim_id=claim_uuid,
        service_code=req.service_code,
        service_description=req.service_description,
        benefit_type=req.benefit_type,
        confirmed_amount=req.confirmed_amount,
        payment_method=req.payment_method,
    )

    if "error" in result:
        status_map = {
            "provider_not_found": 404,
            "claim_not_found": 404,
            "invalid_payment_method": 400,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result,
        )

    return result


@router.post("/charge")
def submit_charge(req: ChargeRequest, db: Session = Depends(get_db)):
    """Submit a charge against a pre-service authorization.

    Constitution: "Payment executed directly to the provider via direct
    electronic payment at the earliest moment a verified charge is presented."

    Validates the charge against the pre-authorized amount:
    - Within 1% tolerance: approved, proceeds to payment
    - Discrepancy: programmatic resolution attempted, then human review
    """
    from app.services.provider_portal import submit_provider_charge

    try:
        auth_uuid = uuid.UUID(req.auth_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid auth_id format. Must be a valid UUID.",
        )

    try:
        provider_uuid = uuid.UUID(req.provider_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider_id format. Must be a valid UUID.",
        )

    if req.charge_amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="charge_amount must be positive.",
        )

    result = submit_provider_charge(
        db=db,
        auth_id=auth_uuid,
        provider_id=provider_uuid,
        charge_amount=req.charge_amount,
    )

    if "error" in result:
        status_map = {
            "authorization_not_found": 404,
            "provider_mismatch": 403,
            "invalid_authorization_state": 409,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result,
        )

    return result


@router.post("/dispute")
def file_dispute(req: DisputeRequest, db: Session = Depends(get_db)):
    """File a provider dispute.

    Constitution: "If the provider disagrees, the documented price trail
    — the provider's own published price, the pre-service confirmation,
    and the payment — constitutes the factual record."

    Auto-populates the documented price trail and attempts immediate
    resolution for factual errors and close price disagreements.
    """
    from app.services.provider_portal import file_provider_dispute

    try:
        claim_uuid = uuid.UUID(req.claim_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid claim_id format. Must be a valid UUID.",
        )

    try:
        provider_uuid = uuid.UUID(req.provider_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider_id format. Must be a valid UUID.",
        )

    if req.disputed_amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="disputed_amount must be positive.",
        )

    result = file_provider_dispute(
        db=db,
        claim_id=claim_uuid,
        provider_id=provider_uuid,
        dispute_reason=req.dispute_reason,
        disputed_amount=req.disputed_amount,
    )

    if "error" in result:
        status_map = {
            "claim_not_found": 404,
            "provider_not_found": 404,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result,
        )

    return result


@router.post("/dispute/{dispute_id}/resolve")
def resolve_dispute_endpoint(
    dispute_id: str,
    req: ResolveDisputeRequest,
    db: Session = Depends(get_db),
):
    """Resolve a provider dispute.

    Constitution: "If the provider cannot accept these terms, they may
    decline future patients from this plan."

    Records the resolution and updates authorization/claim as needed.
    """
    from app.services.provider_portal import resolve_dispute

    try:
        dispute_uuid = uuid.UUID(dispute_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid dispute_id format. Must be a valid UUID.",
        )

    result = resolve_dispute(
        db=db,
        dispute_id=dispute_uuid,
        resolution=req.resolution,
        resolution_amount=req.resolution_amount,
        resolution_method=req.resolution_method,
    )

    if "error" in result:
        status_map = {
            "dispute_not_found": 404,
            "invalid_resolution_method": 400,
            "dispute_not_resolvable": 409,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result,
        )

    return result


@router.post("/dispute/{dispute_id}/decline")
def decline_future_patients(
    dispute_id: str,
    db: Session = Depends(get_db),
):
    """Record that a provider declines future patients from this plan.

    Constitution: "If the provider cannot accept these terms, they may
    decline future patients from this plan."

    This does not affect existing authorizations or payments.
    """
    from app.services.provider_portal import record_provider_decline

    try:
        dispute_uuid = uuid.UUID(dispute_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid dispute_id format. Must be a valid UUID.",
        )

    result = record_provider_decline(db=db, dispute_id=dispute_uuid)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result)

    return result


@router.get("/{provider_id}/dashboard")
def provider_dashboard(
    provider_id: str,
    db: Session = Depends(get_db),
):
    """Get the full provider dashboard.

    Constitution: "The provider portal is web-based, requiring no software
    installation."

    Returns pre-service notifications, submitted charges, active disputes,
    payment history, and summary metrics — everything a provider needs
    in a single API call to power the web portal.
    """
    from app.services.provider_portal import get_provider_dashboard

    try:
        provider_uuid = uuid.UUID(provider_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider_id format. Must be a valid UUID.",
        )

    result = get_provider_dashboard(db=db, provider_id=provider_uuid)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result)

    return result


@router.get("/{provider_id}/payments")
def provider_payments(
    provider_id: str,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Get payment history for a provider.

    Returns all payments with amounts, dates, methods, and speed.
    Supports pagination via limit/offset query parameters.
    """
    from app.services.provider_portal import get_provider_payment_history

    try:
        provider_uuid = uuid.UUID(provider_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider_id format. Must be a valid UUID.",
        )

    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=400,
            detail="limit must be between 1 and 500.",
        )

    if offset < 0:
        raise HTTPException(
            status_code=400,
            detail="offset must be non-negative.",
        )

    result = get_provider_payment_history(
        db=db,
        provider_id=provider_uuid,
        limit=limit,
        offset=offset,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result)

    return result


@router.get("/{provider_id}/authorizations")
def provider_authorizations(
    provider_id: str,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Get authorizations for a provider.

    Optionally filter by status: pending, notified, charge_received,
    validated, paid, disputed.
    """
    from app.services.provider_portal import get_provider_authorizations

    try:
        provider_uuid = uuid.UUID(provider_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid provider_id format. Must be a valid UUID.",
        )

    result = get_provider_authorizations(
        db=db,
        provider_id=provider_uuid,
        status_filter=status,
    )

    if "error" in result:
        status_map = {
            "provider_not_found": 404,
            "invalid_status_filter": 400,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result,
        )

    return result
