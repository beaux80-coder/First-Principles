"""Data pipeline API endpoints for managing public data ingestion."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, UploadFile, File
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.services.data_ingestion import (
    ingest_hospital_transparency_csv,
    ingest_hospital_transparency_json,
    ingest_medicare_physician_fee_schedule,
    ingest_nadac_pharmacy,
    get_ingestion_stats,
)
from app.services.data_downloaders import (
    download_hospital_compare,
    download_physician_quality,
    match_quality_to_providers,
    run_full_ingestion,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/data-pipeline", tags=["data-pipeline"])


@router.get("/stats")
def pipeline_stats(db: Session = Depends(get_db)):
    """Get current data pipeline statistics. Public endpoint."""
    return get_ingestion_stats(db)


@router.post("/ingest/auto")
def trigger_auto_ingestion(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger automated ingestion from all CMS public data sources.

    Downloads real data from CMS (NADAC, Medicare PFS, hospital MRFs).
    Runs in the background — returns immediately.
    """
    def _run():
        from app.database import SessionLocal
        session = SessionLocal()
        try:
            results = run_full_ingestion(session)
            logger.info(f"Auto-ingestion complete: {results}")
        except Exception as e:
            logger.error(f"Auto-ingestion failed: {e}")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {"status": "ingestion_started", "message": "Downloading from CMS sources in background"}


