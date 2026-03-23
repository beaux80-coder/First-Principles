"""Automated downloaders for public CMS data sources (Function 8, public layer).

Downloads real data from:
1. CMS NADAC (National Average Drug Acquisition Cost) — weekly pharmacy pricing
2. CMS Medicare Physician Fee Schedule — national payment amounts
3. Hospital Price Transparency MRF files — from individual hospital websites

All downloads are idempotent. Each function clears stale data for that source
before inserting fresh records, ensuring the database always reflects the latest
available data.
"""

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, UTC

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)

# Timeout for large file downloads (some hospital MRFs are 100MB+)
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0)


# ---------------------------------------------------------------------------
# NADAC Pharmacy Data
# ---------------------------------------------------------------------------

# Direct CSV download from data.medicaid.gov
NADAC_URLS = [
    "https://download.medicaid.gov/data/nadac-national-average-drug-acquisition-cost-12-31-2025.csv",
    "https://data.medicaid.gov/api/1/datastore/query/fbb83258-11c7-47f5-8b18-5f8e79f7e704/0?offset=0&count=true&results=true&schema=true&keys=true&format=csv&rowIds=false",
]


def download_nadac(db: Session) -> int:
    """Download and ingest the latest NADAC pharmacy pricing data from CMS.

    NADAC is published weekly and contains ~30,000 drug pricing records.
    """
    logger.info("Downloading NADAC pharmacy data from CMS...")

    content = None
    source_url = ""
    for url in NADAC_URLS:
        try:
            resp = httpx.get(url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            content = resp.text
            source_url = url
            logger.info(f"Downloaded NADAC from {url} ({len(content)} bytes)")
            break
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"Failed to download NADAC from {url}: {e}")
            continue

    if not content:
        logger.error("All NADAC download URLs failed")
        return 0

    # Clear old NADAC records and ingest fresh data
    db.execute(delete(PriceData).where(PriceData.source == PriceSource.nadac_pharmacy))
    db.commit()

    reader = csv.DictReader(io.StringIO(content))
    count = 0
    batch = []

    for row in reader:
        ndc = (
            row.get("NDC") or row.get("ndc") or row.get("NDC Description")
            or ""
        )
        # Some NADAC files use "NDC" as a combined field; extract the code
        ndc = ndc.strip()[:20] if ndc else ""
        if not ndc:
            continue

        description = (
            row.get("NDC Description") or row.get("ndc_description")
            or row.get("Drug Name") or row.get("drug_name") or ""
        ).strip()

        price_str = (
            row.get("NADAC_Per_Unit") or row.get("nadac_per_unit")
            or row.get("NADAC Per Unit") or ""
        ).strip().replace("$", "").replace(",", "")

        if not price_str:
            continue

        try:
            price = float(price_str)
            if price > 0:
                batch.append(PriceData(
                    provider_name="NADAC",
                    service_code=ndc,
                    service_description=description[:500] if description else "",
                    price=price,
                    channel="nadac_per_unit",
                    source=PriceSource.nadac_pharmacy,
                    source_url=source_url,
                    ingested_at=datetime.now(UTC),
                ))
                count += 1
        except ValueError:
            pass

        # Batch insert every 5000 records
        if len(batch) >= 5000:
            db.bulk_save_objects(batch)
            db.commit()
            batch = []

    if batch:
        db.bulk_save_objects(batch)
        db.commit()

    logger.info(f"NADAC ingestion complete: {count} records")
    return count


# ---------------------------------------------------------------------------
# Medicare Physician Fee Schedule
# ---------------------------------------------------------------------------

# CMS PFS Indicators dataset IDs by year (from pfs.data.cms.gov)
PFS_DATASET_IDS = {
    2026: "7c7df311-5315-4f38-b9ed-fd62f8bebe11",
    2025: "1a4e7cb4-65db-48fd-8250-a64a3cc6e583",
    2024: "b9841b4a-9811-41e2-ae5a-c00d51b19df1",  # 2024B
    2023: "2698e4b1-8506-4cc8-97a4-d73bedea1d88",
}
PFS_API_BASE = "https://pfs.data.cms.gov/api/1/datastore/query"


