"""Background task scheduler for automated data ingestion (Function 8).

Runs data pipeline tasks on a schedule:
- NADAC pharmacy: weekly (published weekly by CMS)
- Medicare PFS: monthly (updated quarterly, check monthly)
- Hospital transparency: weekly (hospitals update at varying frequencies)

Uses APScheduler for scheduling. In production, this would run as a separate
process alongside the main FastAPI app.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal
from app.services.data_downloaders import (
    download_nadac,
    download_medicare_pfs,
    download_hospital_transparency,
    download_hospital_compare,
    download_opps_rates,
    download_quality_benchmarks,
    download_medicaid_dental,
    download_samhsa_data,
    download_state_medicaid,
    download_all_payer_claims,
    download_dmepos_fee_schedule,
    download_asp_drug_pricing,
    download_va_fee_schedule,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _run_nadac_ingestion():
    """Scheduled task: download and ingest NADAC pharmacy data."""
    logger.info("Scheduled task: NADAC ingestion starting...")
    db = SessionLocal()
    try:
        count = download_nadac(db)
        logger.info(f"Scheduled NADAC ingestion complete: {count} records")
    except Exception as e:
        logger.error(f"Scheduled NADAC ingestion failed: {e}")
    finally:
        db.close()


def _run_medicare_pfs_ingestion():
    """Scheduled task: download and ingest Medicare PFS data."""
    logger.info("Scheduled task: Medicare PFS ingestion starting...")
    db = SessionLocal()
    try:
        count = download_medicare_pfs(db)
        logger.info(f"Scheduled Medicare PFS ingestion complete: {count} records")
    except Exception as e:
        logger.error(f"Scheduled Medicare PFS ingestion failed: {e}")
    finally:
        db.close()


def _run_hospital_transparency_ingestion():
    """Scheduled task: download and ingest hospital transparency files."""
    logger.info("Scheduled task: Hospital transparency ingestion starting...")
    db = SessionLocal()
    try:
        count = download_hospital_transparency(db)
        logger.info(f"Scheduled hospital transparency ingestion complete: {count} records")
    except Exception as e:
        logger.error(f"Scheduled hospital transparency ingestion failed: {e}")
    finally:
        db.close()


def _run_additional_sources_ingestion():
    """Scheduled task: download dental, SAMHSA, state Medicaid, APCD data."""
    logger.info("Scheduled task: Additional sources ingestion starting...")
    db = SessionLocal()
    try:
        results = {}
        for name, func in [
            ("hospital_compare", download_hospital_compare),
            ("opps_rates", download_opps_rates),
            ("quality_benchmarks", download_quality_benchmarks),
            ("medicaid_dental", download_medicaid_dental),
            ("samhsa", download_samhsa_data),
            ("state_medicaid", download_state_medicaid),
            ("all_payer_claims", download_all_payer_claims),
            ("dmepos", download_dmepos_fee_schedule),
            ("asp_drug", download_asp_drug_pricing),
            ("va_fee", download_va_fee_schedule),
        ]:
            try:
                results[name] = func(db)
            except Exception as e:
                logger.error(f"  {name} failed: {e}")
                results[name] = 0
        logger.info(f"Additional sources ingestion complete: {results}")
    except Exception as e:
        logger.error(f"Additional sources ingestion failed: {e}")
    finally:
        db.close()


def _run_cross_type_analytics():
    """Scheduled task: run cross-type pattern detection and generate signals.

    Constitution F8: "Is the data pipeline actively detecting cross-benefit-type
    patterns (not just collecting data by type)?"
    """
    logger.info("Scheduled task: Cross-type analytics starting...")
    db = SessionLocal()
    try:
        from app.services.cross_type_analytics import generate_cross_type_signals
        signals = generate_cross_type_signals(db)
        logger.info(f"Cross-type analytics complete: {len(signals)} signals generated")
    except Exception as e:
        logger.error(f"Cross-type analytics failed: {e}")
    finally:
        db.close()


def _run_clinical_guideline_update():
    """Scheduled task: update clinical guidelines from recognized authorities.

    Constitution F1: "Updates determination models as new peer-reviewed evidence
    is published. Latency between guideline publication and engine incorporation
    must be measured and minimized."
    """
    logger.info("Scheduled task: Clinical guideline update starting...")
    db = SessionLocal()
    try:
        from app.services.clinical_guidelines_ingester import ingest_cms_ncd_guidelines
        count = ingest_cms_ncd_guidelines(db)
        logger.info(f"Scheduled clinical guideline update complete: {count} guidelines")
    except Exception as e:
        logger.error(f"Scheduled clinical guideline update failed: {e}")
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler with all data pipeline jobs."""
    # NADAC: every Sunday at 3:00 AM
    scheduler.add_job(
        _run_nadac_ingestion,
        CronTrigger(day_of_week="sun", hour=3, minute=0),
        id="nadac_weekly",
        replace_existing=True,
    )

    # Medicare PFS: 1st of every month at 4:00 AM
    scheduler.add_job(
        _run_medicare_pfs_ingestion,
        CronTrigger(day=1, hour=4, minute=0),
        id="medicare_pfs_monthly",
        replace_existing=True,
    )

    # Hospital transparency: every Saturday at 2:00 AM
    scheduler.add_job(
        _run_hospital_transparency_ingestion,
        CronTrigger(day_of_week="sat", hour=2, minute=0),
        id="hospital_transparency_weekly",
        replace_existing=True,
    )

    # Clinical guidelines: daily at 5:00 AM (minimize incorporation latency)
    scheduler.add_job(
        _run_clinical_guideline_update,
        CronTrigger(hour=5, minute=0),
        id="clinical_guidelines_daily",
        replace_existing=True,
    )

    # Additional sources (dental, SAMHSA, state Medicaid, APCD): weekly Wednesday 3:00 AM
    scheduler.add_job(
        _run_additional_sources_ingestion,
        CronTrigger(day_of_week="wed", hour=3, minute=0),
        id="additional_sources_weekly",
        replace_existing=True,
    )

    # Cross-type analytics: daily at 6:00 AM (after guideline update, before business hours)
    scheduler.add_job(
        _run_cross_type_analytics,
        CronTrigger(hour=6, minute=0),
        id="cross_type_analytics_daily",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Data pipeline scheduler started with 6 jobs")


def stop_scheduler():
    """Stop the background scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Data pipeline scheduler stopped")
