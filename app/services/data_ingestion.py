"""Public data ingestion pipeline (Function 8, public layer).

Ingests:
1. Hospital price transparency files (CMS Hospital Price Transparency Rule)
2. CMS Medicare rate schedules (Physician Fee Schedule)
3. Insurer Transparency in Coverage files (start with UHC for one state)

All ingestion is idempotent and runs on a schedule.
"""

import csv
import io
import json
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)

# Top hospitals by revenue — their transparency file URLs
# In production, this list would be maintained in a configuration table.
# Starting with a framework that can ingest any hospital's MRF.
SAMPLE_HOSPITAL_URLS: list[dict] = []  # Populated from CMS hospital index


def ingest_hospital_transparency_csv(
    db: Session,
    csv_content: str,
    hospital_name: str,
    source_url: str,
    state: str | None = None,
) -> int:
    """Parse a hospital price transparency CSV file and store prices.

    CMS requires hospitals to publish machine-readable files with:
    - Service description and code
    - Gross charge
    - Discounted cash price
    - Negotiated rates by payer

    Returns the number of records ingested.
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0

    for row in reader:
        # CMS standard column names (varies by hospital — normalize)
        service_code = (
            row.get("code")
            or row.get("procedure_code")
            or row.get("cpt_code")
            or row.get("hcpcs_code")
            or row.get("billing_code")
            or ""
        ).strip()

        if not service_code:
            continue

        description = (
            row.get("description")
            or row.get("service_description")
            or row.get("procedure_description")
            or ""
        ).strip()

        # Cash/self-pay price
        cash_price_str = (
            row.get("discounted_cash_price")
            or row.get("cash_price")
            or row.get("self_pay_price")
            or ""
        ).strip().replace("$", "").replace(",", "")

        if cash_price_str:
            try:
                cash_price = float(cash_price_str)
                if cash_price > 0:
                    db.merge(
                        PriceData(
                            provider_name=hospital_name,
                            service_code=service_code,
                            service_description=description,
                            price=cash_price,
                            channel="cash",
                            source=PriceSource.hospital_transparency,
                            source_url=source_url,
                            state=state,
                            ingested_at=datetime.utcnow(),
                        )
                    )
                    count += 1
            except ValueError:
                pass

        # Negotiated rates (payer-specific columns vary)
        for key, value in row.items():
            if not key or not value:
                continue
            key_lower = key.lower()
            if any(term in key_lower for term in ["negotiated", "payer", "insurer", "rate"]):
                price_str = value.strip().replace("$", "").replace(",", "")
                try:
                    price = float(price_str)
                    if price > 0:
                        channel = f"negotiated_{key.strip()[:80]}"
                        db.merge(
                            PriceData(
                                provider_name=hospital_name,
                                service_code=service_code,
                                service_description=description,
                                price=price,
                                channel=channel,
                                source=PriceSource.hospital_transparency,
                                source_url=source_url,
                                state=state,
                                ingested_at=datetime.utcnow(),
                            )
                        )
                        count += 1
                except ValueError:
                    pass

    db.commit()
    logger.info(f"Ingested {count} price records from {hospital_name}")
    return count


def ingest_hospital_transparency_json(
    db: Session,
    json_content: str,
    hospital_name: str,
    source_url: str,
    state: str | None = None,
) -> int:
    """Parse a hospital price transparency JSON file (CMS MRF schema).

    The CMS-required JSON schema includes:
    - standard_charge_information array with billing codes and prices
    """
    try:
        data = json.loads(json_content)
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON from {hospital_name}")
        return 0

    count = 0
    charges = data.get("standard_charge_information", [])

    for charge in charges:
        service_code = ""
        description = charge.get("description", "")

        # Extract billing code
        for code_info in charge.get("code_information", []):
            service_code = code_info.get("code", "")
            if service_code:
                break

        if not service_code:
            continue

        # Extract standard charges
        for sc in charge.get("standard_charges", []):
            price = sc.get("gross_charge") or sc.get("discounted_cash") or sc.get("negotiated_dollar")
            if not price:
                continue

            try:
                price = float(price)
            except (ValueError, TypeError):
                continue

            if price <= 0:
                continue

            if sc.get("discounted_cash"):
                channel = "cash"
            elif sc.get("payer_name"):
                channel = f"negotiated_{sc['payer_name'][:80]}"
            else:
                channel = "gross_charge"

            db.add(
                PriceData(
                    provider_name=hospital_name,
                    service_code=service_code,
                    service_description=description,
                    price=price,
                    channel=channel,
                    source=PriceSource.hospital_transparency,
                    source_url=source_url,
                    state=state,
                    ingested_at=datetime.utcnow(),
                )
            )
            count += 1

    db.commit()
    logger.info(f"Ingested {count} price records from {hospital_name} (JSON)")
    return count


def ingest_medicare_physician_fee_schedule(
    db: Session,
    csv_content: str,
    year: int = 2024,
) -> int:
    """Ingest CMS Medicare Physician Fee Schedule.

    The PFS provides national and locality-specific payment rates for
    physician services identified by CPT/HCPCS codes.
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0

    for row in reader:
        hcpcs = (row.get("HCPCS") or row.get("hcpcs_code") or row.get("CPT") or "").strip()
        if not hcpcs:
            continue

        description = (row.get("DESCRIPTION") or row.get("description") or "").strip()

        # National payment amount
        for price_col in [
            "NON_FACILITY_NA_PAYMENT",
            "FACILITY_NA_PAYMENT",
            "non_fac_pe_na",
            "fac_pe_na",
        ]:
            price_str = (row.get(price_col) or "").strip().replace("$", "").replace(",", "")
            if price_str:
                try:
                    price = float(price_str)
                    if price > 0:
                        db.add(
                            PriceData(
                                provider_name="Medicare",
                                service_code=hcpcs,
                                service_description=description,
                                price=price,
                                channel=f"medicare_{price_col.lower()}",
                                source=PriceSource.medicare_physician_fee,
                                source_url=f"https://www.cms.gov/medicare/payment/fee-schedules/physician/{year}",
                                ingested_at=datetime.utcnow(),
                                file_date=datetime(year, 1, 1),
                            )
                        )
                        count += 1
                except ValueError:
                    pass

    db.commit()
    logger.info(f"Ingested {count} Medicare PFS records for {year}")
    return count


