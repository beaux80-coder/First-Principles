"""Provider Intelligence Feed API — Constitution F2 Q14-Q15.

Private feed giving providers continuous visibility into their competitive
position. All competitor data anonymized.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.provider_intelligence import (
    generate_provider_feed,
    generate_cross_metro_arbitrage,
)

router = APIRouter(prefix="/providers", tags=["provider-intelligence"])


@router.get("/{npi}/intelligence")
def api_provider_feed(
    npi: str,
    service_code: str = Query(description="CPT/HCPCS service code"),
    state: str | None = Query(default=None, description="State filter"),
    db: Session = Depends(get_db),
):
    """Provider Intelligence Feed: price percentile, outcomes, volume, projections."""
    return generate_provider_feed(db, npi, service_code, state)


@router.get("/{npi}/intelligence/arbitrage")
def api_cross_metro_arbitrage(
    npi: str,
    service_code: str = Query(description="CPT/HCPCS service code"),
    db: Session = Depends(get_db),
):
    """Cross-metro price arbitrage data showing volume leaving provider's metro."""
    return generate_cross_metro_arbitrage(db, npi, service_code)
