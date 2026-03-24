"""State All-Payer Claims Database (APCD) ingestion (Function 8).

Constitution F8: "Is there any publicly available data source the system
does not ingest? ... every legally accessible data source."

Several states publish APCD data freely:
- Colorado (CIVHC): https://civhc.org/get-data/public-data/
- New Hampshire (NHCHIS): https://nhchis.com/
- Maine (MHDO): https://mhdo.maine.gov/
- Rhode Island: https://health.ri.gov/data/

This module attempts to download whatever is freely available from each
state's APCD portal without requiring registration.
"""

import csv
import io
import json
import logging
import re
from datetime import datetime, UTC
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# State APCD portals with their data URLs
STATE_APCD_SOURCES = {
    "CO": {
        "name": "Colorado CIVHC",
        "portal_url": "https://civhc.org/get-data/public-data/",
        "description": "Colorado All-Payer Claims Database via CIVHC",
        "data_urls": [
            # CIVHC publishes aggregate reports and some downloadable data
            "https://civhc.org/get-data/public-data/focus-areas/cost-of-care/",
            "https://civhc.org/get-data/public-data/focus-areas/",
        ],
    },
    "NH": {
        "name": "New Hampshire CHIS",
        "portal_url": "https://nhchis.com/",
        "description": "New Hampshire Comprehensive Health Information System",
        "data_urls": [
            "https://nhchis.com/",
        ],
    },
    "ME": {
        "name": "Maine MHDO",
        "portal_url": "https://mhdo.maine.gov/",
        "description": "Maine Health Data Organization",
        "data_urls": [
            "https://mhdo.maine.gov/",
            "https://mhdo.maine.gov/claims-data/",
        ],
    },
    "RI": {
        "name": "Rhode Island DOH",
        "portal_url": "https://health.ri.gov/data/",
        "description": "Rhode Island Department of Health data portal",
        "data_urls": [
            "https://health.ri.gov/data/",
        ],
    },
}


def discover_apcd_downloads(state: str) -> dict:
    """Discover downloadable APCD data files from a state portal.

    Crawls the state's APCD portal page to find:
    - Direct CSV/Excel/JSON download links
    - Links to data portals (Socrata, CKAN, etc.)
    - Registration requirements
    - Available report categories

    Returns a dict with discovered resources and any barriers.
    """
    source = STATE_APCD_SOURCES.get(state)
    if not source:
        return {"state": state, "error": f"No APCD source configured for {state}"}

    result = {
        "state": state,
        "source_name": source["name"],
        "portal_url": source["portal_url"],
        "downloadable_files": [],
        "data_portal_links": [],
        "registration_required": False,
        "barriers": [],
        "pages_checked": [],
    }

    for url in source["data_urls"]:
        try:
            resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
            result["pages_checked"].append({"url": url, "status": resp.status_code})

            if resp.status_code != 200:
                result["barriers"].append(f"HTTP {resp.status_code} at {url}")
                continue

            html = resp.text
            soup = BeautifulSoup(html, "lxml")

            # Find all links
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True).lower()

                # Resolve relative URLs
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(url, href)

                # Check for direct download links
                if any(ext in href.lower() for ext in [".csv", ".xlsx", ".xls", ".json", ".zip", ".txt"]):
                    result["downloadable_files"].append({
                        "url": href,
                        "text": link.get_text(strip=True)[:200],
                        "file_type": _detect_file_type(href),
                    })

                # Check for data portal links
                if any(portal in href.lower() for portal in [
                    "data.gov", "socrata", "ckan", "opendata", "tableau",
                    "public-data", "download", "dataset",
                ]):
                    result["data_portal_links"].append({
                        "url": href,
                        "text": link.get_text(strip=True)[:200],
                    })

                # Check for registration requirements
                if any(term in text for term in [
                    "register", "sign up", "create account", "request access",
                    "data use agreement", "dua", "application",
                ]):
                    result["registration_required"] = True
                    result["barriers"].append(
                        f"Registration/DUA likely required: found '{link.get_text(strip=True)[:100]}' at {url}"
                    )

        except httpx.TimeoutException:
            result["barriers"].append(f"Timeout accessing {url}")
        except httpx.ConnectError as e:
            result["barriers"].append(f"Connection error to {url}: {e}")
        except Exception as e:
            result["barriers"].append(f"Error crawling {url}: {type(e).__name__}: {e}")

    return result