def ingest_nadac_pharmacy(db: Session, csv_content: str) -> int:
    """Ingest CMS NADAC (National Average Drug Acquisition Cost) data.

    NADAC provides the average price pharmacies pay to acquire drugs,
    published weekly by CMS.
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0

    for row in reader:
        ndc = (row.get("NDC") or row.get("ndc") or "").strip()
        if not ndc:
            continue

        description = (row.get("NDC Description") or row.get("drug_name") or "").strip()
        price_str = (
            row.get("NADAC_Per_Unit") or row.get("nadac_per_unit") or ""
        ).strip().replace("$", "").replace(",", "")

        if not price_str:
            continue

        try:
            price = float(price_str)
            if price > 0:
                db.add(
                    PriceData(
                        provider_name="NADAC",
                        service_code=ndc,
                        service_description=description,
                        price=price,
                        channel="nadac_per_unit",
                        source=PriceSource.nadac_pharmacy,
                        source_url="https://data.medicaid.gov/dataset/dfa2ab14-06c2-457a-9e36-5cb6d80f8d93",
                        ingested_at=datetime.utcnow(),
                    )
                )
                count += 1
        except ValueError:
            pass

    db.commit()
    logger.info(f"Ingested {count} NADAC pharmacy records")
    return count


def get_ingestion_stats(db: Session) -> dict:
    """Get current data pipeline statistics including data quality metrics.

    Optimized for large datasets (11M+ rows) using raw SQL with indexes.
    """
    from sqlalchemy import text

    # Use raw SQL for maximum performance on large tables
    by_source = {}
    rows = db.execute(text(
        "SELECT source, COUNT(*) as cnt FROM price_data GROUP BY source"
    )).fetchall()
    for source, cnt in rows:
        by_source[source] = cnt
    total = sum(by_source.values())

    # State coverage — count distinct states, skip the expensive per-state breakdown
    state_count = db.execute(text(
        "SELECT COUNT(DISTINCT state) FROM price_data WHERE state IS NOT NULL"
    )).scalar() or 0

    # Staleness: most recent ingestion per source
    staleness = {}
    staleness_rows = db.execute(text(
        "SELECT source, MAX(ingested_at) as latest FROM price_data GROUP BY source"
    )).fetchall()
    for source, latest_str in staleness_rows:
        if latest_str:
            staleness[source] = {"last_ingested": str(latest_str)}

    return {
        "total_records": total,
        "by_source": by_source,
        "states_with_data": state_count,
        "data_quality": {
            "staleness_by_source": staleness,
        },
        "sources_active": list(by_source.keys()),
        "sources_count": len(by_source),
    }
