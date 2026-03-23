"""Seed the database with public pricing data.

Sources:
1. Hospital price transparency — generates realistic records for 150+ hospitals
   based on CMS-published data patterns. In production, these come from actual
   hospital MRF files; this script creates representative seed data matching
   the same schema so the benchmark tool returns meaningful results.

2. Medicare Physician Fee Schedule — generates records from the CMS PFS structure
   covering common CPT/HCPCS codes with realistic national payment amounts.

Run: cd beneflex && python -m scripts.seed_public_data
"""

import sys
import os
import uuid
import random
from datetime import datetime

# Add parent dir to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, engine, SessionLocal
from app.models import *  # noqa: F401,F403
from app.models.price_data import PriceData, PriceSource

# Create tables
Base.metadata.create_all(bind=engine)

# --- Hospital Data ---

# Top 150+ US hospitals by revenue (real hospital names and states)
HOSPITALS = [
    ("Mayo Clinic", "MN"), ("Cleveland Clinic", "OH"), ("Johns Hopkins Hospital", "MD"),
    ("Massachusetts General Hospital", "MA"), ("UCLA Medical Center", "CA"),
    ("UCSF Medical Center", "CA"), ("NYU Langone Hospitals", "NY"),
    ("Northwestern Memorial Hospital", "IL"), ("Cedars-Sinai Medical Center", "CA"),
    ("Mount Sinai Hospital", "NY"), ("Brigham and Women's Hospital", "MA"),
    ("Stanford Health Care", "CA"), ("Duke University Hospital", "NC"),
    ("Hospital of the University of Pennsylvania", "PA"), ("Barnes-Jewish Hospital", "MO"),
    ("UPMC Presbyterian", "PA"), ("Houston Methodist Hospital", "TX"),
    ("NY-Presbyterian Hospital Columbia", "NY"), ("Emory University Hospital", "GA"),
    ("Rush University Medical Center", "IL"), ("Vanderbilt University Medical Center", "TN"),
    ("University of Michigan Medical Center", "MI"), ("Oregon Health & Science University", "OR"),
    ("Yale New Haven Hospital", "CT"), ("Thomas Jefferson University Hospital", "PA"),
    ("Beaumont Hospital Royal Oak", "MI"), ("Spectrum Health Butterworth Hospital", "MI"),
    ("Abbott Northwestern Hospital", "MN"), ("Advocate Christ Medical Center", "IL"),
    ("Advocate Lutheran General Hospital", "IL"), ("Baylor University Medical Center", "TX"),
    ("Beth Israel Deaconess Medical Center", "MA"), ("Christiana Hospital", "DE"),
    ("Froedtert Hospital", "WI"), ("Good Samaritan Hospital", "OH"),
    ("Hartford Hospital", "CT"), ("Henry Ford Hospital", "MI"),
    ("Huntington Memorial Hospital", "CA"), ("Indiana University Health Methodist", "IN"),
    ("Intermountain Medical Center", "UT"), ("Kaiser Permanente Los Angeles", "CA"),
    ("Kaiser Permanente San Francisco", "CA"), ("Lahey Hospital", "MA"),
    ("Lenox Hill Hospital", "NY"), ("Maimonides Medical Center", "NY"),
    ("MedStar Georgetown University Hospital", "DC"), ("MedStar Washington Hospital Center", "DC"),
    ("Memorial Hermann Texas Medical Center", "TX"), ("Mercy Hospital Springfield", "MO"),
    ("Miami Valley Hospital", "OH"), ("Morristown Medical Center", "NJ"),
    ("Nebraska Medicine", "NE"), ("North Shore University Hospital", "NY"),
    ("Northwell Health Lenox Hill", "NY"), ("Ohio State University Wexner Medical Center", "OH"),
    ("Ochsner Medical Center", "LA"), ("Parkland Memorial Hospital", "TX"),
    ("Providence St Joseph Medical Center", "CA"), ("Regions Hospital", "MN"),
    ("Rhode Island Hospital", "RI"), ("Riverside Methodist Hospital", "OH"),
    ("Robert Wood Johnson University Hospital", "NJ"), ("Ronald Reagan UCLA Medical Center", "CA"),
    ("Scripps La Jolla Hospital", "CA"), ("Sentara Norfolk General Hospital", "VA"),
    ("Sharp Memorial Hospital", "CA"), ("St Luke's Hospital Boise", "ID"),
    ("St Vincent Indianapolis", "IN"), ("Tampa General Hospital", "FL"),
    ("The Christ Hospital", "OH"), ("Tufts Medical Center", "MA"),
    ("Tulane Medical Center", "LA"), ("UNC Medical Center", "NC"),
    ("University of Colorado Hospital", "CO"), ("University of Iowa Hospitals", "IA"),
    ("University of Kansas Hospital", "KS"), ("University of Kentucky Chandler Hospital", "KY"),
    ("University of Maryland Medical Center", "MD"), ("University of Virginia Medical Center", "VA"),
    ("University of Wisconsin Hospital", "WI"), ("UT Southwestern Medical Center", "TX"),
    ("Virginia Mason Medical Center", "WA"), ("Wake Forest Baptist Medical Center", "NC"),
    ("Wentworth-Douglass Hospital", "NH"), ("WellStar Kennestone Hospital", "GA"),
    ("Westchester Medical Center", "NY"), ("Yale New Haven St Raphael", "CT"),
    ("Zuckerberg San Francisco General", "CA"), ("Grady Memorial Hospital", "GA"),
    ("Harborview Medical Center", "WA"), ("Jackson Memorial Hospital", "FL"),
    ("LAC+USC Medical Center", "CA"), ("Medical University of South Carolina", "SC"),
    ("Montefiore Medical Center", "NY"), ("Mount Sinai West", "NY"),
    ("NewYork-Presbyterian Queens", "NY"), ("Providence Portland Medical Center", "OR"),
    ("Saint Barnabas Medical Center", "NJ"), ("SSM Health St Louis University Hospital", "MO"),
    ("Stony Brook University Hospital", "NY"), ("Strong Memorial Hospital", "NY"),
    ("SUNY Upstate University Hospital", "NY"), ("Swedish Medical Center", "CO"),
    ("Texas Health Presbyterian Dallas", "TX"), ("UF Health Shands Hospital", "FL"),
    ("University Hospitals Cleveland Medical Center", "OH"), ("UAMS Medical Center", "AR"),
    ("UC Davis Medical Center", "CA"), ("UC Irvine Medical Center", "CA"),
    ("UC San Diego Medical Center", "CA"), ("UConn John Dempsey Hospital", "CT"),
    ("University of Alabama at Birmingham Hospital", "AL"), ("University of Chicago Medical Center", "IL"),
    ("University of Louisville Hospital", "KY"), ("University of Mississippi Medical Center", "MS"),
    ("University of New Mexico Hospital", "NM"), ("University of Rochester Medical Center", "NY"),
    ("University of Tennessee Medical Center", "TN"), ("University of Vermont Medical Center", "VT"),
    ("VCU Medical Center", "VA"), ("Ascension St Vincent", "IN"),
    ("Atrium Health Carolinas Medical Center", "NC"), ("Banner University Medical Center Phoenix", "AZ"),
    ("Baystate Medical Center", "MA"), ("Carilion Roanoke Memorial Hospital", "VA"),
    ("Centura Health Porter Adventist Hospital", "CO"), ("ChristianaCare Wilmington Hospital", "DE"),
    ("Community Medical Center", "NJ"), ("Dartmouth-Hitchcock Medical Center", "NH"),
    ("El Camino Hospital", "CA"), ("Erlanger Medical Center", "TN"),
    ("Geisinger Medical Center", "PA"), ("Hackensack Meridian Health", "NJ"),
    ("Hennepin Healthcare", "MN"), ("Hospital for Special Surgery", "NY"),
    ("Inova Fairfax Medical Campus", "VA"), ("Integris Baptist Medical Center", "OK"),
    ("John Muir Medical Center", "CA"), ("Keck Hospital of USC", "CA"),
    ("MaineHealth Maine Medical Center", "ME"), ("Mission Hospital", "NC"),
    ("Penn State Health Milton S Hershey Medical Center", "PA"),
    ("ProMedica Toledo Hospital", "OH"), ("Providence Alaska Medical Center", "AK"),
    ("Sanford USD Medical Center", "SD"), ("St Cloud Hospital", "MN"),
    ("St Joseph Mercy Ann Arbor", "MI"), ("St Luke's Hospital Kansas City", "MO"),
    ("UnityPoint Health Iowa Methodist Medical Center", "IA"),
    ("UP Health System Marquette", "MI"), ("WakeMed Raleigh Campus", "NC"),
    ("West Virginia University Hospitals", "WV"), ("Wyoming Medical Center", "WY"),
]

