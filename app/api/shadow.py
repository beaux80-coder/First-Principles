"""Shadow Mode Comparison API (Function 6A).

Constitution: "Run parallel to existing carrier. Compare every determination,
price, and claim against what the carrier actually did. Prove savings before
going live."

Endpoints:
  POST /shadow/compare                    -- Submit carrier claim for shadow comparison
  GET  /shadow/report/{employer_id}       -- Shadow mode aggregate report
  GET  /shadow/confidence/{employer_id}   -- Statistical confidence report
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/shadow", tags=["F6A Shadow Mode Comparison"])


# ── Request Models ───────────────────────────────────────────────────────────


class ShadowCompareRequest(BaseModel):
    """Carrier claim data for shadow comparison."""
    employer_id: str = Field(description="Employer UUID")
    carrier_claim_id: str = Field(default="", description="Original carrier claim ID")
    employee_external_id: str = Field(
        default="unknown", description="Employee ID from carrier system"
    )
    service_code: str = Field(description="CPT/HCPCS/CDT/NDC code")
    service_description: str = Field(default="", description="Human-readable description")
    benefit_type: str = Field(default="health", description="Benefit type")
    billed_amount: float = Field(ge=0, description="Amount billed by provider")
    carrier_paid_amount: float = Field(ge=0, description="What carrier paid")
    carrier_decision: str = Field(
        default="approved", description="Carrier's determination (approved/denied/modified)"
    )
    carrier_processing_days: int = Field(
        default=14, ge=0, description="Carrier processing time in days"
    )
    employee_oop: float = Field(
        default=0, ge=0, description="Employee out-of-pocket under carrier"
    )
    carrier_reasoning: str = Field(default="", description="Carrier's reasoning")


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/compare")
def shadow_compare(request: ShadowCompareRequest, db: Session = Depends(get_db)):
    """Submit a carrier claim for shadow comparison.

    Constitution: "Compare every determination, price, and claim against
    what the carrier actually did."

    Takes the carrier's actual claim data and runs the beneflex engine in
    parallel. Returns a side-by-side comparison of:
    - Determination: what carrier decided vs what beneflex would decide
    - Price: what carrier paid vs what beneflex would pay
    - Speed: carrier processing time vs beneflex processing time
    - Employee impact: OOP under carrier vs zero OOP under beneflex
    """
    from app.services.shadow_comparison import run_shadow_comparison

    try:
        return run_shadow_comparison(db, request.employer_id, request.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/report/{employer_id}")
def shadow_report(employer_id: str, db: Session = Depends(get_db)):
    """Shadow mode aggregate report.

    Constitution: "Prove savings before going live."

    Aggregates all shadow comparison results for an employer:
    - Total and per-claim savings vs carrier
    - Savings % by benefit type
    - Determination accuracy (agreement rate)
    - Speed improvement
    - Employee OOP eliminated
    - Projected annual savings
    """
    from app.services.shadow_comparison import get_shadow_report

    try:
        return get_shadow_report(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/confidence/{employer_id}")
def shadow_confidence(employer_id: str, db: Session = Depends(get_db)):
    """Statistical confidence that savings are real (not noise).

    Constitution: "Prove savings before going live."

    Uses statistical hypothesis testing to determine whether observed
    savings are statistically significant. Returns confidence levels
    at 90%, 95%, and 99%, plus an activation readiness assessment.

    The employer should see >95% confidence before activating from
    shadow to live mode.
    """
    from app.services.shadow_comparison import get_shadow_confidence

    try:
        return get_shadow_confidence(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/activate/{employer_id}")
def activate_from_shadow_mode(employer_id: str, db: Session = Depends(get_db)):
    """One-click activation: transition from shadow to live.

    Constitution: "One-click transition from shadow to live. Zero data
    re-entry." All configuration, employee mappings, and carrier
    connections carry over automatically.

    This is the one-click activation endpoint accessible from the shadow
    dashboard. It validates that the employer is in shadow mode, gathers
    shadow performance data, and transitions to live.
    """
    from app.services.shadow_mode import activate_from_shadow

    result = activate_from_shadow(db, employer_id)
    if "error" in result:
        status_code = 404 if result["error"] == "employer_not_found" else 400
        raise HTTPException(status_code=status_code, detail=result)
    return result
