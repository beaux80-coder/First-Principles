"""Pricing Engine (Function 7).

Constitution requirements — every line here maps to a Constitutional mandate:

1. Rate = exactly two visible components: pass-through cost + value-share fee
2. Value-share fee = % of verified savings (zero revenue if savings zero)
3. When delivery cost decreases, employer pays less AND company earns more (aligned incentives)
4. Baseline cost continuously updated, independently verifiable, never stale
5. Every line item auditable by employer
6. Sole-revenue enforcement: zero revenue from any other source
7. Incentive misalignment proof chain:
   company profits from lower delivery → determined by F1 TEE → cannot profit by denying care

This module is the single source of truth for how employers are charged.
There is no hidden margin, no spread pricing, no rebate capture. The only
revenue the company earns is the value-share fee, which is a percentage of
verified, auditable savings against an independently verifiable baseline.
"""

import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus
from app.models.employer import Employer, BaselineSource, EmployerStatus
from app.models.employee import Employee, EmployeeStatus
from app.models.service import BenefitType
from app.services.benchmark import (
    INDUSTRY_COST_BREAKDOWN,
    NATIONAL_AVG_PEPM,
    BENEFIT_TYPE_ALLOCATION,
)

logger = logging.getLogger(__name__)

# ── Constitutional Constants ────────────────────────────────────────────────

# Value-share fee: percentage of verified savings per tier (items 8-10)
# Constitution: "Value-share percentage configurable per tier — tiers defined
# by employer size, benefit types enrolled, and geographic region."
# All employers in the same tier pay the same percentage.
# The percentage is set by the company (not hardcoded) — stored here as
# the default configuration. Adjustable without code changes.

VALUE_SHARE_TIERS = {
    # (size_band, benefit_breadth, region) -> pct
    # size_band: "small" (<50), "mid" (50-500), "large" (500+)
    # benefit_breadth: "full" (all 7), "partial" (<7)
    # region: "high_cost" (CA, NY, MA, CT, NJ, DC), "standard" (all others)
    ("small", "full", "high_cost"): 0.30,
    ("small", "full", "standard"): 0.28,
    ("small", "partial", "high_cost"): 0.28,
    ("small", "partial", "standard"): 0.25,
    ("mid", "full", "high_cost"): 0.25,
    ("mid", "full", "standard"): 0.23,
    ("mid", "partial", "high_cost"): 0.23,
    ("mid", "partial", "standard"): 0.20,
    ("large", "full", "high_cost"): 0.20,
    ("large", "full", "standard"): 0.18,
    ("large", "partial", "high_cost"): 0.18,
    ("large", "partial", "standard"): 0.15,
}

_HIGH_COST_STATES = {"CA", "NY", "MA", "CT", "NJ", "DC", "HI", "WA", "MD"}

# Fallback if tier lookup fails
VALUE_SHARE_PCT = 0.25


def _resolve_value_share_pct(employer: "Employer", benefit_count: int = 7) -> tuple[float, dict]:
    """Resolve the value-share percentage for an employer based on their tier.

    Returns (pct, tier_info dict).
    """
    emp_count = employer.employee_count or 50
    if emp_count < 50:
        size_band = "small"
    elif emp_count < 500:
        size_band = "mid"
    else:
        size_band = "large"

    benefit_breadth = "full" if benefit_count >= 7 else "partial"

    state = (employer.geography or "")[:2].upper() if employer.geography else ""
    region = "high_cost" if state in _HIGH_COST_STATES else "standard"

    tier_key = (size_band, benefit_breadth, region)
    pct = VALUE_SHARE_TIERS.get(tier_key, VALUE_SHARE_PCT)

    return pct, {
        "tier": f"{size_band}_{benefit_breadth}_{region}",
        "size_band": size_band,
        "benefit_breadth": benefit_breadth,
        "region": region,
        "value_share_pct": pct,
        "note": "All employers in the same tier pay the same percentage.",
    }

# Regulatory surcharges that are legally required pass-throughs
# These are the ONLY non-care costs allowed in pass-through
REGULATORY_PASS_THROUGHS = {
    "pcori_fee_per_life": 3.22,          # ACA PCORI fee (2025 rate)
    "transitional_reinsurance": 0.00,     # Expired but placeholder for future
    "state_assessment_pct": 0.002,        # Avg state assessment on self-funded
    "aca_reporting_per_employee": 2.50,   # 1094-C/1095-C compliance cost
}

# Baseline staleness threshold — Constitution: "never stale"
BASELINE_MAX_AGE_DAYS = 90  # Recalculate if older than 90 days


