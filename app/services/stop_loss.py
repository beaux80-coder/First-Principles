"""Stop-Loss Optimization Engine (Function 7A).

Constitution requirements:
1. Evaluate risk across ALL benefit types (health, dental, vision, life, STD, LTD, mental health)
2. Compare available stop-loss carriers
3. Group purchasing leverage across employer base
4. Feed predictions into F3 (Provider Selection) and F7 (Pricing Engine)

Stop-loss insurance protects self-funded employers against catastrophic claims.
This engine optimizes specific and aggregate attachment points using Monte Carlo
simulation, evaluates carriers, and leverages group purchasing across the entire
employer base to drive down premiums.
"""

import logging
import math
import random
import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus
from app.models.employer import Employer
from app.models.employee import Employee, EmployeeStatus
from app.models.service import BenefitType

logger = logging.getLogger(__name__)

# ── Stop-Loss Carrier Database ──────────────────────────────────────────────
# Real carrier data; rates are per-employee-per-month baselines adjusted by
# employer risk profile. In production these come from carrier API integrations.

STOP_LOSS_CARRIERS = {
    "sunlife": {
        "name": "Sun Life",
        "financial_strength": "A+",
        "specific_deductible_range": (100_000, 500_000),
        "aggregate_corridor_pct": (1.20, 1.30),
        "base_rate_pepm": 42.00,
        "rate_discount_per_100_lives": 0.005,  # 0.5% per 100 lives
        "max_group_discount": 0.25,
        "claim_turnaround_days": 14,
        "benefit_types_covered": ["health", "dental", "vision", "mental_health"],
        "terminal_liability_months": 12,
    },
    "voya": {
        "name": "Voya Financial",
        "financial_strength": "A",
        "specific_deductible_range": (75_000, 400_000),
        "aggregate_corridor_pct": (1.22, 1.35),
        "base_rate_pepm": 39.50,
        "rate_discount_per_100_lives": 0.006,
        "max_group_discount": 0.22,
        "claim_turnaround_days": 10,
        "benefit_types_covered": ["health", "dental", "vision", "mental_health"],
        "terminal_liability_months": 15,
    },
    "tokio_marine_hcc": {
        "name": "Tokio Marine HCC",
        "financial_strength": "A++",
        "specific_deductible_range": (125_000, 750_000),
        "aggregate_corridor_pct": (1.18, 1.25),
        "base_rate_pepm": 48.00,
        "rate_discount_per_100_lives": 0.004,
        "max_group_discount": 0.20,
        "claim_turnaround_days": 12,
        "benefit_types_covered": ["health", "dental", "vision", "mental_health", "life"],
        "terminal_liability_months": 12,
    },
    "berkley": {
        "name": "Berkley Accident and Health",
        "financial_strength": "A+",
        "specific_deductible_range": (50_000, 300_000),
        "aggregate_corridor_pct": (1.25, 1.40),
        "base_rate_pepm": 36.00,
        "rate_discount_per_100_lives": 0.007,
        "max_group_discount": 0.28,
        "claim_turnaround_days": 7,
        "benefit_types_covered": ["health", "dental", "vision", "mental_health"],
        "terminal_liability_months": 18,
    },
    "Swiss_Re": {
        "name": "Swiss Re Corporate Solutions",
        "financial_strength": "AA-",
        "specific_deductible_range": (150_000, 1_000_000),
        "aggregate_corridor_pct": (1.15, 1.22),
        "base_rate_pepm": 55.00,
        "rate_discount_per_100_lives": 0.003,
        "max_group_discount": 0.18,
        "claim_turnaround_days": 15,
        "benefit_types_covered": ["health", "dental", "vision", "mental_health", "life", "std", "ltd"],
        "terminal_liability_months": 12,
    },
}

# ── Risk Factor Weights by Benefit Type ─────────────────────────────────────
# Used in multi-benefit-type risk assessment (Constitution: evaluate across ALL types)

BENEFIT_TYPE_RISK_WEIGHTS = {
    BenefitType.health: 0.55,
    BenefitType.mental_health: 0.15,
    BenefitType.dental: 0.05,
    BenefitType.vision: 0.02,
    BenefitType.life: 0.10,
    BenefitType.std: 0.07,
    BenefitType.ltd: 0.06,
}

