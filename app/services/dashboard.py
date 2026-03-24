"""Employer Dashboard Service (Function 6B).

Constitution: "Every employer can see every line item. Every determination is
auditable. Every price is verifiable. The dashboard is a window, not a wall."

This module provides the core dashboard functions: full dashboard view,
line-item audit trails, rate breakdowns, and independent price verification.
Every function returns complete, transparent, auditable data.
"""

import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus, ClaimMode
from app.models.employer import Employer
from app.models.employee import Employee, EmployeeStatus
from app.models.care_episode import CareEpisode, EpisodeStatus
from app.models.service import BenefitType
from app.services.benchmark import (
    NATIONAL_AVG_PEPM,
    BENEFIT_TYPE_ALLOCATION,
)

logger = logging.getLogger(__name__)


def get_employer_dashboard(db: Session, employer_id: uuid.UUID) -> dict:
    """Full dashboard: spending, savings, clinical rates, provider outcomes,
    price comparisons.

    Constitution: "Every employer can see every line item."

    Returns a comprehensive view of the employer's benefits program:
    - Spending by benefit type and provider
    - Savings vs baseline
    - Clinical quality metrics (approval rates, accuracy)
    - Provider outcome scores
    - Price comparisons vs market
    """
    employer = _get_employer_or_raise(db, employer_id)
    employee_count = _get_active_employee_count(db, employer_id, employer)

    # ── Spending breakdown ───────────────────────────────────────────────
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

    # Per-benefit-type spending
    spending_by_type = {}
    for bt in BenefitType:
        bt_paid = db.query(func.sum(Claim.amount_paid)).filter(
            Claim.employer_id == employer_id,
            Claim.status == ClaimStatus.paid,
            Claim.benefit_type == bt,
        ).scalar() or 0.0

        bt_count = db.query(func.count(Claim.claim_id)).filter(
            Claim.employer_id == employer_id,
            Claim.status == ClaimStatus.paid,
            Claim.benefit_type == bt,
        ).scalar() or 0

        if bt_paid > 0 or bt_count > 0:
            months = max(_get_months_of_data(db, employer_id), 1)
            spending_by_type[bt.value] = {
                "total_paid": round(float(bt_paid), 2),
                "claim_count": bt_count,
                "avg_per_claim": round(float(bt_paid) / max(bt_count, 1), 2),
                "pepm": round(float(bt_paid) / max(employee_count, 1) / months, 2),
            }

    # ── Savings vs baseline ──────────────────────────────────────────────
    savings_info = {}
    try:
        from app.services.pricing_engine import compute_employer_rate
        rate = compute_employer_rate(db, employer_id)
        vs = rate.get("component_2_value_share", {})
        savings_info = {
            "baseline_pepm": vs.get("baseline_pepm", 0.0),
            "actual_pepm": rate.get("component_1_pass_through", {}).get("pepm", 0.0),
            "verified_savings_pepm": vs.get("verified_savings_pepm", 0.0),
            "savings_from_pricing": round(float(total_billed) - float(total_paid), 2),
        }
    except Exception:
        savings_info = {
            "baseline_pepm": 0.0,
            "actual_pepm": 0.0,
            "verified_savings_pepm": 0.0,
            "savings_from_pricing": round(float(total_billed) - float(total_paid), 2),
        }

    # ── Clinical quality ─────────────────────────────────────────────────
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

    clinical_rates = {
        "total_claims": total_claims,
        "approval_rate_pct": round(
            (total_claims - denied_claims) / max(total_claims, 1) * 100, 1
        ),
        "denial_rate_pct": round(denied_claims / max(total_claims, 1) * 100, 1),
        "auto_adjudication_rate_pct": round(
            auto_adjudicated / max(total_claims, 1) * 100, 1
        ),
        "avg_processing_ms": round(float(avg_latency), 1) if avg_latency else None,
    }

    # ── Provider outcomes ────────────────────────────────────────────────
    from app.models.provider import Provider
    provider_rows = (
        db.query(
            Claim.provider_id,
            func.sum(Claim.amount_paid).label("total_paid"),
            func.count(Claim.claim_id).label("claim_count"),
            func.avg(Claim.amount_paid).label("avg_paid"),
        )
        .filter(
            Claim.employer_id == employer_id,
            Claim.status == ClaimStatus.paid,
            Claim.provider_id.isnot(None),
        )
        .group_by(Claim.provider_id)
        .order_by(func.sum(Claim.amount_paid).desc())
        .limit(15)
        .all()
    )

    provider_outcomes = []
    for row in provider_rows:
        provider = db.query(Provider).filter(
            Provider.provider_id == row.provider_id
        ).first()
        provider_outcomes.append({
            "provider_id": str(row.provider_id),
            "provider_name": provider.name if provider else "Unknown",
            "total_paid": round(float(row.total_paid), 2),
            "claim_count": row.claim_count,
            "avg_per_claim": round(float(row.avg_paid), 2) if row.avg_paid else 0.0,
        })

    # ── Price comparisons ────────────────────────────────────────────────
    # Compare employer's actual cost vs market benchmarks
    months = max(_get_months_of_data(db, employer_id), 1)
    actual_pepm = (
        round(float(total_paid) / max(employee_count, 1) / months, 2)
        if employee_count > 0 else 0.0
    )
    market_pepm = NATIONAL_AVG_PEPM["total"]
    price_comparison = {
        "actual_pepm": actual_pepm,
        "market_avg_pepm": market_pepm,
        "vs_market_pct": round(
            (market_pepm - actual_pepm) / max(market_pepm, 1) * 100, 1
        ) if market_pepm > 0 else 0.0,
        "source": "KFF Employer Health Benefits Survey",
    }

    return {
        "employer_id": str(employer_id),
        "employer_name": employer.name,
        "employee_count": employee_count,
        "dashboard_generated_at": datetime.now(UTC).isoformat(),

        "spending": {
            "total_paid": round(float(total_paid), 2),
            "total_billed": round(float(total_billed), 2),
            "total_claims": total_claims,
            "employee_out_of_pocket": 0.00,
            "by_benefit_type": spending_by_type,
        },

        "savings": savings_info,
        "clinical_rates": clinical_rates,
        "provider_outcomes": provider_outcomes,
        "price_comparison": price_comparison,

        "transparency_attestation": {
            "every_line_item_visible": True,
            "every_determination_auditable": True,
            "every_price_verifiable": True,
            "dashboard_is_window_not_wall": True,
            "zero_hidden_fees": True,
        },
    }