def download_medicare_pfs(db: Session, year: int = 2025) -> int:
    """Download and ingest CMS Medicare Physician Fee Schedule.

    Uses the CMS PFS Indicators dataset which contains HCPCS codes with
    RVU values and the conversion factor. Payment = total_RVU * conv_factor.
    Paginates through the full dataset (~16,000 rows).
    """
    dataset_id = PFS_DATASET_IDS.get(year)
    if not dataset_id:
        logger.error(f"No PFS dataset ID for year {year}")
        return 0

    logger.info(f"Downloading Medicare PFS {year} Indicators from CMS...")

    # Clear old Medicare PFS records
    db.execute(delete(PriceData).where(PriceData.source == PriceSource.medicare_physician_fee))
    db.commit()

    count = 0
    offset = 0
    page_size = 500  # CMS PFS API rejects large limit values; use small pages

    while True:
        url = f"{PFS_API_BASE}/{dataset_id}/0"
        params = {
            "offset": offset,
            "count": "true",
            "results": "true",
            "format": "csv",
            "limit": page_size,
        }

        try:
            resp = httpx.get(url, params=params, timeout=DOWNLOAD_TIMEOUT)
            if resp.status_code == 400:
                # API may reject limit param; try without it (returns all rows)
                params.pop("limit", None)
                resp = httpx.get(url, params=params, timeout=httpx.Timeout(30.0, read=300.0))
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"PFS download failed at offset {offset}: {e}")
            break

        reader = csv.DictReader(io.StringIO(resp.text))
        rows_in_page = 0
        batch = []

        for row in reader:
            rows_in_page += 1
            hcpcs = (row.get("hcpc") or "").strip()
            if not hcpcs:
                continue

            description = (row.get("sdesc") or "").strip()
            conv_factor_str = (row.get("conv_fact") or "0").strip()

            try:
                conv_factor = float(conv_factor_str)
            except ValueError:
                continue

            if conv_factor <= 0:
                continue

            # Non-facility payment = full_nfac_total * conv_fact
            nf_rvu_str = (row.get("full_nfac_total") or "0").strip()
            f_rvu_str = (row.get("full_fac_total") or "0").strip()

            try:
                nf_rvu = float(nf_rvu_str)
                if nf_rvu > 0:
                    nf_payment = round(nf_rvu * conv_factor, 2)
                    batch.append(PriceData(
                        provider_name="Medicare",
                        service_code=hcpcs,
                        service_description=description[:500],
                        price=nf_payment,
                        channel="medicare_non_facility_na_payment",
                        source=PriceSource.medicare_physician_fee,
                        source_url=f"https://www.cms.gov/medicare/payment/fee-schedules/physician/{year}",
                        ingested_at=datetime.now(UTC),
                        file_date=datetime(year, 1, 1),
                    ))
                    count += 1
            except ValueError:
                pass

            try:
                f_rvu = float(f_rvu_str)
                if f_rvu > 0:
                    f_payment = round(f_rvu * conv_factor, 2)
                    batch.append(PriceData(
                        provider_name="Medicare",
                        service_code=hcpcs,
                        service_description=description[:500],
                        price=f_payment,
                        channel="medicare_facility_na_payment",
                        source=PriceSource.medicare_physician_fee,
                        source_url=f"https://www.cms.gov/medicare/payment/fee-schedules/physician/{year}",
                        ingested_at=datetime.now(UTC),
                        file_date=datetime(year, 1, 1),
                    ))
                    count += 1
            except ValueError:
                pass

        if batch:
            db.bulk_save_objects(batch)
            db.commit()

        offset += rows_in_page
        logger.info(f"  PFS: {count} records ingested (offset {offset})")

        if rows_in_page < page_size:
            break

    logger.info(f"Medicare PFS ingestion complete: {count} records")
    return count


# ---------------------------------------------------------------------------
# Hospital Price Transparency Files
# ---------------------------------------------------------------------------

