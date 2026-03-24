"""Shadow Mode API — Function 6A, Stage 2 + Stage 3 + Proof Chain.

Endpoints:
  POST /shadow/start               — Start shadow mode (carrier connection + initial claims)
  POST /shadow/claim               — Process a single shadow claim
  GET  /shadow/{employer_id}/dashboard — Real-time shadow mode dashboard
  POST /shadow/activate            — One-click activation (shadow -> live)
  GET  /shadow/{employer_id}/proof  — Proof chain summary (4 layers)
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.shadow_mode import (
    start_shadow_mode,
    process_shadow_claim,
    get_shadow_dashboard,
    activate_from_shadow,
)
from app.services.carrier_integration import ingest_carrier_claim
from app.services.proof_chain import tag_proof_layer, get_proof_summary

router = APIRouter(prefix="/shadow", tags=["shadow_mode"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ShadowStartRequest(BaseModel):
    employer_id: str = Field(description="Employer UUID")
    carrier_name: str = Field(
        default="mock",
        description="Carrier/TPA name (e.g. 'aetna', 'cigna', 'mock')",
    )
    connection_type: str = Field(
        default="mock",
        description="Connection type: oauth, sftp, edi, api_key, mock",
    )
    credentials: dict | None = Field(
        default=None,
        description="Carrier-specific credentials (OAuth token, SFTP creds, etc.)",
    )


class ShadowClaimRequest(BaseModel):
    employer_id: str = Field(description="Employer UUID")
    carrier_claim_id: str = Field(default="", description="Original carrier claim ID")
    employee_external_id: str = Field(description="Employee ID from carrier system")
    service_date: str = Field(description="Date of service (ISO 8601)")
    service_code: str = Field(description="CPT/HCPCS/CDT/NDC code")
    service_description: str = Field(default="", description="Human-readable description")
    benefit_type: str = Field(default="health", description="Benefit type")
    billed_amount: float = Field(ge=0, description="Amount billed")
    carrier_paid_amount: float = Field(ge=0, description="What carrier paid")
    employee_oop: float = Field(ge=0, default=0, description="Employee out-of-pocket under carrier")
    carrier_decision: str = Field(default="approved", description="Carrier's decision")
    carrier_reasoning: str = Field(default="", description="Carrier's reasoning")


class ActivateRequest(BaseModel):
    employer_id: str = Field(description="Employer UUID")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/start")
def start_shadow(request: ShadowStartRequest, db: Session = Depends(get_db)):
    """Start shadow mode for an employer.

    Constitution: "One-click API connection to employer's current carrier/TPA.
    Max 3 employer actions to enter shadow mode."

    Steps: select carrier -> authorise -> confirm. That's it.
    """
    return start_shadow_mode(
        db=db,
        employer_id=request.employer_id,
        carrier_name=request.carrier_name,
        connection_type=request.connection_type,
        credentials=request.credentials,
    )


@router.post("/claim")
def shadow_claim(request: ShadowClaimRequest, db: Session = Depends(get_db)):
    """Process a single claim in shadow mode.

    Constitution: "Process every real claim in parallel using full production
    logic (F1, F2, F4, F9). Side-by-side comparison per claim."

    Accepts carrier claim data, runs the full adjudication pipeline in
    shadow mode, returns the side-by-side comparison.
    """
    carrier_claim = ingest_carrier_claim(request.model_dump())
    result = process_shadow_claim(db, request.employer_id, carrier_claim)
    db.commit()
    return result


@router.get("/{employer_id}/dashboard")
def shadow_dashboard(employer_id: str, db: Session = Depends(get_db)):
    """Real-time shadow mode dashboard.

    Constitution: "Real-time dashboard updating per claim, per employee,
    per benefit type." Shows aggregate savings, per-benefit-type breakdown,
    per-employee breakdown, and individual claim details.

    Shadow mode = zero cost, zero risk, zero disruption.
    """
    return get_shadow_dashboard(db, employer_id)


@router.post("/activate")
def activate(request: ActivateRequest, db: Session = Depends(get_db)):
    """One-click activation: transition from shadow to live.

    Constitution: "One-click transition from shadow to live. Zero data
    re-entry." All configuration, employee mappings, and carrier
    connections carry over automatically.
    """
    return activate_from_shadow(db, request.employer_id)


@router.get("/{employer_id}/proof")
def proof_summary(employer_id: str, db: Session = Depends(get_db)):
    """Proof chain summary for an employer (4 layers).

    Constitution proof chain:
    - Layer 1: Every price independently verifiable
    - Layer 2: Every care coordination claim backed by execution logs
    - Layer 3: Aggregate results actuarially certified
    - Layer 4: Shadow-to-live track record published
    """
    return get_proof_summary(db, employer_id)