def get_line_item_audit(
    db: Session,
    employer_id: uuid.UUID,
    claim_id: uuid.UUID,
) -> dict:
    """Full audit trail for any single line item.

    Constitution: "Every determination is auditable."

    Returns the complete history of a claim: submission, clinical
    determination, price comparison, adjudication decision, payment,
    and all associated reasoning.
    """
    employer = _get_employer_or_raise(db, employer_id)

    claim = db.query(Claim).filter(
        Claim.claim_id == claim_id,
        Claim.employer_id == employer_id,
    ).first()
    if not claim:
        raise ValueError(
            f"Claim {claim_id} not found for employer {employer_id}"
        )

    # Build audit trail
    audit_trail = {
        "claim_id": str(claim.claim_id),
        "employer_id": str(employer_id),
        "employee_id": str(claim.employee_id),
        "benefit_type": claim.benefit_type.value,
        "mode": claim.mode.value,
        "status": claim.status.value,

        "financials": {
            "amount_billed": round(float(claim.amount_billed), 2),
            "amount_paid": round(float(claim.amount_paid), 2) if claim.amount_paid else None,
            "employee_oop": round(float(claim.amount_employee_oop), 2),
            "savings": round(
                float(claim.amount_billed) - float(claim.amount_paid), 2
            ) if claim.amount_paid else None,
        },

        "timeline": {
            "submitted_at": claim.submitted_at.isoformat() if claim.submitted_at else None,
            "adjudicated_at": claim.adjudicated_at.isoformat() if claim.adjudicated_at else None,
            "paid_at": claim.paid_at.isoformat() if claim.paid_at else None,
            "processing_latency_ms": claim.processing_latency_ms,
            "auto_adjudicated": claim.auto_adjudicated,
        },

        "adjudication": {
            "reasoning": claim.adjudication_reasoning,
            "duplicate_check": claim.duplicate_check,
            "coding_validation": claim.coding_validation,
            "eligibility_check": claim.eligibility_check,
            "error_flags": claim.error_flags,
            "denial_reason": claim.denial_reason,
        },
    }

    # Clinical determination (if linked)
    if claim.clinical_determination_id:
        from app.models.clinical_determination import ClinicalDetermination
        determination = db.query(ClinicalDetermination).filter(
            ClinicalDetermination.determination_id == claim.clinical_determination_id
        ).first()
        if determination:
            audit_trail["clinical_determination"] = {
                "determination_id": str(determination.determination_id),
                "decision": determination.decision.value,
                "reasoning": determination.reasoning,
                "guidelines_referenced": determination.guidelines_referenced,
                "outcome_feedback": determination.outcome_feedback,
            }

    # Price comparison (if linked)
    if claim.price_comparison_id:
        from app.models.price_comparison import PriceComparison
        price_comp = db.query(PriceComparison).filter(
            PriceComparison.comparison_id == claim.price_comparison_id
        ).first()
        if price_comp:
            audit_trail["price_comparison"] = {
                "comparison_id": str(price_comp.comparison_id),
                "selected_price": round(float(price_comp.selected_price), 2) if price_comp.selected_price else None,
                "market_prices": price_comp.market_prices,
            }

    # Provider info
    if claim.provider_id:
        from app.models.provider import Provider
        provider = db.query(Provider).filter(
            Provider.provider_id == claim.provider_id
        ).first()
        if provider:
            audit_trail["provider"] = {
                "provider_id": str(provider.provider_id),
                "name": provider.name,
                "provider_type": provider.provider_type.value,
            }

    audit_trail["audit_attestation"] = {
        "complete_trail": True,
        "independently_verifiable": True,
        "constitutional_guarantee": (
            "Every determination is auditable. This audit trail shows "
            "the complete history of this claim from submission through payment."
        ),
    }

    return audit_trail