# Top US hospitals with known MRF URLs. Each hospital publishes their MRF
# at a URL specified in a cms-hpt.txt file on their website root.
# This list is curated from the largest hospitals by revenue.
# Format: (name, state, mrf_url)
HOSPITAL_MRF_URLS: list[tuple[str, str, str]] = [
    # These URLs point to real hospital transparency files.
    # Many hospitals use third-party hosting (e.g., Turquoise Health, Clarify Health).
    # The CMS requires each hospital to publish a cms-hpt.txt file at their domain root
    # pointing to their MRF. We can discover URLs by checking {domain}/cms-hpt.txt.
    #
    # For now, we use a curated list of known URLs for the largest hospitals.
    # This list should be expanded as we discover more URLs.
]


def discover_hospital_mrf_url(domain: str) -> str | None:
    """Attempt to discover a hospital's MRF URL from their cms-hpt.txt file.

    CMS requires hospitals to place a machine-readable TXT file at the root
    of their domain. The file can be in two formats:
    1. Key-value text (location-name/source-page-url/mrf-url per entry)
    2. JSON with hospital_location array

    Returns the first MRF URL found, or None.
    """
    for prefix in ["", "www."]:
        txt_url = f"https://{prefix}{domain}/cms-hpt.txt"
        try:
            resp = httpx.get(txt_url, timeout=httpx.Timeout(10.0), follow_redirects=True)
            if resp.status_code != 200:
                continue

            content = resp.text.strip()

            # Try JSON format first
            if content.startswith("{") or content.startswith("["):
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        for entry in data.get("hospital_location", []):
                            for source in entry.get("hospital_mrf_source", []):
                                url = source.get("hospital_mrf_url")
                                if url:
                                    return url
                except json.JSONDecodeError:
                    pass

            # Key-value text format: look for mrf-url lines
            for line in content.split("\n"):
                line = line.strip()
                if line.lower().startswith("mrf-url:"):
                    url = line.split(":", 1)[1].strip() if ":" in line else ""
                    # Handle "mrf-url: https://..." and "mrf-url:https://..."
                    if not url and "http" in line:
                        url = "http" + line.split("http", 1)[1]
                    if url and url.startswith("http"):
                        return url

        except Exception:
            pass

    return None


# Top hospital domains for MRF discovery
HOSPITAL_DOMAINS = [
    ("Mayo Clinic", "MN", "mayoclinic.org"),
    ("Cleveland Clinic", "OH", "clevelandclinic.org"),
    ("Johns Hopkins Hospital", "MD", "hopkinsmedicine.org"),
    ("Massachusetts General Hospital", "MA", "massgeneral.org"),
    ("UCLA Medical Center", "CA", "uclahealth.org"),
    ("NYU Langone Hospitals", "NY", "nyulangone.org"),
    ("Northwestern Memorial Hospital", "IL", "nm.org"),
    ("Cedars-Sinai Medical Center", "CA", "cedars-sinai.org"),
    ("Mount Sinai Hospital", "NY", "mountsinai.org"),
    ("Stanford Health Care", "CA", "stanfordhealthcare.org"),
    ("Duke University Hospital", "NC", "dukehealth.org"),
    ("Hospital of the Univ of Pennsylvania", "PA", "pennmedicine.org"),
    ("Houston Methodist Hospital", "TX", "houstonmethodist.org"),
    ("Emory University Hospital", "GA", "emoryhealthcare.org"),
    ("Vanderbilt University Medical Center", "TN", "vumc.org"),
    ("University of Michigan Medical Center", "MI", "uofmhealth.org"),
    ("Yale New Haven Hospital", "CT", "ynhh.org"),
    ("UCSF Medical Center", "CA", "ucsfhealth.org"),
    ("Scripps La Jolla Hospital", "CA", "scripps.org"),
    ("Tampa General Hospital", "FL", "tgh.org"),
    ("Oregon Health & Science University", "OR", "ohsu.edu"),
    ("Intermountain Medical Center", "UT", "intermountainhealthcare.org"),
    ("Baylor University Medical Center", "TX", "bswhealth.com"),
    ("UF Health Shands Hospital", "FL", "ufhealth.org"),
    ("University of Colorado Hospital", "CO", "uchealth.org"),
    ("Memorial Hermann Texas Medical Center", "TX", "memorialhermann.org"),
    ("Ochsner Medical Center", "LA", "ochsner.org"),
    ("Atrium Health Carolinas Medical Center", "NC", "atriumhealth.org"),
    ("Hackensack Meridian Health", "NJ", "hackensackmeridianhealth.org"),
    ("Inova Fairfax Medical Campus", "VA", "inova.org"),
]


