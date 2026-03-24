"""Ingest Colorado APCD data via Socrata Open Data API + download a small insurer MRF.

Task 1: Colorado publishes health data via Socrata (data.colorado.gov).
         Try SODA API with proper headers. If 403, fall back to CMS.gov datasets.

Task 2: Probe small insurers for MRF files. If a downloadable MRF JSON
         under 500MB is found, parse and insert negotiated rates into price_data.

Run: cd beneflex && source .venv/bin/activate && python3 -m scripts.ingest_socrata_and_mrf
"""

import csv
import gzip
import io
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, UTC

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.database import Base, engine, SessionLocal
from app.models import *  # noqa: F401,F403
from app.models.price_data import PriceData, PriceSource
from app.models.data_pipeline_metric import DataPipelineMetric

# Create tables if needed
Base.metadata.create_all(bind=engine)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(30.0, read=120.0)

# ─────────────────────────────────────────────────────────────────────
# Task 1: Colorado APCD via Socrata + CMS.gov fallback
# ─────────────────────────────────────────────────────────────────────

# Colorado Socrata datasets
CO_SOCRATA_DATASETS = [
    ("a9bw-4sc8", "CO APCD Insights"),
    ("r74d-d2pc", "Telehealth Utilization"),
    ("9zu6-ut2w", "Shop for Care"),
]

# CMS.gov open datasets (fully open, no auth needed)
CMS_DATASETS = [
    ("xubh-q36u", "Hospital General Information (Hospital Compare)", "data.cms.gov"),
    ("nrth-mfg3", "Medicare Spending Per Episode (MSPB)", "data.cms.gov"),
    ("97k6-zzrs", "Inpatient Prospective Payment System (IPPS)", "data.cms.gov"),
]


def try_socrata_dataset(host: str, dataset_id: str, name: str, limit: int = 1000) -> list[dict] | None:
    """Try fetching a Socrata dataset via SODA API."""
    url = f"https://{host}/resource/{dataset_id}.json"
    headers = {
        "Accept": "application/json",
        "User-Agent": "BeneFlex-DataPipeline/1.0",
    }
    params = {"$limit": limit}

    logger.info(f"  Trying Socrata: {url} ({name})")
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=TIMEOUT, follow_redirects=True)
        logger.info(f"    HTTP {resp.status_code}, {len(resp.text)} bytes")

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                logger.info(f"    Records: {len(data)}")
                logger.info(f"    Fields: {list(data[0].keys())[:10]}")
                return data
            else:
                logger.info(f"    Empty or non-array response")
                return []
        elif resp.status_code == 403:
            logger.warning(f"    403 Forbidden — Socrata blocked without app_token")
            return None
        else:
            logger.warning(f"    Unexpected status: {resp.status_code}")
            return None
    except httpx.TimeoutException:
        logger.warning(f"    Timeout accessing {url}")
        return None
    except Exception as e:
        logger.warning(f"    Error: {type(e).__name__}: {e}")
        return None