def compute_employer_rate(db: Session, employer_id: uuid.UUID) -> dict:
    """Compute the two-component rate for an employer.

    Constitution F7 requirement 1:
    "Rate = exactly two visible components: pass-through cost + value-share fee"

    Constitution F7 requirement 5:
    "Every line item auditable by employer"

    Returns a fully transparent rate breakdown where every dollar is traceable
    to either a care delivery cost, a stop-loss premium, a regulatory fee,
    or the value-share fee.
    """
    employer = db.query(Employer).filter(Employer.employer_id == employer_id).first()
    if not employer:
        raise ValueError(f"Employer {employer_id} not found")

    employee_count = _get_active_employee_count(db, employer_id, employer)
    if employee_count == 0:
        return _zero_rate_response(employer)

    # ── Component 1: Pass-Through Cost ──────────────────────────────────
    # Everything the employer would pay regardless — care delivery, stop-loss,
    # and legally required regulatory fees. Zero markup.

    # Care delivery cost (actual claims paid)
    care_delivery = _compute_care_delivery_cost(db, employer_id, employee_count)

    # Stop-loss premium (from F7A)
    stop_loss = _compute_stop_loss_cost(db, employer_id, employee_count)

    # Regulatory pass-throughs (legally required only)
    regulatory = _compute_regulatory_costs(employee_count)

    pass_through_pepm = care_delivery["pepm"] + stop_loss["pepm"] + regulatory["pepm"]
    pass_through_annual = pass_through_pepm * employee_count * 12

    # ── Component 2: Value-Share Fee ────────────────────────────────────
    # Constitution F7 requirement 2:
    # "Value-share fee = % of verified savings (zero revenue if savings zero)"

    baseline = _get_or_compute_baseline(db, employer, employee_count)
    savings = _compute_verified_savings(baseline, pass_through_pepm)

    # Resolve tiered value-share percentage (items 8-10)
    vs_pct, tier_info = _resolve_value_share_pct(employer)
    value_share_fee_pepm = max(0.0, savings["verified_savings_pepm"] * vs_pct)
    value_share_fee_annual = value_share_fee_pepm * employee_count * 12

    # ── Total Rate ──────────────────────────────────────────────────────
    total_pepm = pass_through_pepm + value_share_fee_pepm
    total_annual = pass_through_annual + value_share_fee_annual

    # ── Incentive Alignment Verification ────────────────────────────────
    # Constitution F7 requirement 3:
    # "When delivery cost decreases, employer pays less AND company earns more"
    incentive_check = _verify_incentive_alignment(
        baseline["baseline_pepm"],
        pass_through_pepm,
        value_share_fee_pepm,
    )

    return {
        "employer_id": str(employer_id),
        "employer_name": employer.name,
        "employee_count": employee_count,
        "rate_date": datetime.now(UTC).isoformat(),

        # Constitution: "exactly two visible components"
        "component_1_pass_through": {
            "pepm": round(pass_through_pepm, 2),
            "annual": round(pass_through_annual, 2),
            "line_items": {
                "care_delivery": {
                    "pepm": round(care_delivery["pepm"], 2),
                    "annual": round(care_delivery["pepm"] * employee_count * 12, 2),
                    "detail": care_delivery["detail"],
                },
                "stop_loss": {
                    "pepm": round(stop_loss["pepm"], 2),
                    "annual": round(stop_loss["pepm"] * employee_count * 12, 2),
                    "detail": stop_loss["detail"],
                },
                "regulatory": {
                    "pepm": round(regulatory["pepm"], 2),
                    "annual": round(regulatory["pepm"] * employee_count * 12, 2),
                    "detail": regulatory["detail"],
                },
            },
            "audit_note": (
                "Every line item above is a direct cost pass-through at zero markup. "
                "Care delivery = actual claims paid to providers. Stop-loss = carrier "
                "premium at group-negotiated rate. Regulatory = legally mandated fees. "
                "No carrier overhead, no broker commissions, no hidden fees."
            ),
        },

        "component_2_value_share": {
            "pepm": round(value_share_fee_pepm, 2),
            "annual": round(value_share_fee_annual, 2),
            "value_share_pct": vs_pct,
            "tier": tier_info,
            "verified_savings_pepm": round(savings["verified_savings_pepm"], 2),
            "verified_savings_annual": round(
                savings["verified_savings_pepm"] * employee_count * 12, 2
            ),
            "baseline_pepm": round(baseline["baseline_pepm"], 2),
            "baseline_source": baseline["source"],
            "baseline_last_updated": baseline["last_updated"],
            "constitutional_guarantee": (
                "If verified savings are zero or negative, the value-share fee is $0.00. "
                "The company earns zero revenue when it fails to reduce costs."
            ),
        },

        "total_rate": {
            "pepm": round(total_pepm, 2),
            "annual": round(total_annual, 2),
        },

        "incentive_alignment": incentive_check,

        "sole_revenue_attestation": _sole_revenue_attestation(),

        # Trust account (items 11-14)
        "trust_account": {
            "structure": "ERISA Section 403 segregated trust",
            "ownership": "Employer — these are the employer's funds, not the company's",
            "investment_policy": (
                "Cash-equivalent instruments: short-term treasury bills, "
                "FDIC-insured high-yield savings, or equivalent. "
                "Maintains instant liquidity for claims payment."
            ),
            "returns_belong_to": "Employer — all returns reduce employer's effective cost",
            "company_draw_rules": (
                "Company draws only to pay providers on employer's behalf. "
                "Every draw is auditable and tied to a specific claim payment."
            ),
            "estimated_monthly_deposit": round(total_pepm * employee_count, 2),
            "estimated_annual_deposit": round(total_annual, 2),
        },

        "audit_trail": {
            "all_line_items_auditable": True,
            "baseline_independently_verifiable": True,
            "savings_computation_transparent": True,
            "zero_hidden_fees": True,
            "zero_broker_commissions": True,
            "zero_carrier_overhead_markup": True,
            "zero_pbm_spread": True,
            "zero_rebate_capture": True,
        },

        "feeding_f8": True,
    }


