"""CMS Insurer MRF (Machine-Readable File) Index Ingestion (Function 8).

Constitution F8: "Is there any publicly available data source the system
does not ingest?"

CMS publishes a master index of all insurer Transparency in Coverage
Machine-Readable Files at:
  https://transparency-in-coverage.cms.gov/

The full MRF files are 10-100GB each. This module:
1. Ingests the CMS MRF index (list of all insurer MRF file locations)
2. Records the number of insurers with published MRFs
3. For one small insurer, attempts to download and parse a sample
4. Documents disk space barriers for large files
"""

import csv
import gzip
import io
import json
import logging
import os
from datetime import datetime, UTC
from typing import Optional

import httpx
from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)

# CMS Transparency in Coverage index
# The index itself is a JSON file listing all insurer MRF URLs
CMS_TIC_INDEX_URL = "https://transparency-in-coverage.cms.gov/"

# Known insurer MRF index URLs from CMS
# Each insurer publishes a table_of_contents JSON pointing to their MRF files
KNOWN_INSURER_MRF_INDEXES = [
    {
        "insurer": "United Healthcare",
        "index_url": "https://transparency-in-coverage.uhc.com/",
        "size_estimate": "very_large",
    },
    {
        "insurer": "Anthem / Elevance",
        "index_url": "https://antm-pt-prod-dataz-nogbd-nophi-us-east1.s3.amazonaws.com/anthem/",
        "size_estimate": "very_large",
    },
    {
        "insurer": "Aetna / CVS Health",
        "index_url": "https://health1.aetna.com/app/public/#/one/insurerCode498702",
        "size_estimate": "very_large",
    },
    {
        "insurer": "Cigna",
        "index_url": "https://www.cigna.com/legal/compliance/machine-readable-files",
        "size_estimate": "very_large",
    },
    {
        "insurer": "Humana",
        "index_url": "https://developers.humana.com/apis/mrf",
        "size_estimate": "very_large",
    },
    {
        "insurer": "BCBS (various)",
        "index_url": "https://www.bcbs.com/smarter-health-care/interoperability/transparency-in-coverage",
        "size_estimate": "very_large",
    },
    {
        "insurer": "Kaiser Permanente",
        "index_url": "https://healthy.kaiserpermanente.org/front-door/machine-readable",
        "size_estimate": "large",
    },
    {
        "insurer": "Molina Healthcare",
        "index_url": "https://www.molinahealthcare.com/members/common/en-us/abt/interop/mrfinfo.aspx",
        "size_estimate": "medium",
    },
    {
        "insurer": "Centene / WellCare",
        "index_url": "https://www.centene.com/price-transparency.html",
        "size_estimate": "large",
    },
    {
        "insurer": "Oscar Health",
        "index_url": "https://www.hioscar.com/transparency-in-coverage",
        "size_estimate": "small",
    },
    {
        "insurer": "Bright Health",
        "index_url": "https://www.brighthealthplan.com/transparency-in-coverage",
        "size_estimate": "small",
    },
    {
        "insurer": "Friday Health Plans",
        "index_url": "https://www.fridayhealthplans.com/transparency-in-coverage",
        "size_estimate": "small",
    },
    {
        "insurer": "Clover Health",
        "index_url": "https://www.cloverhealth.com/en/transparency-in-coverage",
        "size_estimate": "small",
    },
    {
        "insurer": "Alignment Healthcare",
        "index_url": "https://www.alignmenthealthcare.com/transparency-in-coverage",
        "size_estimate": "small",
    },
    {
        "insurer": "Ambetter (Centene)",
        "index_url": "https://ambfrm.centene.com/mrf",
        "size_estimate": "medium",
    },
]

# Highmark MRF Hub provides direct JSON index access for BCBS affiliates
# The mrfdata.hmhs.com portal publishes CMS-format table-of-contents JSON
# files with signed S3 URLs to actual in-network rate files.
# URL pattern: /files/{region_code}/{state}/inbound/local/{YYYY-MM-DD}_{insurer}_index.json
HIGHMARK_MRF_HUB_INSURERS = [
    {"region_code": "460", "state": "wy", "name": "Blue Cross Blue Shield of Wyoming",
     "template": "{date}_Blue_Cross_Blue_Shield_of_Wyoming_index.json"},
    {"region_code": "320", "state": "nd", "name": "Blue Cross Blue Shield of North Dakota",
     "template": "{date}_BlueCrossBlueShieldND_index.json"},
    {"region_code": "070", "state": "del", "name": "Highmark Blue Cross Blue Shield Delaware",
     "template": "{date}_Highmark_Blue_Cross_Blue_Shield_of_Delaware_index.json"},
]


