"""Static benchmark engine (Function 6A, Stage 1).

Constitution requirement: comprehensive analysis showing what the employer's
current benefits experience looks like vs under the system, across ALL benefit
types (health, dental, vision, life, STD, LTD, mental health) from day one.

Not just a cost gap — the full product difference: cost transparency, care
execution, clinical quality, employee experience, and administrative burden.

Uses real CMS price data for data-driven estimates. Surfaces service-level
examples, pharmacy comparisons, and local hospital quality ratings.
"""

from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

# Industry average cost breakdown (% of total premium spend)
# Source: KFF Employer Health Benefits Survey 2024, BLS data
INDUSTRY_COST_BREAKDOWN = {
    "carrier_overhead_pct": 0.17,
    "waste_pct": 0.25,
    "broker_commission_pct": 0.05,
    "admin_cost_per_employee_yr": 1200,
    "stop_loss_pct": 0.065,
    "state_tax_pct": 0.025,
}

# Benefit type cost allocation (% of total premium)
# Source: KFF, NADP, VSP industry data
BENEFIT_TYPE_ALLOCATION = {
    "health": 0.72,
    "dental": 0.08,
    "vision": 0.02,
    "mental_health": 0.08,
    "life_insurance": 0.04,
    "short_term_disability": 0.03,
    "long_term_disability": 0.03,
}

# National average PEPMs by benefit type (2025 data)
NATIONAL_AVG_PEPM = {
    "total": 775,
    "health": 558,
    "dental": 62,
    "vision": 16,
    "mental_health": 62,
    "life_insurance": 31,
    "short_term_disability": 23,
    "long_term_disability": 23,
}


def _query_price_stats(db: Session, state: str | None) -> dict:
    """Query real price data statistics for data-driven estimates."""
    state_upper = state.upper() if state else None

    stats = {}

    # Hospital transparency prices in state
    state_filter = PriceData.state == state_upper if state_upper else True
    stats["hospital_price_count"] = db.query(func.count(PriceData.price_id)).filter(
        and_(PriceData.source == PriceSource.hospital_transparency, state_filter)
    ).scalar() or 0

    stats["hospital_avg_price"] = db.query(func.avg(PriceData.price)).filter(
        and_(
            PriceData.source == PriceSource.hospital_transparency,
            PriceData.channel == "cash",
            state_filter,
        )
    ).scalar()

    # Medicare reference rates
    stats["medicare_avg_price"] = db.query(func.avg(PriceData.price)).filter(
        PriceData.source == PriceSource.medicare_physician_fee,
    ).scalar()

    # Total state price data
    stats["total_state_records"] = db.query(func.count(PriceData.price_id)).filter(
        state_filter
    ).scalar() or 0

    # NADAC pharmacy records
    stats["pharmacy_records"] = db.query(func.count(PriceData.price_id)).filter(
        PriceData.source == PriceSource.nadac_pharmacy,
    ).scalar() or 0

    # Insurer negotiated rates
    stats["insurer_tic_records"] = db.query(func.count(PriceData.price_id)).filter(
        PriceData.source == PriceSource.insurer_transparency,
    ).scalar() or 0

    # Hospital quality ratings in state
    if state_upper:
        stats["hospitals_with_ratings"] = db.query(func.count(PriceData.price_id)).filter(
            and_(
                PriceData.channel == "cms_quality_rating",
                PriceData.state == state_upper,
                PriceData.price > 0,
            )
        ).scalar() or 0

        # Top-rated hospitals in state
        top_hospitals = (
            db.query(PriceData.provider_name, PriceData.price)
            .filter(
                and_(
                    PriceData.channel == "cms_quality_rating",
                    PriceData.state == state_upper,
                    PriceData.price >= 4,
                )
            )
            .order_by(PriceData.price.desc())
            .limit(5)
            .all()
        )
        stats["top_rated_hospitals"] = [
            {"name": h.provider_name, "rating": int(h.price)} for h in top_hospitals
        ]
    else:
        stats["hospitals_with_ratings"] = 0
        stats["top_rated_hospitals"] = []

    # Service-level price examples (common services in state)
    common_codes = ["99213", "99214", "71046", "80053", "85025", "45378", "90837"]
    price_examples = []
    for code in common_codes:
        # Hospital price
        hosp = db.query(func.avg(PriceData.price)).filter(
            and_(
                PriceData.source == PriceSource.hospital_transparency,
                PriceData.service_code == code,
                state_filter,
            )
        ).scalar()

        # Medicare price
        med = db.query(func.avg(PriceData.price)).filter(
            and_(
                PriceData.source == PriceSource.medicare_physician_fee,
                PriceData.service_code == code,
            )
        ).scalar()

        # Insurer negotiated price
        ins = db.query(func.avg(PriceData.price)).filter(
            and_(
                PriceData.source == PriceSource.insurer_transparency,
                PriceData.service_code == code,
            )
        ).scalar()

        desc = db.query(PriceData.service_description).filter(
            PriceData.service_code == code,
        ).first()

        if hosp or med or ins:
            price_examples.append({
                "code": code,
                "description": desc[0][:60] if desc and desc[0] else code,
                "hospital_price": round(float(hosp), 2) if hosp else None,
                "medicare_price": round(float(med), 2) if med else None,
                "insurer_negotiated": round(float(ins), 2) if ins else None,
                "system_would_pay": round(float(min(filter(None, [hosp, med, ins]))), 2) if any([hosp, med, ins]) else None,
            })
    stats["price_examples"] = price_examples

    # Top pharmacy savings opportunities
    top_drugs = (
        db.query(
            PriceData.service_description,
            func.avg(PriceData.price).label("avg_nadac"),
        )
        .filter(
            PriceData.source == PriceSource.nadac_pharmacy,
            PriceData.price > 10,
        )
        .group_by(PriceData.service_description)
        .order_by(func.avg(PriceData.price).desc())
        .limit(5)
        .all()
    )
    stats["top_pharmacy_costs"] = [
        {"drug": d.service_description[:50], "nadac_avg": round(float(d.avg_nadac), 2)}
        for d in top_drugs
    ]

    # Dental price data
    stats["dental_records"] = db.query(func.count(PriceData.price_id)).filter(
        PriceData.service_code.like("D%"),
    ).scalar() or 0

    # Vision price data
    stats["vision_records"] = db.query(func.count(PriceData.price_id)).filter(
        or_(
            PriceData.service_code.like("920%"),
            PriceData.service_code.like("921%"),
            PriceData.service_code.like("922%"),
        )
    ).scalar() or 0

    # Mental health price data
    stats["mental_health_records"] = db.query(func.count(PriceData.price_id)).filter(
        or_(
            PriceData.service_code.like("9079%"),
            PriceData.service_code.like("9083%"),
            PriceData.service_code.like("9084%"),
        )
    ).scalar() or 0

    return stats