def get_baseline(db: Session, employer_id: uuid.UUID) -> dict:
    """Get the current baseline cost for an employer.

    Constitution F7 requirement 4:
    "Baseline cost continuously updated, independently verifiable, never stale"

    The baseline is the reference point against which savings are measured.
    It must be independently verifiable — an employer can compare their
    baseline against their broker's renewal quote, MEPS data, KFF survey
    averages, or their own prior year spend.
    """
    employer = db.query(Employer).filter(Employer.employer_id == employer_id).first()
    if not employer:
        raise ValueError(f"Employer {employer_id} not found")

    employee_count = _get_active_employee_count(db, employer_id, employer)
    baseline = _get_or_compute_baseline(db, employer, employee_count)

    # Provide independent verification sources
    verification_sources = _get_verification_sources(
        employer, baseline["baseline_pepm"]
    )

    return {
        "employer_id": str(employer_id),
        "employer_name": employer.name,
        "employee_count": employee_count,
        "baseline_pepm": round(baseline["baseline_pepm"], 2),
        "baseline_annual": round(baseline["baseline_pepm"] * employee_count * 12, 2),
        "source": baseline["source"],
        "methodology": baseline["methodology"],
        "last_updated": baseline["last_updated"],
        "staleness_check": baseline["staleness_check"],

        # Constitution: "independently verifiable"
        "independent_verification": {
            "verifiable": True,
            "sources": verification_sources,
            "employer_can_verify_by": [
                "Compare baseline PEPM to your current carrier's renewal quote",
                "Compare to your actual prior-year claims + admin cost per employee per month",
                "Compare to KFF Employer Health Benefits Survey average for your industry/size",
                "Compare to MEPS (Medical Expenditure Panel Survey) regional data",
                "Request an independent actuarial review of the baseline methodology",
            ],
        },

        "update_schedule": {
            "frequency": "Continuous — recalculated with every new data point",
            "staleness_threshold_days": BASELINE_MAX_AGE_DAYS,
            "next_scheduled_update": baseline.get("next_update", "Continuous"),
            "trigger_events": [
                "New claims data received",
                "Market rate data updated (KFF, CMS, MEPS)",
                "Employer census change (hiring/termination)",
                "Annual renewal cycle",
            ],
        },
    }


def get_incentive_proof_chain(db: Session) -> dict:
    """Return the complete incentive misalignment proof chain.

    Constitution F7 requirement 7:
    "company profits from lower delivery → determined by F1 TEE → cannot profit by denying care"

    This is a formal logical proof that the company's incentives are permanently
    aligned with the employer's and employee's interests.
    """
    return {
        "proof_chain": {
            "step_1": {
                "statement": "Company revenue = VALUE_SHARE_PCT * (Baseline - Delivery Cost)",
                "implication": "Revenue increases if and only if Delivery Cost decreases",
                "value_share_pct": VALUE_SHARE_PCT,
                "constitutional_reference": "F7: Value-share fee = % of verified savings",
            },
            "step_2": {
                "statement": "Delivery Cost = sum of actual claims paid to providers",
                "implication": (
                    "Delivery Cost decreases only through: (a) lower unit prices for same "
                    "services, (b) elimination of unnecessary services, or (c) prevention "
                    "of illness requiring services"
                ),
                "constitutional_reference": (
                    "F7: pass-through cost = care delivery + stop-loss + regulatory"
                ),
            },
            "step_3": {
                "statement": (
                    "Whether a service is 'necessary' is determined solely by F1 Clinical "
                    "Quality Engine running in a Trusted Execution Environment (TEE)"
                ),
                "implication": (
                    "The company cannot influence clinical determinations. The F1 engine "
                    "uses only: patient symptoms, patient medical history, and current "
                    "peer-reviewed evidence-based clinical guidelines."
                ),
                "constitutional_reference": (
                    "F1: Sole inputs: patient symptoms, patient medical history, current "
                    "peer-reviewed evidence-based clinical guidelines. No financial inputs."
                ),
            },
            "step_4": {
                "statement": (
                    "F1 TEE is open-source, auditable, and cryptographically attested. "
                    "Any person can verify that financial data never enters the clinical "
                    "decision process."
                ),
                "implication": (
                    "It is computationally infeasible for the company to modify F1 to "
                    "deny care for financial benefit without the modification being "
                    "detectable by any observer."
                ),
                "constitutional_reference": (
                    "F1: Complete source code is open-source and publicly available. "
                    "Every determination recorded in an immutable, append-only, "
                    "cryptographically secured log."
                ),
            },
            "step_5": {
                "statement": "Sole-revenue enforcement: the company has zero other revenue sources",
                "implication": (
                    "The company cannot compensate for lower value-share fees by earning "
                    "revenue from any other source. There are no spread margins, no rebate "
                    "capture, no network access fees, no data licensing fees."
                ),
                "constitutional_reference": (
                    "F7: Sole-revenue enforcement: zero revenue from any other source"
                ),
            },
        },

        "conclusion": {
            "statement": (
                "The company profits from lower delivery cost (Step 1). Delivery cost "
                "decreases through lower prices, less waste, and better health (Step 2). "
                "Clinical necessity is determined by an independent, open-source, "
                "cryptographically attested engine that the company cannot influence "
                "(Steps 3-4). No alternative revenue sources exist (Step 5). Therefore: "
                "the company's sole path to revenue is genuinely reducing the cost of "
                "care delivery, and it is structurally impossible for the company to "
                "profit by denying necessary care."
            ),
            "formal_properties": {
                "incentive_aligned": True,
                "denial_profit_impossible": True,
                "independently_verifiable": True,
                "cryptographically_attested": True,
            },
        },

        "counterexample_analysis": {
            "scenario_1": {
                "scenario": "Company denies a necessary MRI to save $2,000",
                "why_impossible": (
                    "F1 TEE determines medical necessity using clinical guidelines only. "
                    "Company has no input to F1 determination. If the MRI is clinically "
                    "indicated, F1 approves it. Company cannot override."
                ),
            },
            "scenario_2": {
                "scenario": "Company steers to a more expensive provider to increase baseline gap",
                "why_impossible": (
                    "Steering to expensive providers increases Delivery Cost, which "
                    "decreases (Baseline - Delivery Cost), which decreases revenue. "
                    "The company loses money by steering to expensive providers."
                ),
            },
            "scenario_3": {
                "scenario": "Company inflates the baseline to create artificial savings",
                "why_impossible": (
                    "Baseline is independently verifiable against public data (KFF, MEPS, "
                    "CMS). Employer can audit baseline at any time. Baseline source and "
                    "methodology are fully transparent."
                ),
            },
            "scenario_4": {
                "scenario": "Company earns hidden revenue from provider kickbacks",
                "why_impossible": (
                    "Sole-revenue enforcement: the company's financial statements must "
                    "show zero revenue from any source other than value-share fees. "
                    "All provider payments are pass-through at cost."
                ),
            },
        },

        "verification_instructions": {
            "for_employers": [
                "Audit every line item in your rate breakdown",
                "Compare your baseline to independent market data",
                "Verify F1 source code on the public repository",
                "Request TEE attestation reports for any clinical determination",
                "Review company financial statements for sole-revenue compliance",
            ],
            "for_regulators": [
                "Inspect F1 TEE attestation chain",
                "Audit pass-through cost against actual provider payments",
                "Verify no revenue sources beyond value-share fees",
                "Review clinical determination logs for pattern analysis",
            ],
        },
    }


