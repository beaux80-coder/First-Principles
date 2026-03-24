"""Employer Dashboard API (Function 6B).

Constitution: "Every employer can see every line item. Every determination is
auditable. Every price is verifiable. The dashboard is a window, not a wall."

Endpoints:
  GET /dashboard/{employer_id}                     -- Full dashboard
  GET /dashboard/{employer_id}/audit/{claim_id}    -- Line item audit
  GET /dashboard/{employer_id}/rate                -- Rate breakdown
  GET /dashboard/{employer_id}/verify/{claim_id}   -- Price verification
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/dashboard", tags=["F6B Employer Dashboard"])


@router.get("/{employer_id}")
def get_dashboard(employer_id: str, db: Session = Depends(get_db)):
    """Full employer dashboard.

    Constitution: "Every employer can see every line item."

    Returns comprehensive dashboard with spending, savings, clinical
    rates, provider outcomes, and price comparisons.
    """
    from app.services.dashboard import get_employer_dashboard

    try:
        return get_employer_dashboard(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/audit/{claim_id}")
def get_line_item_audit(
    employer_id: str,
    claim_id: str,
    db: Session = Depends(get_db),
):
    """Full audit trail for any single line item.

    Constitution: "Every determination is auditable."

    Returns the complete history of a claim: submission, clinical
    determination, price comparison, adjudication decision, payment,
    and all associated reasoning.
    """
    from app.services.dashboard import get_line_item_audit as _get_audit

    try:
        return _get_audit(db, employer_id, claim_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/rate")
def get_rate_breakdown(employer_id: str, db: Session = Depends(get_db)):
    """Itemized rate breakdown (pass-through + value-share).

    Constitution: "Every price is verifiable."

    Returns the complete rate structure showing exactly how the
    employer's rate is computed.
    """
    from app.services.dashboard import get_rate_breakdown as _get_rate

    try:
        return _get_rate(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/verify/{claim_id}")
def verify_price(
    employer_id: str,
    claim_id: str,
    db: Session = Depends(get_db),
):
    """Independent price verification for any claim.

    Constitution: "Every price is verifiable."

    Takes any claim and returns independent price verification data
    with market reference prices and comparison to benchmarks.
    """
    from app.services.dashboard import verify_any_price

    try:
        return verify_any_price(db, claim_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