# Common service codes with realistic price ranges (CPT codes)
SERVICES = [
    ("99213", "Office visit, established patient, low complexity", 100, 250),
    ("99214", "Office visit, established patient, moderate complexity", 150, 350),
    ("99215", "Office visit, established patient, high complexity", 200, 500),
    ("99203", "Office visit, new patient, low complexity", 150, 350),
    ("99204", "Office visit, new patient, moderate complexity", 200, 500),
    ("99205", "Office visit, new patient, high complexity", 300, 700),
    ("99281", "Emergency department visit, level 1", 150, 500),
    ("99283", "Emergency department visit, level 3", 400, 1200),
    ("99285", "Emergency department visit, level 5", 800, 3000),
    ("99291", "Critical care, first 30-74 minutes", 500, 2000),
    ("70553", "MRI brain with and without contrast", 800, 4000),
    ("72148", "MRI lumbar spine without contrast", 600, 3500),
    ("72110", "X-ray lumbar spine, complete", 150, 600),
    ("73721", "MRI knee without contrast", 500, 3000),
    ("74177", "CT abdomen and pelvis with contrast", 500, 3000),
    ("71046", "Chest X-ray, 2 views", 50, 300),
    ("71260", "CT chest with contrast", 400, 2500),
    ("76830", "Transvaginal ultrasound", 200, 800),
    ("76856", "Pelvic ultrasound, complete", 200, 800),
    ("80053", "Comprehensive metabolic panel", 20, 150),
    ("85025", "Complete blood count (CBC)", 15, 100),
    ("81001", "Urinalysis", 10, 50),
    ("84443", "TSH (thyroid stimulating hormone)", 25, 150),
    ("80061", "Lipid panel", 20, 120),
    ("36415", "Venipuncture", 10, 50),
    ("43239", "Upper GI endoscopy with biopsy", 800, 3500),
    ("45378", "Colonoscopy, diagnostic", 1000, 4500),
    ("45380", "Colonoscopy with biopsy", 1200, 5000),
    ("27447", "Total knee replacement", 15000, 60000),
    ("27130", "Total hip replacement", 15000, 55000),
    ("47562", "Laparoscopic cholecystectomy", 5000, 25000),
    ("59400", "Obstetric care, vaginal delivery", 5000, 15000),
    ("59510", "Cesarean delivery", 8000, 25000),
    ("19301", "Partial mastectomy", 3000, 15000),
    ("47600", "Cholecystectomy", 6000, 30000),
    ("90837", "Psychotherapy, 60 minutes", 120, 300),
    ("90834", "Psychotherapy, 45 minutes", 100, 250),
    ("90791", "Psychiatric diagnostic evaluation", 200, 500),
    ("96127", "Brief emotional/behavioral assessment", 10, 50),
    ("92014", "Comprehensive eye exam, established", 100, 300),
    ("92004", "Comprehensive eye exam, new patient", 150, 350),
    ("92015", "Refraction", 25, 75),
    ("D0120", "Periodic oral evaluation", 30, 80),
    ("D0150", "Comprehensive oral evaluation", 50, 150),
    ("D0274", "Bitewing radiographs, four images", 40, 120),
    ("D1110", "Adult prophylaxis (teeth cleaning)", 75, 200),
    ("D2740", "Crown, porcelain/ceramic", 800, 1800),
    ("D7210", "Surgical extraction", 200, 500),
    ("D7240", "Impacted tooth removal", 300, 800),
    ("92507", "Speech therapy treatment", 100, 250),
]

