"""Data pipeline API endpoints for managing public data ingestion."""

import logging
from datetime import datetime, UTC

from fastapi import APIRouter, BackgroundTasks, Depends, UploadFile, File, HTTPException
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
    download_nadac,
    download_medicare_pfs,
    download_hospital_transparency,
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
    from app.services.goodrx_scraper import scrape_goodrx_batch, TOP_PRESCRIBED_DRUGS
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