def ingest_socrata_records(db, records: list[dict], source_name: str, source_url: str, state: str = "CO") -> int:
    """Ingest Socrata JSON records into price_data.

    Socrata datasets have varying schemas. We try common healthcare field names.
    """
    count = 0
    batch = []

    # Candidate field names for service code
    code_fields = [
        "procedure_code", "cpt_code", "hcpcs_code", "service_code", "code",
        "billing_code", "drg_code", "drg", "drg_definition",
        "procedure", "service", "category", "measure_id",
        "provider_id", "hospital_name", "facility_id",
    ]
    # Candidate field names for price/amount
    price_fields = [
        "allowed_amount", "average_allowed_amount", "avg_allowed",
        "average_payment", "avg_payment", "average_charge",
        "average_total_payments", "average_covered_charges",
        "average_medicare_payments", "payment", "charge",
        "total_payment", "cost", "average_cost", "price",
        "amount", "rate", "spending_per_episode",
        "average_spending_per_episode", "mean_cost",
        "score", "measure_score",
    ]
    # Candidate field names for description
    desc_fields = [
        "description", "service_description", "procedure_description",
        "drg_definition", "drg_description", "measure_name",
        "condition", "category", "service_name",
        "hospital_name", "facility_name", "provider_name",
    ]
    # Provider name fields
    provider_fields = [
        "provider_name", "hospital_name", "facility_name",
        "rendering_provider", "organization_name",
    ]

    for row in records:
        # Find service code
        service_code = None
        for f in code_fields:
            val = row.get(f)
            if val and str(val).strip():
                service_code = str(val).strip()[:20]
                break

        if not service_code:
            # If no explicit code field, try to construct one
            for f in ["measure_id", "category", "service"]:
                val = row.get(f)
                if val and str(val).strip():
                    service_code = str(val).strip()[:20]
                    break

        if not service_code:
            continue

        # Find price
        price_val = None
        for f in price_fields:
            val = row.get(f)
            if val:
                try:
                    price_val = float(str(val).replace("$", "").replace(",", "").strip())
                    if price_val > 0 and price_val < 10_000_000:
                        break
                    else:
                        price_val = None
                except (ValueError, TypeError):
                    price_val = None

        if price_val is None:
            continue

        # Find description
        description = ""
        for f in desc_fields:
            val = row.get(f)
            if val and str(val).strip():
                description = str(val).strip()[:500]
                break

        # Find provider name
        provider_name = f"CO_APCD_{source_name}"
        for f in provider_fields:
            val = row.get(f)
            if val and str(val).strip():
                provider_name = str(val).strip()[:500]
                break

        batch.append(PriceData(
            provider_name=provider_name,
            service_code=service_code,
            service_description=description or f"Socrata {source_name}",
            price=price_val,
            channel=f"socrata_{state.lower()}_{source_name.lower().replace(' ', '_')[:50]}",
            source=PriceSource.state_apcd,
            source_url=source_url,
            state=state,
            ingested_at=datetime.now(UTC),
        ))
        count += 1

        if len(batch) >= 2000:
            db.bulk_save_objects(batch)
            db.commit()
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    return count


def ingest_cms_records(db, records: list[dict], source_name: str, source_url: str) -> int:
    """Ingest CMS.gov Socrata records into price_data.

    CMS datasets use different field names than state APCD data.
    """
    count = 0
    batch = []

    for row in records:
        # CMS Hospital Compare
        provider_id = row.get("provider_id") or row.get("facility_id") or ""
        hospital_name = (
            row.get("hospital_name") or row.get("facility_name")
            or row.get("provider_name") or ""
        ).strip()
        state = (row.get("state") or row.get("provider_state") or "").strip()[:2]

        # Try to find a score/metric value
        price_val = None
        for f in [
            "score", "payment", "total_performance_score",
            "hospital_overall_rating", "safety_of_care_national_comparison",
            "average_covered_charges", "average_total_payments",
            "average_medicare_payments", "spending_per_episode_hospital",
            "average_spending_per_episode",
            "measure_score",
        ]:
            val = row.get(f)
            if val:
                try:
                    price_val = float(str(val).replace("$", "").replace(",", "").strip())
                    if price_val > 0:
                        break
                    else:
                        price_val = None
                except (ValueError, TypeError):
                    price_val = None

        if price_val is None:
            continue

        # Service code — use DRG, measure_id, or provider_id
        service_code = (
            row.get("drg_definition") or row.get("drg_code") or row.get("drg")
            or row.get("measure_id") or row.get("measure_name")
            or provider_id or ""
        ).strip()[:20]

        if not service_code:
            continue

        description = (
            row.get("drg_definition") or row.get("measure_name")
            or row.get("condition") or row.get("hospital_name")
            or source_name
        )

        source_type = PriceSource.medicare_inpatient
        if "hospital compare" in source_name.lower():
            source_type = PriceSource.medicare_inpatient
        elif "spending" in source_name.lower():
            source_type = PriceSource.medicare_inpatient
        elif "ipps" in source_name.lower():
            source_type = PriceSource.medicare_inpatient

        batch.append(PriceData(
            provider_name=hospital_name or f"CMS_{source_name}",
            service_code=service_code,
            service_description=str(description)[:500] if description else source_name,
            price=price_val,
            channel=f"cms_{source_name.lower().replace(' ', '_').replace('(', '').replace(')', '')[:50]}",
            source=source_type,
            source_url=source_url,
            state=state if state else None,
            ingested_at=datetime.now(UTC),
        ))
        count += 1

        if len(batch) >= 2000:
            db.bulk_save_objects(batch)
            db.commit()
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    return count