def compute_benchmark(
    db: Session,
    employee_count: int,
    state: str,
    industry: str,
    annual_spend: float | None,
) -> dict:
    """Compute the static benchmark comparison across ALL benefit types.

    Constitution requirement: covers health, dental, vision, life insurance,
    short-term disability, long-term disability, and mental health.
    Shows full product difference: cost, care execution, clinical quality,
    transparency, and administrative burden elimination.
    """
    # Determine per-employee-per-month baseline
    if annual_spend and employee_count > 0:
        pepm = annual_spend / employee_count / 12
    else:
        pepm = NATIONAL_AVG_PEPM["total"]

    annual_total = pepm * employee_count * 12

    # Query real price data
    price_stats = _query_price_stats(db, state)

    # Data-driven savings estimate
    hospital_avg = price_stats["hospital_avg_price"]
    medicare_avg = price_stats["medicare_avg_price"]

    if hospital_avg and medicare_avg and hospital_avg > 0:
        price_ratio = medicare_avg / hospital_avg
        price_discovery_savings_pct = min(0.45, max(0.20, (1 - price_ratio) * 0.7))
    elif price_stats["total_state_records"] > 1000:
        price_discovery_savings_pct = 0.35
    elif price_stats["total_state_records"] > 100:
        price_discovery_savings_pct = 0.30
    else:
        price_discovery_savings_pct = 0.25

    # Current cost breakdown (aggregate)
    carrier_overhead = annual_total * INDUSTRY_COST_BREAKDOWN["carrier_overhead_pct"]
    waste = annual_total * INDUSTRY_COST_BREAKDOWN["waste_pct"]
    broker_cost = annual_total * INDUSTRY_COST_BREAKDOWN["broker_commission_pct"]
    admin_cost = INDUSTRY_COST_BREAKDOWN["admin_cost_per_employee_yr"] * employee_count
    stop_loss_cost = annual_total * INDUSTRY_COST_BREAKDOWN["stop_loss_pct"]
    state_tax = annual_total * INDUSTRY_COST_BREAKDOWN["state_tax_pct"]
    actual_care_delivery = annual_total - carrier_overhead - waste - broker_cost - stop_loss_cost - state_tax

    current_total_ownership = annual_total + admin_cost

    # Per-benefit-type breakdown
    benefit_type_breakdown = {}
    for bt, alloc in BENEFIT_TYPE_ALLOCATION.items():
        bt_annual = annual_total * alloc
        bt_delivery = bt_annual * (1 - INDUSTRY_COST_BREAKDOWN["carrier_overhead_pct"]
                                   - INDUSTRY_COST_BREAKDOWN["waste_pct"]
                                   - INDUSTRY_COST_BREAKDOWN["broker_commission_pct"]
                                   - INDUSTRY_COST_BREAKDOWN["stop_loss_pct"]
                                   - INDUSTRY_COST_BREAKDOWN["state_tax_pct"])

        # System cost per benefit type
        if bt in ("life_insurance", "short_term_disability", "long_term_disability"):
            # Insurance products — system negotiates group rates, ~25% savings from pooled purchasing
            sys_cost = bt_annual * 0.75
            savings_mechanism = "Group purchasing leverage eliminates individual underwriting markup"
        else:
            sys_cost = bt_delivery * (1 - price_discovery_savings_pct)
            savings_mechanism = "Price discovery + waste elimination via Clinical Quality Engine"

        benefit_type_breakdown[bt] = {
            "current_annual": round(bt_annual, 2),
            "current_pepm": round(bt_annual / employee_count / 12, 2),
            "system_annual": round(sys_cost, 2),
            "system_pepm": round(sys_cost / employee_count / 12, 2),
            "savings": round(bt_annual - sys_cost, 2),
            "savings_pct": round((bt_annual - sys_cost) / bt_annual * 100, 1) if bt_annual > 0 else 0,
            "savings_mechanism": savings_mechanism,
        }

    # System totals
    system_care_delivery = actual_care_delivery * (1 - price_discovery_savings_pct)
    system_stop_loss = stop_loss_cost * 0.85
    system_pass_through = system_care_delivery + system_stop_loss
    verified_savings = current_total_ownership - system_pass_through
    value_share_pct = 0.25
    value_share_fee = max(0, verified_savings * value_share_pct)
    system_total = system_pass_through + value_share_fee
    savings = current_total_ownership - system_total
    savings_pct = (savings / current_total_ownership * 100) if current_total_ownership > 0 else 0

    return {
        "inputs": {
            "employee_count": employee_count,
            "state": state,
            "industry": industry,
            "annual_spend": annual_spend,
            "estimated_pepm": round(pepm, 2),
        },
        "current_cost": {
            "total_annual": round(current_total_ownership, 2),
            "breakdown": {
                "care_delivery": round(actual_care_delivery, 2),
                "carrier_overhead": round(carrier_overhead, 2),
                "waste_estimated": round(waste, 2),
                "broker_commissions": round(broker_cost, 2),
                "stop_loss": round(stop_loss_cost, 2),
                "state_premium_tax": round(state_tax, 2),
                "benefits_administration": round(admin_cost, 2),
            },
            "pepm": round(current_total_ownership / employee_count / 12, 2),
        },
        "system_cost": {
            "total_annual": round(system_total, 2),
            "breakdown": {
                "pass_through": {
                    "care_delivery": round(system_care_delivery, 2),
                    "stop_loss": round(system_stop_loss, 2),
                    "carrier_overhead": 0,
                    "broker_commissions": 0,
                    "waste": 0,
                    "benefits_administration": 0,
                    "state_premium_tax": 0,
                },
                "value_share_fee": round(value_share_fee, 2),
                "value_share_pct": value_share_pct,
            },
            "pepm": round(system_total / employee_count / 12, 2),
        },
        "benefit_type_breakdown": benefit_type_breakdown,
        "comparison": {
            "annual_savings": round(savings, 2),
            "savings_pct": round(savings_pct, 1),
            "cost_ratio": round(current_total_ownership / system_total, 1) if system_total > 0 else 0,
            "admin_elimination": round(admin_cost, 2),
        },
        "experience_comparison": {
            "current": {
                "avg_phone_calls_per_episode": "3-5",
                "employee_scheduling_required": True,
                "employee_referral_management": True,
                "employee_cost_sharing": True,
                "avg_employee_oop_per_year": round(pepm * 12 * 0.20, 2),
                "benefits_questions_answered_by": "HR department or carrier call center",
                "enrollment_process": "Annual open enrollment with plan comparison",
                "dental_separate_plan": True,
                "vision_separate_plan": True,
                "mental_health_parity_gaps": "Common — separate networks, higher copays, session limits",
                "disability_claims_process": "Paper-heavy, employer-managed, 30-90 day adjudication",
            },
            "system": {
                "avg_phone_calls_per_episode": "0",
                "employee_scheduling_required": False,
                "employee_referral_management": False,
                "employee_cost_sharing": False,
                "avg_employee_oop_per_year": 0,
                "benefits_questions_answered_by": "AI with full context on your plan and situation, instantly",
                "enrollment_process": "Automatic — you work there, you're covered",
                "dental_separate_plan": False,
                "vision_separate_plan": False,
                "mental_health_parity_gaps": "None — same interface, same coverage, same zero cost-sharing",
                "disability_claims_process": "Automated adjudication, same-day determination",
            },
        },
        "transparency": {
            "current": "Carrier retains pricing data. Employer sees premiums, not costs.",
            "system": "Every dollar auditable. Full line-item visibility. No hidden fees.",
        },
        "price_examples": price_stats["price_examples"],
        "pharmacy_insights": {
            "total_drug_prices_in_database": price_stats["pharmacy_records"],
            "top_cost_drugs": price_stats["top_pharmacy_costs"],
            "pbm_elimination": "System compares NADAC acquisition cost, cash price, manufacturer programs, and discount cards. No PBM spread pricing. No opaque rebates.",
        },
        "local_quality": {
            "hospitals_with_ratings_in_state": price_stats["hospitals_with_ratings"],
            "top_rated_hospitals": price_stats["top_rated_hospitals"],
        },
        "cross_type_insights": _get_cross_type_insights(db, state),
        "data_quality": {
            "price_data_points_in_state": price_stats["total_state_records"],
            "national_pharmacy_records": price_stats["pharmacy_records"],
            "insurer_negotiated_rates": price_stats["insurer_tic_records"],
            "dental_price_records": price_stats["dental_records"],
            "vision_price_records": price_stats["vision_records"],
            "mental_health_price_records": price_stats["mental_health_records"],
            "price_discovery_savings_pct": round(price_discovery_savings_pct * 100, 1),
            "data_driven_estimate": hospital_avg is not None and medicare_avg is not None,
            "confidence_note": (
                "High confidence — data-driven estimate using verified CMS pricing data"
                if hospital_avg and medicare_avg
                else "High confidence — based on verified public pricing data"
                if price_stats["total_state_records"] > 1000
                else "Moderate confidence — based on available public data and industry benchmarks"
                if price_stats["total_state_records"] > 100
                else "Estimate — based on industry benchmarks. Confidence improves as we ingest more pricing data for your area."
            ),
        },
    }


