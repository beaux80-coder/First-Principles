"""Broker Channel Tools (Function 10).

Constitution: "Brokers are the distribution channel. They need tools to
compare beneflex vs incumbents, model savings, and onboard employers."

This module provides the analytical tools brokers need to:
1. Model projected savings for prospective employers
2. Compare beneflex vs incumbent carriers side-by-side
3. Generate broker proposals with savings projections and implementation timelines
"""

import logging
import uuid
from datetime import datetime, UTC, timedelta

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus
from app.models.employer import Employer
from app.models.employee import Employee, EmployeeStatus
from app.models.service import BenefitType
from app.services.benchmark import (
    NATIONAL_AVG_PEPM,
    INDUSTRY_COST_BREAKDOWN,
    BENEFIT_TYPE_ALLOCATION,
)
from app.services.pricing_engine import VALUE_SHARE_PCT

logger = logging.getLogger(__name__)


# ── Savings projection constants ─────────────────────────────────────────────

# Expected cost reduction by source
SAVINGS_SOURCES = {
    "carrier_overhead_elimination": {
        "pct": INDUSTRY_COST_BREAKDOWN["carrier_overhead_pct"],
        "description": "Eliminate carrier overhead (17% of premium)",
    },
    "waste_reduction": {
        "pct": INDUSTRY_COST_BREAKDOWN["waste_pct"] * 0.40,
        "description": "Reduce 40% of the 25% industry waste through clinical quality engine",
    },
    "broker_commission_elimination": {
        "pct": INDUSTRY_COST_BREAKDOWN["broker_commission_pct"],
        "description": (
            "Eliminate traditional broker commissions (broker advisory fee "
            "comes from value-share, not employer pass-through)"
        ),
    },
    "price_transparency": {
        "pct": 0.05,
        "description": "5% savings from transparent, competitive pricing",
    },
    "group_purchasing": {
        "pct": 0.02,
        "description": "2% savings from group purchasing leverage on stop-loss",
    },
}

# Industry risk multipliers for savings adjustment
INDUSTRY_RISK_FACTORS = {
    "technology": 0.90,
    "finance": 1.05,
    "healthcare": 1.10,
    "manufacturing": 1.05,
    "retail": 1.00,
    "education": 0.95,
    "government": 0.95,
    "default": 1.00,
}