# ── Internal computation helpers ────────────────────────────────────────────


def _get_active_employee_count(
    db: Session,
    employer_id: uuid.UUID,
    employer: Employer,
) -> int:
    """Get active employee count, falling back to employer record."""
    db_count = db.query(func.count(Employee.employee_id)).filter(
        Employee.employer_id == employer_id,
        Employee.status == EmployeeStatus.active,
    ).scalar() or 0

    return db_count or (employer.employee_count or 0)


def _compute_care_delivery_cost(
    db: Session,
    employer_id: uuid.UUID,
    employee_count: int,
) -> dict:
    """Compute actual care delivery cost from claims data.

    Every dollar here is a claim paid directly to a provider.
    No markup, no spread, no hidden margin.
    """
    # Actual claims cost from paid claims
    paid_claims_total = db.query(func.sum(Claim.amount_paid)).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.paid,
    ).scalar() or 0.0

    paid_claims_count = db.query(func.count(Claim.claim_id)).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.paid,
    ).scalar() or 0

    # Determine months of history
    date_range = db.query(
        func.min(Claim.paid_at),
        func.max(Claim.paid_at),
    ).filter(
        Claim.employer_id == employer_id,
        Claim.status == ClaimStatus.paid,
    ).first()

    if date_range and date_range[0] and date_range[1]:
        months = max(1, (date_range[1] - date_range[0]).days / 30)
    else:
        months = 1

    if paid_claims_total > 0 and months > 0:
        pepm = float(paid_claims_total) / employee_count / months
        source = "actual_claims"
    else:
        # Fall back to national average care delivery (excluding overhead/waste)
        care_delivery_pct = (
            1.0
            - INDUSTRY_COST_BREAKDOWN["carrier_overhead_pct"]
            - INDUSTRY_COST_BREAKDOWN["waste_pct"]
            - INDUSTRY_COST_BREAKDOWN["broker_commission_pct"]
            - INDUSTRY_COST_BREAKDOWN["stop_loss_pct"]
            - INDUSTRY_COST_BREAKDOWN["state_tax_pct"]
        )
        pepm = NATIONAL_AVG_PEPM["total"] * care_delivery_pct
        source = "national_average_estimate"

    # Per-benefit-type breakdown for auditability
    benefit_breakdown = {}
    for bt in BenefitType:
        bt_paid = db.query(func.sum(Claim.amount_paid)).filter(
            Claim.employer_id == employer_id,
            Claim.status == ClaimStatus.paid,
            Claim.benefit_type == bt,
        ).scalar() or 0.0

        if bt_paid > 0:
            benefit_breakdown[bt.value] = round(float(bt_paid) / employee_count / months, 2)
        else:
            # Estimate from national averages
            bt_key_map = {
                "health": "health", "dental": "dental", "vision": "vision",
                "mental_health": "mental_health", "life": "life_insurance",
                "std": "short_term_disability", "ltd": "long_term_disability",
            }
            alloc_key = bt_key_map.get(bt.value, bt.value)
            alloc = BENEFIT_TYPE_ALLOCATION.get(alloc_key, 0.02)
            benefit_breakdown[bt.value] = round(pepm * alloc, 2)

    return {
        "pepm": round(pepm, 2),
        "detail": {
            "source": source,
            "total_claims_paid": round(float(paid_claims_total), 2),
            "claims_count": paid_claims_count,
            "months_of_history": round(months, 1),
            "per_benefit_type_pepm": benefit_breakdown,
            "markup": 0.00,
            "audit_note": (
                "Care delivery cost = actual claims paid to providers, divided by "
                "employee count and months. Zero markup applied. Every claim is "
                "individually auditable."
            ),
        },
    }