def probe_mrf_index(url: str) -> dict:
    """Probe an insurer's MRF index URL to check availability and discover files.

    Returns metadata about what's available at the URL without downloading
    full MRF files.
    """
    result = {
        "url": url,
        "accessible": False,
        "status_code": None,
        "content_type": None,
        "mrf_files_found": [],
        "table_of_contents_found": False,
        "estimated_total_size": None,
        "barrier": None,
    }

    try:
        # First try a HEAD request to check availability
        resp = httpx.head(
            url,
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            headers={
                "User-Agent": "BeneFlex-DataPipeline/1.0 (healthcare price transparency)",
                "Accept": "application/json, text/html, */*",
            },
        )
        result["status_code"] = resp.status_code
        result["content_type"] = resp.headers.get("content-type", "")

        if resp.status_code == 200:
            result["accessible"] = True

            # Try GET for small responses to find table_of_contents
            resp2 = httpx.get(
                url,
                timeout=httpx.Timeout(30.0, read=60.0),
                follow_redirects=True,
                headers={
                    "User-Agent": "BeneFlex-DataPipeline/1.0",
                    "Accept": "application/json, text/html, */*",
                },
            )

            content_length = len(resp2.content)

            # If response is small enough, try to parse
            if content_length < 10_000_000:  # 10MB limit for index parsing
                content_type = resp2.headers.get("content-type", "")

                if "json" in content_type or resp2.text.lstrip().startswith("{"):
                    try:
                        data = resp2.json()
                        mrf_urls = _extract_mrf_urls_from_index(data)
                        result["table_of_contents_found"] = True
                        result["mrf_files_found"] = mrf_urls[:100]  # Cap at 100
                    except Exception:
                        pass
                elif "html" in content_type:
                    # Parse HTML for MRF links
                    mrf_links = _extract_mrf_links_from_html(resp2.text)
                    if mrf_links:
                        result["mrf_files_found"] = mrf_links[:100]
            else:
                result["barrier"] = (
                    f"Index file is {content_length / 1_000_000:.1f}MB — "
                    f"too large to parse in memory"
                )

        elif resp.status_code == 403:
            result["barrier"] = "HTTP 403 Forbidden — insurer blocks automated access"
        elif resp.status_code == 404:
            result["barrier"] = "HTTP 404 — MRF index URL not found or moved"
        else:
            result["barrier"] = f"HTTP {resp.status_code}"

    except httpx.TimeoutException:
        result["barrier"] = "Timeout connecting to insurer MRF index"
    except httpx.ConnectError as e:
        result["barrier"] = f"Connection error: {e}"
    except Exception as e:
        result["barrier"] = f"Error probing: {type(e).__name__}: {e}"

    return result


def _extract_mrf_urls_from_index(data: dict) -> list[dict]:
    """Extract MRF file URLs from a CMS-format table of contents JSON."""
    mrf_files = []

    # Standard CMS TiC JSON format
    if "reporting_entity_name" in data:
        for plan in data.get("reporting_structure", []):
            for mrf in plan.get("in_network_files", []):
                mrf_files.append({
                    "description": mrf.get("description", ""),
                    "location": mrf.get("location", ""),
                    "file_type": "in_network",
                })
            for mrf in plan.get("allowed_amount_files", []):
                mrf_files.append({
                    "description": mrf.get("description", ""),
                    "location": mrf.get("location", ""),
                    "file_type": "allowed_amount",
                })

    # Alternative: flat list of URLs
    elif isinstance(data, list):
        for item in data[:500]:
            if isinstance(item, str) and item.startswith("http"):
                mrf_files.append({"location": item, "description": "", "file_type": "unknown"})
            elif isinstance(item, dict):
                url = item.get("url") or item.get("location") or item.get("href")
                if url:
                    mrf_files.append({
                        "location": url,
                        "description": item.get("description", ""),
                        "file_type": item.get("type", "unknown"),
                    })

    return mrf_files


