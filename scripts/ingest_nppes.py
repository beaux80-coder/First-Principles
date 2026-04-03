"""Download and ingest the full NPPES provider database.

Constitution mandate: "Is there any reachable, legal, quality-meeting provider
the engine does not evaluate?" — the answer must be YES for ALL.

The NPPES (National Plan and Provider Enumeration System) contains ~7.4 million
active NPI records covering every licensed healthcare provider in the US.

Strategy (in order of preference):
1. Bulk CSV download from CMS (~1GB zip) — gets ALL providers
2. Aggressive API pagination: all 56 US states/territories × both enumeration types
3. Supplemental taxonomy-based API queries for any missed providers

Usage:
    cd beneflex
    source .venv/bin/activate
    python -m scripts.ingest_nppes            # Full ingestion (bulk + API)
    python -m scripts.ingest_nppes --api-only  # Skip bulk, use API only
    python -m scripts.ingest_nppes --bulk-only # Skip API, use bulk CSV only
    python -m scripts.ingest_nppes --stats     # Just print current provider stats
"""

import sys
import os
import argparse
import logging
import time

# Add parent dir to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, engine, SessionLocal
from app.models import *  # noqa: F401,F403
from app.models.provider import Provider
from sqlalchemy import func

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nppes_ingest")


def print_provider_stats(db):
    """Print current provider database statistics."""
    total = db.query(Provider).count()
    print(f"\n{'=' * 60}")
    print("PROVIDER DATABASE STATISTICS")
    print(f"{'=' * 60}")
    print(f"Total providers: {total:,}")
    print()

    print("By type:")
    for ptype, count in (
        db.query(Provider.provider_type, func.count())
        .group_by(Provider.provider_type)
        .order_by(func.count().desc())
        .all()
    ):
        print(f"  {ptype.value:15s}: {count:>10,}")

    print()
    print("By state (top 20):")
    for state, count in (
        db.query(Provider.state, func.count())
        .group_by(Provider.state)
        .order_by(func.count().desc())
        .limit(20)
        .all()
    ):
        print(f"  {state or 'N/A':2s}: {count:>10,}")

    print()
    with_specialties = db.query(Provider).filter(Provider.specialties.isnot(None)).count()
    with_state = db.query(Provider).filter(Provider.state.isnot(None)).count()
    print(f"With specialties: {with_specialties:,} ({with_specialties/max(total,1)*100:.1f}%)")
    print(f"With state:       {with_state:,} ({with_state/max(total,1)*100:.1f}%)")
    print(f"{'=' * 60}\n")


def run_ingestion(bulk_only=False, api_only=False):
    """Run the NPPES provider ingestion."""
    # Create tables if needed
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Show initial stats
        initial_count = db.query(Provider).count()
        logger.info(f"Starting NPPES ingestion. Current providers: {initial_count:,}")

        start_time = time.time()

        if not api_only:
            # Strategy 1: Bulk CSV download
            from app.services.data_downloaders import _download_nppes_bulk_csv
            try:
                bulk_count = _download_nppes_bulk_csv(db)
                logger.info(f"Bulk CSV: {bulk_count:,} new providers")
                if bulk_count > 100_000 and bulk_only:
                    logger.info("Bulk download successful, skipping API")
                elif bulk_count > 100_000:
                    logger.info("Bulk download successful with good coverage")
            except Exception as e:
                logger.error(f"Bulk CSV download failed: {e}")
                if bulk_only:
                    return

        if not bulk_only:
            # Strategy 2: Aggressive API pagination
            from app.services.data_downloaders import _download_nppes_api_aggressive
            try:
                api_count = _download_nppes_api_aggressive(db)
                logger.info(f"API state pagination: {api_count:,} new providers")
            except Exception as e:
                logger.error(f"API pagination failed: {e}")

            # Strategy 3: Taxonomy-based supplemental
            from app.services.data_downloaders import _download_nppes_api_by_taxonomy
            try:
                tax_count = _download_nppes_api_by_taxonomy(db)
                logger.info(f"API taxonomy pass: {tax_count:,} new providers")
            except Exception as e:
                logger.error(f"Taxonomy pass failed: {e}")

        elapsed = time.time() - start_time
        final_count = db.query(Provider).count()
        new_providers = final_count - initial_count

        logger.info(f"{'=' * 60}")
        logger.info("NPPES INGESTION COMPLETE")
        logger.info(f"  Time elapsed: {elapsed/60:.1f} minutes")
        logger.info(f"  Providers before: {initial_count:,}")
        logger.info(f"  Providers after:  {final_count:,}")
        logger.info(f"  New providers:    {new_providers:,}")
        logger.info(f"{'=' * 60}")

        print_provider_stats(db)

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Download and ingest the full NPPES provider database"
    )
    parser.add_argument(
        "--api-only", action="store_true",
        help="Skip bulk CSV download, use API pagination only"
    )
    parser.add_argument(
        "--bulk-only", action="store_true",
        help="Skip API pagination, use bulk CSV download only"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print current provider database statistics and exit"
    )
    args = parser.parse_args()

    if args.stats:
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            print_provider_stats(db)
        finally:
            db.close()
        return

    if args.api_only and args.bulk_only:
        print("Error: --api-only and --bulk-only are mutually exclusive")
        sys.exit(1)

    run_ingestion(bulk_only=args.bulk_only, api_only=args.api_only)


if __name__ == "__main__":
    main()