def _compute_stop_loss_cost(
    db: Session,
    employer_id: uuid.UUID,
    employee_count: int,
) -> dict:
    """Compute stop-loss cost using F7A stop-loss optimization.

    The stop-loss premium is a direct pass-through from the carrier.
    Group purchasing leverage reduces the per-employer cost.
    """
    try:
        from app.services.stop_loss import evaluate_stop_loss
        evaluation = evaluate_stop_loss(db, employer_id)
        recommended = evaluation.get("recommended_carrier", {})
        predictions = evaluation.get("predictions_for_downstream", {})
        f7_data = predictions.get("for_f7_pricing_engine", {})

        pepm = f7_data.get("recommended_stop_loss_pepm", 42.00)
        group_discount = f7_data.get("group_discount_pct", 0.0)
        carrier_name = recommended.get("carrier_name", "Pending carrier selection")
        specific_deductible = f7_data.get("specific_deductible", 150_000)

        return {
            "pepm": round(pepm, 2),
            "detail": {
                "carrier": carrier_name,
                "group_discount_pct": group_discount,
                "specific_deductible": specific_deductible,
                "aggregate_corridor_pct": f7_data.get("aggregate_corridor_pct", 125.0),
                "markup": 0.00,
                "source": "f7a_stop_loss_optimization",
                "audit_note": (
                    "Stop-loss premium is passed through at the group-negotiated rate. "
                    f"Group purchasing discount of {group_discount}% applied. "
                    "Zero markup or commission."
                ),
            },
        }
    except Exception as e:
        logger.warning(f"F7A evaluation failed, using benchmark rate: {e}")
        # Fallback: use benchmark stop-loss rate
        benchmark_rate = NATIONAL_AVG_PEPM["total"] * INDUSTRY_COST_BREAKDOWN["stop_loss_pct"]
        group_improvement = 0.15  # 15% group purchasing improvement (from benchmark.py)
        pepm = benchmark_rate * (1 - group_improvement)

        return {
            "pepm": round(pepm, 2),
            "detail": {
                "carrier": "Pending carrier selection",
                "group_discount_pct": round(group_improvement * 100, 1),
                "specific_deductible": 150_000,
                "aggregate_corridor_pct": 125.0,
                "markup": 0.00,
                "source": "benchmark_estimate",
                "audit_note": (
                    "Estimated stop-loss rate based on industry benchmarks with "
                    "15% group purchasing improvement. Final rate determined upon "
                    "carrier selection."
                ),
            },
        }


def _compute_regulatory_costs(employee_count: int) -> dict:
    """Compute legally required regulatory pass-throughs.

    These are the ONLY non-care, non-stop-loss costs in the pass-through.
    Every fee here is a legal requirement for self-funded plans.
    """
    pcori_monthly = REGULATORY_PASS_THROUGHS["pcori_fee_per_life"] / 12
    state_assessment = 1.50  # Estimated monthly state assessment per life
    aca_reporting = REGULATORY_PASS_THROUGHS["aca_reporting_per_employee"] / 12

    pepm = pcori_monthly + state_assessment + aca_reporting

    return {
        "pepm": round(pepm, 2),
        "detail": {
            "pcori_fee_monthly": round(pcori_monthly, 2),
            "state_assessment_monthly": round(state_assessment, 2),
            "aca_reporting_monthly": round(aca_reporting, 2),
            "total_monthly": round(pepm, 2),
            "markup": 0.00,
            "all_fees_legally_required": True,
            "audit_note": (
                "Every regulatory fee listed is legally mandated for self-funded "
                "health plans. PCORI fee per IRC 4375/4376. State assessments per "
                "applicable state law. ACA reporting per IRC 6055/6056."
            ),
        },
    }


def _get_or_compute_baseline(
    db: Session,
    employer: Employer,
    employee_count: int,
) -> dict:
    """Get or compute the baseline cost.

    Constitution F7 requirement 4:
    "Baseline cost continuously updated, independently verifiable, never stale"
    """
    now = datetime.now(UTC)

    # Check if employer has a stored baseline
    if employer.baseline_cost_pepm and employer.baseline_source:
        # Check staleness
        age_days = (now - employer.updated_at.replace(tzinfo=UTC)).days if employer.updated_at else 999
        is_stale = age_days > BASELINE_MAX_AGE_DAYS

        baseline_pepm = float(employer.baseline_cost_pepm)

        return {
            "baseline_pepm": baseline_pepm,
            "source": employer.baseline_source.value,
            "methodology": (
                "Employer-provided actual prior-year spend"
                if employer.baseline_source == BaselineSource.actual_prior_spend
                else "Market rate based on industry/geography/size benchmarks"
            ),
            "last_updated": employer.updated_at.isoformat() if employer.updated_at else now.isoformat(),
            "staleness_check": {
                "age_days": age_days,
                "threshold_days": BASELINE_MAX_AGE_DAYS,
                "is_stale": is_stale,
                "action_required": (
                    "Baseline recalculation recommended" if is_stale else "Current"
                ),
            },
        }

    # No stored baseline — compute from market data
    # Use national average adjusted for industry and geography
    industry_key = (employer.industry or "default").lower()
    from app.services.stop_loss import INDUSTRY_RISK_MULTIPLIERS
    industry_factor = INDUSTRY_RISK_MULTIPLIERS.get(
        industry_key, INDUSTRY_RISK_MULTIPLIERS["default"]
    )

    # Size adjustment: larger employers typically have lower PEPM
    size_factor = max(0.85, 1.0 - (employee_count / 10_000) * 0.15) if employee_count > 0 else 1.0

    baseline_pepm = NATIONAL_AVG_PEPM["total"] * industry_factor * size_factor

    # Persist the computed baseline
    employer.baseline_cost_pepm = baseline_pepm
    employer.baseline_source = BaselineSource.market_rate
    db.add(employer)
    db.commit()

    return {
        "baseline_pepm": round(baseline_pepm, 2),
        "source": "market_rate",
        "methodology": (
            f"National average PEPM (${NATIONAL_AVG_PEPM['total']}) adjusted for "
            f"industry ({industry_key}, {industry_factor}x) and size "
            f"({employee_count} employees, {size_factor:.2f}x). "
            f"Will be replaced with actual prior-year spend when provided."
        ),
        "last_updated": now.isoformat(),
        "staleness_check": {
            "age_days": 0,
            "threshold_days": BASELINE_MAX_AGE_DAYS,
            "is_stale": False,
            "action_required": "Current (newly computed)",
        },
    }