def _extract_mrf_links_from_html(html: str) -> list[dict]:
    """Extract MRF file links from an HTML page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(ext in href.lower() for ext in [".json", ".json.gz", ".csv", ".csv.gz", ".zip"]):
            links.append({
                "location": href,
                "description": a.get_text(strip=True)[:200],
                "file_type": "from_html",
            })

    return links


def ingest_mrf_index(db: Session, max_sample_size_mb: int = 50) -> dict:
    """Ingest the CMS MRF index and attempt to sample one small insurer's data.

    Steps:
    1. Probe all known insurer MRF index URLs
    2. Record the number of insurers with published MRFs
    3. For one small insurer, attempt to download and parse a sample
    4. Document disk space barriers for large files

    Returns a comprehensive report.
    """
    logger.info("Starting CMS MRF index ingestion...")

    report = {
        "insurers_probed": 0,
        "insurers_accessible": 0,
        "insurers_with_mrf_files": 0,
        "total_mrf_files_discovered": 0,
        "sample_insurer": None,
        "sample_records_ingested": 0,
        "disk_space_available_gb": None,
        "barriers": [],
        "insurer_reports": [],
        "ingested_at": datetime.now(UTC).isoformat(),
    }

    # Check available disk space
    try:
        import shutil
        disk = shutil.disk_usage("/")
        report["disk_space_available_gb"] = round(disk.free / (1024**3), 2)
    except Exception:
        pass

    # Probe each insurer
    small_insurer_to_sample = None

    for insurer_info in KNOWN_INSURER_MRF_INDEXES:
        report["insurers_probed"] += 1
        insurer_name = insurer_info["insurer"]

        logger.info(f"  Probing MRF index for {insurer_name}...")
        probe_result = probe_mrf_index(insurer_info["index_url"])

        insurer_report = {
            "insurer": insurer_name,
            "index_url": insurer_info["index_url"],
            "size_estimate": insurer_info["size_estimate"],
            "accessible": probe_result["accessible"],
            "status_code": probe_result["status_code"],
            "mrf_files_found": len(probe_result["mrf_files_found"]),
            "table_of_contents_found": probe_result["table_of_contents_found"],
            "barrier": probe_result["barrier"],
        }
        report["insurer_reports"].append(insurer_report)

        if probe_result["accessible"]:
            report["insurers_accessible"] += 1

        if probe_result["mrf_files_found"]:
            report["insurers_with_mrf_files"] += 1
            report["total_mrf_files_discovered"] += len(probe_result["mrf_files_found"])

            # Store MRF index entries in price_data
            for mrf_file in probe_result["mrf_files_found"][:50]:
                db.add(PriceData(
                    provider_name=insurer_name,
                    service_code=f"MRF_INDEX_{mrf_file['file_type']}",
                    service_description=mrf_file.get("description", "")[:500] or f"MRF file: {insurer_name}",
                    price=0,  # Index entry, not a price
                    channel=f"mrf_index_{insurer_name.lower().replace(' ', '_')[:50]}",
                    source=PriceSource.insurer_mrf_index,
                    source_url=mrf_file.get("location", insurer_info["index_url"])[:500],
                    ingested_at=datetime.now(UTC),
                ))

            # Identify a small insurer for sampling
            if (insurer_info["size_estimate"] == "small"
                    and small_insurer_to_sample is None
                    and probe_result["mrf_files_found"]):
                small_insurer_to_sample = {
                    "insurer": insurer_name,
                    "mrf_files": probe_result["mrf_files_found"],
                }

        if probe_result["barrier"]:
            report["barriers"].append({
                "insurer": insurer_name,
                "barrier": probe_result["barrier"],
            })

    # Commit index entries
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to commit MRF index entries: {e}")

    # Attempt to sample one small insurer's MRF data
    if small_insurer_to_sample:
        report["sample_insurer"] = small_insurer_to_sample["insurer"]
        sample_count = _sample_insurer_mrf(
            db,
            small_insurer_to_sample["insurer"],
            small_insurer_to_sample["mrf_files"],
            max_sample_size_mb,
            report,
        )
        report["sample_records_ingested"] = sample_count
    else:
        report["barriers"].append({
            "insurer": "ALL",
            "barrier": (
                "No small insurer with accessible MRF files found for sampling. "
                "Large insurer MRF files are typically 10-100GB each, which exceeds "
                f"available disk space ({report['disk_space_available_gb']}GB free). "
                "Constitution documents this barrier: the data physically exists "
                "but file sizes exceed current machine capacity."
            ),
        })

    # Record pipeline metric
    _record_mrf_pipeline_metric(db, report)

    logger.info(
        f"MRF index ingestion complete: {report['insurers_accessible']}/{report['insurers_probed']} "
        f"accessible, {report['total_mrf_files_discovered']} MRF files discovered"
    )

    return report


def _sample_insurer_mrf(
    db: Session,
    insurer_name: str,
    mrf_files: list[dict],
    max_size_mb: int,
    report: dict,
) -> int:
    """Attempt to download and parse a sample MRF from a small insurer.

    Only downloads files under max_size_mb.
    """
    count = 0

    for mrf_file in mrf_files[:5]:  # Try up to 5 files
        url = mrf_file.get("location", "")
        if not url or not url.startswith("http"):
            continue

        logger.info(f"  Attempting to sample MRF from {insurer_name}: {url[:100]}...")

        try:
            # First check size with HEAD
            head = httpx.head(url, timeout=httpx.Timeout(15.0), follow_redirects=True)
            content_length = int(head.headers.get("content-length", 0))
            size_mb = content_length / (1024 * 1024)

            if content_length > 0 and size_mb > max_size_mb:
                report["barriers"].append({
                    "insurer": insurer_name,
                    "barrier": (
                        f"MRF file is {size_mb:.1f}MB (limit: {max_size_mb}MB). "
                        f"Constitution documents this disk space barrier."
                    ),
                })
                continue

            # Download the file
            resp = httpx.get(url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()

            content = resp.content
            actual_size_mb = len(content) / (1024 * 1024)

            if actual_size_mb > max_size_mb:
                report["barriers"].append({
                    "insurer": insurer_name,
                    "barrier": f"Downloaded MRF is {actual_size_mb:.1f}MB — exceeds limit",
                })
                continue

            # Try to decompress if gzipped
            text_content = None
            if url.endswith(".gz") or resp.headers.get("content-encoding") == "gzip":
                try:
                    text_content = gzip.decompress(content).decode("utf-8", errors="replace")
                except Exception:
                    text_content = content.decode("utf-8", errors="replace")
            else:
                text_content = content.decode("utf-8", errors="replace")

            # Parse as JSON (CMS MRF format)
            if text_content and (text_content.lstrip().startswith("{") or text_content.lstrip().startswith("[")):
                try:
                    data = json.loads(text_content)
                    count += _ingest_mrf_json(db, data, insurer_name, url)
                    if count > 0:
                        logger.info(f"  Sampled {count} records from {insurer_name} MRF")
                        break
                except json.JSONDecodeError as e:
                    logger.warning(f"  Failed to parse MRF JSON: {e}")

            # Try as CSV
            elif text_content:
                try:
                    reader = csv.DictReader(io.StringIO(text_content))
                    batch = []
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

                        batch.append(PriceData(
                            provider_name=insurer_name,
                            service_code=code[:20],
                            service_description=row.get("description", "")[:500],
                            price=price,
                            channel=f"insurer_mrf_{insurer_name.lower().replace(' ', '_')[:50]}",
                            source=PriceSource.insurer_transparency,
                            source_url=url[:500],
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

                    if count > 0:
                        break
                except Exception as e:
                    logger.warning(f"  Failed to parse MRF CSV: {e}")

        except httpx.TimeoutException:
            report["barriers"].append({
                "insurer": insurer_name,
                "barrier": f"Timeout downloading MRF from {url[:100]}",
            })
        except Exception as e:
            report["barriers"].append({
                "insurer": insurer_name,
                "barrier": f"Error sampling MRF: {type(e).__name__}: {e}",
            })

    return count


def _ingest_mrf_json(db: Session, data: dict, insurer_name: str, source_url: str) -> int:
    """Ingest a CMS-format MRF JSON file into price_data.

    CMS MRF JSON schema includes:
    - reporting_entity_name, reporting_entity_type
    - in_network array with negotiation_arrangement, negotiated_rates
    """
    count = 0
    batch = []

    # Standard CMS TiC in-network format
    in_network = data.get("in_network", [])
    if not in_network and isinstance(data, list):
        in_network = data

    for item in in_network[:10000]:  # Cap at 10k for sampling
        if not isinstance(item, dict):
            continue

        billing_code = item.get("billing_code", "")
        billing_code_type = item.get("billing_code_type", "")
        description = item.get("description", "") or item.get("name", "")

        for rate_group in item.get("negotiated_rates", [])[:20]:
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

                batch.append(PriceData(
                    provider_name=insurer_name,
                    service_code=billing_code[:20],
                    service_description=description[:500],
                    price=rate,
                    channel=f"insurer_mrf_negotiated_{billing_code_type.lower()[:30]}",
                    source=PriceSource.insurer_transparency,
                    source_url=source_url[:500],
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

    return count


def _record_mrf_pipeline_metric(db: Session, report: dict):
    """Record MRF index ingestion results as F8 pipeline metric."""
    from app.models.data_pipeline_metric import DataPipelineMetric

    metric = DataPipelineMetric(
        metric_type="insurer_mrf_index_ingestion",
        benefit_type="health",
        value=float(report["total_mrf_files_discovered"]),
        details={
            "insurers_probed": report["insurers_probed"],
            "insurers_accessible": report["insurers_accessible"],
            "insurers_with_mrf_files": report["insurers_with_mrf_files"],
            "total_mrf_files_discovered": report["total_mrf_files_discovered"],
            "sample_insurer": report["sample_insurer"],
            "sample_records_ingested": report["sample_records_ingested"],
            "disk_space_available_gb": report["disk_space_available_gb"],
            "barriers_count": len(report["barriers"]),
        },
        measured_at=datetime.now(UTC),
    )
    db.add(metric)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to record MRF metric: {e}")