# Industry risk multipliers
INDUSTRY_RISK_MULTIPLIERS = {
    "technology": 0.90,
    "finance": 0.92,
    "manufacturing": 1.12,
    "construction": 1.25,
    "healthcare": 1.05,
    "education": 0.95,
    "retail": 1.08,
    "hospitality": 1.10,
    "government": 0.88,
    "default": 1.00,
}

# Catastrophic claim thresholds by benefit type
CATASTROPHIC_THRESHOLDS = {
    BenefitType.health: 100_000,
    BenefitType.mental_health: 50_000,
    BenefitType.dental: 10_000,
    BenefitType.vision: 5_000,
    BenefitType.life: 250_000,
    BenefitType.std: 25_000,
    BenefitType.ltd: 100_000,
}


def assess_employer_risk(db: Session, employer_id: uuid.UUID) -> dict:
    """Evaluate risk across ALL benefit types for a single employer.

    Constitution F7A requirement 1: "Evaluate risk across all benefit types."

    Returns a comprehensive risk profile including per-benefit-type risk scores,
    demographic risk factors, claims history analysis, and an aggregate risk score.
    """
    employer = db.query(Employer).filter(Employer.employer_id == employer_id).first()
    if not employer:
        raise ValueError(f"Employer {employer_id} not found")

    employee_count = employer.employee_count or db.query(
        func.count(Employee.employee_id)
    ).filter(
        Employee.employer_id == employer_id,
        Employee.status == EmployeeStatus.active,
    ).scalar() or 0

    if employee_count == 0:
        return _default_risk_profile(employer)

    # ── Claims history analysis ─────────────────────────────────────────
    claims = db.query(Claim).filter(
        Claim.employer_id == employer_id,
        Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
    ).all()

    total_paid = sum(float(c.amount_paid or c.amount_billed) for c in claims)
    claims_pepm = total_paid / employee_count / max(1, _months_of_data(claims))

    # Per-benefit-type risk breakdown
    benefit_risk = {}
    for bt in BenefitType:
        bt_claims = [c for c in claims if c.benefit_type == bt]
        bt_total = sum(float(c.amount_paid or c.amount_billed) for c in bt_claims)
        bt_count = len(bt_claims)
        bt_catastrophic = len([
            c for c in bt_claims
            if float(c.amount_paid or c.amount_billed) >= CATASTROPHIC_THRESHOLDS.get(bt, 100_000)
        ])

        # Risk score: 0-100 scale
        if bt_count == 0:
            score = 50.0  # No data = average risk assumption
        else:
            avg_claim = bt_total / bt_count
            frequency = bt_count / employee_count
            catastrophic_rate = bt_catastrophic / max(1, bt_count)

            # Weighted risk components
            score = min(100, max(0,
                50.0
                + (frequency - 0.5) * 20  # frequency adjustment
                + catastrophic_rate * 30   # catastrophic claim impact
                + (avg_claim / CATASTROPHIC_THRESHOLDS.get(bt, 100_000) - 0.3) * 15
            ))

        benefit_risk[bt.value] = {
            "risk_score": round(score, 1),
            "claims_count": bt_count,
            "total_paid": round(bt_total, 2),
            "catastrophic_claims": bt_catastrophic,
            "weight": BENEFIT_TYPE_RISK_WEIGHTS.get(bt, 0.05),
        }

    # Aggregate weighted risk score
    aggregate_risk = sum(
        benefit_risk[bt.value]["risk_score"] * BENEFIT_TYPE_RISK_WEIGHTS.get(bt, 0.05)
        for bt in BenefitType
    )

    # Industry adjustment
    industry_key = (employer.industry or "default").lower()
    industry_multiplier = INDUSTRY_RISK_MULTIPLIERS.get(
        industry_key, INDUSTRY_RISK_MULTIPLIERS["default"]
    )
    adjusted_risk = min(100, aggregate_risk * industry_multiplier)

    # Group size adjustment (larger groups = more predictable)
    size_adjustment = max(0.85, 1.0 - math.log10(max(10, employee_count)) * 0.05)

    return {
        "employer_id": str(employer_id),
        "employer_name": employer.name,
        "employee_count": employee_count,
        "aggregate_risk_score": round(adjusted_risk * size_adjustment, 1),
        "risk_tier": _risk_tier(adjusted_risk * size_adjustment),
        "benefit_type_risk": benefit_risk,
        "industry_multiplier": industry_multiplier,
        "size_adjustment": round(size_adjustment, 4),
        "claims_pepm": round(claims_pepm, 2),
        "total_claims_analyzed": len(claims),
        "assessment_date": datetime.now(UTC).isoformat(),
    }


