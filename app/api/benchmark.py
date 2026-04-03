"""Benchmark API — public, zero login required.

Function 6A, Stage 1: static benchmark showing employers what their benefits
could look like under the system vs. what they have now.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.benchmark_query import BenchmarkQuery, BenchmarkStage
from app.services.benchmark import compute_benchmark

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


class BenchmarkRequest(BaseModel):
    company_name: str | None = Field(
        default=None,
        description="Company name for automatic lookup from public records",
    )
    employee_count: int | None = Field(default=None, ge=1, le=100000, description="Number of employees")
    state: str | None = Field(default=None, min_length=2, max_length=2, description="Primary state (2-letter code)")
    industry: str | None = Field(default=None, min_length=1, description="Industry")
    annual_spend: float | None = Field(
        default=None, ge=0, description="Approximate annual benefits spend in USD"
    )


class BenchmarkResponse(BaseModel):
    query_id: str
    inputs: dict
    current_cost: dict
    system_cost: dict
    comparison: dict
    experience_comparison: dict
    transparency: dict
    data_quality: dict
    benefit_type_breakdown: dict = {}
    pharmacy_insights: dict = {}
    cross_type_insights: dict = {}
    price_examples: list = []
    local_quality: dict = {}
    shadow_mode_entry: dict = {}
    entry_options: dict = {}
    share_url: str


@router.post("/", response_model=BenchmarkResponse)
def create_benchmark(request: BenchmarkRequest, db: Session = Depends(get_db)):
    """Generate a benchmark comparison. Zero login required.

    Accepts minimal inputs and returns a comprehensive analysis showing
    the employer's current estimated cost vs. system cost, plus the full
    product difference (cost, care execution, transparency).

    Two entry paths (Build Manifest items 2-3):
      1. Enter company name -> auto-populate from Form 5500 / public records
      2. Manually provide employee_count, state, industry, annual_spend
    """
    # If company_name provided, auto-populate from public records (item 3)
    public_data = None
    if request.company_name:
        from app.services.benchmark import lookup_employer_public_data

        public_data = lookup_employer_public_data(db, request.company_name)
        if public_data.get("found"):
            if not request.employee_count and public_data.get("employee_count"):
                request.employee_count = public_data["employee_count"]
            if not request.state and public_data.get("state"):
                request.state = public_data["state"]
            if not request.industry and public_data.get("industry"):
                request.industry = public_data["industry"]
            if not request.annual_spend and public_data.get("annual_spend"):
                request.annual_spend = public_data["annual_spend"]

    # Apply defaults for any fields still missing after lookup
    employee_count = request.employee_count or 50
    state = request.state or "TX"
    industry = request.industry or "general"

    result = compute_benchmark(
        db=db,
        employee_count=employee_count,
        state=state,
        industry=industry,
        annual_spend=request.annual_spend,
    )

    # Record the query — every query feeds the data pipeline (Function 8)
    query_record = BenchmarkQuery(
        inputs={
            "company_name": request.company_name,
            "employee_count": employee_count,
            "state": state,
            "industry": industry,
            "annual_spend": request.annual_spend,
            "public_data_source": public_data.get("source") if public_data else None,
        },
        results=result,
        stage=BenchmarkStage.static,
    )
    db.add(query_record)
    db.commit()
    db.refresh(query_record)

    # Item 8: one-click shadow mode entry from benchmark result
    shadow_mode_entry = {
        "action": "Enter Shadow Mode",
        "description": (
            "Connect to your current carrier for a claim-by-claim "
            "analysis — zero cost, zero risk"
        ),
        "endpoint": "/api/v1/shadow/start",
        "one_click": True,
    }

    # Items 2-3: communicate both entry paths
    entry_options = {
        "path_a": {
            "label": "Quick benchmark via company name",
            "description": (
                "Enter your company name and we auto-populate employee count, "
                "location, industry, and spend from Form 5500 filings and "
                "public records."
            ),
            "fields": ["company_name"],
        },
        "path_b": {
            "label": "Shadow mode via carrier connection",
            "description": (
                "Authorize a connection to your current carrier/TPA for a "
                "real claim-by-claim shadow analysis. Zero cost, zero risk."
            ),
            "endpoint": "/api/v1/shadow/start",
        },
    }

    return BenchmarkResponse(
        query_id=str(query_record.query_id),
        inputs=result["inputs"],
        current_cost=result["current_cost"],
        system_cost=result["system_cost"],
        comparison=result["comparison"],
        experience_comparison=result["experience_comparison"],
        transparency=result["transparency"],
        data_quality=result["data_quality"],
        benefit_type_breakdown=result.get("benefit_type_breakdown", {}),
        pharmacy_insights=result.get("pharmacy_insights", {}),
        cross_type_insights=result.get("cross_type_insights", {}),
        price_examples=result.get("price_examples", []),
        local_quality=result.get("local_quality", {}),
        shadow_mode_entry=shadow_mode_entry,
        entry_options=entry_options,
        share_url=f"/benchmark/{query_record.query_id}",
    )


@router.get("/{query_id}")
def get_benchmark(query_id: str, db: Session = Depends(get_db)):
    """Retrieve a previously generated benchmark by ID. Enables shareable URLs."""
    query = db.query(BenchmarkQuery).filter(
        BenchmarkQuery.query_id == query_id
    ).first()
    if not query:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Benchmark not found")
    return {
        "query_id": str(query.query_id),
        "inputs": query.inputs,
        "results": query.results,
        "created_at": query.created_at.isoformat(),
    }


@router.post("/{query_id}/interaction")
def track_interaction(query_id: str, interaction: dict, db: Session = Depends(get_db)):
    """Track user interaction with benchmark (items 26-28).

    Records searches, drop-offs, time spent, funnel stage, and company info.
    Feeds F8 for auto-optimization of benchmark presentation.
    """
    from app.services.benchmark import record_benchmark_interaction

    return record_benchmark_interaction(
        db, query_id, interaction.get("company_name"), interaction
    )