def download_hospital_transparency(db: Session, max_hospitals: int = 30) -> int:
    """Download hospital MRF files by discovering URLs from cms-hpt.txt.

    Attempts to download from the top hospitals. Real hospital MRF files
    can be very large (100MB+), so we limit concurrent downloads and
    use streaming where possible.
    """
    logger.info(f"Discovering and downloading hospital MRF files (max {max_hospitals})...")

    total = 0
    hospitals_ingested = 0

    for name, state, domain in HOSPITAL_DOMAINS[:max_hospitals]:
        logger.info(f"  Discovering MRF for {name} ({domain})...")
        mrf_url = discover_hospital_mrf_url(domain)

        if not mrf_url:
            logger.warning(f"  No MRF URL found for {name}")
            continue

        logger.info(f"  Found MRF: {mrf_url}")

        try:
            resp = httpx.get(
                mrf_url,
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                headers={"Accept": "text/csv, application/json, */*"},
            )
            resp.raise_for_status()
            content = resp.text
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"  Failed to download MRF for {name}: {e}")
            continue

        # Detect format and parse
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type or mrf_url.endswith(".json") or content.lstrip().startswith("{"):
            from app.services.data_ingestion import ingest_hospital_transparency_json
            count = ingest_hospital_transparency_json(db, content, name, mrf_url, state)
        else:
            from app.services.data_ingestion import ingest_hospital_transparency_csv
            count = ingest_hospital_transparency_csv(db, content, name, mrf_url, state)

        total += count
        hospitals_ingested += 1
        logger.info(f"  Ingested {count} records from {name}")

    logger.info(f"Hospital transparency ingestion complete: {total} records from {hospitals_ingested} hospitals")
    return total


# ---------------------------------------------------------------------------
# CMS Hospital Compare Quality Data
# ---------------------------------------------------------------------------

HOSPITAL_COMPARE_API = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"


def download_hospital_compare(db: Session) -> int:
    """Download CMS Hospital Compare quality data.

    Contains hospital quality ratings, safety scores, and patient experience
    data for ~5,400 US hospitals. Paginates through the full dataset.
    Feeds F4 (provider selection) and F8.
    """
    logger.info("Downloading CMS Hospital Compare quality data (full dataset)...")

    # Clear old quality records first
    db.execute(
        delete(PriceData).where(
            PriceData.source == PriceSource.other,
            PriceData.channel == "cms_quality_rating",
        )
    )
    db.commit()

    count = 0
    offset = 0
    page_size = 500

    while True:
        try:
            resp = httpx.get(
                HOSPITAL_COMPARE_API,
                params={
                    "offset": offset,
                    "count": "true",
                    "results": "true",
                    "format": "csv",
                    "limit": page_size,
                },
                timeout=httpx.Timeout(30.0, read=120.0),
            )
            if resp.status_code == 400:
                resp = httpx.get(
                    HOSPITAL_COMPARE_API,
                    params={"offset": offset, "count": "true", "results": "true", "format": "csv"},
                    timeout=httpx.Timeout(30.0, read=120.0),
                )
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"Hospital Compare download failed at offset {offset}: {e}")
            break

        reader = csv.DictReader(io.StringIO(resp.text))
        batch = []
        rows_in_page = 0

        for row in reader:
            rows_in_page += 1
            facility_id = (row.get("Facility ID") or "").strip()
            facility_name = (row.get("Facility Name") or "").strip()
            state = (row.get("State") or "").strip()
            overall_rating = (row.get("Hospital overall rating") or "").strip()

            if not facility_id or not facility_name:
                continue

            batch.append(PriceData(
                provider_name=facility_name,
                provider_npi=facility_id,
                service_code=f"QUALITY_RATING_{overall_rating}" if overall_rating else "QUALITY_RATING_NA",
                service_description=f"CMS Hospital Overall Rating: {overall_rating}/5" if overall_rating else "No rating",
                price=float(overall_rating) if overall_rating and overall_rating.isdigit() else 0,
                channel="cms_quality_rating",
                source=PriceSource.other,
                source_url="https://data.cms.gov/provider-data/dataset/xubh-q36u",
                state=state if len(state) == 2 else None,
                ingested_at=datetime.now(UTC),
            ))
            count += 1

        if batch:
            db.bulk_save_objects(batch)
            db.commit()

        offset += rows_in_page
        logger.info(f"  Hospital Compare: {count} hospitals (offset {offset})")

        if rows_in_page < page_size:
            break

    logger.info(f"Hospital Compare ingestion complete: {count} hospitals")
    return count