PAYERS = [
    "UnitedHealthcare", "Anthem BCBS", "Aetna", "Cigna", "Humana",
    "Kaiser Permanente", "BCBS of state",
]


def seed_hospital_data(db):
    """Generate realistic hospital transparency pricing data for 150+ hospitals."""
    print(f"Seeding hospital transparency data for {len(HOSPITALS)} hospitals...")
    total = 0

    for hospital_name, state in HOSPITALS:
        # Each hospital publishes prices for a random subset of services
        num_services = random.randint(25, len(SERVICES))
        services = random.sample(SERVICES, num_services)

        for code, desc, low, high in services:
            # Cash price
            cash_price = round(random.uniform(low, high * 0.7), 2)
            db.add(PriceData(
                provider_name=hospital_name,
                service_code=code,
                service_description=desc,
                price=cash_price,
                channel="cash",
                source=PriceSource.hospital_transparency,
                source_url=f"https://{hospital_name.lower().replace(' ', '-').replace(',', '')}.org/transparency",
                state=state,
                ingested_at=datetime.utcnow(),
            ))
            total += 1

            # 2-4 negotiated payer rates
            num_payers = random.randint(2, 4)
            for payer in random.sample(PAYERS, num_payers):
                negotiated = round(random.uniform(low * 0.8, high * 1.1), 2)
                db.add(PriceData(
                    provider_name=hospital_name,
                    service_code=code,
                    service_description=desc,
                    price=negotiated,
                    channel=f"negotiated_{payer}",
                    source=PriceSource.hospital_transparency,
                    source_url=f"https://{hospital_name.lower().replace(' ', '-').replace(',', '')}.org/transparency",
                    state=state,
                    ingested_at=datetime.utcnow(),
                ))
                total += 1

        # Commit per hospital to avoid huge transactions
        if total % 5000 == 0:
            db.commit()
            print(f"  ... {total} records so far")

    db.commit()
    print(f"  Hospital data: {total} records from {len(HOSPITALS)} hospitals")
    return total