def model_savings(db: Session, employer_profile: dict) -> dict:
    """Project savings vs current carrier based on employer demographics
    and claims history.

    Constitution: "Brokers need tools to model savings."

    Takes an employer profile (industry, size, geography, current spend)
    and projects what they would save switching to beneflex.

    Args:
        db: Database session
        employer_profile: Dict with:
            - employee_count: Number of employees
            - industry: Industry vertical
            - geography: State or region
            - current_pepm: Current per-employee-per-month cost (optional)
            - current_carrier: Current carrier name (optional)
            - claims_history: Recent claims data (optional)

    Returns:
        Detailed savings projection with per-source breakdown.
    """
    employee_count = employer_profile.get("employee_count", 100)
    industry = employer_profile.get("industry", "default").lower()
    geography = employer_profile.get("geography", "National")
    current_pepm = employer_profile.get("current_pepm")
    current_carrier = employer_profile.get("current_carrier", "Unknown")

    # Determine baseline PEPM
    if current_pepm and current_pepm > 0:
        baseline_pepm = current_pepm
        baseline_source = "employer_provided"
    else:
        # Estimate from national averages adjusted for industry/size
        industry_factor = INDUSTRY_RISK_FACTORS.get(industry, INDUSTRY_RISK_FACTORS["default"])

        # Size adjustment: larger employers typically have lower PEPM
        size_factor = max(0.85, 1.0 - (employee_count / 10_000) * 0.15)

        baseline_pepm = NATIONAL_AVG_PEPM["total"] * industry_factor * size_factor
        baseline_source = "market_estimate"

    # Calculate savings from each source
    savings_breakdown = {}
    total_savings_pct = 0.0

    for source_key, source_info in SAVINGS_SOURCES.items():
        savings_pct = source_info["pct"]
        savings_pepm = baseline_pepm * savings_pct
        savings_annual = savings_pepm * employee_count * 12

        savings_breakdown[source_key] = {
            "savings_pct": round(savings_pct * 100, 1),
            "savings_pepm": round(savings_pepm, 2),
            "savings_annual": round(savings_annual, 2),
            "description": source_info["description"],
        }
        total_savings_pct += savings_pct

    total_savings_pepm = baseline_pepm * total_savings_pct
    total_savings_annual = total_savings_pepm * employee_count * 12
    projected_pepm = baseline_pepm - total_savings_pepm

    # Value-share fee (beneflex revenue)
    value_share_pepm = total_savings_pepm * VALUE_SHARE_PCT
    value_share_annual = value_share_pepm * employee_count * 12

    # Net employer savings (after value-share fee)
    net_savings_pepm = total_savings_pepm - value_share_pepm
    net_savings_annual = net_savings_pepm * employee_count * 12

    return {
        "projection_date": datetime.now(UTC).isoformat(),

        "employer_profile": {
            "employee_count": employee_count,
            "industry": industry,
            "geography": geography,
            "current_carrier": current_carrier,
        },

        "baseline": {
            "current_pepm": round(baseline_pepm, 2),
            "current_annual": round(baseline_pepm * employee_count * 12, 2),
            "source": baseline_source,
        },

        "projected_with_beneflex": {
            "pepm": round(projected_pepm, 2),
            "annual": round(projected_pepm * employee_count * 12, 2),
        },

        "savings_summary": {
            "gross_savings_pepm": round(total_savings_pepm, 2),
            "gross_savings_annual": round(total_savings_annual, 2),
            "gross_savings_pct": round(total_savings_pct * 100, 1),
            "value_share_fee_pepm": round(value_share_pepm, 2),
            "value_share_fee_annual": round(value_share_annual, 2),
            "net_employer_savings_pepm": round(net_savings_pepm, 2),
            "net_employer_savings_annual": round(net_savings_annual, 2),
            "net_savings_pct": round(
                net_savings_pepm / max(baseline_pepm, 1) * 100, 1
            ),
        },

        "savings_by_source": savings_breakdown,

        "additional_benefits": {
            "zero_employee_cost_sharing": True,
            "all_7_benefit_types": True,
            "clinical_quality_engine": True,
            "real_time_claims_processing": True,
            "full_transparency": True,
        },

        "methodology_note": (
            "Savings projections are based on industry cost structure analysis. "
            "Actual savings are verified through shadow mode (F6A) before "
            "going live. The employer pays nothing until savings are proven."
        ),
    }


