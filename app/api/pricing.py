"""Pricing & Stop-Loss API endpoints (Functions 7 and 7A).

F7 — Pricing Engine:
  Constitution: "Rate = exactly two visible components: pass-through cost + value-share fee"
  Every line item auditable. Baseline independently verifiable. Sole-revenue enforcement.

F7A — Stop-Loss Optimization:
  Constitution: "Evaluate risk across all benefit types. Compare carriers.
  Group purchasing leverage. Feed predictions into F3 and F7."
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(tags=["F7 Pricing Engine & F7A Stop-Loss"])


# ── F7 Pricing Engine Endpoints ─────────────────────────────────────────────


@router.get("/pricing/rate/{employer_id}")
def get_employer_rate(employer_id: str, db: Session = Depends(get_db)):
    """Get the full rate breakdown for an employer.

    Constitution F7: "Rate = exactly two visible components:
    pass-through cost + value-share fee."

    Returns the complete, auditable rate with:
    - Component 1: Pass-through (care delivery + stop-loss + regulatory)
    - Component 2: Value-share fee (% of verified savings)
    - Incentive alignment verification
    - Sole-revenue attestation
    - Full audit trail
    """
    from app.services.pricing_engine import compute_employer_rate

    try:
        rate = compute_employer_rate(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return rate


@router.get("/pricing/baseline/{employer_id}")
def get_employer_baseline(employer_id: str, db: Session = Depends(get_db)):
    """Get the baseline cost for an employer.

    Constitution F7: "Baseline cost continuously updated,
    independently verifiable, never stale."

    Returns:
    - Current baseline PEPM and annual
    - Baseline source and methodology
    - Staleness check
    - Independent verification sources
    - Update schedule
    """
    from app.services.pricing_engine import get_baseline

    try:
        baseline = get_baseline(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return baseline


@router.get("/pricing/incentive-proof")
def get_incentive_proof(db: Session = Depends(get_db)):
    """Get the complete incentive misalignment proof chain.

    Constitution F7: "company profits from lower delivery ->
    determined by F1 TEE -> cannot profit by denying care"

    Returns a formal logical proof that:
    1. Company revenue increases only when delivery cost decreases
    2. Delivery cost decreases only through genuine cost reduction
    3. Clinical necessity is determined by an independent TEE
    4. The company cannot influence clinical determinations
    5. No alternative revenue sources exist

    This endpoint requires no employer_id — it describes the structural
    properties of the pricing model itself.
    """
    from app.services.pricing_engine import get_incentive_proof_chain

    return get_incentive_proof_chain(db)


# ── F7A Stop-Loss Optimization Endpoints ────────────────────────────────────


@router.get("/stop-loss/evaluate/{employer_id}")
def evaluate_stop_loss(employer_id: str, db: Session = Depends(get_db)):
    """Full stop-loss evaluation for an employer.

    Constitution F7A: "Evaluate risk across all benefit types. Compare
    available stop-loss carriers. Group purchasing leverage across
    employer base. Feed predictions into F3 and F7."

    Returns:
    - Risk profile across all benefit types
    - Carrier comparison with composite scores
    - Recommended carrier
    - Group purchasing leverage analysis
    - Monte Carlo catastrophic claim simulation
    - Predictions for downstream functions (F3, F7)
    """
    from app.services.stop_loss import evaluate_stop_loss as _evaluate

    try:
        evaluation = _evaluate(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return evaluation


@router.get("/stop-loss/risk/{employer_id}")
def get_risk_assessment(employer_id: str, db: Session = Depends(get_db)):
    """Get risk assessment across all benefit types for an employer.

    Constitution F7A requirement 1: "Evaluate risk across all benefit types."

    Returns per-benefit-type risk scores, aggregate risk, and industry/size
    adjustments.
    """
    from app.services.stop_loss import assess_employer_risk

    try:
        risk = assess_employer_risk(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return risk


@router.get("/stop-loss/carriers/{employer_id}")
def get_carrier_comparison(employer_id: str, db: Session = Depends(get_db)):
    """Compare stop-loss carriers for an employer.

    Constitution F7A requirement 2: "Compare available stop-loss carriers."

    Returns carrier evaluations sorted by composite score, including
    premium rates, coverage breadth, financial strength, and group
    discount eligibility.
    """
    from app.services.stop_loss import compare_carriers

    try:
        carriers = compare_carriers(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {"employer_id": employer_id, "carriers": carriers}


@router.get("/stop-loss/group-leverage")
def get_group_leverage(db: Session = Depends(get_db)):
    """Get group purchasing leverage across the entire employer base.

    Constitution F7A requirement 3: "Group purchasing leverage across employer base."

    Returns platform-wide leverage tier, per-carrier discounts, and
    equivalent purchasing power.
    """
    from app.services.stop_loss import compute_group_purchasing_leverage

    return compute_group_purchasing_leverage(db)


@router.get("/stop-loss/simulation/{employer_id}")
def run_simulation(
    employer_id: str,
    simulations: int = 5_000,
    db: Session = Depends(get_db),
):
    """Run Monte Carlo catastrophic claim simulation for an employer.

    Simulates N years of claims experience to determine optimal specific
    and aggregate attachment points. Uses historical claims data plus
    industry actuarial assumptions.
    """
    from app.services.stop_loss import run_monte_carlo_simulation

    if simulations < 100 or simulations > 50_000:
        raise HTTPException(
            status_code=400,
            detail="simulations must be between 100 and 50,000",
        )

    try:
        result = run_monte_carlo_simulation(
            db, employer_id, num_simulations=simulations
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result
