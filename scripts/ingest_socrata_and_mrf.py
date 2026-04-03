"""Ingest Colorado APCD data via Socrata Open Data API + download a small insurer MRF.

Task 1: Colorado publishes health data via Socrata (data.colorado.gov).
         Try SODA API with proper headers. If 403, fall back to CMS.gov datasets
         which are available as direct CSV downloads from the provider-data API.

Task 2: Download a small MRF file from BCBS Wyoming via the Highmark MRF hub
         (mrfdata.hmhs.com). Parse the CMS-format in-network JSON and insert
         negotiated rates into price_data.

Run: cd beneflex && source .venv/bin/activate && python3 -m scripts.ingest_socrata_and_mrf
"""

import csv
import gzip
import io
import json
import logging
import os
import sys
from datetime import datetime, timedelta, UTC

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
# Task 1: Colorado APCD via Socrata + CMS.gov CSV fallback
# ─────────────────────────────────────────────────────────────────────

CO_SOCRATA_DATASETS = [
    ("a9bw-4sc8", "CO APCD Insights"),
    ("r74d-d2pc", "Telehealth Utilization"),
    ("9zu6-ut2w", "Shop for Care"),
]

# CMS.gov direct CSV downloads (from the provider-data API catalog, confirmed working)
CMS_CSV_DATASETS = [
    {
        "id": "xubh-q36u",
        "name": "Hospital General Information",
        "url": "https://data.cms.gov/provider-data/sites/default/files/resources/893c372430d9d71a1c52737d01239d47_1770163599/Hospital_General_Information.csv",
    },
    {
        "id": "nrth-mfg3",
        "name": "Medicare Hospital Spending by Claim",
        "url": "https://data.cms.gov/provider-data/sites/default/files/resources/1f8cde9e222d5d49f88a894bcf7a8981_1770163638/Medicare_Hospital_Spending_by_Claim.csv",
    },
    {
        "id": "rrqw-56er",
        "name": "Medicare Spending Per Beneficiary - Hospital",
        "url": "https://data.cms.gov/provider-data/sites/default/files/resources/69874ce604586980ac088283c1b35095_1770163639/Medicare_Hospital_Spending_Per_Patient-Hospital.csv",
    },
]


def try_socrata_dataset(host: str, dataset_id: str, name: str, limit: int = 1000) -> list[dict] | None:
    """Try fetching a Socrata dataset via SODA API."""
    url = f"https://{host}/resource/{dataset_id}.json"
    headers = {"Accept": "application/json", "User-Agent": "BeneFlex-DataPipeline/1.0"}
    params = {"$limit": limit}

    logger.info(f"  Trying Socrata: {url} ({name})")
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=TIMEOUT, follow_redirects=True)
        logger.info(f"    HTTP {resp.status_code}, {len(resp.text)} bytes")

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                logger.info(f"    Records: {len(data)}, Fields: {list(data[0].keys())[:8]}")
                return data
            return []
        elif resp.status_code == 403:
            logger.warning("    403 Forbidden -- Socrata blocked without app_token")
            return None
        else:
            logger.warning(f"    Unexpected status: {resp.status_code}")
            return None
    except Exception as e:
        logger.warning(f"    Error: {type(e).__name__}: {e}")
        return None


def download_cms_csv(url: str, name: str, max_bytes: int = 50_000_000) -> str | None:
    """Download a CMS CSV file. Returns CSV text or None."""
    logger.info(f"  Downloading CMS CSV: {name}")
    try:
        # HEAD first to check size
        head = httpx.head(url, timeout=httpx.Timeout(15.0), follow_redirects=True,
                         headers={"User-Agent": "BeneFlex-DataPipeline/1.0"})
        cl = int(head.headers.get("content-length", 0))
        if cl > max_bytes:
            logger.warning(f"    CSV is {cl/(1024*1024):.1f}MB -- too large, will stream first {max_bytes/(1024*1024):.0f}MB")

        # Download (stream if large)
        with httpx.stream("GET", url, timeout=httpx.Timeout(30.0, read=180.0),
                         follow_redirects=True,
                         headers={"User-Agent": "BeneFlex-DataPipeline/1.0"}) as resp:
            resp.raise_for_status()
            chunks = []
            total = 0
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break

        content = b"".join(chunks).decode("utf-8", errors="replace")
        logger.info(f"    Downloaded: {len(content)} chars")
        return content
    except Exception as e:
        logger.warning(f"    Download failed: {type(e).__name__}: {e}")
        return None