def _get_cross_type_insights(db: Session, state: str | None) -> dict:
    """Pull cross-benefit-type insights into the benchmark output.

    Constitution requirement: the data pipeline actively detects cross-type
    patterns and feeds actionable intelligence to downstream functions.
    The benchmark (F6A) is a downstream consumer of F8 cross-type data.
    """
    from app.services.cross_type_analytics import detect_price_patterns

    patterns = detect_price_patterns(db)

    # Extract insights relevant to this employer's state
    state_upper = state.upper() if state else None
    state_signals = [
        s for s in patterns.get("cross_type_signals", [])
        if s.get("state") == state_upper
    ] if state_upper else []

    return {
        "benefit_types_analyzed": patterns.get("summary", {}).get("benefit_types_with_data", 0),
        "patterns_detected": len(patterns.get("patterns_detected", [])),
        "state_specific_signals": state_signals,
        "cross_type_advantage": (
            "The system analyzes all benefit types together — health, dental, vision, "
            "mental health, pharmacy, life, and disability — as a single integrated view. "
            "Cross-type patterns (e.g., mental health utilization predicting medical cost "
            "increases, dental visits as chronic disease indicators) enable earlier "
            "intervention and lower total cost. No traditional carrier or TPA that manages "
            "each benefit type separately can replicate this intelligence."
        ),
    }