def compare_vs_incumbent(db: Session, employer_id: uuid.UUID) -> dict:
    """Side-by-side comparison of beneflex vs incumbent carrier.

    Constitution: "Brokers need tools to compare beneflex vs incumbents."

    For an existing employer, compares their actual beneflex results
    against what the incumbent carrier was delivering.
    """
    employer = _get_employer_or_raise(db, employer_id)
    employee_count = _get_active_employee_count(db, employer_id, employer)

    # Get beneflex actual performance
    total_paid = db.query(func.sum(Claim.amount_paid)).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.paid,
    ).scalar() or 0.0

    total_billed = db.query(func.sum(Claim.amount_billed)).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.paid,
    ).scalar() or 0.0

    total_claims = db.query(func.count(Claim.claim_id)).filter(
        Claim.employer_id == employer_id,
    ).scalar() or 0

    denied_claims = db.query(func.count(Claim.claim_id)).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.denied,
    ).scalar() or 0

    auto_adjudicated = db.query(func.count(Claim.claim_id)).filter(
        Claim.employer_id == employer_id,
        Claim.auto_adjudicated.is_(True),
    ).scalar() or 0

    avg_latency = db.query(func.avg(Claim.processing_latency_ms)).filter(
        Claim.employer_id == employer_id,
        Claim.processing_latency_ms.isnot(None),
    ).scalar()

    months = max(_get_months_of_data(db, employer_id), 1)
    actual_pepm = (
        round(float(total_paid) / max(employee_count, 1) / months, 2)
        if employee_count > 0 else 0.0
    )

    # Incumbent estimates (from baseline or industry averages)
    baseline_pepm = float(employer.baseline_cost_pepm) if employer.baseline_cost_pepm else NATIONAL_AVG_PEPM["total"]

    # Industry average metrics for comparison
    incumbent_metrics = {
        "pepm": round(baseline_pepm, 2),
        "annual": round(baseline_pepm * employee_count * 12, 2),
        "denial_rate_pct": 17.0,  # Industry average
        "avg_processing_days": 14,
        "employee_oop_pct": 28.0,  # Average employee cost-sharing
        "transparency": "Opaque",
        "benefit_types": "Typically 3-4",
        "overhead_pct": round(INDUSTRY_COST_BREAKDOWN["carrier_overhead_pct"] * 100, 1),
    }

    beneflex_metrics = {
        "pepm": actual_pepm,
        "annual": round(actual_pepm * employee_count * 12, 2),
        "denial_rate_pct": round(denied_claims / max(total_claims, 1) * 100, 1),
        "avg_processing_ms": round(float(avg_latency), 1) if avg_latency else None,
        "employee_oop_pct": 0.0,
        "transparency": "Full — every line item auditable",
        "benefit_types": "All 7 types from day one",
        "overhead_pct": 0.0,
    }

    savings_pepm = baseline_pepm - actual_pepm
    savings_annual = savings_pepm * employee_count * 12

    # Auto-adjudication rate
    auto_rate = round(auto_adjudicated / max(total_claims, 1) * 100, 1)

    # ── Full product comparison (not just cost) ─────────────────────
    product_comparison = {
        "cost": {
            "dimension": "Total Cost of Benefits",
            "incumbent": f"${round(baseline_pepm, 2):,.2f} PEPM",
            "beneflex": f"${actual_pepm:,.2f} PEPM",
            "advantage": f"${round(savings_pepm, 2):,.2f}/employee/month savings",
            "winner": "beneflex" if savings_pepm > 0 else "incumbent",
        },
        "employee_cost_sharing": {
            "dimension": "Employee Out-of-Pocket",
            "incumbent": "28% average cost-sharing (copays, deductibles, coinsurance)",
            "beneflex": "$0 — zero employee cost-sharing across all benefit types",
            "advantage": "Employees pay nothing out-of-pocket",
            "winner": "beneflex",
        },
        "benefit_coverage": {
            "dimension": "Benefit Types Covered",
            "incumbent": "Typically 3-4 types (health, dental, vision; others separate)",
            "beneflex": "All 7 types from day one (health, dental, vision, mental health, Rx, disability, life)",
            "advantage": "Complete coverage without separate carriers or enrollments",
            "winner": "beneflex",
        },
        "claims_speed": {
            "dimension": "Claims Processing Speed",
            "incumbent": "14 days average (industry standard)",
            "beneflex": f"{round(float(avg_latency), 1) if avg_latency else '<10'} ms average",
            "advantage": "Real-time vs weeks of waiting",
            "winner": "beneflex",
        },
        "automation": {
            "dimension": "Claims Automation",
            "incumbent": "Manual review for most claims, batch processing",
            "beneflex": f"{auto_rate}% auto-adjudicated, real-time processing",
            "advantage": "Fewer errors, faster resolution, lower admin burden",
            "winner": "beneflex",
        },
        "transparency": {
            "dimension": "Price and Decision Transparency",
            "incumbent": "Opaque — negotiated rates hidden, decision logic proprietary",
            "beneflex": "Full — every line item auditable, every price independently verifiable",
            "advantage": "Employer can verify every dollar spent",
            "winner": "beneflex",
        },
        "clinical_independence": {
            "dimension": "Clinical Decision Making",
            "incumbent": "Financial incentives influence coverage decisions (denial-driven revenue)",
            "beneflex": "F1 Clinical Quality Engine — evidence-based, no financial inputs",
            "advantage": "Decisions driven by clinical evidence, not profit motive",
            "winner": "beneflex",
        },
        "carrier_overhead": {
            "dimension": "Administrative Overhead",
            "incumbent": f"{round(INDUSTRY_COST_BREAKDOWN['carrier_overhead_pct'] * 100, 1)}% of premium goes to carrier overhead",
            "beneflex": "0% carrier overhead — pass-through cost model only",
            "advantage": "Every dollar goes to care, not carrier profit",
            "winner": "beneflex",
        },
        "pricing_model": {
            "dimension": "Revenue / Pricing Model",
            "incumbent": "Premium-based — carrier profits from higher premiums",
            "beneflex": "Value-share only — earns only when employer saves vs verifiable baseline",
            "advantage": "Incentives permanently aligned with employer outcomes",
            "winner": "beneflex",
        },
        "broker_compensation": {
            "dimension": "Broker Compensation",
            "incumbent": "Commission from premium (3-6%), creates misaligned incentives",
            "beneflex": "Advisory fee from value-share revenue, fully disclosed, no exclusivity",
            "advantage": "Broker earns more when employer saves more",
            "winner": "beneflex",
        },
        "onboarding": {
            "dimension": "Implementation / Onboarding",
            "incumbent": "60-90 day implementation, data re-entry, manual enrollment",
            "beneflex": "Shadow mode (zero-risk proof) then one-click activation, zero data re-entry",
            "advantage": "Prove savings before commitment, then instant go-live",
            "winner": "beneflex",
        },
    }

    return {
        "employer_id": str(employer_id),
        "employer_name": employer.name,
        "employee_count": employee_count,
        "comparison_date": datetime.now(UTC).isoformat(),

        "cost_comparison": {
            "incumbent": incumbent_metrics,
            "beneflex": beneflex_metrics,
            "savings_pepm": round(savings_pepm, 2),
            "savings_annual": round(savings_annual, 2),
            "savings_pct": round(
                savings_pepm / max(baseline_pepm, 1) * 100, 1
            ),
        },

        "product_comparison": product_comparison,

        "product_summary": {
            "total_dimensions": len(product_comparison),
            "beneflex_wins": sum(
                1 for d in product_comparison.values() if d["winner"] == "beneflex"
            ),
            "incumbent_wins": sum(
                1 for d in product_comparison.values() if d["winner"] == "incumbent"
            ),
            "note": (
                "This comparison covers the full product experience, not just cost. "
                "Beneflex delivers a fundamentally different benefits model: zero "
                "employee cost-sharing, all 7 benefit types, full transparency, "
                "and aligned incentives."
            ),
        },

        "constitutional_guarantees": {
            "zero_employee_cost_sharing": True,
            "every_line_item_auditable": True,
            "every_price_verifiable": True,
            "clinical_decisions_independent": True,
            "sole_revenue_from_savings": True,
            "all_7_benefit_types": True,
            "no_carrier_overhead": True,
            "broker_fee_from_value_share_only": True,
        },
    }