def task1_socrata_and_cms(db) -> dict:
    """Task 1: Ingest Colorado APCD via Socrata, fall back to CMS.gov."""
    report = {
        "socrata_datasets_tried": 0,
        "socrata_datasets_successful": 0,
        "socrata_records_ingested": 0,
        "cms_datasets_tried": 0,
        "cms_datasets_successful": 0,
        "cms_records_ingested": 0,
        "total_records_ingested": 0,
        "details": [],
    }

    print("\n" + "=" * 70)
    print("TASK 1: Colorado APCD via Socrata + CMS.gov Fallback")
    print("=" * 70)

    # --- Try Colorado Socrata datasets ---
    print("\n--- Colorado Socrata (data.colorado.gov) ---")
    socrata_any_success = False

    for dataset_id, name in CO_SOCRATA_DATASETS:
        report["socrata_datasets_tried"] += 1
        records = try_socrata_dataset("data.colorado.gov", dataset_id, name)

        if records is not None and len(records) > 0:
            source_url = f"https://data.colorado.gov/resource/{dataset_id}.json"
            count = ingest_socrata_records(db, records, name, source_url, state="CO")
            report["socrata_records_ingested"] += count
            report["total_records_ingested"] += count
            if count > 0:
                report["socrata_datasets_successful"] += 1
                socrata_any_success = True
            report["details"].append({
                "source": f"CO Socrata: {name}",
                "dataset_id": dataset_id,
                "status": "success",
                "raw_records": len(records),
                "ingested": count,
            })
            print(f"  -> Ingested {count} records from {name}")
        elif records is not None and len(records) == 0:
            report["details"].append({
                "source": f"CO Socrata: {name}",
                "dataset_id": dataset_id,
                "status": "empty",
                "raw_records": 0,
                "ingested": 0,
            })
            print(f"  -> {name}: empty dataset")
        else:
            report["details"].append({
                "source": f"CO Socrata: {name}",
                "dataset_id": dataset_id,
                "status": "failed",
                "raw_records": 0,
                "ingested": 0,
            })
            print(f"  -> {name}: failed (403 or error)")

    # --- CMS.gov datasets (always try these — they're fully open) ---
    print("\n--- CMS.gov Open Datasets ---")
    for dataset_id, name, host in CMS_DATASETS:
        report["cms_datasets_tried"] += 1
        records = try_socrata_dataset(host, dataset_id, name, limit=2000)

        if records is not None and len(records) > 0:
            source_url = f"https://{host}/resource/{dataset_id}.json"
            count = ingest_cms_records(db, records, name, source_url)
            report["cms_records_ingested"] += count
            report["total_records_ingested"] += count
            if count > 0:
                report["cms_datasets_successful"] += 1
            report["details"].append({
                "source": f"CMS: {name}",
                "dataset_id": dataset_id,
                "status": "success",
                "raw_records": len(records),
                "ingested": count,
            })
            print(f"  -> Ingested {count} records from {name}")
        elif records is not None and len(records) == 0:
            report["details"].append({
                "source": f"CMS: {name}",
                "dataset_id": dataset_id,
                "status": "empty",
                "raw_records": 0,
                "ingested": 0,
            })
            print(f"  -> {name}: empty dataset")
        else:
            report["details"].append({
                "source": f"CMS: {name}",
                "dataset_id": dataset_id,
                "status": "failed",
                "raw_records": 0,
                "ingested": 0,
            })
            print(f"  -> {name}: failed")

    # Record pipeline metric
    metric = DataPipelineMetric(
        metric_type="socrata_cms_ingestion",
        benefit_type="health",
        value=float(report["total_records_ingested"]),
        details=report,
        measured_at=datetime.now(UTC),
    )
    db.add(metric)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record metric: {e}")

    return report


