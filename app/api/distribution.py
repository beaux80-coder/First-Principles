"""Distribution Engine API (Function 12).

Constitution: "Ensure every new employer is acquired by the system itself,
with zero human sales effort."
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/distribution", tags=["F12 Distribution"])


# -- Request Models ----------------------------------------------------------


class PublicationRequest(BaseModel):
    """Request to generate a data-driven research publication."""
    metro: str
    topic: str


class BrokerContentRequest(BaseModel):
    """Request to generate broker-specific content."""
    broker_id: str
    industry_focus: str


class Form5500IngestRequest(BaseModel):
    """Batch of raw Form 5500 records for ingestion."""
    records: list[dict]


class FullFunnelRequest(BaseModel):
    """Trigger full funnel automation (optional body for future params)."""
    pass


# -- Endpoints ---------------------------------------------------------------


@router.post("/publication")
def generate_publication(
    request: PublicationRequest,
    db: Session = Depends(get_db),
):
    """Generate a data-driven research publication for a metro area.

    Constitution F12: publications establish credibility and attract brokers.
    Uses real statistical analysis from ingested price data.
    """
    from app.services.distribution_engine import generate_data_driven_publication

    result = generate_data_driven_publication(db, metro=request.metro, topic=request.topic)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.get("/metro-focus")
def metro_focus(db: Session = Depends(get_db)):
    """Auto-shift metro focus based on employer density and conversion rates.

    Constitution F12: optimise distribution resources toward highest-value
    metro areas automatically.
    """
    from app.services.distribution_engine import auto_shift_metro_focus

    return auto_shift_metro_focus(db)


@router.get("/brokers/{state}")
def identify_brokers(state: str, db: Session = Depends(get_db)):
    """Identify licensed brokers from state DOI databases.

    Constitution F12: broker-mediated distribution requires identifying
    brokers by state.
    """
    from app.services.distribution_engine import identify_brokers_from_doi

    result = identify_brokers_from_doi(db, state=state)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/broker-content")
def broker_content(
    request: BrokerContentRequest,
    db: Session = Depends(get_db),
):
    """Generate customized research content for a broker's client portfolio.

    Constitution F12: content tailored to the broker's industry focus
    increases conversion from research to benchmark to shadow to activation.
    """
    from app.services.distribution_engine import generate_broker_specific_content

    result = generate_broker_specific_content(
        db,
        broker_id=request.broker_id,
        industry_focus=request.industry_focus,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.get("/optimization")
def distribution_optimization(db: Session = Depends(get_db)):
    """Run distribution channel optimization across all channels.

    Constitution F12: continuously optimize which distribution channels
    (publications, brokers, benchmarks, referrals) convert best.
    """
    from app.services.distribution_engine import run_distribution_optimization

    return run_distribution_optimization(db)


@router.post("/full-funnel")
def full_funnel(db: Session = Depends(get_db)):
    """End-to-end funnel automation: research -> benchmark -> shadow -> activation.

    Constitution F12: automate the entire acquisition funnel with zero
    human sales effort.
    """
    from app.services.distribution_engine import automate_full_funnel

    return automate_full_funnel(db)


@router.post("/form5500/ingest")
def ingest_form5500(request: Form5500IngestRequest):
    """Ingest a batch of DOL Form 5500 filings for proactive benchmarking.

    Constitution F12: Form 5500 public filings reveal employer benefit
    costs, enabling proactive outreach with benchmark comparisons.
    """
    from app.services.distribution_engine import Form5500Ingester

    ingester = Form5500Ingester()
    result = ingester.ingest_batch(request.records)
    return result


@router.get("/tipping-points")
def tipping_points(metro_code: Optional[str] = None):
    """Get service-level thresholds that trigger employer switching.

    Constitution F12: understanding switching tipping points allows
    targeted outreach when employers are most likely to change.
    """
    from app.services.distribution_engine import get_service_level_thresholds

    return get_service_level_thresholds(metro_code=metro_code)


@router.get("/channel-performance")
def channel_performance(db: Session = Depends(get_db)):
    """Return distribution channel performance metrics.

    Constitution F12: track which channels (publications, brokers,
    benchmarks, Form 5500, referrals) drive the most conversions
    to optimize distribution spend.
    """
    from app.services.distribution_engine import run_distribution_optimization

    optimization = run_distribution_optimization(db)

    # Extract channel-level metrics from the optimization result
    channels = optimization.get("channel_metrics", {})
    return {
        "channels": channels,
        "total_employers_in_funnel": optimization.get("total_employers", 0),
        "funnel_stages": optimization.get("funnel_metrics", {}),
        "recommendations": optimization.get("recommendations", []),
    }
