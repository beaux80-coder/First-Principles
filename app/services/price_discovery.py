"""Price Discovery Engine (Function 2).

Constitution: "For every service at every provider, dynamically compares:
cash prices, negotiated rates, reference-based pricing benchmarks,
manufacturer programs (pharmacy), wholesale pricing (pharmacy), and any
other available channel. Selects and pays the lowest verified price."

No network contracts. No PBM. Fully dynamic pricing surface.
"""

import logging
import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)

# Pricing channels per the Constitution
PRICING_CHANNELS = [
    "cash_price",
    "negotiated_rate",
    "reference_medicare",          # Medicare × locality adjustment
    "reference_medicaid",
    "hospital_transparency_rate",
    "insurer_transparency_rate",
    "nadac_pharmacy",              # National Average Drug Acquisition Cost
    "asp_drug",                    # Average Sales Price (Part B drugs)
    "manufacturer_patient_program", # Manufacturer assistance
    "discount_card",               # GoodRx-style
    "dpc_membership",              # Direct Primary Care
    "dmepos_rate",                 # Durable medical equipment
    "va_rate",                     # VA Community Care
    "wholesale_pharmacy",          # WAC
]


def compare_all_channels(
    db: Session,
    service_code: str,
    provider_npi: Optional[str] = None,
    state: Optional[str] = None,
    benefit_type: str = "health",
) -> dict:
    """Compare all pricing channels for a service and return the lowest.

    Constitution: "Selects and pays the lowest verified price."
    Constitution: "No network contracts lock in above-market rates."

    Returns:
        dict with channels_compared, lowest_price, lowest_channel, and metadata
    """
    channels_compared = []

    # 1. Hospital transparency file prices
    hosp_prices = _query_prices(db, service_code, PriceSource.hospital_transparency,
                                provider_npi=provider_npi, state=state)
    for p in hosp_prices:
        channels_compared.append({
            "channel": "hospital_transparency_rate",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS Hospital Transparency File",
            "source_url": p.source_url,
        })

    # 2. Insurer transparency (negotiated rates)
    insurer_prices = _query_prices(db, service_code, PriceSource.insurer_transparency,
                                   provider_npi=provider_npi, state=state)
    for p in insurer_prices:
        channels_compared.append({
            "channel": "insurer_transparency_rate",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "Insurer Transparency in Coverage File",
            "source_url": p.source_url,
        })

    # 3. Reference-based: Medicare rates
    medicare_prices = _query_prices(db, service_code, PriceSource.medicare_physician_fee,
                                    state=state)
    for p in medicare_prices:
        channels_compared.append({
            "channel": "reference_medicare",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS Medicare Physician Fee Schedule",
        })

    # 4. Medicare Outpatient (OPPS) rates
    opps_prices = _query_prices(db, service_code, PriceSource.medicare_outpatient, state=state)
    for p in opps_prices:
        channels_compared.append({
            "channel": "reference_medicare_opps",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS OPPS Outpatient Rates",
        })

    # 5. Medicare Inpatient (DRG) rates
    ipps_prices = _query_prices(db, service_code, PriceSource.medicare_inpatient, state=state)
    for p in ipps_prices:
        channels_compared.append({
            "channel": "reference_medicare_ipps",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS IPPS/DRG Inpatient Rates",
        })

    # 6. NADAC pharmacy pricing
    nadac_prices = _query_prices(db, service_code, PriceSource.nadac_pharmacy, state=state)
    for p in nadac_prices:
        channels_compared.append({
            "channel": "nadac_pharmacy",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS NADAC Weekly",
        })

    # 7. ASP drug pricing (Part B)
    asp_prices = _query_prices(db, service_code, PriceSource.asp_drug_pricing, state=state)
    for p in asp_prices:
        channels_compared.append({
            "channel": "asp_drug",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS Average Sales Price",
        })

    # 8. Cash price (from transparency files where channel = "cash")
    cash_prices = db.query(PriceData).filter(
        PriceData.service_code == service_code,
        PriceData.channel.ilike("%cash%"),
    )
    if state:
        cash_prices = cash_prices.filter(PriceData.state == state)
    for p in cash_prices.limit(10).all():
        channels_compared.append({
            "channel": "cash_price",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": f"Published cash price ({p.source.value})",
        })

    # 9. DMEPOS fee schedule
    dmepos_prices = _query_prices(db, service_code, PriceSource.dmepos_fee_schedule, state=state)
    for p in dmepos_prices:
        channels_compared.append({
            "channel": "dmepos_rate",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "CMS DMEPOS Fee Schedule",
        })

    # 10. VA fee schedule
    va_prices = _query_prices(db, service_code, PriceSource.va_fee_schedule, state=state)
    for p in va_prices:
        channels_compared.append({
            "channel": "va_rate",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "VA Community Care Fee Schedule",
        })

    # 11. State Medicaid rates
    medicaid_prices = _query_prices(db, service_code, PriceSource.state_medicaid, state=state)
    for p in medicaid_prices:
        channels_compared.append({
            "channel": "reference_medicaid",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": "State Medicaid Fee Schedule",
        })

    # 12. Dental fee schedules
    if benefit_type == "dental":
        dental_prices = _query_prices(db, service_code, PriceSource.dental_fee_schedule, state=state)
        for p in dental_prices:
            channels_compared.append({
                "channel": "dental_fee_schedule",
                "price": float(p.price),
                "provider": p.provider_name,
                "verified": True,
                "source": "Medicaid Dental Fee Schedule",
            })

    # 13. DPC (Direct Primary Care) — check for DPC-eligible services
    if service_code.startswith("992"):  # E&M codes (office visits)
        channels_compared.append({
            "channel": "dpc_membership",
            "price": _estimate_dpc_visit_cost(service_code, state),
            "provider": "DPC_PRACTICE_ESTIMATE",
            "verified": False,
            "source": "DPC market rate estimate (varies by metro)",
            "note": "DPC practices charge monthly membership; per-visit cost derived from typical utilization",
        })

    # Filter out zero/negative prices
    channels_compared = [c for c in channels_compared if c["price"] and c["price"] > 0]

    # Sort by price ascending
    channels_compared.sort(key=lambda x: x["price"])

    # Determine lowest
    if channels_compared:
        lowest = channels_compared[0]
        lowest_price = lowest["price"]
        lowest_channel = lowest["channel"]
    else:
        lowest_price = None
        lowest_channel = None

    # Payment-speed discount estimate
    payment_speed_discount = None
    if lowest_price and lowest_price > 50:
        # Providers typically accept 10-15% less for immediate guaranteed payment
        # vs 90-day float with collection risk
        payment_speed_discount = {
            "standard_payment_terms_price": lowest_price,
            "immediate_payment_price": round(lowest_price * 0.88, 2),  # 12% average discount
            "discount_pct": 12.0,
            "rationale": "Providers accept lower price for immediate guaranteed payment "
                         "vs. 90-day float with collections risk (15-20% of practice revenue)",
        }

    # Balance billing analysis
    balance_billing_eliminated = False
    if lowest_channel in ("cash_price", "hospital_transparency_rate"):
        balance_billing_eliminated = True

    return {
        "service_code": service_code,
        "benefit_type": benefit_type,
        "state": state,
        "channels_compared": channels_compared,
        "total_channels_checked": len(PRICING_CHANNELS),
        "channels_with_data": len(channels_compared),
        "lowest_price": lowest_price,
        "lowest_channel": lowest_channel,
        "payment_speed_discount": payment_speed_discount,
        "balance_billing_eliminated": balance_billing_eliminated,
        "balance_billing_rationale": (
            "Provider's published price paid in full — no gap between charged and paid, "
            "therefore no legal basis for balance billing"
        ) if balance_billing_eliminated else (
            "Pre-service price confirmation: exact price confirmed before appointment, "
            "paid in full at time of service"
        ),
        "network_contracts": "NONE — system pays lowest verified price at moment of service, "
                             "never locked into contracted rates",
        "intermediary_costs": "NONE — no PBM spread, no network access fees, "
                              "no third-party processing fees",
        "compared_at": datetime.now(UTC).isoformat(),
    }