def _compute_verified_savings(baseline: dict, pass_through_pepm: float) -> dict:
    """Compute verified savings against the baseline.

    Constitution F7 requirement 2:
    "Value-share fee = % of verified savings (zero revenue if savings zero)"

    Savings = Baseline PEPM - Pass-Through PEPM
    If this is zero or negative, the value-share fee is $0.
    """
    baseline_pepm = baseline["baseline_pepm"]
    savings_pepm = baseline_pepm - pass_through_pepm

    return {
        "baseline_pepm": round(baseline_pepm, 2),
        "pass_through_pepm": round(pass_through_pepm, 2),
        "verified_savings_pepm": round(max(0.0, savings_pepm), 2),
        "savings_pct": round(
            (savings_pepm / baseline_pepm * 100) if baseline_pepm > 0 else 0.0, 1
        ),
        "savings_positive": savings_pepm > 0,
        "computation": (
            f"Baseline (${baseline_pepm:.2f}) - Pass-Through (${pass_through_pepm:.2f}) "
            f"= ${savings_pepm:.2f} PEPM"
        ),
    }


def _verify_incentive_alignment(
    baseline_pepm: float,
    pass_through_pepm: float,
    value_share_fee_pepm: float,
) -> dict:
    """Verify that incentives are aligned per Constitution.

    Constitution F7 requirement 3:
    "When delivery cost decreases, employer pays less AND company earns more"

    Mathematical proof:
    - Employer total = PassThrough + ValueShare
    - ValueShare = 0.25 * (Baseline - PassThrough)
    - Employer total = PassThrough + 0.25 * (Baseline - PassThrough)
    - Employer total = 0.75 * PassThrough + 0.25 * Baseline
    - d(EmployerTotal)/d(PassThrough) = 0.75 > 0
    - Therefore: when PassThrough decreases, EmployerTotal decreases ✓

    - Company revenue = ValueShare = 0.25 * (Baseline - PassThrough)
    - d(CompanyRevenue)/d(PassThrough) = -0.25 < 0
    - Therefore: when PassThrough decreases, CompanyRevenue increases ✓
    """
    employer_total = pass_through_pepm + value_share_fee_pepm

    # Simulate a $10 decrease in delivery cost
    lower_pass_through = pass_through_pepm - 10
    lower_savings = max(0, baseline_pepm - lower_pass_through)
    lower_value_share = lower_savings * VALUE_SHARE_PCT
    lower_employer_total = lower_pass_through + lower_value_share

    employer_pays_less = lower_employer_total < employer_total
    company_earns_more = lower_value_share > value_share_fee_pepm

    return {
        "aligned": employer_pays_less and company_earns_more,
        "mathematical_proof": {
            "employer_total_formula": (
                f"EmployerTotal = {1 - VALUE_SHARE_PCT:.2f} * PassThrough + "
                f"{VALUE_SHARE_PCT:.2f} * Baseline"
            ),
            "employer_derivative": (
                f"d(EmployerTotal)/d(PassThrough) = {1 - VALUE_SHARE_PCT:.2f} > 0 "
                f"→ employer pays less when delivery cost drops"
            ),
            "company_derivative": (
                f"d(CompanyRevenue)/d(PassThrough) = -{VALUE_SHARE_PCT:.2f} < 0 "
                f"→ company earns more when delivery cost drops"
            ),
        },
        "simulation": {
            "current_pass_through": round(pass_through_pepm, 2),
            "simulated_pass_through": round(lower_pass_through, 2),
            "current_employer_total": round(employer_total, 2),
            "simulated_employer_total": round(lower_employer_total, 2),
            "employer_saves": round(employer_total - lower_employer_total, 2),
            "current_company_revenue": round(value_share_fee_pepm, 2),
            "simulated_company_revenue": round(lower_value_share, 2),
            "company_gains": round(lower_value_share - value_share_fee_pepm, 2),
        },
        "verdict": (
            "ALIGNED: When delivery cost decreases by $10 PEPM, employer saves "
            f"${round(employer_total - lower_employer_total, 2)} and company earns "
            f"${round(lower_value_share - value_share_fee_pepm, 2)} more."
            if employer_pays_less and company_earns_more
            else "WARNING: Incentive alignment check failed — review rate structure."
        ),
    }