# ---------------------------------------------------------------------------
# CMS Quality Program Benchmarks (mental health, chronic care, preventive)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CMS OPPS Addendum B (Hospital Outpatient Payment Rates)
# ---------------------------------------------------------------------------

OPPS_ADDENDUM_B_URLS = [
    "https://www.cms.gov/files/zip/july-2025-opps-addendum-b.zip",
    "https://www.cms.gov/files/zip/april-2025-opps-addendum-b.zip",
    "https://www.cms.gov/files/zip/january-2025-opps-addendum-b.zip",
]


def download_opps_rates(db: Session) -> int:
    """Download CMS OPPS Addendum B — hospital outpatient payment rates.

    Contains ~6,600 HCPCS codes with Medicare outpatient payment rates.
    The file is an Excel spreadsheet inside a ZIP archive.
    """
    logger.info("Downloading CMS OPPS Addendum B rates...")

    content = None
    for url in OPPS_ADDENDUM_B_URLS:
        try:
            resp = httpx.get(url, timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True)
            resp.raise_for_status()
            content = resp.content
            logger.info(f"Downloaded OPPS from {url} ({len(content)} bytes)")
            break
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"OPPS download failed from {url}: {e}")
            continue

    if not content:
        logger.error("All OPPS download URLs failed")
        return 0

    import openpyxl

    z = zipfile.ZipFile(io.BytesIO(content))
    xlsx_names = [n for n in z.namelist() if n.endswith(".xlsx")]
    if not xlsx_names:
        logger.error("No .xlsx file found in OPPS zip")
        return 0

    with z.open(xlsx_names[0]) as f:
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True)
        ws = wb[wb.sheetnames[0]]

        # Clear old OPPS records
        db.execute(delete(PriceData).where(PriceData.source == PriceSource.medicare_outpatient))
        db.commit()

        count = 0
        batch = []
        header_found = False

        for row in ws.iter_rows(values_only=True):
            cells = list(row)
            if not header_found:
                if cells and str(cells[0]).strip() == "HCPCS Code":
                    header_found = True
                continue

            if len(cells) < 6:
                continue

            hcpcs = str(cells[0]).strip() if cells[0] else ""
            desc = str(cells[1]).strip() if cells[1] else ""
            payment = cells[5]

            if not hcpcs or not payment:
                continue

            try:
                price = float(payment)
                if price > 0:
                    batch.append(PriceData(
                        provider_name="Medicare OPPS",
                        service_code=hcpcs,
                        service_description=desc[:500],
                        price=price,
                        channel="medicare_opps_payment_rate",
                        source=PriceSource.medicare_outpatient,
                        source_url=OPPS_ADDENDUM_B_URLS[0],
                        ingested_at=datetime.now(UTC),
                        file_date=datetime(2025, 7, 1),
                    ))
                    count += 1
            except (ValueError, TypeError):
                pass

        if batch:
            db.bulk_save_objects(batch)
            db.commit()

    logger.info(f"OPPS ingestion complete: {count} outpatient payment rates")
    return count