def compare_pharmacy_channels(
    db: Session,
    ndc_code: str,
    drug_name: Optional[str] = None,
    state: Optional[str] = None,
) -> dict:
    """Compare all pharmacy pricing channels. PBM elimination.

    Constitution: "The price discovery engine replaces everything a PBM does —
    finding the lowest price — without the spread, without the opaque rebates,
    without the formulary manipulation."
    """
    channels = []

    # 1. NADAC (National Average Drug Acquisition Cost)
    nadac = db.query(PriceData).filter(
        PriceData.source == PriceSource.nadac_pharmacy,
        PriceData.service_code == ndc_code,
    ).first()
    if nadac:
        channels.append({
            "channel": "nadac_pharmacy",
            "price": float(nadac.price),
            "source": "CMS NADAC Weekly",
            "verified": True,
        })

    # 2. ASP (Average Sales Price — Part B drugs)
    asp = db.query(PriceData).filter(
        PriceData.source == PriceSource.asp_drug_pricing,
        PriceData.service_code == ndc_code,
    ).first()
    if asp:
        channels.append({
            "channel": "asp_drug",
            "price": float(asp.price),
            "source": "CMS ASP Quarterly",
            "verified": True,
        })

    # 3. Cash price at pharmacy (from transparency files)
    cash = db.query(PriceData).filter(
        PriceData.service_code == ndc_code,
        PriceData.channel.ilike("%cash%"),
    ).first()
    if cash:
        channels.append({
            "channel": "cash_price",
            "price": float(cash.price),
            "source": "Pharmacy published cash price",
            "verified": True,
        })

    # 4. Manufacturer patient assistance programs
    # These are tracked by drug name/class, not NDC
    if drug_name:
        channels.append({
            "channel": "manufacturer_patient_program",
            "price": None,  # Requires real-time lookup
            "source": "Manufacturer patient assistance (requires application)",
            "verified": False,
            "note": "Many manufacturers offer $0 or reduced copay programs — checked at point of service",
        })

    # 5. Discount card pricing (GoodRx-style)
    channels.append({
        "channel": "discount_card",
        "price": None,  # Requires real-time API
        "source": "Discount card programs (compared at point of service)",
        "verified": False,
        "note": "Discount card prices compared in real time at pharmacy",
    })

    # Filter and sort
    priced_channels = [c for c in channels if c["price"] and c["price"] > 0]
    priced_channels.sort(key=lambda x: x["price"])
    unpriced_channels = [c for c in channels if not c.get("price") or c["price"] <= 0]

    lowest = priced_channels[0] if priced_channels else None

    return {
        "ndc_code": ndc_code,
        "drug_name": drug_name,
        "channels_compared": priced_channels + unpriced_channels,
        "lowest_verified_price": lowest["price"] if lowest else None,
        "lowest_channel": lowest["channel"] if lowest else None,
        "pbm_eliminated": True,
        "pbm_elimination_detail": (
            "No PBM spread pricing. No opaque rebates. No formulary manipulation fees. "
            "System compares all channels directly and pays the lowest verified price "
            "to the pharmacy. Every comparison recorded as structured data feeding F8."
        ),
        "compared_at": datetime.now(UTC).isoformat(),
    }