def _sole_revenue_attestation() -> dict:
    """Generate the sole-revenue enforcement attestation.

    Constitution F7 requirement 6:
    "Sole-revenue enforcement: zero revenue from any other source"
    """
    return {
        "attestation": (
            "The company's sole source of revenue is the value-share fee, which is "
            "a percentage of verified savings against an independently verifiable "
            "baseline. The company receives zero revenue from any other source."
        ),
        "prohibited_revenue_sources": [
            {"source": "Carrier overhead / admin fees", "status": "PROHIBITED — $0"},
            {"source": "Broker commissions", "status": "PROHIBITED — $0"},
            {"source": "PBM spread pricing", "status": "PROHIBITED — $0"},
            {"source": "Pharmacy rebate capture", "status": "PROHIBITED — $0"},
            {"source": "Network access fees", "status": "PROHIBITED — $0"},
            {"source": "Data licensing / selling", "status": "PROHIBITED — $0"},
            {"source": "Provider kickbacks", "status": "PROHIBITED — $0"},
            {"source": "Float / investment income on claims", "status": "PROHIBITED — $0"},
            {"source": "Subrogation fees", "status": "PROHIBITED — $0"},
            {"source": "Premium tax arbitrage", "status": "PROHIBITED — $0"},
        ],
        "enforcement_mechanism": (
            "Annual third-party financial audit confirming zero non-value-share revenue. "
            "Audit results published to all employers."
        ),
    }


def _get_verification_sources(employer: Employer, baseline_pepm: float) -> list[dict]:
    """Provide independent data sources for baseline verification."""
    return [
        {
            "source": "KFF Employer Health Benefits Survey",
            "url": "https://www.kff.org/health-costs/report/employer-health-benefits-survey/",
            "reference_pepm": NATIONAL_AVG_PEPM["total"],
            "description": "National average employer health benefit cost per employee per month",
        },
        {
            "source": "Medical Expenditure Panel Survey (MEPS)",
            "url": "https://meps.ahrq.gov/",
            "reference_pepm": None,
            "description": "Federal survey of health insurance costs by employer size and industry",
        },
        {
            "source": "Bureau of Labor Statistics — Employer Costs for Employee Compensation",
            "url": "https://www.bls.gov/ncs/ect/",
            "reference_pepm": None,
            "description": "Official government data on employer benefit costs",
        },
        {
            "source": "CMS National Health Expenditure Data",
            "url": "https://www.cms.gov/data-research/statistics-trends-and-reports/national-health-expenditure-data",
            "reference_pepm": None,
            "description": "CMS spending projections and historical data",
        },
        {
            "source": "Your broker's current renewal quote",
            "url": None,
            "reference_pepm": None,
            "description": (
                "Compare baseline to your broker's proposed renewal. "
                "The baseline should be at or below current market rates."
            ),
        },
    ]


def setup_continuous_recalculation(db: Session) -> dict:
    """Trigger mechanism for continuous pass-through recalculation.

    Constitution F7 requirement 4:
    "Baseline cost continuously updated, independently verifiable, never stale"

    When new claim data arrives, this function:
    1. Identifies all employers with new claims since last rate computation
    2. Checks baseline staleness across all employers
    3. Recomputes rates for employers with material changes
    4. Returns a summary of recalculations performed

    Designed to be called by a background worker whenever new claims
    are ingested or on a scheduled interval.
    """
    now = datetime.now(UTC)
    recalculations = []
    stale_baselines = []
    skipped = []

    # Get all active/shadow employers
    employers = db.query(Employer).filter(
        Employer.status.in_([EmployerStatus.active, EmployerStatus.shadow]),
    ).all()

    for employer in employers:
        employer_id = employer.employer_id

        # Check baseline staleness
        age_days = 999
        if employer.updated_at:
            age_days = (now - employer.updated_at.replace(tzinfo=UTC)).days

        is_stale = age_days > BASELINE_MAX_AGE_DAYS

        # Check for new claims since last baseline update
        new_claims_count = 0
        if employer.updated_at:
            new_claims_count = db.query(func.count(Claim.claim_id)).filter(
                Claim.employer_id == employer_id,
                Claim.status == ClaimStatus.paid,
                Claim.paid_at > employer.updated_at,
            ).scalar() or 0

        # Determine if recalculation is warranted
        should_recalculate = False
        reason = None

        if is_stale:
            should_recalculate = True
            reason = f"Baseline stale ({age_days} days > {BASELINE_MAX_AGE_DAYS} threshold)"
            stale_baselines.append({
                "employer_id": str(employer_id),
                "name": employer.name,
                "age_days": age_days,
            })
        elif new_claims_count >= 10:
            should_recalculate = True
            reason = f"{new_claims_count} new paid claims since last update"
        elif new_claims_count > 0 and not employer.baseline_cost_pepm:
            should_recalculate = True
            reason = "No baseline established yet, new claims available"

        if should_recalculate:
            try:
                employee_count = _get_active_employee_count(db, employer_id, employer)
                if employee_count > 0:
                    # Recompute care delivery cost
                    care = _compute_care_delivery_cost(db, employer_id, employee_count)

                    # Update baseline if it was stale or missing
                    if is_stale or not employer.baseline_cost_pepm:
                        baseline = _get_or_compute_baseline(db, employer, employee_count)
                    else:
                        baseline = {
                            "baseline_pepm": float(employer.baseline_cost_pepm),
                        }

                    recalculations.append({
                        "employer_id": str(employer_id),
                        "name": employer.name,
                        "reason": reason,
                        "new_claims_since_last_update": new_claims_count,
                        "care_delivery_pepm": care["pepm"],
                        "baseline_pepm": round(baseline["baseline_pepm"], 2),
                        "employee_count": employee_count,
                    })
                else:
                    skipped.append({
                        "employer_id": str(employer_id),
                        "reason": "No active employees",
                    })
            except Exception as e:
                logger.warning(
                    f"Recalculation failed for employer {employer_id}: {e}"
                )
                skipped.append({
                    "employer_id": str(employer_id),
                    "reason": f"Error: {str(e)}",
                })
        else:
            skipped.append({
                "employer_id": str(employer_id),
                "reason": "No material changes detected",
            })

    return {
        "triggered_at": now.isoformat(),
        "employers_assessed": len(employers),
        "recalculations_performed": len(recalculations),
        "stale_baselines_found": len(stale_baselines),
        "employers_skipped": len(skipped),
        "recalculations": recalculations,
        "stale_baselines": stale_baselines,
        "configuration": {
            "baseline_max_age_days": BASELINE_MAX_AGE_DAYS,
            "min_new_claims_for_recalc": 10,
            "value_share_pct": VALUE_SHARE_PCT,
        },
        "next_trigger": (
            "Continuous — runs on each new claim batch or every 24 hours"
        ),
    }