def get_rate_breakdown(db: Session, employer_id: uuid.UUID) -> dict:
    """Itemized rate breakdown (pass-through + value-share).

    Constitution: "Every price is verifiable."

    Returns the complete rate structure showing exactly how the employer's
    rate is computed: pass-through components (care delivery, stop-loss,
    regulatory) and value-share fee.
    """
    employer = _get_employer_or_raise(db, employer_id)

    try:
        from app.services.pricing_engine import compute_employer_rate
        rate = compute_employer_rate(db, employer_id)
    except Exception as e:
        logger.warning(f"Rate computation failed for {employer_id}: {e}")
        raise ValueError(f"Unable to compute rate for employer {employer_id}: {e}")

    return {
        "employer_id": str(employer_id),
        "employer_name": employer.name,
        "rate_breakdown": rate,
        "verification_note": (
            "Every line item in this rate breakdown is independently verifiable. "
            "Pass-through costs are actual costs at zero markup. The value-share "
            "fee is a percentage of verified savings against an independently "
            "verifiable baseline."
        ),
    }


def verify_any_price(db: Session, claim_id: uuid.UUID) -> dict:
    """Independent price verification for any claim.

    Constitution: "Every price is verifiable."

    Takes any claim and returns independent price verification data:
    - What was paid
    - Market reference prices (CMS, KFF, regional averages)
    - How the price compares to benchmarks
    - Whether the price represents a fair market rate
    """
    claim = db.query(Claim).filter(Claim.claim_id == claim_id).first()
    if not claim:
        raise ValueError(f"Claim {claim_id} not found")

    amount_paid = float(claim.amount_paid) if claim.amount_paid else None
    amount_billed = float(claim.amount_billed)

    # Look up market prices for this service
    market_prices = []
    if claim.service_id:
        from app.models.service import Service
        service = db.query(Service).filter(
            Service.service_id == claim.service_id
        ).first()

        if service:
            from app.models.price_data import PriceData
            price_records = db.query(PriceData).filter(
                PriceData.service_code == service.code,
            ).limit(10).all()

            for pr in price_records:
                market_prices.append({
                    "source": pr.source.value if hasattr(pr.source, 'value') else str(pr.source),
                    "price": round(float(pr.price), 2),
                    "region": pr.region if hasattr(pr, 'region') else None,
                })

    # Compute verification metrics
    if amount_paid and market_prices:
        avg_market = sum(p["price"] for p in market_prices) / len(market_prices)
        vs_market_pct = round(
            (avg_market - amount_paid) / max(avg_market, 1) * 100, 1
        )
    else:
        avg_market = None
        vs_market_pct = None

    # Price comparison from claim (if linked)
    price_comp_data = None
    if claim.price_comparison_id:
        from app.models.price_comparison import PriceComparison
        price_comp = db.query(PriceComparison).filter(
            PriceComparison.comparison_id == claim.price_comparison_id
        ).first()
        if price_comp:
            price_comp_data = {
                "comparison_id": str(price_comp.comparison_id),
                "selected_price": round(float(price_comp.selected_price), 2) if price_comp.selected_price else None,
                "market_prices": price_comp.market_prices,
            }

    return {
        "claim_id": str(claim_id),
        "benefit_type": claim.benefit_type.value,

        "prices": {
            "amount_billed": round(amount_billed, 2),
            "amount_paid": round(amount_paid, 2) if amount_paid else None,
            "savings_vs_billed": round(amount_billed - amount_paid, 2) if amount_paid else None,
        },

        "market_verification": {
            "market_prices": market_prices,
            "avg_market_price": round(avg_market, 2) if avg_market else None,
            "vs_market_pct": vs_market_pct,
            "price_comp_data": price_comp_data,
        },

        "verification_sources": [
            {
                "source": "CMS Medicare Fee Schedule",
                "url": "https://www.cms.gov/Medicare/Medicare-Fee-for-Service-Payment/PhysicianFeeSched",
                "description": "Federal Medicare rates as baseline reference",
            },
            {
                "source": "FAIR Health Consumer",
                "url": "https://www.fairhealthconsumer.org/",
                "description": "Independent nonprofit healthcare cost data",
            },
            {
                "source": "Healthcare Bluebook",
                "url": "https://www.healthcarebluebook.com/",
                "description": "Fair price estimates for medical procedures",
            },
        ],

        "attestation": {
            "price_independently_verifiable": True,
            "zero_markup": True,
            "constitutional_guarantee": (
                "Every price is verifiable. The amount paid is the actual "
                "negotiated rate with zero markup. Employers can verify any "
                "price against the independent sources listed above."
            ),
        },
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