def ingest_cms_hospital_info_csv(db, csv_content: str, source_url: str) -> int:
    """Ingest CMS Hospital General Information CSV."""
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0
    batch = []

    for row in reader:
        facility_id = (row.get("Facility ID") or row.get("facility_id") or "").strip()
        hospital_name = (row.get("Facility Name") or row.get("Hospital Name")
                        or row.get("hospital_name") or "").strip()
        state = (row.get("State") or row.get("state") or "").strip()[:2]

        # Hospital overall rating (1-5 stars)
        rating = (row.get("Hospital overall rating") or row.get("hospital_overall_rating") or "").strip()
        if not rating or rating == "Not Available":
            continue
        try:
            rating_val = float(rating)
            if rating_val <= 0:
                continue
        except (ValueError, TypeError):
            continue

        if not facility_id:
            continue

        batch.append(PriceData(
            provider_name=hospital_name[:500] or f"CMS Hospital {facility_id}",
            service_code=facility_id[:20],
            service_description=f"Hospital Overall Quality Rating ({hospital_name})",
            price=rating_val,
            channel="cms_hospital_quality_rating",
            source=PriceSource.medicare_inpatient,
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


def ingest_cms_spending_csv(db, csv_content: str, source_url: str, source_name: str) -> int:
    """Ingest CMS Medicare Spending CSV data.

    Handles two CMS spending CSV formats:
    1. Medicare Hospital Spending by Claim:
       Columns: Facility Name, Facility ID, State, Period, Claim Type,
                Avg Spndg Per EP Hospital, Avg Spndg Per EP State, etc.
    2. Medicare Spending Per Beneficiary:
       Columns: Facility ID, Facility Name, State, Measure ID, Score, etc.
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    count = 0
    batch = []

    for row in reader:
        facility_id = (row.get("Facility ID") or row.get("Provider ID") or "").strip()
        hospital_name = (row.get("Facility Name") or row.get("Hospital Name") or "").strip()
        state = (row.get("State") or "").strip()[:2]

        if not facility_id:
            continue

        # Format 1: Spending by Claim (has Claim Type, Avg Spndg Per EP Hospital)
        claim_type = (row.get("Claim Type") or "").strip()
        avg_spending = (row.get("Avg Spndg Per EP Hospital") or "").strip()

        if avg_spending and avg_spending != "Not Available":
            try:
                price_val = float(avg_spending.replace("$", "").replace(",", ""))
                if price_val > 0:
                    period = (row.get("Period") or "").strip()[:50]
                    service_code = claim_type[:20] if claim_type else facility_id[:20]
                    batch.append(PriceData(
                        provider_name=hospital_name[:500] or f"CMS Hospital {facility_id}",
                        service_code=service_code,
                        service_description=f"Medicare Spending by Claim: {claim_type} ({period})"[:500],
                        price=price_val,
                        channel="cms_medicare_spending_per_episode",
                        source=PriceSource.medicare_inpatient,
                        source_url=source_url,
                        state=state if state else None,
                        ingested_at=datetime.now(UTC),
                    ))
                    count += 1
            except (ValueError, TypeError):
                pass

        # Format 2: MSPB Score (has Score, Measure ID)
        score = (row.get("Score") or "").strip()
        measure_id = (row.get("Measure ID") or "").strip()

        if score and score != "Not Available" and measure_id:
            try:
                score_val = float(score)
                if score_val > 0:
                    measure_name = (row.get("Measure Name") or measure_id)[:500]
                    batch.append(PriceData(
                        provider_name=hospital_name[:500] or f"CMS Hospital {facility_id}",
                        service_code=measure_id[:20],
                        service_description=f"MSPB: {measure_name}",
                        price=score_val,
                        channel="cms_mspb_score",
                        source=PriceSource.medicare_inpatient,
                        source_url=source_url,
                        state=state if state else None,
                        ingested_at=datetime.now(UTC),
                    ))
                    count += 1
            except (ValueError, TypeError):
                pass

        if len(batch) >= 2000:
            db.bulk_save_objects(batch)
            db.commit()
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    return count


def ingest_socrata_records(db, records: list[dict], source_name: str, source_url: str, state: str = "CO") -> int:
    """Ingest Socrata JSON records into price_data."""
    count = 0
    batch = []

    code_fields = [
        "procedure_code", "cpt_code", "hcpcs_code", "service_code", "code",
        "billing_code", "drg_code", "drg", "drg_definition",
        "procedure", "service", "category", "measure_id",
        "provider_id", "hospital_name", "facility_id",
    ]
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
    desc_fields = [
        "description", "service_description", "procedure_description",
        "drg_definition", "drg_description", "measure_name",
        "condition", "category", "service_name",
        "hospital_name", "facility_name", "provider_name",
    ]
    provider_fields = [
        "provider_name", "hospital_name", "facility_name",
        "rendering_provider", "organization_name",
    ]

    for row in records:
        service_code = None
        for f in code_fields:
            val = row.get(f)
            if val and str(val).strip():
                service_code = str(val).strip()[:20]
                break
        if not service_code:
            continue

        price_val = None
        for f in price_fields:
            val = row.get(f)
            if val:
                try:
                    price_val = float(str(val).replace("$", "").replace(",", "").strip())
                    if 0 < price_val < 10_000_000:
                        break
                    price_val = None
                except (ValueError, TypeError):
                    price_val = None
        if price_val is None:
            continue

        description = ""
        for f in desc_fields:
            val = row.get(f)
            if val and str(val).strip():
                description = str(val).strip()[:500]
                break

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


def task1_socrata_and_cms(db) -> dict:
    """Task 1: Ingest Colorado APCD via Socrata, fall back to CMS.gov CSV downloads."""
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
    print("TASK 1: Colorado APCD via Socrata + CMS.gov CSV Fallback")
    print("=" * 70)

    # --- Try Colorado Socrata datasets ---
    print("\n--- Colorado Socrata (data.colorado.gov) ---")
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
            report["details"].append({
                "source": f"CO Socrata: {name}", "dataset_id": dataset_id,
                "status": "success", "raw_records": len(records), "ingested": count,
            })
            print(f"  -> Ingested {count} records from {name}")
        else:
            status = "empty" if records is not None else "failed_403"
            report["details"].append({
                "source": f"CO Socrata: {name}", "dataset_id": dataset_id,
                "status": status, "raw_records": 0, "ingested": 0,
            })
            print(f"  -> {name}: {status}")

    # --- CMS.gov CSV direct downloads ---
    print("\n--- CMS.gov Direct CSV Downloads ---")
    for ds in CMS_CSV_DATASETS:
        report["cms_datasets_tried"] += 1
        csv_content = download_cms_csv(ds["url"], ds["name"])

        if csv_content:
            if "hospital general" in ds["name"].lower():
                count = ingest_cms_hospital_info_csv(db, csv_content, ds["url"])
            else:
                count = ingest_cms_spending_csv(db, csv_content, ds["url"], ds["name"])

            report["cms_records_ingested"] += count
            report["total_records_ingested"] += count
            if count > 0:
                report["cms_datasets_successful"] += 1
            report["details"].append({
                "source": f"CMS CSV: {ds['name']}", "dataset_id": ds["id"],
                "status": "success", "ingested": count,
            })
            print(f"  -> Ingested {count} records from {ds['name']}")
        else:
            report["details"].append({
                "source": f"CMS CSV: {ds['name']}", "dataset_id": ds["id"],
                "status": "download_failed", "ingested": 0,
            })
            print(f"  -> {ds['name']}: download failed")

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

MAX_MRF_SIZE_MB = 200


def get_bcbs_wyoming_mrf_index() -> dict | None:
    """Fetch the BCBS Wyoming MRF table-of-contents JSON from the Highmark MRF hub."""
    now = datetime.now()
    first_day = now.replace(day=1)

    # Try current month, then previous month
    dates_to_try = [
        first_day.strftime("%Y-%m-%d"),
        (first_day - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d"),
    ]

    for date_str in dates_to_try:
        url = f"https://mrfdata.hmhs.com/files/460/wy/inbound/local/{date_str}_Blue_Cross_Blue_Shield_of_Wyoming_index.json"
        logger.info(f"  Trying BCBS WY index: {url[:100]}")
        try:
            resp = httpx.get(
                url,
                timeout=httpx.Timeout(30.0, read=120.0),
                follow_redirects=True,
                headers={"User-Agent": "BeneFlex-DataPipeline/1.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "reporting_entity_name" in data:
                    logger.info(f"    Success: {data['reporting_entity_name']}")
                    return data
        except Exception as e:
            logger.warning(f"    Error: {type(e).__name__}: {e}")

    return None


def find_smallest_mrf_files(index_data: dict, max_files: int = 50) -> list[dict]:
    """Extract MRF file URLs from the index and sort by checking content-length."""
    all_files = []
    for plan in index_data.get("reporting_structure", []):
        for mrf in plan.get("in_network_files", []):
            loc = mrf.get("location", "")
            if loc:
                all_files.append({
                    "url": loc,
                    "description": mrf.get("description", ""),
                    "type": "in_network",
                })
        for mrf in plan.get("allowed_amount_files", []):
            loc = mrf.get("location", "")
            if loc:
                all_files.append({
                    "url": loc,
                    "description": mrf.get("description", ""),
                    "type": "allowed_amount",
                })

    logger.info(f"  Total MRF files in index: {len(all_files)}")

    # Check sizes of a sample to find the smallest
    sized_files = []
    for f in all_files[:max_files]:
        try:
            head = httpx.head(
                f["url"], timeout=httpx.Timeout(10.0), follow_redirects=True,
                headers={"User-Agent": "BeneFlex-DataPipeline/1.0"},
            )
            cl = int(head.headers.get("content-length", 0))
            f["size_bytes"] = cl
            f["size_mb"] = cl / (1024 * 1024)
            sized_files.append(f)
        except Exception:
            continue

    # Sort by size (smallest first)
    sized_files.sort(key=lambda x: x["size_bytes"])
    return sized_files


def download_and_parse_mrf(url: str, insurer_name: str) -> tuple[list[dict], str | None]:
    """Download a gzipped MRF JSON file and parse negotiated rates."""
    records = []

    try:
        logger.info("    Downloading MRF...")
        resp = httpx.get(
            url,
            timeout=httpx.Timeout(60.0, read=300.0),
            follow_redirects=True,
            headers={"User-Agent": "BeneFlex-DataPipeline/1.0"},
        )
        resp.raise_for_status()

        content_bytes = resp.content
        actual_size_mb = len(content_bytes) / (1024 * 1024)
        logger.info(f"    Downloaded: {actual_size_mb:.1f} MB")

        if actual_size_mb > MAX_MRF_SIZE_MB:
            return [], f"Downloaded file is {actual_size_mb:.1f}MB -- exceeds limit"

        # Detect gzip by magic bytes (1f 8b) regardless of URL extension or headers
        is_gzip = (len(content_bytes) >= 2 and content_bytes[:2] == b"\x1f\x8b")

        text_content = None
        if is_gzip:
            try:
                decompressed = gzip.decompress(content_bytes)
                text_content = decompressed.decode("utf-8", errors="replace")
                logger.info(f"    Decompressed gzip: {len(text_content) / (1024*1024):.1f} MB")
            except Exception as e:
                logger.warning(f"    Gzip decompress failed: {e}")
                text_content = content_bytes.decode("utf-8", errors="replace")
        else:
            text_content = content_bytes.decode("utf-8", errors="replace")

        if not text_content:
            return [], "Empty content after decoding"

        # Parse JSON
        stripped = text_content.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            data = json.loads(text_content)
            records = parse_mrf_json(data, insurer_name, url)
            return records, None

        return [], f"Content is not JSON (starts with: {repr(stripped[:50])})"

    except httpx.TimeoutException:
        return [], "Timeout downloading"
    except httpx.HTTPStatusError as e:
        return [], f"HTTP {e.response.status_code}"
    except json.JSONDecodeError as e:
        return [], f"JSON parse error: {e}"
    except MemoryError:
        return [], "Out of memory decompressing/parsing MRF"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def parse_mrf_json(data: dict | list, insurer_name: str, source_url: str) -> list[dict]:
    """Parse CMS-format MRF JSON into rate records."""
    records = []

    in_network = []
    reporting_entity = insurer_name
    if isinstance(data, dict):
        in_network = data.get("in_network", [])
        reporting_entity = data.get("reporting_entity_name", insurer_name)
    elif isinstance(data, list):
        in_network = data

    for item in in_network[:50000]:  # Cap
        if not isinstance(item, dict):
            continue

        billing_code = item.get("billing_code", "")
        billing_code_type = item.get("billing_code_type", "")
        description = item.get("description", "") or item.get("name", "")

        if not billing_code:
            continue

        for rate_group in item.get("negotiated_rates", [])[:50]:
            if not isinstance(rate_group, dict):
                continue

            for price_info in rate_group.get("negotiated_prices", [])[:20]:
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
    """Task 2: Download and parse a small insurer MRF file via BCBS Wyoming."""
    report = {
        "index_fetched": False,
        "insurer": "Blue Cross Blue Shield of Wyoming",
        "mrf_files_in_index": 0,
        "files_size_checked": 0,
        "smallest_file_mb": None,
        "mrf_downloads_attempted": 0,
        "mrf_records_parsed": 0,
        "mrf_records_ingested": 0,
        "barriers": [],
        "details": [],
    }

    print("\n" + "=" * 70)
    print("TASK 2: Download and Parse Small Insurer MRF File")
    print("=" * 70)

    print("\n--- BCBS Wyoming via Highmark MRF Hub ---")

    # Step 1: Fetch the index
    index_data = get_bcbs_wyoming_mrf_index()
    if not index_data:
        report["barriers"].append("Failed to fetch BCBS Wyoming MRF index")
        print("  -> Failed to fetch MRF index")
        _record_mrf_metric(db, report)
        return report

    report["index_fetched"] = True
    reporting_entity = index_data.get("reporting_entity_name", "Unknown")
    print(f"  Index fetched: {reporting_entity}")

    # Count total files
    total_files = sum(
        len(plan.get("in_network_files", [])) + len(plan.get("allowed_amount_files", []))
        for plan in index_data.get("reporting_structure", [])
    )
    report["mrf_files_in_index"] = total_files
    print(f"  Total MRF files in index: {total_files}")

    # Step 2: Find smallest files
    print("  Checking file sizes (HEAD requests)...")
    sized_files = find_smallest_mrf_files(index_data, max_files=40)
    report["files_size_checked"] = len(sized_files)

    if not sized_files:
        report["barriers"].append("Could not determine file sizes")
        print("  -> No files could be size-checked")
        _record_mrf_metric(db, report)
        return report

    report["smallest_file_mb"] = sized_files[0]["size_mb"]
    print(f"  Smallest file: {sized_files[0]['size_mb']:.1f} MB (compressed)")
    print(f"  Files under 50MB: {sum(1 for f in sized_files if f['size_mb'] < 50)}")

    # Step 3: Download and parse the smallest files
    for mrf_file in sized_files[:3]:  # Try up to 3 smallest
        if mrf_file["size_mb"] > MAX_MRF_SIZE_MB:
            report["barriers"].append(f"Smallest available file is {mrf_file['size_mb']:.1f}MB")
            break

        report["mrf_downloads_attempted"] += 1
        print(f"\n  Downloading: {mrf_file['size_mb']:.1f} MB, {mrf_file['type']}")
        print(f"    URL: {mrf_file['url'][:120]}...")

        records, error = download_and_parse_mrf(mrf_file["url"], reporting_entity)

        if error:
            print(f"    Error: {error}")
            report["barriers"].append(error)
            continue

        if records:
            report["mrf_records_parsed"] = len(records)
            print(f"    Parsed {len(records)} negotiated rate records!")

            # Ingest into price_data
            batch = []
            for rec in records:
                src_url = rec["source_url"]
                # Truncate signed URLs to just the base path (remove signature params)
                if "?" in src_url:
                    src_url = src_url[:src_url.index("?")]
                batch.append(PriceData(
                    provider_name=rec["insurer"][:500],
                    service_code=rec["billing_code"][:20],
                    service_description=rec.get("description", "")[:500] or "MRF negotiated rate",
                    price=rec["negotiated_rate"],
                    channel=f"insurer_mrf_{rec.get('billing_code_type', 'unknown').lower()[:30]}",
                    source=PriceSource.insurer_transparency,
                    source_url=src_url[:500],
                    ingested_at=datetime.now(UTC),
                ))

                if len(batch) >= 5000:
                    db.bulk_save_objects(batch)
                    db.commit()
                    batch = []

            if batch:
                db.bulk_save_objects(batch)
                db.commit()

            report["mrf_records_ingested"] = len(records)
            print(f"    -> Ingested {len(records)} records into price_data!")

            report["details"].append({
                "file_url_base": src_url[:200],
                "compressed_size_mb": mrf_file["size_mb"],
                "records_parsed": len(records),
                "records_ingested": len(records),
                "billing_code_types": list(set(r.get("billing_code_type", "") for r in records[:100])),
            })
            break  # Success

    if report["mrf_records_ingested"] == 0 and not report["barriers"]:
        report["barriers"].append(
            "MRF files parsed but contained no extractable negotiated rates."
        )

    _record_mrf_metric(db, report)
    return report


def _record_mrf_metric(db, report: dict):
    """Record MRF download pipeline metric."""
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


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("BeneFlex: Socrata/CMS Ingestion + MRF File Download")
    print("=" * 70)

    db = SessionLocal()

    try:
        # Task 1
        report1 = task1_socrata_and_cms(db)

        # Task 2
        report2 = task2_download_mrf(db)

        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)

        print("\nTask 1 -- Socrata/CMS Ingestion:")
        print(f"  Socrata datasets tried:      {report1['socrata_datasets_tried']}")
        print(f"  Socrata datasets successful:  {report1['socrata_datasets_successful']}")
        print(f"  Socrata records ingested:     {report1['socrata_records_ingested']}")
        print(f"  CMS CSV datasets tried:       {report1['cms_datasets_tried']}")
        print(f"  CMS CSV datasets successful:  {report1['cms_datasets_successful']}")
        print(f"  CMS records ingested:         {report1['cms_records_ingested']}")
        print(f"  TOTAL records ingested:       {report1['total_records_ingested']}")

        print("\nTask 2 -- MRF File Download (BCBS Wyoming):")
        print(f"  Index fetched:                {report2['index_fetched']}")
        print(f"  MRF files in index:           {report2['mrf_files_in_index']}")
        print(f"  Files size-checked:           {report2['files_size_checked']}")
        if report2['smallest_file_mb'] is not None:
            print(f"  Smallest file:                {report2['smallest_file_mb']:.1f} MB")
        print(f"  Downloads attempted:          {report2['mrf_downloads_attempted']}")
        print(f"  Records parsed:               {report2['mrf_records_parsed']}")
        print(f"  Records ingested:             {report2['mrf_records_ingested']}")

        if report2["barriers"]:
            print(f"\n  Barriers ({len(report2['barriers'])}):")
            for b in report2["barriers"][:10]:
                print(f"    - {b[:120]}")

        # Print overall DB stats
        from sqlalchemy import text
        total = db.execute(text("SELECT MAX(rowid) FROM price_data")).scalar() or 0
        print(f"\n  Total price_data records in DB: {total}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