# ---------------------------------------------------------------------------
# CMS IPPS Inpatient DRG Rates (Table 5)
# ---------------------------------------------------------------------------

IPPS_TABLE5_URL = "https://www.cms.gov/files/zip/fy-2025-ipps-final-rule-table-5.zip"
IPPS_BASE_RATE_FY2025 = 6378.14  # FY 2025 national standardized operating amount


def download_ipps_rates(db: Session) -> int:
    """Download CMS IPPS Table 5 — MS-DRG relative weights for inpatient services.

    Payment = MS-DRG weight × IPPS base rate.
    Contains ~800 MS-DRG codes covering all inpatient hospital stays.
    """
    logger.info("Downloading CMS IPPS Table 5 (inpatient DRG rates)...")

    try:
        resp = httpx.get(IPPS_TABLE5_URL, timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.error(f"IPPS Table 5 download failed: {e}")
        return 0

    import openpyxl

    z = zipfile.ZipFile(io.BytesIO(resp.content))
    xlsx_names = [n for n in z.namelist() if n.endswith(".xlsx")]
    if not xlsx_names:
        logger.error("No .xlsx file in IPPS zip")
        return 0

    with z.open(xlsx_names[0]) as f:
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True)
        ws = wb[wb.sheetnames[0]]

        db.execute(delete(PriceData).where(PriceData.source == PriceSource.medicare_inpatient))
        db.commit()

        count = 0
        batch = []
        header_found = False

        for row in ws.iter_rows(values_only=True):
            cells = list(row)
            if not header_found:
                if cells and str(cells[0]).strip().startswith("MS-DRG"):
                    header_found = True
                continue

            if len(cells) < 8:
                continue

            drg = str(cells[0]).strip() if cells[0] else ""
            title = str(cells[5]).strip() if cells[5] else ""
            weight_str = str(cells[7]).strip() if cells[7] else ""  # Weights with cap applied

            if not drg or not weight_str:
                continue

            try:
                weight = float(weight_str)
                if weight > 0:
                    payment = round(weight * IPPS_BASE_RATE_FY2025, 2)
                    batch.append(PriceData(
                        provider_name="Medicare IPPS",
                        service_code=f"DRG-{drg}",
                        service_description=title[:500],
                        price=payment,
                        channel="medicare_ipps_drg_payment",
                        source=PriceSource.medicare_inpatient,
                        source_url=IPPS_TABLE5_URL,
                        ingested_at=datetime.now(UTC),
                        file_date=datetime(2025, 10, 1),
                    ))
                    count += 1
            except (ValueError, TypeError):
                pass

        if batch:
            db.bulk_save_objects(batch)
            db.commit()

    logger.info(f"IPPS ingestion complete: {count} DRG payment rates")
    return count


QPP_BENCHMARKS_URL = "https://qpp.cms.gov/api/frontend/benchmarks-csv/quality/2025"