def generate_proposal(db: Session, employer_profile: dict) -> dict:
    """Generate broker proposal with projected savings, rate structure,
    and implementation timeline.

    Constitution: "Brokers need tools to onboard employers."

    Creates a complete proposal document for a broker to present to
    a prospective employer, including:
    - Projected savings with methodology
    - Rate structure explanation
    - Implementation timeline (benchmark -> shadow -> live)
    - Constitutional guarantees
    """
    # Run savings model first
    savings = model_savings(db, employer_profile)

    employee_count = employer_profile.get("employee_count", 100)
    industry = employer_profile.get("industry", "default")
    geography = employer_profile.get("geography", "National")
    current_carrier = employer_profile.get("current_carrier", "Unknown")

    # Implementation timeline
    today = datetime.now(UTC)
    timeline = {
        "phase_1_benchmark": {
            "name": "F6A Static Benchmark",
            "duration": "Immediate",
            "start": today.isoformat(),
            "description": (
                "Comprehensive analysis showing current benefits experience "
                "vs under beneflex. All 7 benefit types. Data-driven using "
                "real CMS price data. Zero obligation."
            ),
            "employer_actions": 0,
            "cost": "$0",
        },
        "phase_2_shadow": {
            "name": "F6A Shadow Mode",
            "duration": "30-90 days",
            "start": (today + timedelta(days=1)).isoformat(),
            "description": (
                "Run beneflex in parallel with current carrier. Compare every "
                "determination, price, and claim. Prove savings with real data. "
                "Zero cost, zero risk, zero disruption to employees."
            ),
            "employer_actions": 3,
            "cost": "$0",
        },
        "phase_3_activation": {
            "name": "Go Live",
            "duration": "One click",
            "start": (today + timedelta(days=91)).isoformat(),
            "description": (
                "One-click transition from shadow to live. Zero data re-entry. "
                "All shadow mode configuration carries over automatically."
            ),
            "employer_actions": 1,
            "cost": "Pass-through + value-share (only if savings are positive)",
        },
    }

    # Rate structure explanation
    rate_structure = {
        "component_1_pass_through": {
            "description": "Actual cost of care delivery at zero markup",
            "includes": [
                "Care delivery (claims paid to providers)",
                "Stop-loss premium (group-negotiated rate)",
                "Regulatory fees (legally mandated only)",
            ],
            "markup": "$0.00",
            "hidden_fees": "$0.00",
        },
        "component_2_value_share": {
            "description": (
                f"Value-share fee = {VALUE_SHARE_PCT * 100:.0f}% of verified savings"
            ),
            "zero_savings_guarantee": (
                "If savings are zero or negative, the value-share fee is $0.00. "
                "Beneflex earns zero revenue when it fails to reduce costs."
            ),
            "alignment": (
                "When delivery cost decreases, employer pays less AND beneflex "
                "earns more. Incentives are permanently aligned."
            ),
        },
    }

    return {
        "proposal_date": today.isoformat(),
        "proposal_type": "Broker Sales Proposal",

        "employer_profile": {
            "employee_count": employee_count,
            "industry": industry,
            "geography": geography,
            "current_carrier": current_carrier,
        },

        "savings_projection": savings["savings_summary"],
        "savings_by_source": savings["savings_by_source"],

        "rate_structure": rate_structure,
        "implementation_timeline": timeline,

        "value_proposition": {
            "cost_savings": (
                f"Projected {savings['savings_summary']['net_savings_pct']}% "
                f"net savings (${savings['savings_summary']['net_employer_savings_annual']:,.2f}/year)"
            ),
            "zero_employee_oop": "Zero employee cost-sharing across all 7 benefit types",
            "full_transparency": "Every line item auditable, every price verifiable",
            "clinical_quality": "Evidence-based clinical decisions, not financial",
            "risk_free_evaluation": "Shadow mode proves savings before commitment",
        },

        "constitutional_guarantees": [
            "Rate = exactly two components: pass-through + value-share",
            "Zero revenue if savings are zero",
            "Every line item auditable by employer",
            "Baseline independently verifiable",
            "Clinical decisions made by independent F1 engine (no financial inputs)",
            "Zero carrier overhead, zero broker commissions from pass-through",
            "All 7 benefit types from day one",
        ],

        "next_steps": [
            "Run the F6A benchmark for a detailed, data-driven comparison",
            "Enter shadow mode to prove savings with real claims data",
            "One-click activation when confident in the results",
        ],
    }


# ── Internal helpers ─────────────────────────────────────────────────────────


def _get_employer_or_raise(db: Session, employer_id: uuid.UUID) -> Employer:
    employer = db.query(Employer).filter(
        Employer.employer_id == employer_id
    ).first()
    if not employer:
        raise ValueError(f"Employer {employer_id} not found")
    return employer


def _get_active_employee_count(
    db: Session,
    employer_id: uuid.UUID,
    employer: Employer,
) -> int:
    db_count = db.query(func.count(Employee.employee_id)).filter(
        Employee.employer_id == employer_id,
        Employee.status == EmployeeStatus.active,
    ).scalar() or 0
    return db_count or (employer.employee_count or 0)


def _get_months_of_data(db: Session, employer_id: uuid.UUID) -> float:
    date_range = db.query(
        func.min(Claim.paid_at),
        func.max(Claim.paid_at),
    ).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.paid,
    ).first()
    if date_range and date_range[0] and date_range[1]:
        return max(1, (date_range[1] - date_range[0]).days / 30)
    return 1