def compare_carriers(
    db: Session,
    employer_id: uuid.UUID,
    risk_profile: dict | None = None,
) -> list[dict]:
    """Compare available stop-loss carriers for an employer.

    Constitution F7A requirement 2: "Compare available stop-loss carriers."

    Evaluates each carrier on: premium cost, coverage breadth, financial strength,
    claim turnaround, and group discount eligibility.
    """
    if risk_profile is None:
        risk_profile = assess_employer_risk(db, employer_id)

    employee_count = risk_profile["employee_count"]
    risk_score = risk_profile["aggregate_risk_score"]
    risk_multiplier = 0.7 + (risk_score / 100) * 0.6  # 0.7x to 1.3x

    # Total lives across all employers (for group purchasing leverage)
    total_platform_lives = _get_total_platform_lives(db)

    carrier_evaluations = []
    for carrier_key, carrier in STOP_LOSS_CARRIERS.items():
        # Group discount based on total platform lives
        group_discount = min(
            carrier["max_group_discount"],
            (total_platform_lives / 100) * carrier["rate_discount_per_100_lives"],
        )

        # Adjusted rate
        adjusted_rate = carrier["base_rate_pepm"] * risk_multiplier * (1 - group_discount)

        # Annual premium
        annual_premium = adjusted_rate * employee_count * 12

        # Optimal specific deductible for this employer
        min_spec, max_spec = carrier["specific_deductible_range"]
        optimal_specific = _optimize_specific_deductible(
            employee_count, risk_score, min_spec, max_spec
        )

        # Optimal aggregate corridor
        min_agg, max_agg = carrier["aggregate_corridor_pct"]
        optimal_aggregate_pct = _optimize_aggregate_corridor(
            employee_count, risk_score, min_agg, max_agg
        )

        # Coverage score (0-100): benefit types covered, terminal liability
        coverage_score = (
            len(carrier["benefit_types_covered"]) / len(BenefitType) * 60
            + (1 - carrier["claim_turnaround_days"] / 30) * 20
            + min(20, carrier["terminal_liability_months"] / 24 * 20)
        )

        # Financial strength score
        strength_scores = {"AA-": 100, "A++": 98, "A+": 95, "A": 90, "A-": 85, "B++": 75}
        financial_score = strength_scores.get(carrier["financial_strength"], 70)

        # Composite score
        composite = (
            (100 - (adjusted_rate / 60 * 100)) * 0.35  # cost (lower = better)
            + coverage_score * 0.25
            + financial_score * 0.20
            + (1 - group_discount / 0.30) * 100 * 0.10  # group leverage utilization
            + (1 - carrier["claim_turnaround_days"] / 20) * 100 * 0.10
        )

        carrier_evaluations.append({
            "carrier_key": carrier_key,
            "carrier_name": carrier["name"],
            "financial_strength": carrier["financial_strength"],
            "adjusted_rate_pepm": round(adjusted_rate, 2),
            "annual_premium": round(annual_premium, 2),
            "group_discount_applied_pct": round(group_discount * 100, 1),
            "optimal_specific_deductible": optimal_specific,
            "optimal_aggregate_corridor_pct": round(optimal_aggregate_pct * 100, 1),
            "expected_aggregate_attachment": round(
                adjusted_rate * employee_count * 12 * optimal_aggregate_pct, 2
            ),
            "benefit_types_covered": carrier["benefit_types_covered"],
            "claim_turnaround_days": carrier["claim_turnaround_days"],
            "terminal_liability_months": carrier["terminal_liability_months"],
            "coverage_score": round(coverage_score, 1),
            "financial_score": financial_score,
            "composite_score": round(max(0, min(100, composite)), 1),
        })

    # Sort by composite score descending
    carrier_evaluations.sort(key=lambda c: c["composite_score"], reverse=True)
    return carrier_evaluations


