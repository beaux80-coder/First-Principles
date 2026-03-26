"""Benchmark API — public, zero login required.

Function 6A, Stage 1: static benchmark showing employers what their benefits
could look like under the system vs. what they have now.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.benchmark_query import BenchmarkQuery, BenchmarkStage
from app.services.benchmark import compute_benchmark

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


class BenchmarkRequest(BaseModel):
    employee_count: int = Field(ge=1, le=100000, description="Number of employees")
    state: str = Field(min_length=2, max_length=2, description="Primary state (2-letter code)")
    industry: str = Field(min_length=1, description="Industry")
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
    share_url: str


@router.post("/", response_model=BenchmarkResponse)
def create_benchmark(request: BenchmarkRequest, db: Session = Depends(get_db)):
    """Generate a benchmark comparison. Zero login required.

    Accepts minimal inputs and returns a comprehensive analysis showing
    the employer's current estimated cost vs. system cost, plus the full
    product difference (cost, care execution, transparency).
    """
    result = compute_benchmark(
        db=db,
        employee_count=request.employee_count,
        state=request.state,
        industry=request.industry,
        annual_spend=request.annual_spend,
    )

    # Record the query — every query feeds the data pipeline (Function 8)
    query_record = BenchmarkQuery(
        inputs={
            "employee_count": request.employee_count,
            "state": request.state,
            "industry": request.industry,
            "annual_spend": request.annual_spend,
        },
        results=result,
        stage=BenchmarkStage.static,
    )
    db.add(query_record)
    db.commit()
    db.refresh(query_record)

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