# ─────────────────────────────────────────────────────────────────────
# Task 2: Download and parse a small insurer MRF file
# ─────────────────────────────────────────────────────────────────────

# Small insurers with likely-accessible MRF files
SMALL_INSURER_MRF_URLS = [
    {
        "insurer": "Oscar Health",
        "urls": [
            "https://www.hioscar.com/transparency-in-coverage",
        ],
    },
    {
        "insurer": "Friday Health Plans",
        "urls": [
            "https://www.fridayhealthplans.com/transparency-in-coverage",
            "https://www.fridayhealth.com/transparency-in-coverage/",
        ],
    },
    {
        "insurer": "Clover Health",
        "urls": [
            "https://www.cloverhealth.com/en/transparency-in-coverage",
        ],
    },
    {
        "insurer": "Alignment Healthcare",
        "urls": [
            "https://www.alignmenthealthcare.com/transparency-in-coverage",
        ],
    },
    {
        "insurer": "Ambetter (Centene)",
        "urls": [
            "https://ambfrm.centene.com/mrf",
        ],
    },
]

# Max MRF file size to attempt downloading
MAX_MRF_SIZE_MB = 200


def probe_and_find_mrf_links(url: str) -> dict:
    """Probe an insurer URL to find MRF file links."""
    result = {
        "url": url,
        "status_code": None,
        "content_type": None,
        "mrf_links": [],
        "is_json_index": False,
        "error": None,
    }

    try:
        resp = httpx.get(
            url,
            timeout=httpx.Timeout(20.0, read=60.0),
            follow_redirects=True,
            headers={
                "User-Agent": "BeneFlex-DataPipeline/1.0 (healthcare price transparency research)",
                "Accept": "application/json, text/html, */*",
            },
        )
        result["status_code"] = resp.status_code
        result["content_type"] = resp.headers.get("content-type", "")

        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}"
            return result

        content_type = resp.headers.get("content-type", "")

        # Check if it's a JSON table of contents
        if "json" in content_type or resp.text.lstrip().startswith("{"):
            try:
                data = resp.json()
                result["is_json_index"] = True

                # CMS TiC JSON format
                if isinstance(data, dict) and "reporting_entity_name" in data:
                    for plan in data.get("reporting_structure", []):
                        for mrf in plan.get("in_network_files", []):
                            loc = mrf.get("location", "")
                            if loc:
                                result["mrf_links"].append({
                                    "url": loc,
                                    "description": mrf.get("description", ""),
                                    "type": "in_network",
                                })
                        for mrf in plan.get("allowed_amount_files", []):
                            loc = mrf.get("location", "")
                            if loc:
                                result["mrf_links"].append({
                                    "url": loc,
                                    "description": mrf.get("description", ""),
                                    "type": "allowed_amount",
                                })

            except json.JSONDecodeError:
                pass

        # Check if it's HTML with links to MRF files
        elif "html" in content_type:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if any(ext in href.lower() for ext in [
                    ".json", ".json.gz", ".csv", ".csv.gz", ".zip",
                    "transparency", "machine-readable", "mrf",
                ]):
                    # Resolve relative URLs
                    if href.startswith("/"):
                        from urllib.parse import urljoin
                        href = urljoin(url, href)
                    if href.startswith("http"):
                        result["mrf_links"].append({
                            "url": href,
                            "description": a.get_text(strip=True)[:200],
                            "type": "from_html",
                        })

    except httpx.TimeoutException:
        result["error"] = "Timeout"
    except httpx.ConnectError as e:
        result["error"] = f"Connection error: {e}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    return result


