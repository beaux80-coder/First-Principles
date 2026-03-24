"""Broker Distribution API (Function 10).

Constitution: Broker advisory fee paid from value-share revenue,
not employer pass-through. Fully disclosed. No exclusive arrangements.
F6A benchmark serves as broker's analytical tool.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/broker", tags=["F10 Broker Distribution"])


class BrokerRegisterRequest(BaseModel):
    name: str
    firm: Optional[str] = None
    email: Optional[str] = None
    advisory_fee_pct: Optional[float] = None


@router.post("/register")
def register_broker(request: BrokerRegisterRequest, db: Session = Depends(get_db)):
    """Register a new broker on the platform.

    Advisory fee is a percentage of value-share revenue (not employer
    pass-through). Fully disclosed. No exclusive arrangements.
    """
    from app.services.broker_advisory import register_broker as _register

    try:
        return _register(
            db,
            name=request.name,
            firm=request.firm,
            email=request.email,
            advisory_fee_pct=request.advisory_fee_pct,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{broker_id}/analytics")
def get_broker_analytics(broker_id: str, db: Session = Depends(get_db)):
    """Broker's analytical dashboard.

    Shows activations, aggregate client outcomes, total advisory fees,
    and links to F6A benchmark for prospecting.
    """
    from app.services.broker_advisory import get_broker_analytics as _get_analytics

    try:
        return _get_analytics(db, broker_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/advisory-fee")
def get_advisory_fee(db: Session = Depends(get_db)):
    """Fee structure for brokers (transparent, fully disclosed).

    Shows how the advisory fee is calculated from value-share revenue,
    default and maximum rates, and worked examples.
    """
    from app.services.broker_advisory import get_advisory_fee_structure

    return get_advisory_fee_structure(db)


@router.get("/{broker_id}/activations")
def get_activations(broker_id: str, db: Session = Depends(get_db)):
    """Broker-driven activations: employers brought onto the platform
    by this broker.
    """
    from app.services.broker_advisory import get_broker_activations

    try:
        return get_broker_activations(db, broker_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