def _detect_file_type(url: str) -> str:
    """Detect file type from URL extension."""
    url_lower = url.lower()
    for ext in [".csv", ".xlsx", ".xls", ".json", ".zip", ".txt", ".pdf"]:
        if ext in url_lower:
            return ext.lstrip(".")
    return "unknown"


def download_apcd_file(url: str, state: str) -> Optional[str]:
    """Attempt to download a single APCD data file.

    Returns the file content as a string, or None if download fails.
    Only downloads CSV/text files (not binary formats like xlsx).
    """
    try:
        resp = httpx.get(url, headers=BROWSER_HEADERS, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")

        # Only process text-based files
        if "text" in content_type or "csv" in content_type or "json" in content_type:
            return resp.text
        elif url.lower().endswith(".csv") or url.lower().endswith(".json") or url.lower().endswith(".txt"):
            return resp.text

        logger.info(f"APCD file at {url} has content-type {content_type} — skipping binary")
        return None

    except Exception as e:
        logger.warning(f"Failed to download APCD file {url}: {e}")
        return None


def ingest_apcd_csv(db: Session, csv_content: str, state: str, source_url: str) -> int:
    """Ingest an APCD CSV file into price_data.

    APCD data typically contains:
    - Procedure/service codes (CPT/HCPCS)
    - Allowed amounts / paid amounts
    - Provider information
    - Payer information

    Column names vary by state. We normalize to our schema.
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0
    batch = []

    for row in reader:
        # Try to find service code
        service_code = _extract_field(row, [
            "procedure_code", "cpt_code", "hcpcs_code", "service_code",
            "code", "billing_code", "proc_cd", "cpt", "hcpcs",
            "CPT Code", "Procedure Code", "HCPCS Code", "Service Code",
        ])
        if not service_code:
            continue

        # Try to find price/amount
        price_str = _extract_field(row, [
            "allowed_amount", "paid_amount", "average_allowed",
            "avg_allowed", "mean_allowed", "median_allowed",
            "average_payment", "avg_payment", "charge_amount",
            "average_charge", "total_payment", "allowed_amt",
            "Allowed Amount", "Paid Amount", "Average Allowed",
            "Average Payment", "Charge Amount",
        ])
        if not price_str:
            continue

        try:
            price = float(str(price_str).replace("$", "").replace(",", "").strip())
            if price <= 0 or price > 1000000:
                continue
        except (ValueError, TypeError):
            continue

        description = _extract_field(row, [
            "description", "service_description", "procedure_description",
            "proc_desc", "service_desc", "Description",
        ]) or ""

        provider_name = _extract_field(row, [
            "provider_name", "facility_name", "hospital_name",
            "rendering_provider", "Provider Name", "Facility Name",
        ]) or f"APCD_{state}"

        payer = _extract_field(row, [
            "payer_name", "insurance_company", "carrier_name",
            "plan_name", "Payer Name", "Insurance Company",
        ]) or "all_payer"

        batch.append(PriceData(
            provider_name=provider_name[:500],
            service_code=service_code.strip()[:20],
            service_description=description[:500] if description else f"APCD {state} claim",
            price=price,
            channel=f"apcd_{state.lower()}_{payer[:50].lower().replace(' ', '_')}",
            source=PriceSource.state_apcd,
            source_url=source_url,
            state=state,
            ingested_at=datetime.now(UTC),
        ))
        count += 1

        if len(batch) >= 5000:
            db.bulk_save_objects(batch)
            db.commit()
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    logger.info(f"Ingested {count} APCD records from {state}")
    return count


def _extract_field(row: dict, candidate_keys: list[str]) -> Optional[str]:
    """Extract a field value trying multiple possible column names."""
    for key in candidate_keys:
        val = row.get(key)
        if val and str(val).strip():
            return str(val).strip()
    # Also try case-insensitive match
    row_lower = {k.lower(): v for k, v in row.items()}
    for key in candidate_keys:
        val = row_lower.get(key.lower())
        if val and str(val).strip():
            return str(val).strip()
    return None


def ingest_all_apcd_states(db: Session) -> dict:
    """Attempt to discover and ingest APCD data from all configured states.

    For each state:
    1. Discover available downloads from the portal
    2. Attempt to download any freely available CSV files
    3. Ingest what we can
    4. Document barriers for what we can't

    Returns a comprehensive report.
    """
    logger.info("Starting APCD ingestion for all configured states...")

    report = {
        "states_attempted": 0,
        "states_with_data": 0,
        "total_records_ingested": 0,
        "total_files_downloaded": 0,
        "state_reports": {},
        "all_barriers": [],
        "ingested_at": datetime.now(UTC).isoformat(),
    }

    for state, source in STATE_APCD_SOURCES.items():
        report["states_attempted"] += 1
        state_report = {
            "name": source["name"],
            "portal_url": source["portal_url"],
            "records_ingested": 0,
            "files_downloaded": 0,
            "barriers": [],
            "downloadable_files_found": 0,
            "registration_required": False,
        }

        # Discover available downloads
        discovery = discover_apcd_downloads(state)
        state_report["downloadable_files_found"] = len(discovery["downloadable_files"])
        state_report["registration_required"] = discovery["registration_required"]
        state_report["barriers"].extend(discovery["barriers"])

        # Attempt to download and ingest any CSV files
        for file_info in discovery["downloadable_files"]:
            if file_info["file_type"] not in ("csv", "json", "txt"):
                continue

            content = download_apcd_file(file_info["url"], state)
            if content:
                state_report["files_downloaded"] += 1
                report["total_files_downloaded"] += 1

                try:
                    if file_info["file_type"] == "json":
                        # Try to parse JSON APCD data
                        data = json.loads(content)
                        if isinstance(data, list) and data:
                            # Convert JSON array to CSV-like processing
                            csv_content = _json_to_csv(data)
                            if csv_content:
                                count = ingest_apcd_csv(db, csv_content, state, file_info["url"])
                                state_report["records_ingested"] += count
                                report["total_records_ingested"] += count
                    else:
                        count = ingest_apcd_csv(db, content, state, file_info["url"])
                        state_report["records_ingested"] += count
                        report["total_records_ingested"] += count
                except Exception as e:
                    state_report["barriers"].append(
                        f"Failed to parse {file_info['url']}: {type(e).__name__}: {e}"
                    )

        if state_report["records_ingested"] > 0:
            report["states_with_data"] += 1

        if not discovery["downloadable_files"]:
            state_report["barriers"].append(
                f"No direct download links found at {source['portal_url']}. "
                f"Data may require registration, a data use agreement (DUA), "
                f"or may only be available through an interactive portal/dashboard."
            )

        report["state_reports"][state] = state_report
        report["all_barriers"].extend(
            [{"state": state, "barrier": b} for b in state_report["barriers"]]
        )

    # Record pipeline metric
    _record_apcd_pipeline_metric(db, report)

    logger.info(
        f"APCD ingestion complete: {report['states_with_data']}/{report['states_attempted']} "
        f"states with data, {report['total_records_ingested']} total records"
    )

    return report


def _json_to_csv(data: list[dict]) -> Optional[str]:
    """Convert a list of dicts to CSV string for unified processing."""
    if not data:
        return None
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=data[0].keys())
    writer.writeheader()
    for row in data:
        writer.writerow(row)
    return output.getvalue()


def _record_apcd_pipeline_metric(db: Session, report: dict):
    """Record APCD ingestion results as F8 pipeline metric."""
    from app.models.data_pipeline_metric import DataPipelineMetric

    metric = DataPipelineMetric(
        metric_type="state_apcd_ingestion",
        benefit_type="health",
        value=float(report["total_records_ingested"]),
        details={
            "states_attempted": report["states_attempted"],
            "states_with_data": report["states_with_data"],
            "total_files_downloaded": report["total_files_downloaded"],
            "barriers_count": len(report["all_barriers"]),
            "state_summaries": {
                state: {
                    "records": sr["records_ingested"],
                    "files_downloaded": sr["files_downloaded"],
                    "registration_required": sr["registration_required"],
                    "barriers_count": len(sr["barriers"]),
                }
                for state, sr in report["state_reports"].items()
            },
        },
        measured_at=datetime.now(UTC),
    )
    db.add(metric)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record APCD metric: {e}")
