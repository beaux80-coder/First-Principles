"""Provider-initiated price offers API — Constitution F2 Q16-Q17.

Providers proactively submit price offers. The system does not counter-offer.
Offers operate OUTSIDE the TEE.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.provider_offers import (
    submit_offer,
    get_active_offers,
    update_offer,
    withdraw_offer,
)

router = APIRouter(prefix="/providers", tags=["provider-offers"])


class OfferRequest(BaseModel):
    service_code: str = Field(description="CPT/HCPCS service code")
    offered_price: float = Field(ge=0, description="Price the provider will accept")
    volume_capacity: int | None = Field(default=None, ge=1, description="Max patients at this price")
    valid_days: int = Field(default=90, ge=1, le=365, description="Offer validity period in days")


class OfferUpdateRequest(BaseModel):
    offered_price: float | None = Field(default=None, ge=0)
    volume_capacity: int | None = Field(default=None, ge=1)
    valid_days: int | None = Field(default=None, ge=1, le=365)


@router.post("/{npi}/offers")
def api_submit_offer(npi: str, request: OfferRequest, db: Session = Depends(get_db)):
    """Provider submits a price offer for platform patients."""
    return submit_offer(
        db=db,
        provider_npi=npi,
        service_code=request.service_code,
        offered_price=request.offered_price,
        volume_capacity=request.volume_capacity,
        valid_days=request.valid_days,
    )


@router.get("/{npi}/offers")
def api_get_offers(npi: str, db: Session = Depends(get_db)):
    """List active offers for this provider."""
    return get_active_offers(db, provider_npi=npi)


@router.put("/{npi}/offers/{offer_id}")
def api_update_offer(
    npi: str,
    offer_id: str,
    request: OfferUpdateRequest,
    db: Session = Depends(get_db),
):
    """Update an existing offer. Provider can change price at any time."""
    return update_offer(
        db=db,
        offer_id=offer_id,
        offered_price=request.offered_price,
        volume_capacity=request.volume_capacity,
        valid_days=request.valid_days,
    )


@router.delete("/{npi}/offers/{offer_id}")
def api_withdraw_offer(npi: str, offer_id: str, db: Session = Depends(get_db)):
    """Withdraw a price offer."""
    return withdraw_offer(db, offer_id)