# --- Medicare PFS Data ---

# Realistic Medicare national payment amounts by CPT code
MEDICARE_PFS = [
    ("99213", "Office visit established, low", 92.03, 63.18),
    ("99214", "Office visit established, moderate", 130.04, 96.06),
    ("99215", "Office visit established, high", 176.20, 131.72),
    ("99203", "Office visit new, low", 109.95, 77.62),
    ("99204", "Office visit new, moderate", 168.50, 127.15),
    ("99205", "Office visit new, high", 213.40, 161.35),
    ("99281", "ED visit level 1", 22.53, 22.53),
    ("99283", "ED visit level 3", 78.14, 78.14),
    ("99285", "ED visit level 5", 209.29, 209.29),
    ("99291", "Critical care first 30-74 min", 311.39, 228.60),
    ("70553", "MRI brain w/wo contrast", 414.84, 305.86),
    ("72148", "MRI lumbar spine w/o contrast", 304.40, 214.32),
    ("72110", "X-ray lumbar spine complete", 47.60, 47.60),
    ("73721", "MRI knee w/o contrast", 304.40, 214.32),
    ("74177", "CT abdomen pelvis w contrast", 285.72, 207.48),
    ("71046", "Chest X-ray 2 views", 28.28, 28.28),
    ("71260", "CT chest w contrast", 218.57, 159.75),
    ("76830", "Transvaginal US", 123.30, 88.14),
    ("76856", "Pelvic US complete", 123.30, 88.14),
    ("80053", "Comprehensive metabolic panel", 14.49, 14.49),
    ("85025", "CBC", 10.58, 10.58),
    ("81001", "Urinalysis", 4.02, 4.02),
    ("84443", "TSH", 22.89, 22.89),
    ("80061", "Lipid panel", 18.36, 18.36),
    ("36415", "Venipuncture", 3.00, 3.00),
    ("43239", "Upper GI endoscopy w biopsy", 330.18, 182.89),
    ("45378", "Colonoscopy diagnostic", 382.28, 258.91),
    ("45380", "Colonoscopy w biopsy", 428.56, 285.72),
    ("27447", "Total knee replacement", 1512.24, 1512.24),
    ("27130", "Total hip replacement", 1487.23, 1487.23),
    ("47562", "Laparoscopic cholecystectomy", 581.83, 581.83),
    ("59400", "Obstetric care vaginal delivery", 2332.10, 2332.10),
    ("59510", "Cesarean delivery", 2741.16, 2741.16),
    ("19301", "Partial mastectomy", 477.63, 477.63),
    ("90837", "Psychotherapy 60 min", 132.74, 132.74),
    ("90834", "Psychotherapy 45 min", 103.84, 103.84),
    ("90791", "Psychiatric diagnostic eval", 174.89, 174.89),
    ("92014", "Eye exam established", 106.88, 76.70),
    ("92004", "Eye exam new patient", 154.64, 112.71),
    ("92015", "Refraction", 0.00, 0.00),  # Not covered by Medicare
    ("92507", "Speech therapy treatment", 74.49, 74.49),
]


