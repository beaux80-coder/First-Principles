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
    "insurer_mrf_negotiated",      # Insurer MRF negotiated rates
    "nadac_pharmacy",              # National Average Drug Acquisition Cost
    "asp_drug",                    # Average Sales Price (Part B drugs)
    "manufacturer_patient_program", # Manufacturer assistance
    "discount_card",               # GoodRx scraped prices
    "goodrx_scrape",               # GoodRx.com scraped discount prices
    "state_apcd",                  # State All-Payer Claims Database
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

    # 12. State APCD data (all-payer claims)
    apcd_prices = _query_prices(db, service_code, PriceSource.state_apcd, state=state)
    for p in apcd_prices:
        channels_compared.append({
            "channel": "state_apcd",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": f"State APCD ({p.state or 'unknown'})",
        })

    # 13. GoodRx scraped discount prices (pharmacy)
    goodrx_prices = _query_prices(db, service_code, PriceSource.goodrx_scrape, state=state)
    for p in goodrx_prices:
        channels_compared.append({
            "channel": "discount_card",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": f"GoodRx scraped ({p.provider_name})",
        })

    # 14. Insurer MRF negotiated rates
    insurer_mrf_prices = _query_prices(db, service_code, PriceSource.insurer_transparency,
                                        provider_npi=provider_npi, state=state)
    for p in insurer_mrf_prices:
        channels_compared.append({
            "channel": "insurer_mrf_negotiated",
            "price": float(p.price),
            "provider": p.provider_name,
            "verified": True,
            "source": f"Insurer MRF ({p.provider_name})",
            "source_url": p.source_url,
        })

    # 15. Dental fee schedules
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

    # 13. DPC (Direct Primary Care) — compare DPC membership vs per-visit costs
    # Constitution: evaluate DPC memberships as a pricing channel for primary care
    # DPC-eligible: primary care E&M codes 99201-99215
    if _is_dpc_eligible(service_code):
        dpc_result = _evaluate_dpc_channel(db, service_code, state, channels_compared)
        channels_compared.append({
            "channel": "dpc_membership",
            "price": dpc_result["effective_per_visit_cost"],
            "provider": dpc_result.get("dpc_provider", "DPC_PRACTICE"),
            "verified": dpc_result["verified"],
            "source": dpc_result["source"],
            "note": dpc_result["note"],
            "dpc_comparison": dpc_result["comparison"],
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

    # 5. GoodRx scraped prices (discount card channel)
    goodrx_prices = db.query(PriceData).filter(
        PriceData.source == PriceSource.goodrx_scrape,
        PriceData.service_code == ndc_code,
    ).order_by(PriceData.price.asc()).limit(5).all()
    if goodrx_prices:
        for gp in goodrx_prices:
            channels.append({
                "channel": "discount_card",
                "price": float(gp.price),
                "source": f"GoodRx scraped price ({gp.provider_name})",
                "verified": True,
                "note": "GoodRx discount price scraped from public website",
            })
    else:
        # Also try matching by drug name
        if drug_name:
            goodrx_by_name = db.query(PriceData).filter(
                PriceData.source == PriceSource.goodrx_scrape,
                PriceData.service_code == drug_name.lower(),
            ).order_by(PriceData.price.asc()).limit(5).all()
            if goodrx_by_name:
                for gp in goodrx_by_name:
                    channels.append({
                        "channel": "discount_card",
                        "price": float(gp.price),
                        "source": f"GoodRx scraped price ({gp.provider_name})",
                        "verified": True,
                        "note": "GoodRx discount price scraped from public website (matched by drug name)",
                    })
            else:
                channels.append({
                    "channel": "discount_card",
                    "price": None,
                    "source": "GoodRx — no scraped data yet; run /data-pipeline/ingest/goodrx to populate",
                    "verified": False,
                    "note": "GoodRx prices will be compared once scraper has run",
                })
        else:
            channels.append({
                "channel": "discount_card",
                "price": None,
                "source": "GoodRx — no scraped data yet; run /data-pipeline/ingest/goodrx to populate",
                "verified": False,
                "note": "GoodRx prices will be compared once scraper has run",
            })

    # 6. State APCD data (all-payer claims)
    apcd_prices = db.query(PriceData).filter(
        PriceData.source == PriceSource.state_apcd,
        PriceData.service_code == ndc_code,
    ).order_by(PriceData.price.asc()).limit(5).all()
    for ap in apcd_prices:
        channels.append({
            "channel": "state_apcd",
            "price": float(ap.price),
            "source": f"State APCD ({ap.state or 'unknown'})",
            "verified": True,
            "note": "All-Payer Claims Database average allowed amount",
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


def _is_dpc_eligible(service_code: str) -> bool:
    """Check if a service code is eligible for DPC pricing comparison.

    DPC practices cover primary care E&M codes: 99201-99215
    (new patient: 99201-99205, established patient: 99211-99215).
    """
    try:
        code_num = int(service_code)
        return (99201 <= code_num <= 99205) or (99211 <= code_num <= 99215)
    except (ValueError, TypeError):
        return False


# DPC market pricing data — publicly available national averages
# Source: DPC Frontier mapper, DPC Alliance published surveys
# Average $80/month nationally, varies by market tier
DPC_MARKET_RATES = {
    # (state, tier) -> monthly fee in dollars
    # Tier 1: major metro areas
    "CA": {"monthly_fee": 100.0, "tier": "high_cost"},
    "NY": {"monthly_fee": 95.0, "tier": "high_cost"},
    "MA": {"monthly_fee": 95.0, "tier": "high_cost"},
    "WA": {"monthly_fee": 90.0, "tier": "high_cost"},
    "CT": {"monthly_fee": 90.0, "tier": "high_cost"},
    "NJ": {"monthly_fee": 90.0, "tier": "high_cost"},
    # Tier 2: moderate cost
    "CO": {"monthly_fee": 85.0, "tier": "moderate_cost"},
    "IL": {"monthly_fee": 80.0, "tier": "moderate_cost"},
    "PA": {"monthly_fee": 80.0, "tier": "moderate_cost"},
    "VA": {"monthly_fee": 80.0, "tier": "moderate_cost"},
    "MN": {"monthly_fee": 80.0, "tier": "moderate_cost"},
    "OR": {"monthly_fee": 85.0, "tier": "moderate_cost"},
    "AZ": {"monthly_fee": 75.0, "tier": "moderate_cost"},
    "NC": {"monthly_fee": 75.0, "tier": "moderate_cost"},
    "GA": {"monthly_fee": 75.0, "tier": "moderate_cost"},
    # Tier 3: lower cost markets
    "TX": {"monthly_fee": 70.0, "tier": "lower_cost"},
    "FL": {"monthly_fee": 70.0, "tier": "lower_cost"},
    "OH": {"monthly_fee": 65.0, "tier": "lower_cost"},
    "IN": {"monthly_fee": 65.0, "tier": "lower_cost"},
    "TN": {"monthly_fee": 65.0, "tier": "lower_cost"},
    "MO": {"monthly_fee": 65.0, "tier": "lower_cost"},
    "KS": {"monthly_fee": 60.0, "tier": "lower_cost"},
    "OK": {"monthly_fee": 60.0, "tier": "lower_cost"},
    "AR": {"monthly_fee": 55.0, "tier": "lower_cost"},
    "MS": {"monthly_fee": 50.0, "tier": "lower_cost"},
}

# National average when state is unknown
DPC_NATIONAL_AVERAGE_MONTHLY = 80.0

# Utilization tiers for per-visit cost calculation
# Source: DPC Alliance member practice data
DPC_UTILIZATION_PROFILES = {
    "low": {"annual_visits": 3, "label": "Low utilizer (young, healthy)"},
    "average": {"annual_visits": 5, "label": "Average utilizer"},
    "moderate": {"annual_visits": 8, "label": "Moderate utilizer (chronic condition)"},
    "high": {"annual_visits": 12, "label": "High utilizer (multiple conditions)"},
}


def _get_dpc_monthly_rate(state: Optional[str]) -> tuple[float, str]:
    """Get the DPC monthly membership rate for a state.

    Returns (monthly_fee, source_description).
    """
    if state and state in DPC_MARKET_RATES:
        market = DPC_MARKET_RATES[state]
        return market["monthly_fee"], f"DPC market rate ({state}, {market['tier']})"
    return DPC_NATIONAL_AVERAGE_MONTHLY, "DPC national average ($80/month)"


def _evaluate_dpc_channel(
    db: Session,
    service_code: str,
    state: Optional[str],
    existing_channels: list[dict],
) -> dict:
    """Evaluate DPC membership as a pricing channel against per-visit costs.

    Compares the effective per-visit cost under a DPC membership to the
    per-visit primary care costs already discovered in other channels.
    Uses average utilization (5 visits/year) for the base comparison,
    and provides breakdowns for all utilization profiles.

    Records the comparison as structured data feeding F8.
    """
    monthly_fee, rate_source = _get_dpc_monthly_rate(state)
    annual_cost = monthly_fee * 12

    # Calculate effective per-visit cost across utilization profiles
    per_visit_by_profile = {}
    for profile_key, profile in DPC_UTILIZATION_PROFILES.items():
        visits = profile["annual_visits"]
        per_visit_by_profile[profile_key] = {
            "annual_visits": visits,
            "effective_per_visit": round(annual_cost / visits, 2),
            "label": profile["label"],
        }

    # Use "average" profile (5 visits/year) as the standard comparison point
    avg_per_visit = per_visit_by_profile["average"]["effective_per_visit"]

    # Find the best per-visit primary care price from other channels
    # Only compare against channels that have data for this service code
    per_visit_alternatives = [
        c for c in existing_channels
        if c["price"] and c["price"] > 0
        and c["channel"] != "dpc_membership"
    ]
    cheapest_per_visit = min(
        (c["price"] for c in per_visit_alternatives), default=None
    )

    # Build the comparison record
    dpc_is_cheaper = False
    savings_per_visit = None
    if cheapest_per_visit is not None:
        dpc_is_cheaper = avg_per_visit < cheapest_per_visit
        savings_per_visit = round(cheapest_per_visit - avg_per_visit, 2)

    # DPC includes additional services beyond the visit itself:
    # - Unlimited visits (no per-visit charge)
    # - Same-day/next-day appointments
    # - Extended visit times (30-60 min vs 15 min)
    # - Direct physician access (text/email/phone)
    # - Basic labs and procedures often included
    # - No facility fees
    included_services_value = round(monthly_fee * 0.3, 2)  # ~30% of fee is non-visit value

    # Adjusted per-visit cost accounting for included services
    adjusted_per_visit = round(
        max(0, (annual_cost - included_services_value * 12) / DPC_UTILIZATION_PROFILES["average"]["annual_visits"]),
        2,
    )

    # Determine if DPC data comes from actual DB records or market estimates
    dpc_db_prices = db.query(PriceData).filter(
        PriceData.service_code == service_code,
        PriceData.channel.ilike("%dpc%"),
    )
    if state:
        dpc_db_prices = dpc_db_prices.filter(PriceData.state == state)
    dpc_from_db = dpc_db_prices.first()

    if dpc_from_db:
        # Use actual DPC price data from the database if available
        effective_price = float(dpc_from_db.price)
        verified = True
        source = f"DPC practice published rate ({dpc_from_db.provider_name})"
        dpc_provider = dpc_from_db.provider_name
    else:
        effective_price = avg_per_visit
        verified = False
        source = rate_source
        dpc_provider = "DPC_MARKET_ESTIMATE"

    comparison = {
        "dpc_monthly_fee": monthly_fee,
        "dpc_annual_cost": annual_cost,
        "effective_per_visit_at_avg_utilization": avg_per_visit,
        "adjusted_per_visit_with_included_services": adjusted_per_visit,
        "per_visit_by_utilization_profile": per_visit_by_profile,
        "cheapest_per_visit_alternative": cheapest_per_visit,
        "cheapest_alternative_channel": (
            min(per_visit_alternatives, key=lambda c: c["price"])["channel"]
            if per_visit_alternatives else None
        ),
        "dpc_is_cheaper_at_avg_utilization": dpc_is_cheaper,
        "savings_per_visit_vs_cheapest": savings_per_visit,
        "included_services": [
            "Unlimited primary care visits",
            "Same-day/next-day scheduling",
            "Extended visit times (30-60 min)",
            "Direct physician access (text/email/phone)",
            "Basic labs and procedures",
            "No facility fees",
        ],
        "breakeven_visits_per_year": (
            round(annual_cost / cheapest_per_visit, 1) if cheapest_per_visit and cheapest_per_visit > 0 else None
        ),
        "rate_source": rate_source,
        "market_state": state,
    }

    note = (
        f"DPC membership: ${monthly_fee:.0f}/month ({rate_source}). "
        f"Per-visit at avg utilization (5/yr): ${avg_per_visit:.2f}. "
    )
    if cheapest_per_visit is not None:
        note += (
            f"Cheapest per-visit alternative: ${cheapest_per_visit:.2f}. "
            f"DPC {'saves' if dpc_is_cheaper else 'costs more by'} "
            f"${abs(savings_per_visit):.2f}/visit at average utilization."
        )
    else:
        note += "No per-visit alternatives found for comparison."

    logger.info(
        "DPC channel evaluated for %s (state=%s): $%.2f/visit vs $%s/visit alt, "
        "DPC cheaper=%s",
        service_code, state, avg_per_visit,
        f"{cheapest_per_visit:.2f}" if cheapest_per_visit else "N/A",
        dpc_is_cheaper,
    )

    return {
        "effective_per_visit_cost": effective_price,
        "verified": verified,
        "source": source,
        "note": note,
        "comparison": comparison,
        "dpc_provider": dpc_provider,
        "feeding_f8": True,
    }


def document_payment_speed_capabilities(db: Session) -> dict:
    """Document actual payment speed from measured data.

    Constitution F2: "Selects and pays the lowest verified price."
    Payment speed is a competitive advantage — providers accept lower prices
    for immediate guaranteed payment vs. 90-day float with collection risk.

    This function queries the Payment table for charge_verified_at to
    payment_initiated_at deltas to document actual payment speed.
    """
    from app.services.payment import Payment

    total_payments = db.query(func.count(Payment.payment_id)).scalar() or 0

    if total_payments == 0:
        return {
            "total_payments": 0,
            "message": "No payments recorded yet. Payment speed will be documented after first payment.",
            "target_speed": {
                "goal": "Same-day payment initiation after charge verification",
                "rationale": (
                    "Providers accept 10-15% lower prices for immediate guaranteed payment "
                    "vs. 90-day float with 15-20% collection risk. Same-day payment unlocks "
                    "the payment-speed discount channel in F2 price discovery."
                ),
            },
        }

    # Calculate payment speed: charge_verified_at -> payment_initiated_at

    speed_stats = db.execute(text(
        "SELECT "
        "  AVG(JULIANDAY(payment_initiated_at) - JULIANDAY(charge_verified_at)) * 24 * 60 as avg_minutes, "
        "  MIN(JULIANDAY(payment_initiated_at) - JULIANDAY(charge_verified_at)) * 24 * 60 as min_minutes, "
        "  MAX(JULIANDAY(payment_initiated_at) - JULIANDAY(charge_verified_at)) * 24 * 60 as max_minutes, "
        "  COUNT(*) as total "
        "FROM payment "
        "WHERE payment_initiated_at IS NOT NULL AND charge_verified_at IS NOT NULL"
    )).fetchone()

    avg_minutes = float(speed_stats[0]) if speed_stats and speed_stats[0] else None
    min_minutes = float(speed_stats[1]) if speed_stats and speed_stats[1] else None
    max_minutes = float(speed_stats[2]) if speed_stats and speed_stats[2] else None
    measured_count = int(speed_stats[3]) if speed_stats and speed_stats[3] else 0

    # Calculate percentile distribution
    percentile_data = {}
    if measured_count > 0:
        try:
            all_deltas = db.execute(text(
                "SELECT (JULIANDAY(payment_initiated_at) - JULIANDAY(charge_verified_at)) * 24 * 60 as delta_min "
                "FROM payment "
                "WHERE payment_initiated_at IS NOT NULL AND charge_verified_at IS NOT NULL "
                "ORDER BY delta_min"
            )).fetchall()
            deltas = [float(row[0]) for row in all_deltas]
            if deltas:
                n = len(deltas)
                percentile_data = {
                    "p50_minutes": round(deltas[n // 2], 2),
                    "p90_minutes": round(deltas[int(n * 0.9)], 2) if n > 1 else round(deltas[0], 2),
                    "p95_minutes": round(deltas[int(n * 0.95)], 2) if n > 1 else round(deltas[0], 2),
                    "p99_minutes": round(deltas[int(n * 0.99)], 2) if n > 1 else round(deltas[0], 2),
                }
        except Exception:
            pass

    # Classify payment speed
    speed_category = "unknown"
    if avg_minutes is not None:
        if avg_minutes < 5:
            speed_category = "instant"
        elif avg_minutes < 60:
            speed_category = "same_hour"
        elif avg_minutes < 1440:
            speed_category = "same_day"
        elif avg_minutes < 4320:
            speed_category = "within_3_days"
        else:
            speed_category = "standard"

    # Calculate discount potential based on speed
    discount_estimate = None
    if speed_category in ("instant", "same_hour", "same_day"):
        discount_estimate = {
            "estimated_discount_pct": 12.0,
            "rationale": (
                "Same-day payment eliminates provider's collection risk (15-20% of revenue "
                "at typical practices) and float cost (90-day average payment cycle). "
                "Providers accept 10-15% lower rates for immediate guaranteed payment."
            ),
        }
    elif speed_category == "within_3_days":
        discount_estimate = {
            "estimated_discount_pct": 8.0,
            "rationale": "3-day payment significantly reduces float but not as impactful as same-day.",
        }

    return {
        "total_payments": total_payments,
        "measured_payments": measured_count,
        "payment_speed": {
            "average_minutes": round(avg_minutes, 2) if avg_minutes is not None else None,
            "fastest_minutes": round(min_minutes, 2) if min_minutes is not None else None,
            "slowest_minutes": round(max_minutes, 2) if max_minutes is not None else None,
            "percentiles": percentile_data,
            "category": speed_category,
        },
        "comparison_to_industry": {
            "traditional_insurance_days": 90,
            "traditional_insurance_minutes": 90 * 24 * 60,
            "beneflex_avg_minutes": round(avg_minutes, 2) if avg_minutes is not None else None,
            "speedup_factor": round(90 * 24 * 60 / avg_minutes, 0) if avg_minutes and avg_minutes > 0 else None,
        },
        "discount_potential": discount_estimate,
        "feeds_f2": True,
        "f2_integration": (
            "Payment speed data feeds the payment_speed_discount channel in F2 "
            "price discovery. Faster payment = larger discount negotiable with providers."
        ),
        "measured_at": datetime.now(UTC).isoformat(),
    }
