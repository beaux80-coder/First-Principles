"""Broker Advisory Service (Function 10).

Constitution: Broker advisory fee is paid from value-share revenue, not
employer pass-through. Fully disclosed. No exclusive arrangements.
F6A benchmark serves as the broker's analytical tool.

The broker earns a percentage of the company's value-share fee — meaning
the broker is incentive-aligned: they earn more when the employer saves
more, and earn zero when savings are zero.
"""

import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.broker import Broker, BrokerActivation, BrokerStatus
from app.models.employer import Employer
from app.models.employee import Employee, EmployeeStatus
from app.services.pricing_engine import VALUE_SHARE_PCT

logger = logging.getLogger(__name__)

# Advisory fee bounds (percentage of value-share revenue)
DEFAULT_ADVISORY_FEE_PCT = 0.10  # 10% of value-share revenue
MAX_ADVISORY_FEE_PCT = 0.20     # Cap at 20%


def register_broker(
    db: Session,
    name: str,
    firm: str | None = None,
    email: str | None = None,
    advisory_fee_pct: float | None = None,
) -> dict:
    """Register a new broker on the platform.

    Advisory fee is a percentage of value-share revenue (not employer
    pass-through). Fully disclosed. No exclusive arrangements.
    """
    fee_pct = advisory_fee_pct if advisory_fee_pct is not None else DEFAULT_ADVISORY_FEE_PCT
    if fee_pct > MAX_ADVISORY_FEE_PCT:
        raise ValueError(
            f"Advisory fee cannot exceed {MAX_ADVISORY_FEE_PCT * 100}% "
            f"of value-share revenue."
        )
    if fee_pct < 0:
        raise ValueError("Advisory fee cannot be negative.")

    broker = Broker(
        name=name,
        firm=firm,
        email=email,
        advisory_fee_pct=fee_pct,
    )
    db.add(broker)
    db.commit()
    db.refresh(broker)

    return {
        "broker_id": str(broker.broker_id),
        "name": broker.name,
        "firm": broker.firm,
        "email": broker.email,
        "advisory_fee_pct": float(broker.advisory_fee_pct),
        "status": broker.status.value,
        "created_at": broker.created_at.isoformat(),
        "fee_disclosure": {
            "source": "value-share revenue",
            "not_from": "employer pass-through",
            "fully_disclosed": True,
            "no_exclusive_arrangements": True,
            "explanation": (
                f"Your advisory fee of {fee_pct * 100:.1f}% is calculated on the "
                f"company's value-share fee (which is {VALUE_SHARE_PCT * 100:.0f}% of "
                f"verified savings). You earn more only when employers save more. "
                f"If savings are zero, both the company and your fee are zero."
            ),
        },
    }


def get_broker_analytics(db: Session, broker_id: uuid.UUID) -> dict:
    """Broker's analytical dashboard.

    Shows the broker's activations, aggregate client outcomes,
    total advisory fees earned, and links to the F6A benchmark tool
    for prospecting.
    """
    broker = _get_broker_or_raise(db, broker_id)

    # Get all activations for this broker
    activations = db.query(BrokerActivation).filter(
        BrokerActivation.broker_id == broker_id
    ).all()

    employer_ids = [a.employer_id for a in activations]

    # Aggregate metrics across broker's clients
    total_employees = 0
    total_savings_annual = 0.0
    total_advisory_fee_annual = 0.0
    client_details = []

    for emp_id in employer_ids:
        employer = db.query(Employer).filter(Employer.employer_id == emp_id).first()
        if not employer:
            continue

        emp_count = db.query(func.count(Employee.employee_id)).filter(
            Employee.employer_id == emp_id,
            Employee.status == EmployeeStatus.active,
        ).scalar() or (employer.employee_count or 0)

        total_employees += emp_count

        # Compute savings for this employer
        try:
            from app.services.pricing_engine import compute_employer_rate
            rate = compute_employer_rate(db, emp_id)
            vs = rate.get("component_2_value_share", {})
            savings_pepm = vs.get("verified_savings_pepm", 0.0)
            value_share_pepm = vs.get("pepm", 0.0)
        except Exception:
            savings_pepm = 0.0
            value_share_pepm = 0.0

        employer_savings_annual = savings_pepm * emp_count * 12
        advisory_fee_annual = value_share_pepm * float(broker.advisory_fee_pct) * emp_count * 12

        total_savings_annual += employer_savings_annual
        total_advisory_fee_annual += advisory_fee_annual

        client_details.append({
            "employer_id": str(emp_id),
            "employer_name": employer.name,
            "employee_count": emp_count,
            "savings_pepm": round(savings_pepm, 2),
            "savings_annual": round(employer_savings_annual, 2),
            "advisory_fee_annual": round(advisory_fee_annual, 2),
        })

    return {
        "broker_id": str(broker_id),
        "broker_name": broker.name,
        "firm": broker.firm,
        "status": broker.status.value,
        "advisory_fee_pct": float(broker.advisory_fee_pct),
        "summary": {
            "total_activations": len(activations),
            "total_covered_lives": total_employees,
            "total_client_savings_annual": round(total_savings_annual, 2),
            "total_advisory_fee_annual": round(total_advisory_fee_annual, 2),
        },
        "clients": client_details,
        "analytical_tools": {
            "f6a_benchmark": {
                "endpoint": "/api/v1/benchmark",
                "description": (
                    "Use the F6A benchmark tool as your analytical prospecting tool. "
                    "Run benchmarks for prospective employers to show projected savings."
                ),
            },
        },
        "fee_transparency": {
            "fee_source": "Value-share revenue only",
            "fee_formula": (
                f"Advisory Fee = {float(broker.advisory_fee_pct) * 100:.1f}% x "
                f"Value-Share Fee (which = {VALUE_SHARE_PCT * 100:.0f}% x Verified Savings)"
            ),
            "effective_rate_on_savings": round(
                float(broker.advisory_fee_pct) * VALUE_SHARE_PCT * 100, 2
            ),
            "zero_from_employer_pass_through": True,
            "no_exclusive_arrangements": True,
        },
    }