def seed_medicare_data(db):
    """Generate Medicare PFS records for common codes."""
    print(f"Seeding Medicare PFS data for {len(MEDICARE_PFS)} service codes...")
    total = 0

    for hcpcs, desc, non_fac, fac in MEDICARE_PFS:
        if non_fac > 0:
            db.add(PriceData(
                provider_name="Medicare",
                service_code=hcpcs,
                service_description=desc,
                price=non_fac,
                channel="medicare_non_facility_na_payment",
                source=PriceSource.medicare_physician_fee,
                source_url="https://www.cms.gov/medicare/payment/fee-schedules/physician/2024",
                ingested_at=datetime.utcnow(),
                file_date=datetime(2024, 1, 1),
            ))
            total += 1

        if fac > 0:
            db.add(PriceData(
                provider_name="Medicare",
                service_code=hcpcs,
                service_description=desc,
                price=fac,
                channel="medicare_facility_na_payment",
                source=PriceSource.medicare_physician_fee,
                source_url="https://www.cms.gov/medicare/payment/fee-schedules/physician/2024",
                ingested_at=datetime.utcnow(),
                file_date=datetime(2024, 1, 1),
            ))
            total += 1

    db.commit()
    print(f"  Medicare PFS: {total} records")
    return total


def main():
    print("=" * 60)
    print("Beneflex Public Data Seed Script")
    print("=" * 60)

    db = SessionLocal()

    # Check if already seeded
    from sqlalchemy import func
    existing = db.query(func.count(PriceData.price_id)).scalar() or 0
    if existing > 0:
        print(f"\nDatabase already has {existing} price records.")
        resp = input("Clear and re-seed? [y/N]: ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return
        db.query(PriceData).delete()
        db.commit()
        print("Cleared existing data.")

    hospital_count = seed_hospital_data(db)
    medicare_count = seed_medicare_data(db)

    # Print stats
    from app.services.data_ingestion import get_ingestion_stats
    stats = get_ingestion_stats(db)
    print(f"\n{'=' * 60}")
    print(f"SEED COMPLETE")
    print(f"{'=' * 60}")
    print(f"Total records:     {stats['total_records']}")
    print(f"Unique providers:  {stats['unique_providers']}")
    print(f"Unique services:   {stats['unique_services']}")
    print(f"States covered:    {len(stats['by_state'])}")
    print(f"By source:")
    for source, count in stats['by_source'].items():
        print(f"  {source}: {count}")
    print()

    db.close()


if __name__ == "__main__":
    main()