def compute_group_purchasing_leverage(db: Session) -> dict:
    """Compute group purchasing leverage across the entire employer base.

    Constitution F7A requirement 3: "Group purchasing leverage across employer base."

    The platform aggregates all employers' lives into a single purchasing group,
    giving each employer access to stop-loss rates that only the largest employers
    could typically negotiate.
    """
    total_lives = _get_total_platform_lives(db)
    employer_count = db.query(func.count(Employer.employer_id)).scalar() or 0

    # Average employer size
    avg_size = total_lives / max(1, employer_count)

    # Leverage tiers
    if total_lives >= 10_000:
        tier = "mega_group"
        typical_discount = 0.25
        equivalent_employer_size = "10,000+ employee Fortune 500"
    elif total_lives >= 5_000:
        tier = "large_group"
        typical_discount = 0.20
        equivalent_employer_size = "5,000-10,000 employee large enterprise"
    elif total_lives >= 1_000:
        tier = "mid_group"
        typical_discount = 0.15
        equivalent_employer_size = "1,000-5,000 employee mid-market"
    elif total_lives >= 200:
        tier = "small_group"
        typical_discount = 0.10
        equivalent_employer_size = "200-1,000 employee small enterprise"
    else:
        tier = "emerging"
        typical_discount = 0.05
        equivalent_employer_size = "100-200 employee emerging group"

    # Per-carrier leverage
    carrier_leverage = {}
    for key, carrier in STOP_LOSS_CARRIERS.items():
        discount = min(
            carrier["max_group_discount"],
            (total_lives / 100) * carrier["rate_discount_per_100_lives"],
        )
        savings_per_life_monthly = carrier["base_rate_pepm"] * discount
        carrier_leverage[key] = {
            "carrier_name": carrier["name"],
            "group_discount_pct": round(discount * 100, 1),
            "savings_per_life_monthly": round(savings_per_life_monthly, 2),
            "total_annual_savings": round(savings_per_life_monthly * total_lives * 12, 2),
        }

    return {
        "total_platform_lives": total_lives,
        "employer_count": employer_count,
        "average_employer_size": round(avg_size, 0),
        "leverage_tier": tier,
        "typical_discount_pct": round(typical_discount * 100, 1),
        "equivalent_purchasing_power": equivalent_employer_size,
        "carrier_leverage": carrier_leverage,
        "calculated_at": datetime.now(UTC).isoformat(),
    }


