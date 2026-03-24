"""
NPPES Full Provider Database Ingestion
Downloads and ingests all ~7.4M providers from CMS NPPES bulk file.
Uses INSERT OR IGNORE to skip existing NPIs (UNIQUE constraint on npi column).
"""
import csv
import uuid
import os
import sys
import time
import zipfile

import httpx
from sqlalchemy import text

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.database import engine

URL = "https://download.cms.gov/nppes/NPPES_Data_Dissemination_March_2026_V2.zip"
ZIP_PATH = "/tmp/nppes_full.zip"
BATCH_SIZE = 10000

def download():
    if os.path.exists(ZIP_PATH):
        size = os.path.getsize(ZIP_PATH)
        print(f"Zip already exists: {size:,} bytes ({size/1e9:.2f} GB)")
        return
    print(f"Downloading from {URL} ...")
    start = time.time()
    with httpx.stream("GET", URL, timeout=600.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(ZIP_PATH, "wb") as f:
            for chunk in resp.iter_bytes(1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r  {downloaded/1e6:.0f}/{total/1e6:.0f} MB ({pct:.1f}%)", end="", flush=True)
    elapsed = time.time() - start
    print(f"\nDownload complete in {elapsed:.0f}s")

def extract_csv():
    print("Extracting CSV from zip ...")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().startswith("npidata") and n.endswith(".csv")]
        if not csv_names:
            print("ERROR: No npidata CSV found in zip. Contents:")
            for n in zf.namelist():
                print(f"  {n}")
            sys.exit(1)
        csv_name = csv_names[0]
        csv_path = f"/tmp/{csv_name}"
        if os.path.exists(csv_path):
            print(f"CSV already extracted: {csv_path} ({os.path.getsize(csv_path)/1e9:.2f} GB)")
            return csv_path
        print(f"Extracting {csv_name} ...")
        zf.extract(csv_name, "/tmp/")
        print(f"Extracted: {os.path.getsize(csv_path)/1e9:.2f} GB")
        return csv_path

def classify_provider(taxonomy):
    if not taxonomy:
        return "physician"
    if taxonomy.startswith("1223"):
        return "dental"
    if taxonomy.startswith("152"):
        return "vision"
    if taxonomy.startswith("101"):
        return "mental_health"
    if taxonomy.startswith("3336"):
        return "pharmacy"
    if taxonomy.startswith("28") or taxonomy.startswith("27"):
        return "hospital"
    if taxonomy.startswith("174"):
        return "lab"
    return "physician"

def ingest(csv_path):
    print("Starting ingestion ...")
    start = time.time()

    # Get current count
    with engine.connect() as conn:
        before = conn.execute(text("SELECT COUNT(*) FROM providers")).scalar()
    print(f"Providers before: {before:,}")

    with engine.connect() as conn:
        # Optimize SQLite for bulk insert
        conn.execute(text("PRAGMA synchronous = NORMAL"))
        conn.execute(text("PRAGMA cache_size = -64000"))  # 64MB cache
        conn.execute(text("PRAGMA temp_store = MEMORY"))
        conn.commit()

        batch = []
        total_processed = 0
        skipped = 0

        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                npi = row.get("NPI", "").strip()
                if not npi or len(npi) != 10:
                    skipped += 1
                    continue

                org_name = row.get("Provider Organization Name (Legal Business Name)", "").strip()
                last_name = row.get("Provider Last Name (Legal Name)", "").strip()
                first_name = row.get("Provider First Name", "").strip()
                name = org_name or f"{last_name} {first_name}".strip()
                if not name:
                    skipped += 1
                    continue

                state = row.get("Provider Business Practice Location Address State Name", "").strip()
                taxonomy = row.get("Healthcare Provider Taxonomy Code_1", "").strip()
                ptype = classify_provider(taxonomy)

                batch.append({
                    "id": str(uuid.uuid4()),
                    "npi": npi,
                    "name": name[:255],
                    "ptype": ptype,
                    "specs": f'["{taxonomy}"]' if taxonomy else "[]",
                    "state": state[:2] if state else None,
                })

                if len(batch) >= BATCH_SIZE:
                    conn.execute(
                        text(
                            "INSERT OR IGNORE INTO providers "
                            "(provider_id, npi, name, provider_type, specialties, state, outcome_data_points) "
                            "VALUES (:id, :npi, :name, :ptype, :specs, :state, 0)"
                        ),
                        batch,
                    )
                    conn.commit()
                    total_processed += len(batch)
                    batch = []
                    if total_processed % 500000 == 0:
                        elapsed = time.time() - start
                        rate = total_processed / elapsed
                        print(f"  Processed {total_processed:,} rows ({elapsed:.0f}s, {rate:.0f} rows/s)")

        # Final batch
        if batch:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO providers "
                    "(provider_id, npi, name, provider_type, specialties, state, outcome_data_points) "
                    "VALUES (:id, :npi, :name, :ptype, :specs, :state, 0)"
                ),
                batch,
            )
            conn.commit()
            total_processed += len(batch)

    elapsed = time.time() - start
    print(f"\nProcessed {total_processed:,} rows, skipped {skipped:,} invalid rows in {elapsed:.0f}s")

    # Final count
    with engine.connect() as conn:
        after = conn.execute(text("SELECT COUNT(*) FROM providers")).scalar()
    print(f"Providers after:  {after:,}")
    print(f"New providers:    {after - before:,}")
    print(f"Target: ~7.4M")

def cleanup(csv_path):
    if os.path.exists(csv_path):
        print(f"Removing extracted CSV: {csv_path}")
        os.remove(csv_path)
    # Keep zip for potential re-runs

if __name__ == "__main__":
    download()
    csv_path = extract_csv()
    ingest(csv_path)
    cleanup(csv_path)
    print("\nDone.")
