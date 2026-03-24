"""Broker Channel API (Function 10).

Constitution: "Brokers are the distribution channel. They need tools to
compare beneflex vs incumbents, model savings, and onboard employers."

Endpoints:
  POST /broker/model-savings        -- Savings projection
  GET  /broker/compare/{employer_id} -- Comparison vs incumbent
  POST /broker/proposal             -- Generate proposal
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/broker", tags=["F10 Broker Channel"])


# ── Request Models ───────────────────────────────────────────────────────────


class ModelSavingsRequest(BaseModel):
    """Employer profile for savings modeling."""
    employee_count: int = Field(ge=1, description="Number of employees")
    industry: str = Field(default="default", description="Industry vertical")
    geography: str = Field(default="National", description="State or region")
    current_pepm: Optional[float] = Field(
        default=None, ge=0,
        description="Current per-employee-per-month cost (optional)",
    )
    current_carrier: str = Field(
        default="Unknown", description="Current carrier name"
    )


class GenerateProposalRequest(BaseModel):
    """Employer profile for proposal generation."""
    employee_count: int = Field(ge=1, description="Number of employees")
    industry: str = Field(default="default", description="Industry vertical")
    geography: str = Field(default="National", description="State or region")
    current_pepm: Optional[float] = Field(
        default=None, ge=0,
        description="Current per-employee-per-month cost (optional)",
    )
    current_carrier: str = Field(
        default="Unknown", description="Current carrier name"
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/model-savings")
def model_savings(request: ModelSavingsRequest, db: Session = Depends(get_db)):
    """Savings projection for a prospective employer.

    Constitution: "Brokers need tools to model savings."

    Takes employer demographics and current spend, projects what they
    would save with beneflex. Returns per-source breakdown of savings
    with methodology notes.
    """
    from app.services.broker_tools import model_savings as _model

    return _model(db, request.model_dump())


@router.get("/compare/{employer_id}")
def compare_vs_incumbent(employer_id: str, db: Session = Depends(get_db)):
    """Side-by-side comparison of beneflex vs incumbent carrier.

    Constitution: "Brokers need tools to compare beneflex vs incumbents."

    For an existing employer on the platform, compares their actual
    beneflex results against what the incumbent carrier was delivering.
    """
    from app.services.broker_tools import compare_vs_incumbent as _compare

    try:
        return _compare(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/proposal")
def generate_proposal(
    request: GenerateProposalRequest,
    db: Session = Depends(get_db),
):
    """Generate broker proposal with projected savings, rate structure,
    and implementation timeline.

    Constitution: "Brokers need tools to onboard employers."

    Creates a complete proposal for a broker to present to a
    prospective employer, including savings projections, rate
    structure, implementation timeline, and constitutional guarantees.
    """
    from app.services.broker_tools import generate_proposal as _generate

    return _generate(db, request.model_dump())