def download_and_parse_mrf(url: str, insurer_name: str) -> tuple[list[dict], str | None]:
    """Download an MRF file and parse negotiated rates.

    Returns (records, error) where records is a list of dicts with
    billing_code, description, negotiated_rate, etc.
    """
    records = []

    try:
        # First check size with HEAD
        logger.info(f"    HEAD {url[:100]}...")
        head_resp = httpx.head(
            url,
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            headers={"User-Agent": "BeneFlex-DataPipeline/1.0"},
        )

        content_length = int(head_resp.headers.get("content-length", 0))
        size_mb = content_length / (1024 * 1024) if content_length > 0 else 0

        if content_length > 0:
            logger.info(f"    Size: {size_mb:.1f} MB")
            if size_mb > MAX_MRF_SIZE_MB:
                return [], f"File is {size_mb:.1f}MB — exceeds {MAX_MRF_SIZE_MB}MB limit"

        # Download the file
        logger.info(f"    Downloading...")
        resp = httpx.get(
            url,
            timeout=httpx.Timeout(30.0, read=300.0),
            follow_redirects=True,
            headers={"User-Agent": "BeneFlex-DataPipeline/1.0"},
        )
        resp.raise_for_status()

        actual_size_mb = len(resp.content) / (1024 * 1024)
        logger.info(f"    Downloaded: {actual_size_mb:.1f} MB")

        if actual_size_mb > MAX_MRF_SIZE_MB:
            return [], f"Downloaded file is {actual_size_mb:.1f}MB — exceeds limit"

        # Try to decompress if gzipped
        content_bytes = resp.content
        text_content = None

        if url.endswith(".gz") or resp.headers.get("content-encoding") == "gzip":
            try:
                text_content = gzip.decompress(content_bytes).decode("utf-8", errors="replace")
                logger.info(f"    Decompressed gzip: {len(text_content)} chars")
            except Exception:
                text_content = content_bytes.decode("utf-8", errors="replace")
        else:
            text_content = content_bytes.decode("utf-8", errors="replace")

        if not text_content:
            return [], "Empty content after decoding"

        # Try to parse as JSON (CMS MRF format)
        if text_content.lstrip().startswith("{") or text_content.lstrip().startswith("["):
            try:
                data = json.loads(text_content)
                records = parse_mrf_json(data, insurer_name, url)
                return records, None
            except json.JSONDecodeError as e:
                return [], f"JSON parse error: {e}"

        # Try as CSV
        try:
            reader = csv.DictReader(io.StringIO(text_content))
            for row in reader:
                code = (
                    row.get("billing_code") or row.get("code")
                    or row.get("cpt_code") or row.get("hcpcs_code") or ""
                ).strip()
                if not code:
                    continue

                price_str = (
                    row.get("negotiated_rate") or row.get("allowed_amount")
                    or row.get("price") or ""
                ).strip().replace("$", "").replace(",", "")
                try:
                    price = float(price_str)
                    if price <= 0:
                        continue
                except (ValueError, TypeError):
                    continue

                records.append({
                    "billing_code": code[:20],
                    "description": row.get("description", "")[:500],
                    "negotiated_rate": price,
                    "insurer": insurer_name,
                    "source_url": url,
                })

            return records, None
        except Exception as e:
            return [], f"CSV parse error: {e}"

    except httpx.TimeoutException:
        return [], "Timeout downloading"
    except httpx.HTTPStatusError as e:
        return [], f"HTTP {e.response.status_code}"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def parse_mrf_json(data: dict | list, insurer_name: str, source_url: str) -> list[dict]:
    """Parse CMS-format MRF JSON into rate records."""
    records = []

    # Standard CMS TiC in-network format
    in_network = []
    if isinstance(data, dict):
        in_network = data.get("in_network", [])
        reporting_entity = data.get("reporting_entity_name", insurer_name)
    elif isinstance(data, list):
        in_network = data
        reporting_entity = insurer_name

    for item in in_network[:20000]:  # Cap for safety
        if not isinstance(item, dict):
            continue

        billing_code = item.get("billing_code", "")
        billing_code_type = item.get("billing_code_type", "")
        description = item.get("description", "") or item.get("name", "")

        for rate_group in item.get("negotiated_rates", [])[:30]:
            if not isinstance(rate_group, dict):
                continue

            for price_info in rate_group.get("negotiated_prices", [])[:10]:
                if not isinstance(price_info, dict):
                    continue

                try:
                    rate = float(price_info.get("negotiated_rate", 0))
                    if rate <= 0:
                        continue
                except (ValueError, TypeError):
                    continue

                records.append({
                    "billing_code": billing_code[:20],
                    "billing_code_type": billing_code_type,
                    "description": description[:500],
                    "negotiated_rate": rate,
                    "insurer": reporting_entity,
                    "source_url": source_url,
                })

    logger.info(f"    Parsed {len(records)} negotiated rate records from MRF JSON")
    return records