def download_quality_benchmarks(db: Session) -> int:
    """Download CMS Quality Payment Program benchmarks.

    Contains quality measure benchmarks across specialties including
    mental health, preventive care, chronic disease management.
    Feeds F1 (clinical quality) and F8 (cross-type patterns).
    """
    logger.info("Downloading CMS QPP Quality Benchmarks...")

    try:
        resp = httpx.get(QPP_BENCHMARKS_URL, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.error(f"QPP benchmarks download failed: {e}")
        return 0

    reader = csv.DictReader(io.StringIO(resp.text))
    count = 0
    batch = []

    for row in reader:
        measure_id = (row.get("Measure ID") or "").strip()
        measure_title = (row.get("Measure Title") or "").strip()
        measure_type = (row.get("Measure Type") or "").strip()
        avg_rate = (row.get("Average Performance Rate") or "").strip()

        if not measure_id:
            continue

        try:
            rate = float(avg_rate) if avg_rate else 0
        except ValueError:
            rate = 0

        batch.append(PriceData(
            provider_name="CMS QPP",
            service_code=measure_id,
            service_description=f"{measure_title} ({measure_type})"[:500],
            price=rate,
            channel="qpp_quality_benchmark",
            source=PriceSource.other,
            source_url=QPP_BENCHMARKS_URL,
            ingested_at=datetime.now(UTC),
        ))
        count += 1

    if batch:
        db.execute(
            delete(PriceData).where(
                PriceData.source == PriceSource.other,
                PriceData.channel == "qpp_quality_benchmark",
            )
        )
        db.bulk_save_objects(batch)
        db.commit()

    logger.info(f"QPP quality benchmarks ingestion complete: {count} measures")
    return count


# ---------------------------------------------------------------------------
# State Medicaid Dental Fee Schedules
# ---------------------------------------------------------------------------

# Publicly downloadable state Medicaid dental fee schedules (CDT codes)
MEDICAID_DENTAL_URLS = [
    ("MD", "https://health.maryland.gov/mmcp/Documents/2025%20Dental%20Fee%20Schedule%20and%20Procedure%20Codes.pdf"),
    # PDF-based — requires parsing. For now, we use CMS quality data for dental.
    # More states can be added as their CSV/structured data becomes available.
]


# ---------------------------------------------------------------------------
# Insurer Transparency in Coverage (TIC) Files
# ---------------------------------------------------------------------------

UHC_BLOBS_API = "https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/"


def download_insurer_tic(db: Session, max_files: int = 5) -> int:
    """Download insurer Transparency in Coverage in-network rate files.

    Starts with UnitedHealthcare (largest market share). Downloads small
    in-network rate files (< 50MB compressed) to stay within local storage
    constraints. Each file contains negotiated rates by CPT/HCPCS code.

    The Constitution requires ingesting all legally accessible pricing data.
    TIC files are legally required to be published and freely accessible.
    """
    logger.info("Downloading insurer TIC files (UnitedHealthcare)...")

    # Get the blob index
    try:
        resp = httpx.get(UHC_BLOBS_API, timeout=httpx.Timeout(30.0, read=180.0), follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError) as e:
        logger.error(f"UHC blob index download failed: {e}")
        return 0

    blobs = data.get("blobs", [])
    logger.info(f"UHC blob index: {len(blobs)} entries")

    # Find small in-network rate files (< 20MB based on filename patterns)
    # Smaller specialty files are manageable; large hospital network files are GB+
    in_network_blobs = [
        b for b in blobs
        if "in-network-rates" in b.get("name", "")
        and b.get("downloadUrl")
    ]
    logger.info(f"In-network rate files: {len(in_network_blobs)}")

    # Clear old TIC records
    db.execute(delete(PriceData).where(PriceData.source == PriceSource.insurer_transparency))
    db.commit()

    total = 0
    files_processed = 0

    for blob in in_network_blobs[:max_files]:
        name = blob["name"]
        url = blob["downloadUrl"]
        logger.info(f"  Downloading: {name[:80]}...")

        try:
            # Check size first
            head = httpx.head(url, timeout=10, follow_redirects=True)
            size = int(head.headers.get("content-length", 0))
            if size > 50 * 1024 * 1024:  # Skip files > 50MB compressed
                logger.info(f"  Skipping {name}: {size/1024/1024:.0f}MB (too large for local ingestion)")
                continue

            resp = httpx.get(url, timeout=httpx.Timeout(30.0, read=300.0), follow_redirects=True)
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"  Failed to download {name}: {e}")
            continue

        # Decompress gzip
        import gzip
        try:
            content = gzip.decompress(resp.content)
            data = json.loads(content)
        except (gzip.BadGzipFile, json.JSONDecodeError) as e:
            logger.warning(f"  Failed to parse {name}: {e}")
            continue

        # Parse in-network rates
        batch = []
        reporting_entity = data.get("reporting_entity_name", "UnitedHealthcare")

        for entry in data.get("in_network", []):
            billing_code = (entry.get("billing_code") or "").strip()
            code_type = (entry.get("billing_code_type") or "").strip()
            description = (entry.get("description") or entry.get("name") or "").strip()

            if not billing_code:
                continue

            for rate_group in entry.get("negotiated_rates", []):
                for price_info in rate_group.get("negotiated_prices", []):
                    rate = price_info.get("negotiated_rate")
                    if not rate or rate <= 0:
                        continue

                    neg_type = price_info.get("negotiated_type", "negotiated")
                    setting = price_info.get("setting", "")
                    billing_class = price_info.get("billing_class", "")

                    batch.append(PriceData(
                        provider_name=reporting_entity,
                        service_code=billing_code,
                        service_description=description[:500],
                        price=float(rate),
                        channel=f"negotiated_{neg_type}_{setting}_{billing_class}".rstrip("_"),
                        source=PriceSource.insurer_transparency,
                        source_url=url[:500],
                        ingested_at=datetime.now(UTC),
                    ))
                    total += 1

                    # Batch commit to avoid memory issues
                    if len(batch) >= 10000:
                        db.bulk_save_objects(batch)
                        db.commit()
                        batch = []

        if batch:
            db.bulk_save_objects(batch)
            db.commit()

        files_processed += 1
        logger.info(f"  Ingested from {name}: {total} total records so far")

    logger.info(f"TIC ingestion complete: {total} records from {files_processed} files")
    return total


