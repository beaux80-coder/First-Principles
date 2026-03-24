"""Employer Performance Dashboard API (Function 6B).

Constitution: Transparent, auditable dashboard showing every pass-through dollar,
care execution metrics, employee outcomes, verified savings, network effects,
and frictionless referral capability.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/dashboard", tags=["F6B Employer Performance Dashboard"])


@router.get("/{employer_id}")
def get_dashboard(employer_id: str, db: Session = Depends(get_db)):
    """Full employer performance dashboard.

    Shows where every pass-through dollar went, care execution metrics,
    employee health outcomes, verified savings, and network effects.
    """
    from app.services.employer_dashboard import get_full_dashboard

    try:
        return get_full_dashboard(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/cost-breakdown")
def get_cost_breakdown(employer_id: str, db: Session = Depends(get_db)):
    """Line-item cost breakdown by benefit type, service category, and provider.

    Constitution: every pass-through dollar is traceable to a specific
    care delivery cost, stop-loss premium, or regulatory fee.
    """
    from app.services.employer_dashboard import get_cost_breakdown as _get_breakdown

    try:
        return _get_breakdown(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/care-metrics")
def get_care_metrics(employer_id: str, db: Session = Depends(get_db)):
    """Care execution metrics: episodes managed, appointments scheduled,
    referrals coordinated, claims processing speed.
    """
    from app.services.employer_dashboard import get_care_metrics as _get_metrics

    try:
        return _get_metrics(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/outcomes")
def get_outcomes(employer_id: str, db: Session = Depends(get_db)):
    """Employee health outcomes: resolution rates, satisfaction, NPS,
    clinical accuracy, zero cost-sharing verification.
    """
    from app.services.employer_dashboard import get_employee_outcomes

    try:
        return get_employee_outcomes(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{employer_id}/network-effect")
def get_network_effect(employer_id: str, db: Session = Depends(get_db)):
    """Network effect visibility: how platform growth reduces this
    employer's costs through group purchasing leverage.
    """
    from app.services.employer_dashboard import get_network_effect as _get_network

    try:
        return _get_network(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{employer_id}/share")
def share_dashboard(employer_id: str, db: Session = Depends(get_db)):
    """Generate a shareable referral link with anonymized results.

    Creates a link containing anonymized performance data that:
    - Does NOT identify the employer by name
    - Shows savings percentages (not absolute dollars)
    - Links to the F6A benchmark tool for the prospect's own projection
    - Anonymized data flows back to F6A for network-wide benchmarks
    """
    from app.services.employer_dashboard import generate_referral_link

    try:
        return generate_referral_link(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