def get_advisory_fee_structure(db: Session) -> dict:
    """Fee structure for brokers (transparent, disclosed).

    Constitution: Advisory fee paid from value-share revenue,
    not employer pass-through. Fully disclosed.
    """
    return {
        "advisory_fee": {
            "default_pct": DEFAULT_ADVISORY_FEE_PCT * 100,
            "max_pct": MAX_ADVISORY_FEE_PCT * 100,
            "source": "Value-share revenue",
            "not_from": "Employer pass-through costs",
        },
        "how_it_works": {
            "step_1": (
                f"Company earns a value-share fee = {VALUE_SHARE_PCT * 100:.0f}% of "
                f"verified employer savings vs. independently verifiable baseline."
            ),
            "step_2": (
                f"Broker advisory fee = broker's negotiated % of the company's "
                f"value-share fee (default {DEFAULT_ADVISORY_FEE_PCT * 100:.0f}%, "
                f"max {MAX_ADVISORY_FEE_PCT * 100:.0f}%)."
            ),
            "step_3": (
                "If verified savings are zero, the company earns zero and "
                "the broker earns zero. Incentives are fully aligned."
            ),
        },
        "example": {
            "employer_baseline_pepm": 650.00,
            "delivery_cost_pepm": 455.00,
            "verified_savings_pepm": 195.00,
            "value_share_fee_pepm": round(195.00 * VALUE_SHARE_PCT, 2),
            "advisory_fee_pepm_at_10pct": round(195.00 * VALUE_SHARE_PCT * DEFAULT_ADVISORY_FEE_PCT, 2),
            "note": "All figures are per employee per month.",
        },
        "disclosure": {
            "fully_disclosed_to_employer": True,
            "no_exclusive_arrangements": True,
            "no_hidden_commissions": True,
            "no_contingent_commissions": True,
            "employer_aware_of_broker_fee": True,
        },
    }


def get_broker_activations(db: Session, broker_id: uuid.UUID) -> dict:
    """Get all broker-driven activations (employers brought onto platform)."""
    broker = _get_broker_or_raise(db, broker_id)

    activations = db.query(BrokerActivation).filter(
        BrokerActivation.broker_id == broker_id
    ).order_by(BrokerActivation.activated_at.desc()).all()

    activation_list = []
    for a in activations:
        employer = db.query(Employer).filter(Employer.employer_id == a.employer_id).first()
        emp_count = 0
        if employer:
            emp_count = db.query(func.count(Employee.employee_id)).filter(
                Employee.employer_id == a.employer_id,
                Employee.status == EmployeeStatus.active,
            ).scalar() or (employer.employee_count or 0)

        activation_list.append({
            "activation_id": str(a.activation_id),
            "employer_id": str(a.employer_id),
            "employer_name": employer.name if employer else "Unknown",
            "employer_status": employer.status.value if employer else "unknown",
            "employee_count": emp_count,
            "activated_at": a.activated_at.isoformat(),
        })

    return {
        "broker_id": str(broker_id),
        "broker_name": broker.name,
        "total_activations": len(activation_list),
        "activations": activation_list,
    }


def _get_broker_or_raise(db: Session, broker_id: uuid.UUID) -> Broker:
    broker = db.query(Broker).filter(Broker.broker_id == broker_id).first()
    if not broker:
        raise ValueError(f"Broker {broker_id} not found")
    return broker