@router.post("/ingest/hospital-transparency")
async def ingest_hospital_file(
    file: UploadFile = File(...),
    hospital_name: str = "",
    state: str = "",
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and ingest a hospital price transparency file (CSV or JSON)."""
    content = (await file.read()).decode("utf-8", errors="replace")
    source_url = f"upload:{file.filename}"

    if file.filename and file.filename.endswith(".json"):
        count = ingest_hospital_transparency_json(
            db, content, hospital_name or file.filename, source_url, state or None
        )
    else:
        count = ingest_hospital_transparency_csv(
            db, content, hospital_name or file.filename, source_url, state or None
        )

    return {"records_ingested": count, "hospital": hospital_name, "file": file.filename}


@router.post("/ingest/medicare-pfs")
async def ingest_medicare_file(
    file: UploadFile = File(...),
    year: int = 2024,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and ingest a Medicare Physician Fee Schedule CSV."""
    content = (await file.read()).decode("utf-8", errors="replace")
    count = ingest_medicare_physician_fee_schedule(db, content, year)
    return {"records_ingested": count, "year": year}


@router.post("/ingest/nadac")
async def ingest_nadac_file(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload and ingest a NADAC pharmacy pricing CSV."""
    content = (await file.read()).decode("utf-8", errors="replace")
    count = ingest_nadac_pharmacy(db, content)
    return {"records_ingested": count}


@router.post("/ingest/hospital-compare")
def trigger_hospital_compare(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download CMS Hospital Compare quality data and match to providers.

    Fetches ~5,400 hospital quality ratings from CMS, matches them to
    providers by name/state, and updates provider quality_score fields.
    """
    def _run():
        from app.database import SessionLocal
        session = SessionLocal()
        try:
            count = download_hospital_compare(session)
            logger.info(f"Hospital Compare ingestion: {count} ratings ingested")
        except Exception as e:
            logger.error(f"Hospital Compare ingestion failed: {e}")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {
        "status": "ingestion_started",
        "message": "Downloading CMS Hospital Compare quality data in background",
    }


@router.post("/ingest/physician-quality")
def trigger_physician_quality(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download CMS Physician Quality (MIPS) data and match to providers.

    Fetches physician quality/MIPS scores and matches by NPI to update
    provider quality_score fields for physician-type providers.
    """
    def _run():
        from app.database import SessionLocal
        session = SessionLocal()
        try:
            count = download_physician_quality(session)
            logger.info(f"Physician quality ingestion: {count} providers updated")
        except Exception as e:
            logger.error(f"Physician quality ingestion failed: {e}")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {
        "status": "ingestion_started",
        "message": "Downloading CMS Physician Quality data in background",
    }


@router.post("/match-quality-to-providers")
def trigger_quality_matching(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-run quality score matching from existing price_data to providers.

    Uses already-ingested CMS Hospital Compare data (in price_data table)
    to match and update provider quality_score fields.
    """
    matched = match_quality_to_providers(db)
    return {
        "matched_providers": matched,
        "message": f"Updated quality_score for {matched} providers",
    }


@router.get("/cross-type-analytics")
def cross_type_analytics(db: Session = Depends(get_db)):
    """Cross-benefit-type pattern detection and analytics.

    Constitution F8 requirement: actively detect cross-benefit-type patterns
    that predict cost, utilization, and outcomes. Feeds F1, F3, F4, F9.
    """
    from app.services.cross_type_analytics import get_cross_type_report
    return get_cross_type_report(db)


@router.get("/ml-evaluation")
def ml_evaluation(db: Session = Depends(get_db)):
    """ML technique evaluation for the data pipeline.

    Constitution F8 requirement: evaluate all physically possible ML techniques
    that could improve downstream function performance.
    """
    from app.services.ml_evaluation import evaluate_ml_techniques
    return evaluate_ml_techniques(db)


@router.get("/research")
def published_research(db: Session = Depends(get_db)):
    """Aggregate anonymized research findings.

    Constitution F6A requirement: publish aggregate anonymized findings
    as industry research without exposing proprietary data.
    Public endpoint — freely accessible.
    """
    from app.services.research_publications import generate_research_report
    return generate_research_report(db)


@router.get("/metrics")
def pipeline_metrics(db: Session = Depends(get_db)):
    """Full F8 data pipeline metrics dashboard.

    Constitution F8 metrics:
    - Data points per period by type, source, benefit type
    - Public data coverage: % of U.S. hospitals/insurers ingested
    - Data completeness: % of interactions producing structured records
    - Cross-type data coverage
    """
    from app.services.data_pipeline_metrics import get_pipeline_dashboard
    return get_pipeline_dashboard(db)


@router.post("/ingest/goodrx")
def trigger_goodrx_scrape(
    background_tasks: BackgroundTasks,
    max_drugs: int = 20,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Scrape GoodRx.com pharmacy prices for top prescribed drugs.

    Constitution F2: "Any pricing channel that physically exists and is
    legally accessible." GoodRx prices are publicly visible on their website.
    Web scraping public prices is legal (hiQ Labs v. LinkedIn, 2022).

    Compares against NADAC and records every comparison feeding F8.
    If scraping is blocked (403/CAPTCHA), documents the barrier.
    """
    def _run():
        from app.database import SessionLocal
        from app.services.goodrx_scraper import scrape_goodrx_batch
        session = SessionLocal()
        try:
            results = scrape_goodrx_batch(session, max_drugs=max_drugs)
            logger.info(
                f"GoodRx scrape complete: {results['drugs_with_prices']}/{results['drugs_attempted']} "
                f"with prices, {results['drugs_blocked']} blocked"
            )
        except Exception as e:
            logger.error(f"GoodRx scrape failed: {e}")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {
        "status": "scrape_started",
        "max_drugs": max_drugs,
        "message": f"Scraping GoodRx prices for up to {max_drugs} drugs in background",
        "legal_basis": "hiQ Labs v. LinkedIn (2022) — scraping publicly visible prices is legal",
    }


@router.get("/goodrx/status")
def goodrx_status(db: Session = Depends(get_db)):
    """Get GoodRx scraping status and results.

    Shows scraped prices, comparison results vs NADAC, and any barriers.
    """
    from app.services.goodrx_scraper import TOP_PRESCRIBED_DRUGS
    from app.models.price_data import PriceData, PriceSource
    from app.models.data_pipeline_metric import DataPipelineMetric
    from sqlalchemy import func

    # Count stored GoodRx prices
    goodrx_count = db.query(func.count(PriceData.price_id)).filter(
        PriceData.source == PriceSource.goodrx_scrape
    ).scalar() or 0

    # Get latest metric
    latest_metric = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "goodrx_price_scrape"
    ).order_by(DataPipelineMetric.measured_at.desc()).first()

    return {
        "goodrx_prices_stored": goodrx_count,
        "top_drugs_tracked": len(TOP_PRESCRIBED_DRUGS),
        "latest_scrape": latest_metric.details if latest_metric else None,
        "latest_scrape_at": latest_metric.measured_at.isoformat() if latest_metric else None,
        "channel": "discount_card (GoodRx)",
        "legal_basis": "hiQ Labs v. LinkedIn (2022)",
    }


@router.post("/ingest/apcd")
def trigger_apcd_ingestion(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ingest State All-Payer Claims Database (APCD) data.

    Constitution F8: "Is there any publicly available data source the system
    does not ingest?"

    Attempts to download freely available APCD data from:
    - Colorado (CIVHC)
    - New Hampshire (NHCHIS)
    - Maine (MHDO)
    - Rhode Island DOH

    Documents registration requirements where applicable.
    """
    def _run():
        from app.database import SessionLocal
        from app.services.apcd_ingester import ingest_all_apcd_states
        session = SessionLocal()
        try:
            results = ingest_all_apcd_states(session)
            logger.info(
                f"APCD ingestion complete: {results['states_with_data']}/{results['states_attempted']} "
                f"states with data, {results['total_records_ingested']} records"
            )
        except Exception as e:
            logger.error(f"APCD ingestion failed: {e}")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {
        "status": "ingestion_started",
        "states": ["CO", "NH", "ME", "RI"],
        "message": "Discovering and ingesting state APCD data in background",
    }


@router.get("/apcd/status")
def apcd_status(db: Session = Depends(get_db)):
    """Get State APCD ingestion status and results."""
    from app.models.price_data import PriceData, PriceSource
    from app.models.data_pipeline_metric import DataPipelineMetric
    from sqlalchemy import func

    apcd_count = db.query(func.count(PriceData.price_id)).filter(
        PriceData.source == PriceSource.state_apcd
    ).scalar() or 0

    latest_metric = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "state_apcd_ingestion"
    ).order_by(DataPipelineMetric.measured_at.desc()).first()

    return {
        "apcd_records_stored": apcd_count,
        "states_configured": ["CO", "NH", "ME", "RI"],
        "latest_ingestion": latest_metric.details if latest_metric else None,
        "latest_ingestion_at": latest_metric.measured_at.isoformat() if latest_metric else None,
    }


@router.post("/ingest/mrf-index")
def trigger_mrf_index_ingestion(
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Ingest the CMS Insurer MRF (Machine-Readable File) index.

    Constitution F8: Full MRF files are 10-100GB each. This endpoint:
    1. Ingests the CMS MRF index (list of all insurer MRF file locations)
    2. Records the number of insurers with published MRFs
    3. For one small insurer, attempts to download and parse a sample
    4. Documents disk space barriers for large files
    """
    def _run():
        from app.database import SessionLocal
        from app.services.mrf_index_ingester import ingest_mrf_index
        session = SessionLocal()
        try:
            results = ingest_mrf_index(session)
            logger.info(
                f"MRF index ingestion complete: {results['insurers_accessible']}/{results['insurers_probed']} "
                f"accessible, {results['total_mrf_files_discovered']} MRF files discovered"
            )
        except Exception as e:
            logger.error(f"MRF index ingestion failed: {e}")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {
        "status": "ingestion_started",
        "message": "Probing insurer MRF indexes and sampling data in background",
        "insurers_to_probe": 15,  # Number of insurers in KNOWN_INSURER_MRF_INDEXES
    }


@router.get("/mrf-index/status")
def mrf_index_status(db: Session = Depends(get_db)):
    """Get CMS MRF index ingestion status and results."""
    from app.models.price_data import PriceData, PriceSource
    from app.models.data_pipeline_metric import DataPipelineMetric
    from sqlalchemy import func

    mrf_index_count = db.query(func.count(PriceData.price_id)).filter(
        PriceData.source == PriceSource.insurer_mrf_index
    ).scalar() or 0

    latest_metric = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "insurer_mrf_index_ingestion"
    ).order_by(DataPipelineMetric.measured_at.desc()).first()

    return {
        "mrf_index_entries_stored": mrf_index_count,
        "latest_ingestion": latest_metric.details if latest_metric else None,
        "latest_ingestion_at": latest_metric.measured_at.isoformat() if latest_metric else None,
    }


@router.get("/cross-type-signals")
def cross_type_signals(db: Session = Depends(get_db)):
    """Actionable cross-type intelligence signals for downstream functions.

    Constitution F8: "Is cross-type intelligence feeding Functions 1, 3, 4, and 9
    with actionable signals?"
    """
    from app.services.cross_type_analytics import generate_cross_type_signals
    signals = generate_cross_type_signals(db)
    by_function = {}
    for s in signals:
        f = s["target_function"]
        if f not in by_function:
            by_function[f] = []
        by_function[f].append(s)

    return {
        "total_signals": len(signals),
        "functions_receiving_signals": sorted(by_function.keys()),
        "signals_by_function": {k: len(v) for k, v in by_function.items()},
        "signals": signals,
    }


@router.get("/competition-data")
def competition_data(db: Session = Depends(get_db)):
    """Aggregate provider competition data across markets.

    Constitution F8: aggregate pricing and provider data to understand
    competitive dynamics across benefit types and geographies.
    """
    from app.models.price_data import PriceData
    from sqlalchemy import func

    # Aggregate price data by source and state
    rows = (
        db.query(
            PriceData.source,
            PriceData.state,
            func.count(PriceData.price_id).label("records"),
            func.avg(PriceData.price).label("avg_price"),
            func.min(PriceData.price).label("min_price"),
            func.max(PriceData.price).label("max_price"),
        )
        .group_by(PriceData.source, PriceData.state)
        .all()
    )

    by_source: dict = {}
    for row in rows:
        src = row.source.value if row.source else "unknown"
        if src not in by_source:
            by_source[src] = []
        by_source[src].append({
            "state": row.state,
            "records": row.records,
            "avg_price": round(float(row.avg_price or 0), 2),
            "min_price": round(float(row.min_price or 0), 2),
            "max_price": round(float(row.max_price or 0), 2),
        })

    return {
        "sources": by_source,
        "total_sources": len(by_source),
        "total_state_markets": len(rows),
    }


@router.get("/tipping-points")
def tipping_points():
    """Get service-level thresholds that trigger employer switching.

    Constitution F12/F8: understanding switching tipping points allows
    targeted outreach when employers are most likely to change providers.
    """
    from app.services.distribution_engine import get_service_level_thresholds

    return get_service_level_thresholds()


@router.get("/employer-export/{employer_id}")
def export_employer_data(employer_id: str, db: Session = Depends(get_db)):
    """Export all of an employer's raw data. Constitution F8 items 34-36.

    Employers own their raw data and may export it at any time.
    AI models and aggregated datasets are company assets and are NOT exported.
    """
    from app.models.claim import Claim
    from app.models.employee import Employee

    claims = db.query(Claim).filter(Claim.employer_id == employer_id).all()
    employees = db.query(Employee).filter(Employee.employer_id == employer_id).all()

    return {
        "employer_id": employer_id,
        "data_ownership": "Employer owns all raw data. May export at any time.",
        "company_assets_note": (
            "AI models, aggregated datasets, and derived insights are company "
            "assets and are not included in this export."
        ),
        "claims_count": len(claims),
        "employees_count": len(employees),
        "claims": [
            {
                "claim_id": str(c.claim_id),
                "benefit_type": c.benefit_type.value if c.benefit_type else None,
                "status": c.status.value if c.status else None,
                "amount_billed": float(c.amount_billed) if c.amount_billed else None,
                "amount_paid": float(c.amount_paid) if c.amount_paid else None,
                "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
            }
            for c in claims
        ],
        "employees": [
            {
                "employee_id": str(e.employee_id),
                "status": e.status.value if e.status else None,
                "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            }
            for e in employees
        ],
        "proprietary_data_never_exposed": True,
    }


@router.get("/scale-patterns")
def scale_dependent_patterns(db: Session = Depends(get_db)):
    """Track patterns only detectable at current data density.

    Constitution F8 item 40: measures compounding advantage.
    """
    from app.services.cross_type_analytics import detect_scale_dependent_patterns

    return detect_scale_dependent_patterns(db)


@router.get("/conventional-wisdom")
def conventional_wisdom_contradictions(db: Session = Depends(get_db)):
    """Find pricing anomalies that contradict industry assumptions.

    Constitution F8 item 41: identifies structural inefficiencies.
    """
    from app.services.cross_type_analytics import detect_conventional_wisdom_contradictions

    return detect_conventional_wisdom_contradictions(db)