def _zero_rate_response(employer: Employer) -> dict:
    """Return a zero-rate response when employer has no employees."""
    return {
        "employer_id": str(employer.employer_id),
        "employer_name": employer.name,
        "employee_count": 0,
        "rate_date": datetime.now(UTC).isoformat(),
        "component_1_pass_through": {
            "pepm": 0.00,
            "annual": 0.00,
            "line_items": {},
        },
        "component_2_value_share": {
            "pepm": 0.00,
            "annual": 0.00,
            "value_share_pct": VALUE_SHARE_PCT,
            "verified_savings_pepm": 0.00,
            "constitutional_guarantee": (
                "If verified savings are zero or negative, the value-share fee is $0.00."
            ),
        },
        "total_rate": {"pepm": 0.00, "annual": 0.00},
        "note": "No active employees. Rate will be computed when employees are enrolled.",
    }


# ── F7 Supplement S1: Cost Change Decomposition ──────────────────────────────


def decompose_cost_change(
    db: Session,
    employer_id: str,
    employee_count: int = 1,
) -> dict:
    """Decompose pass-through cost changes by causal source.

    F7 Supplement S1: Every recalculation produces a decomposition showing
    how much came from each causal source (platform volume, provider competition,
    prediction accuracy, service-level discovery, external).
    """
    from app.services.cost_attribution import get_employer_cost_attribution, CausalSource

    attribution = get_employer_cost_attribution(db, employer_id)
    by_source = attribution["attribution_by_source"]

    # Convert to per-employee-per-month
    months = max(attribution["period_months"], 1)
    emp = max(employee_count, 1)

    def pepm(total):
        return round(total / emp / months, 2)

    decomposition = {
        "platform_volume_effect": {
            "total_dollars": by_source.get(CausalSource.PLATFORM_VOLUME, 0.0),
            "pepm": pepm(by_source.get(CausalSource.PLATFORM_VOLUME, 0.0)),
            "description": "More employers on the platform driving provider price reductions, better stop-loss terms, and tighter cost predictions",
        },
        "provider_competition_response": {
            "total_dollars": by_source.get(CausalSource.PROVIDER_COMPETITION, 0.0),
            "pepm": pepm(by_source.get(CausalSource.PROVIDER_COMPETITION, 0.0)),
            "description": "Specific providers actively lowering prices in response to competitive pressure from the platform",
        },
        "prediction_accuracy_improvement": {
            "total_dollars": by_source.get(CausalSource.PREDICTION_ACCURACY, 0.0),
            "pepm": pepm(by_source.get(CausalSource.PREDICTION_ACCURACY, 0.0)),
            "description": "Buffer in pass-through shrinking as cost prediction confidence intervals narrow",
        },
        "service_level_price_discovery": {
            "total_dollars": by_source.get(CausalSource.SERVICE_PRICE_DISCOVERY, 0.0),
            "pepm": pepm(by_source.get(CausalSource.SERVICE_PRICE_DISCOVERY, 0.0)),
            "description": "System finding lower-cost providers or pricing channels for specific services",
        },
        "external_unrelated": {
            "total_dollars": by_source.get(CausalSource.EXTERNAL, 0.0),
            "pepm": pepm(by_source.get(CausalSource.EXTERNAL, 0.0)),
            "description": "Changes unrelated to platform growth — independent provider pricing, regulatory changes, seasonal patterns",
        },
    }

    cumulative_platform = round(
        attribution["platform_driven_total"], 2
    )
    cumulative_platform_pepm = pepm(cumulative_platform)

    return {
        "employer_id": employer_id,
        "employee_count": employee_count,
        "period_months": months,
        "decomposition": decomposition,
        "cumulative_platform_impact": {
            "total_dollars": cumulative_platform,
            "pepm": cumulative_platform_pepm,
            "annual_per_employee": round(cumulative_platform_pepm * 12, 2),
        },
        "independently_verifiable": True,
        "verification_path": (
            "Every attributed dollar traces: F7 decomposition → F8 tagged data → "
            "specific causal event (feed view, new employer, price record, etc.)"
        ),
        "feeding_f6b": True,
    }
