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


@router.post("/{employer_id}/share")
def share_results(employer_id: str, db: Session = Depends(get_db)):
    """Generate shareable, anonymized results for referral.

    Constitution F6B: "The employer can share their own verified, anonymized
    results (cost, experience, outcomes — no employee PII) directly with
    another employer, linking to the benchmark tool (Function 6A).
    One action, not a sales pitch — their auditable data speaks for itself."
    """
    from app.services.dashboard import get_employer_dashboard

    try:
        dashboard = get_employer_dashboard(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Anonymize: remove employer name, employee details, provider names
    anonymized = {
        "shared_at": dashboard["dashboard_generated_at"],
        "employee_count": dashboard["employee_count"],
        "results": {
            "total_claims_processed": dashboard["spending"]["total_claims"],
            "employee_out_of_pocket": dashboard["spending"]["employee_out_of_pocket"],
            "auto_adjudication_rate": dashboard["clinical_rates"]["auto_adjudication_rate_pct"],
            "benefit_types_covered": 7,
            "care_episodes_managed": dashboard.get("care_execution", {}).get("total_episodes_managed", 0),
            "zero_phone_calls_pct": dashboard.get("care_execution", {}).get("zero_phone_calls_pct", 100),
        },
        "savings": dashboard.get("savings", {}),
        "transparency": dashboard["transparency_attestation"],
        "benchmark_link": "/benchmark/",
        "note": (
            "These are verified, auditable results from an actual employer "
            "on the platform. No employee PII is included. Click the benchmark "
            "link to see what your own benefits could look like."
        ),
    }

    return {
        "share_id": f"share-{employer_id[:8]}",
        "anonymized_results": anonymized,
        "share_url": f"/benchmark/?ref=share-{employer_id[:8]}",
        "feeding_f6a": True,
    }