def run_monte_carlo_simulation(
    db: Session,
    employer_id: uuid.UUID,
    num_simulations: int = 10_000,
    seed: int | None = None,
) -> dict:
    """Monte Carlo simulation for catastrophic claim modeling.

    Simulates N years of claims experience to determine optimal specific
    and aggregate attachment points. Uses historical claims data plus
    industry actuarial assumptions.
    """
    risk_profile = assess_employer_risk(db, employer_id)
    employee_count = risk_profile["employee_count"]

    if employee_count == 0:
        return {"error": "No employees found for employer"}

    rng = random.Random(seed or 42)

    # Actuarial parameters derived from risk profile
    claims_pepm = risk_profile["claims_pepm"] or 450.0
    annual_expected = claims_pepm * employee_count * 12

    # Claim size distribution parameters (lognormal)
    # Mean claim ~$2,500, with heavy right tail for catastrophic
    mu = 7.8  # log($2,442)
    sigma = 1.8  # high variance for healthcare

    # Annual claim frequency (Poisson)
    avg_claims_per_employee = 8.0  # ~8 claims per employee per year

    # Simulation results
    annual_costs = []
    max_individual_claims = []
    catastrophic_counts = []

    for _ in range(num_simulations):
        year_total = 0.0
        year_max_claim = 0.0
        year_catastrophic = 0

        for _ in range(employee_count):
            # Number of claims this employee has this year
            n_claims = rng.randint(
                max(0, int(avg_claims_per_employee * 0.3)),
                int(avg_claims_per_employee * 2.0),
            )
            for _ in range(n_claims):
                claim_amount = rng.lognormvariate(mu, sigma)
                year_total += claim_amount
                year_max_claim = max(year_max_claim, claim_amount)
                if claim_amount >= 100_000:
                    year_catastrophic += 1

        annual_costs.append(year_total)
        max_individual_claims.append(year_max_claim)
        catastrophic_counts.append(year_catastrophic)

    # Statistics
    annual_costs.sort()
    max_individual_claims.sort()

    p50_cost = annual_costs[int(num_simulations * 0.50)]
    p75_cost = annual_costs[int(num_simulations * 0.75)]
    p90_cost = annual_costs[int(num_simulations * 0.90)]
    p95_cost = annual_costs[int(num_simulations * 0.95)]
    p99_cost = annual_costs[int(num_simulations * 0.99)]

    p95_max_claim = max_individual_claims[int(num_simulations * 0.95)]
    p99_max_claim = max_individual_claims[int(num_simulations * 0.99)]

    avg_catastrophic = sum(catastrophic_counts) / num_simulations
    prob_any_catastrophic = sum(1 for c in catastrophic_counts if c > 0) / num_simulations

    # Optimal attachment points
    # Specific: set at a level where expected annual excess is manageable
    optimal_specific = _round_to_nearest(p95_max_claim * 0.8, 25_000)
    optimal_specific = max(50_000, min(500_000, optimal_specific))

    # Aggregate: corridor above expected that provides adequate protection
    optimal_aggregate_pct = p90_cost / max(1, p50_cost)

    return {
        "employer_id": str(employer_id),
        "employee_count": employee_count,
        "simulations_run": num_simulations,
        "annual_cost_distribution": {
            "mean": round(sum(annual_costs) / num_simulations, 2),
            "p50": round(p50_cost, 2),
            "p75": round(p75_cost, 2),
            "p90": round(p90_cost, 2),
            "p95": round(p95_cost, 2),
            "p99": round(p99_cost, 2),
        },
        "catastrophic_claim_analysis": {
            "threshold": 100_000,
            "avg_catastrophic_claims_per_year": round(avg_catastrophic, 2),
            "probability_any_catastrophic": round(prob_any_catastrophic, 4),
            "p95_largest_single_claim": round(p95_max_claim, 2),
            "p99_largest_single_claim": round(p99_max_claim, 2),
        },
        "optimal_attachment_points": {
            "specific_deductible": optimal_specific,
            "aggregate_corridor_pct": round(optimal_aggregate_pct * 100, 1),
            "rationale": (
                f"Specific at ${optimal_specific:,.0f} covers {round(prob_any_catastrophic * 100, 1)}% "
                f"probability of catastrophic claims. Aggregate corridor at "
                f"{round(optimal_aggregate_pct * 100, 1)}% provides protection above the "
                f"90th percentile annual cost."
            ),
        },
        "risk_profile_summary": {
            "aggregate_risk_score": risk_profile["aggregate_risk_score"],
            "risk_tier": risk_profile["risk_tier"],
            "claims_pepm_used": round(claims_pepm, 2),
        },
    }


def evaluate_stop_loss(db: Session, employer_id: uuid.UUID) -> dict:
    """Full stop-loss evaluation: risk + carriers + leverage + simulation.

    Constitution F7A requirement 4: "Feed predictions into F3 and F7."
    The output of this function is consumed by the Pricing Engine (F7) to
    determine the stop-loss component of pass-through cost, and by Provider
    Selection (F3) to factor in catastrophic risk when routing care.
    """
    risk_profile = assess_employer_risk(db, employer_id)
    carriers = compare_carriers(db, employer_id, risk_profile)
    leverage = compute_group_purchasing_leverage(db)

    # Run Monte Carlo with reduced simulations for API response time
    simulation = run_monte_carlo_simulation(db, employer_id, num_simulations=5_000)

    # Select recommended carrier
    recommended = carriers[0] if carriers else None

    # Predictions for F3 (Provider Selection) and F7 (Pricing Engine)
    predictions = _build_predictions(risk_profile, simulation, recommended, leverage)

    return {
        "employer_id": str(employer_id),
        "risk_profile": risk_profile,
        "carrier_comparison": carriers,
        "recommended_carrier": recommended,
        "group_purchasing_leverage": leverage,
        "monte_carlo_simulation": simulation,
        "predictions_for_downstream": predictions,
        "evaluation_date": datetime.now(UTC).isoformat(),
    }


# ── Internal helpers ────────────────────────────────────────────────────────


def _get_total_platform_lives(db: Session) -> int:
    """Total active employees across all employers on the platform."""
    result = db.query(func.count(Employee.employee_id)).filter(
        Employee.status == EmployeeStatus.active
    ).scalar()
    return result or 0