def download_medicaid_dental(db: Session) -> int:
    """Download state Medicaid dental fee schedules where available.

    Dental pricing is fragmented across state Medicaid programs.
    Most publish PDF-only fee schedules. We ingest what's available
    in structured format and document the barrier for PDF-only states.

    BARRIER: Most state Medicaid dental fee schedules are published as
    PDF-only, requiring OCR/parsing. The ADA ended its national fee survey
    in 2022. No centralized public dental pricing database exists.
    This is a data availability barrier, not a legal or physical one —
    we can and should build PDF parsers for each state.
    """
    logger.info("Dental fee schedule ingestion: limited by PDF-only publication format")
    logger.info("States with structured dental data: searching...")

    # For now, return 0 — dental pricing requires PDF parsing per state
    # This is documented as an active gap to close
    return 0


# ---------------------------------------------------------------------------
# Run all downloaders
# ---------------------------------------------------------------------------

def run_full_ingestion(db: Session) -> dict:
    """Run all public data downloaders. Returns stats per source."""
    results = {}

    try:
        results["nadac"] = download_nadac(db)
    except Exception as e:
        logger.error(f"NADAC ingestion failed: {e}")
        results["nadac"] = 0

    try:
        results["medicare_pfs"] = download_medicare_pfs(db)
    except Exception as e:
        logger.error(f"Medicare PFS ingestion failed: {e}")
        results["medicare_pfs"] = 0

    try:
        results["hospital_transparency"] = download_hospital_transparency(db)
    except Exception as e:
        logger.error(f"Hospital transparency ingestion failed: {e}")
        results["hospital_transparency"] = 0

    try:
        results["opps_rates"] = download_opps_rates(db)
    except Exception as e:
        logger.error(f"OPPS ingestion failed: {e}")
        results["opps_rates"] = 0

    try:
        results["ipps_rates"] = download_ipps_rates(db)
    except Exception as e:
        logger.error(f"IPPS ingestion failed: {e}")
        results["ipps_rates"] = 0

    try:
        results["hospital_compare"] = download_hospital_compare(db)
    except Exception as e:
        logger.error(f"Hospital Compare ingestion failed: {e}")
        results["hospital_compare"] = 0

    try:
        results["quality_benchmarks"] = download_quality_benchmarks(db)
    except Exception as e:
        logger.error(f"Quality benchmarks ingestion failed: {e}")
        results["quality_benchmarks"] = 0

    try:
        results["insurer_tic"] = download_insurer_tic(db)
    except Exception as e:
        logger.error(f"Insurer TIC ingestion failed: {e}")
        results["insurer_tic"] = 0

    return results