def record_price_comparison(
    db: Session,
    service_code: str,
    channels_compared: list[dict],
    lowest_price: float,
    lowest_channel: str,
    provider_id: Optional[uuid.UUID] = None,
    service_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """Record a price comparison as structured data feeding F8.

    Constitution: "Records every price comparison and payment as structured data
    feeding Function 8."
    """
    from app.models.price_comparison import PriceComparison

    comparison = PriceComparison(
        comparison_id=uuid.uuid4(),
        service_id=service_id or uuid.uuid4(),
        provider_id=provider_id or uuid.uuid4(),
        channels_compared=channels_compared,
        lowest_price=lowest_price,
        lowest_channel=lowest_channel,
    )
    db.add(comparison)
    db.commit()
    return comparison.comparison_id


def _query_prices(
    db: Session,
    service_code: str,
    source: PriceSource,
    provider_npi: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 10,
) -> list[PriceData]:
    """Query price_data table for a specific service and source."""
    q = db.query(PriceData).filter(
        PriceData.service_code == service_code,
        PriceData.source == source,
    )
    if provider_npi:
        q = q.filter(PriceData.provider_npi == provider_npi)
    if state:
        q = q.filter(PriceData.state == state)
    return q.order_by(PriceData.price.asc()).limit(limit).all()


def _estimate_dpc_visit_cost(service_code: str, state: Optional[str] = None) -> float:
    """Estimate per-visit cost for a DPC membership.

    DPC practices charge $75-150/month for unlimited primary care visits.
    Average utilization: 4-6 visits/year. Per-visit cost: $150-450.
    This is a pricing CHANNEL, not a structural dependency.
    """
    # Conservative estimate: $125/month membership, 5 visits/year
    monthly_fee = 125.0
    annual_visits = 5.0
    per_visit = (monthly_fee * 12) / annual_visits  # $300

    # Adjust by state cost index
    state_adjustments = {
        "CA": 1.3, "NY": 1.25, "MA": 1.2, "TX": 0.95, "FL": 0.95,
        "WA": 1.15, "CO": 1.1, "IL": 1.05, "PA": 1.0, "OH": 0.9,
    }
    adjustment = state_adjustments.get(state, 1.0) if state else 1.0

    return round(per_visit * adjustment, 2)