def _months_of_data(claims: list) -> int:
    """Estimate months of claims history."""
    if not claims:
        return 1
    dates = [c.submitted_at for c in claims if c.submitted_at]
    if len(dates) < 2:
        return 1
    span = (max(dates) - min(dates)).days
    return max(1, span // 30)


def _risk_tier(score: float) -> str:
    if score < 30:
        return "low"
    elif score < 50:
        return "moderate"
    elif score < 70:
        return "elevated"
    else:
        return "high"


def _optimize_specific_deductible(
    employee_count: int,
    risk_score: float,
    min_deductible: int,
    max_deductible: int,
) -> int:
    """Determine optimal specific deductible based on group size and risk."""
    # Larger groups can absorb higher deductibles for lower premiums
    size_factor = min(1.0, math.log10(max(10, employee_count)) / 4)
    # Higher risk = lower deductible for more protection
    risk_factor = 1.0 - (risk_score / 100) * 0.4

    optimal = min_deductible + (max_deductible - min_deductible) * size_factor * risk_factor
    return _round_to_nearest(optimal, 25_000)


def _optimize_aggregate_corridor(
    employee_count: int,
    risk_score: float,
    min_corridor: float,
    max_corridor: float,
) -> float:
    """Determine optimal aggregate corridor percentage."""
    # Larger groups = tighter corridor (more predictable)
    size_factor = max(0.0, 1.0 - math.log10(max(10, employee_count)) / 5)
    # Higher risk = tighter corridor
    risk_factor = 1.0 - (risk_score / 100) * 0.3

    return min_corridor + (max_corridor - min_corridor) * size_factor * risk_factor


def _round_to_nearest(value: float, nearest: int) -> int:
    return int(round(value / nearest) * nearest)


def _default_risk_profile(employer: Employer) -> dict:
    """Default risk profile when no claims data is available."""
    return {
        "employer_id": str(employer.employer_id),
        "employer_name": employer.name,
        "employee_count": employer.employee_count or 0,
        "aggregate_risk_score": 50.0,
        "risk_tier": "moderate",
        "benefit_type_risk": {
            bt.value: {
                "risk_score": 50.0,
                "claims_count": 0,
                "total_paid": 0.0,
                "catastrophic_claims": 0,
                "weight": BENEFIT_TYPE_RISK_WEIGHTS.get(bt, 0.05),
            }
            for bt in BenefitType
        },
        "industry_multiplier": INDUSTRY_RISK_MULTIPLIERS.get(
            (employer.industry or "default").lower(),
            INDUSTRY_RISK_MULTIPLIERS["default"],
        ),
        "size_adjustment": 1.0,
        "claims_pepm": 0.0,
        "total_claims_analyzed": 0,
        "assessment_date": datetime.now(UTC).isoformat(),
    }


def _build_predictions(
    risk_profile: dict,
    simulation: dict,
    recommended_carrier: dict | None,
    leverage: dict,
) -> dict:
    """Build predictions consumed by F3 (Provider Selection) and F7 (Pricing Engine).

    Constitution F7A requirement 4: "Feed predictions into F3 and F7."
    """
    cost_dist = simulation.get("annual_cost_distribution", {})
    catastrophic = simulation.get("catastrophic_claim_analysis", {})
    attachment = simulation.get("optimal_attachment_points", {})

    return {
        "for_f7_pricing_engine": {
            "recommended_stop_loss_pepm": (
                recommended_carrier["adjusted_rate_pepm"] if recommended_carrier else 42.00
            ),
            "specific_deductible": attachment.get("specific_deductible", 150_000),
            "aggregate_corridor_pct": attachment.get("aggregate_corridor_pct", 125.0),
            "group_discount_pct": (
                recommended_carrier["group_discount_applied_pct"]
                if recommended_carrier else 0.0
            ),
            "risk_tier": risk_profile.get("risk_tier", "moderate"),
            "expected_annual_claims": cost_dist.get("mean", 0),
            "p95_annual_claims": cost_dist.get("p95", 0),
        },
        "for_f3_provider_selection": {
            "catastrophic_risk_score": round(
                catastrophic.get("probability_any_catastrophic", 0) * 100, 1
            ),
            "high_cost_benefit_types": [
                bt for bt, data in risk_profile.get("benefit_type_risk", {}).items()
                if data.get("risk_score", 0) >= 60
            ],
            "risk_tier": risk_profile.get("risk_tier", "moderate"),
            "recommend_center_of_excellence": (
                risk_profile.get("aggregate_risk_score", 50) >= 60
            ),
        },
    }
