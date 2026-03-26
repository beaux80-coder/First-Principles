"""Price Discovery & Direct Payment API (Function 2).

Constitution: "For every service at every provider, dynamically compares
all pricing channels. Selects and pays the lowest verified price."
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/price", tags=["price-discovery"])


class PriceCompareRequest(BaseModel):
    service_code: str
    provider_npi: Optional[str] = None
    state: Optional[str] = None
    benefit_type: str = "health"


class PharmacyCompareRequest(BaseModel):
    ndc_code: str
    drug_name: Optional[str] = None
    state: Optional[str] = None


class PaymentRequest(BaseModel):
    claim_id: str
    provider_name: str
    amount: float
    provider_npi: Optional[str] = None
    standard_price: Optional[float] = None


@router.post("/compare")
def compare_prices(req: PriceCompareRequest, db: Session = Depends(get_db)):
    """Compare all pricing channels for a service. Returns lowest verified price.

    Constitution completion test: "Is there any pricing channel that physically
    exists and is legally accessible that the system does not compare?"
    """
    from app.services.price_discovery import compare_all_channels

    result = compare_all_channels(
        db, req.service_code, req.provider_npi, req.state, req.benefit_type
    )

    # Record comparison feeding F8 (only when we have valid FK references)
    # In production, claim submission provides real service_id and provider_id
    # For now, the comparison data itself is the structured record

    return result


@router.post("/pharmacy/compare")
def compare_pharmacy_prices(req: PharmacyCompareRequest, db: Session = Depends(get_db)):
    """Compare all pharmacy pricing channels. PBM elimination.

    Constitution: "The system eliminates the Pharmacy Benefit Manager entirely."
    """
    from app.services.price_discovery import compare_pharmacy_channels

    return compare_pharmacy_channels(db, req.ndc_code, req.drug_name, req.state)


@router.post("/pay")
def execute_payment(req: PaymentRequest, db: Session = Depends(get_db)):
    """Execute direct payment to provider at earliest moment possible.

    Constitution: "Payment executed directly to the provider via direct
    electronic payment at the earliest moment a verified charge is presented."
    """
    from app.services.payment import execute_payment as _execute

    return _execute(
        db, req.claim_id, req.provider_name, req.amount,
        req.provider_npi, req.standard_price,
    )


@router.get("/payment-speed")
def payment_speed_metrics(db: Session = Depends(get_db)):
    """Get payment speed and discount metrics.

    Constitution: "Payment speed: median time from verified charge to
    payment execution."
    """
    from app.services.payment import get_payment_speed_metrics

    return get_payment_speed_metrics(db)


@router.get("/channels")
def list_channels():
    """List all pricing channels the system compares.

    Constitution: "Is there any pricing channel that physically exists and is
    legally accessible that the system does not compare?"
    """
    from app.services.price_discovery import PRICING_CHANNELS

    return {
        "channels": PRICING_CHANNELS,
        "total": len(PRICING_CHANNELS),
        "network_contracts": "NONE",
        "pbm": "ELIMINATED",
        "intermediary_fees": "NONE (unless legally required)",
    }