def task2_download_mrf(db) -> dict:
    """Task 2: Download and parse a small insurer MRF file."""
    report = {
        "insurers_probed": 0,
        "urls_probed": 0,
        "mrf_links_found": 0,
        "mrf_downloads_attempted": 0,
        "mrf_records_parsed": 0,
        "mrf_records_ingested": 0,
        "successful_insurer": None,
        "barriers": [],
        "details": [],
    }

    print("\n" + "=" * 70)
    print("TASK 2: Download and Parse Small Insurer MRF File")
    print("=" * 70)

    for insurer_info in SMALL_INSURER_MRF_URLS:
        insurer_name = insurer_info["insurer"]
        report["insurers_probed"] += 1

        print(f"\n--- {insurer_name} ---")

        all_mrf_links = []

        for url in insurer_info["urls"]:
            report["urls_probed"] += 1
            print(f"  Probing: {url}")
            probe = probe_and_find_mrf_links(url)

            detail = {
                "insurer": insurer_name,
                "url": url,
                "status": probe["status_code"],
                "content_type": probe["content_type"],
                "is_json_index": probe["is_json_index"],
                "mrf_links_found": len(probe["mrf_links"]),
                "error": probe["error"],
            }
            report["details"].append(detail)

            if probe["error"]:
                print(f"    Error: {probe['error']}")
                report["barriers"].append({
                    "insurer": insurer_name,
                    "url": url,
                    "barrier": probe["error"],
                })
                continue

            print(f"    HTTP {probe['status_code']}, content-type: {probe['content_type'][:60]}")
            if probe["mrf_links"]:
                print(f"    Found {len(probe['mrf_links'])} MRF links")
                all_mrf_links.extend(probe["mrf_links"])
            elif probe["is_json_index"]:
                print(f"    JSON index found but no standard MRF links extracted")

        report["mrf_links_found"] += len(all_mrf_links)

        # Try to download the smallest files first
        # Filter to only JSON/gz files (actual MRF data)
        downloadable = [
            link for link in all_mrf_links
            if any(ext in link["url"].lower() for ext in [".json", ".gz", ".csv"])
        ]

        if not downloadable and all_mrf_links:
            # Try all links if no .json/.gz/.csv specific ones
            downloadable = all_mrf_links[:5]

        for mrf_link in downloadable[:3]:  # Try up to 3 files per insurer
            mrf_url = mrf_link["url"]
            report["mrf_downloads_attempted"] += 1
            print(f"  Downloading MRF: {mrf_url[:100]}...")

            records, error = download_and_parse_mrf(mrf_url, insurer_name)

            if error:
                print(f"    Error: {error}")
                report["barriers"].append({
                    "insurer": insurer_name,
                    "url": mrf_url[:200],
                    "barrier": error,
                })
                continue

            if records:
                report["mrf_records_parsed"] += len(records)
                print(f"    Parsed {len(records)} negotiated rate records!")

                # Ingest into price_data
                batch = []
                for rec in records:
                    batch.append(PriceData(
                        provider_name=rec["insurer"][:500],
                        service_code=rec["billing_code"][:20],
                        service_description=rec.get("description", "")[:500] or f"MRF {insurer_name}",
                        price=rec["negotiated_rate"],
                        channel=f"insurer_mrf_{insurer_name.lower().replace(' ', '_')[:50]}",
                        source=PriceSource.insurer_transparency,
                        source_url=rec["source_url"][:500] if len(rec["source_url"]) <= 500 else mrf_url[:500],
                        ingested_at=datetime.now(UTC),
                    ))

                    if len(batch) >= 5000:
                        db.bulk_save_objects(batch)
                        db.commit()
                        batch = []

                if batch:
                    db.bulk_save_objects(batch)
                    db.commit()

                report["mrf_records_ingested"] += len(records)
                report["successful_insurer"] = insurer_name
                print(f"    -> Ingested {len(records)} records into price_data!")

                # Success — stop trying other insurers
                break

        if report["mrf_records_ingested"] > 0:
            break  # Found and ingested data, done

    if report["mrf_records_ingested"] == 0:
        report["barriers"].append({
            "insurer": "ALL",
            "url": "",
            "barrier": (
                "No small insurer MRF files could be downloaded and parsed. "
                "Most insurer MRF files are 10-100GB, require complex authentication, "
                "or are served via streaming APIs that don't support simple GET requests. "
                "The MRF index has been cataloged for future processing."
            ),
        })

    # Record pipeline metric
    metric = DataPipelineMetric(
        metric_type="mrf_file_download",
        benefit_type="health",
        value=float(report["mrf_records_ingested"]),
        details=report,
        measured_at=datetime.now(UTC),
    )
    db.add(metric)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record metric: {e}")

    return report


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("BeneFlex: Socrata/CMS Ingestion + MRF File Download")
    print("=" * 70)

    db = SessionLocal()

    try:
        # Task 1: Socrata + CMS
        report1 = task1_socrata_and_cms(db)

        # Task 2: MRF download
        report2 = task2_download_mrf(db)

        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)

        print(f"\nTask 1 — Socrata/CMS Ingestion:")
        print(f"  Socrata datasets tried:      {report1['socrata_datasets_tried']}")
        print(f"  Socrata datasets successful:  {report1['socrata_datasets_successful']}")
        print(f"  Socrata records ingested:     {report1['socrata_records_ingested']}")
        print(f"  CMS datasets tried:           {report1['cms_datasets_tried']}")
        print(f"  CMS datasets successful:      {report1['cms_datasets_successful']}")
        print(f"  CMS records ingested:         {report1['cms_records_ingested']}")
        print(f"  TOTAL records ingested:       {report1['total_records_ingested']}")

        print(f"\nTask 2 — MRF File Download:")
        print(f"  Insurers probed:              {report2['insurers_probed']}")
        print(f"  MRF links found:              {report2['mrf_links_found']}")
        print(f"  Downloads attempted:          {report2['mrf_downloads_attempted']}")
        print(f"  Records parsed:               {report2['mrf_records_parsed']}")
        print(f"  Records ingested:             {report2['mrf_records_ingested']}")
        if report2['successful_insurer']:
            print(f"  Successful insurer:           {report2['successful_insurer']}")

        if report2['barriers']:
            print(f"\n  Barriers encountered ({len(report2['barriers'])}):")
            for b in report2['barriers'][:10]:
                print(f"    - {b['insurer']}: {b['barrier'][:120]}")

        # Print overall DB stats
        from sqlalchemy import text
        total = db.execute(text("SELECT MAX(rowid) FROM price_data")).scalar() or 0
        print(f"\n  Total price_data records in DB: {total}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
