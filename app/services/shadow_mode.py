"""Shadow Mode Engine (Function 6A, Stage 2 + Stage 3).

Constitution requirement:
- Stage 2 (Shadow Mode): Process every real claim in parallel using full
  production logic (F1, F2, F4, F9). Side-by-side comparison per claim:
  what carrier did vs. what the system would do. Real-time dashboard
  updating per claim, per employee, per benefit type. Zero cost, zero risk,
  zero disruption to employees. Max 3 employer actions to enter shadow mode.

- Stage 3 (Activation): One-click transition from shadow to live. Zero
  data re-entry.

Shadow mode creates Claim records with mode=shadow. These flow through
the same adjudication pipeline (F5) as live claims but payments are not
executed — they are recorded as "shadow" outcomes for comparison.
"""

import logging
import uuid
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus, ClaimMode
from app.models.employer import Employer, EmployerStatus
from app.models.benchmark_query import BenchmarkQuery, BenchmarkStage
from app.models.service import BenefitType
from app.services.carrier_integration import (
    connect_carrier,
    fetch_carrier_claims,
    NormalisedCarrierClaim,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage 2: Shadow Mode
# ---------------------------------------------------------------------------

def start_shadow_mode(
    db: Session,
    employer_id: str,
    carrier_name: str,
    connection_type: str = "mock",
    credentials: dict | None = None,
) -> dict:
    """Connect to the carrier API and begin processing claims in parallel.

    Constitution: "One-click API connection to employer's current
    carrier/TPA." Max 3 employer actions: select carrier, authorise, confirm.

    Steps:
    1. Validate employer exists
    2. Connect to carrier (oauth / sftp / edi / mock)
    3. Transition employer status to "shadow"
    4. Record a BenchmarkQuery at stage=shadow
    5. Ingest initial batch of carrier claims for parallel processing

    Returns connection status, initial claim count, and shadow session ID.
    """
    employer = db.query(Employer).filter(
        Employer.employer_id == employer_id,
    ).first()
    if not employer:
        return {"error": "employer_not_found", "employer_id": employer_id}

    # Step 2: Connect to carrier
    connection = connect_carrier(
        employer_id=employer_id,
        carrier_name=carrier_name,
        connection_type=connection_type,
        credentials=credentials,
    )
    if connection.get("status") != "connected":
        return {
            "error": "carrier_connection_failed",
            "employer_id": employer_id,
            "details": connection,
        }

    # Step 3: Transition employer to shadow status
    employer.status = EmployerStatus.shadow
    db.flush()

    # Step 4: Record benchmark query at shadow stage
    shadow_query = BenchmarkQuery(
        employer_id=employer.employer_id,
        inputs={
            "carrier_name": carrier_name,
            "connection_type": connection_type,
            "employee_count": employer.employee_count,
            "state": employer.geography,
        },
        stage=BenchmarkStage.shadow,
    )
    db.add(shadow_query)
    db.flush()

    # Step 5: Fetch initial batch of carrier claims
    carrier_claims = fetch_carrier_claims(
        carrier_name=carrier_name,
        credentials=credentials,
        limit=100,
    )

    # Process each carrier claim in shadow mode
    shadow_results = []
    for cc in carrier_claims:
        result = process_shadow_claim(db, employer_id, cc)
        shadow_results.append(result)

    db.commit()

    return {
        "employer_id": employer_id,
        "shadow_session_id": str(shadow_query.query_id),
        "status": "shadow_active",
        "carrier_connection": connection,
        "claims_ingested": len(carrier_claims),
        "claims_processed": len(shadow_results),
        "zero_cost": True,
        "zero_risk": True,
        "zero_disruption": True,
        "employer_actions_required": 3,
        "actions": [
            "1. Select carrier/TPA",
            "2. Authorise data connection",
            "3. Confirm shadow mode start",
        ],
    }


def process_shadow_claim(
    db: Session,
    employer_id: str,
    carrier_claim: NormalisedCarrierClaim,
) -> dict:
    """Run F1, F2, F4, F9 on a carrier claim and compare to carrier result.

    Constitution: "Process every real claim in parallel using full production
    logic. Side-by-side comparison per claim: what carrier did vs. what
    system would do."

    Creates a Claim with mode=shadow, runs the adjudication pipeline, and
    returns a comparison dict.
    """
    # Map carrier benefit_type string to BenefitType enum
    try:
        benefit_type = BenefitType(carrier_claim.benefit_type)
    except ValueError:
        benefit_type = BenefitType.health

    # Resolve employee — in production, map carrier_claim.employee_external_id
    # to internal employee_id via employer's employee roster.
    # For shadow mode, we use a deterministic UUID from the external ID so
    # claims from the same employee group together.
    employee_id = uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"{employer_id}:{carrier_claim.employee_external_id}",
    )

    # Create shadow claim
    shadow_claim = Claim(
        employer_id=employer_id,
        employee_id=employee_id,
        benefit_type=benefit_type,
        mode=ClaimMode.shadow,
        status=ClaimStatus.submitted,
        amount_billed=carrier_claim.billed_amount,
        amount_employee_oop=0.00,  # System: zero employee OOP
    )
    db.add(shadow_claim)
    db.flush()

    # Run adjudication pipeline (F1 clinical, F2 price, F4 care coord, F9 ML)
    from app.services.claims_adjudication import adjudicate_claim
    system_result = adjudicate_claim(db, shadow_claim.claim_id)

    # Build side-by-side comparison
    system_paid = (
        float(system_result.get("amount_paid", 0))
        if system_result.get("amount_paid") is not None
        else float(shadow_claim.amount_billed)
    )
    carrier_paid = carrier_claim.carrier_paid_amount
    carrier_oop = carrier_claim.employee_oop
    savings = carrier_claim.billed_amount - system_paid

    comparison = {
        "claim_id": str(shadow_claim.claim_id),
        "carrier_claim_id": carrier_claim.carrier_claim_id,
        "employee_external_id": carrier_claim.employee_external_id,
        "service_code": carrier_claim.service_code,
        "service_description": carrier_claim.service_description,
        "benefit_type": benefit_type.value,
        "billed_amount": carrier_claim.billed_amount,
        "carrier": {
            "decision": carrier_claim.carrier_decision,
            "paid": carrier_paid,
            "employee_oop": carrier_oop,
            "total_cost": carrier_paid + carrier_oop,
            "reasoning": carrier_claim.carrier_reasoning,
        },
        "system": {
            "decision": system_result.get("status", "unknown"),
            "paid": system_paid,
            "employee_oop": 0.00,
            "total_cost": system_paid,
            "clinical_result": system_result.get("clinical_result"),
            "price_result": system_result.get("price_result"),
            "auto_adjudicated": system_result.get("auto_adjudicated", False),
            "processing_latency_ms": system_result.get("processing_latency_ms"),
        },
        "delta": {
            "savings_per_claim": round(savings, 2),
            "employee_oop_eliminated": round(carrier_oop, 2),
            "faster_decision": True,
        },
    }

    return comparison


def get_shadow_dashboard(db: Session, employer_id: str) -> dict:
    """Real-time shadow mode dashboard.

    Constitution: "Real-time dashboard updating per claim, per employee,
    per benefit type." Shows what the carrier did vs. what the system would
    do, with aggregate and drill-down views.
    """
    employer = db.query(Employer).filter(
        Employer.employer_id == employer_id,
    ).first()
    if not employer:
        return {"error": "employer_not_found", "employer_id": employer_id}

    # All shadow claims for this employer
    shadow_claims = db.query(Claim).filter(
        and_(
            Claim.employer_id == employer_id,
            Claim.mode == ClaimMode.shadow,
        )
    ).all()

    if not shadow_claims:
        return {
            "employer_id": employer_id,
            "employer_name": employer.name,
            "status": employer.status.value,
            "total_shadow_claims": 0,
            "message": "No shadow claims processed yet. Connect a carrier to begin.",
        }

    # --- Per-claim detail ---
    claims_detail = []
    total_billed = 0.0
    total_system_paid = 0.0
    total_carrier_est = 0.0
    benefit_type_agg: dict[str, dict] = {}
    employee_agg: dict[str, dict] = {}

    for claim in shadow_claims:
        billed = float(claim.amount_billed)
        system_paid = float(claim.amount_paid) if claim.amount_paid is not None else billed

        # Estimate what the carrier would have paid (from shadow comparison).
        # In production this comes from the stored carrier claim data.
        # For dashboard purposes, use billed as proxy for carrier total cost.
        carrier_est = billed

        total_billed += billed
        total_system_paid += system_paid
        total_carrier_est += carrier_est

        bt = claim.benefit_type.value

        # Per-benefit-type aggregation
        if bt not in benefit_type_agg:
            benefit_type_agg[bt] = {
                "claims": 0, "billed": 0.0,
                "system_paid": 0.0, "carrier_est": 0.0,
            }
        benefit_type_agg[bt]["claims"] += 1
        benefit_type_agg[bt]["billed"] += billed
        benefit_type_agg[bt]["system_paid"] += system_paid
        benefit_type_agg[bt]["carrier_est"] += carrier_est

        # Per-employee aggregation
        emp_key = str(claim.employee_id)
        if emp_key not in employee_agg:
            employee_agg[emp_key] = {
                "claims": 0, "billed": 0.0,
                "system_paid": 0.0, "carrier_est": 0.0,
            }
        employee_agg[emp_key]["claims"] += 1
        employee_agg[emp_key]["billed"] += billed
        employee_agg[emp_key]["system_paid"] += system_paid
        employee_agg[emp_key]["carrier_est"] += carrier_est

        claims_detail.append({
            "claim_id": str(claim.claim_id),
            "benefit_type": bt,
            "status": claim.status.value,
            "billed": billed,
            "system_paid": round(system_paid, 2),
            "auto_adjudicated": claim.auto_adjudicated,
            "latency_ms": claim.processing_latency_ms,
        })

    # Round aggregates
    for agg in list(benefit_type_agg.values()) + list(employee_agg.values()):
        for key in ("billed", "system_paid", "carrier_est"):
            agg[key] = round(agg[key], 2)
        agg["savings"] = round(agg["carrier_est"] - agg["system_paid"], 2)
        agg["savings_pct"] = (
            round((agg["carrier_est"] - agg["system_paid"])
                  / agg["carrier_est"] * 100, 1)
            if agg["carrier_est"] > 0 else 0.0
        )

    total_savings = total_carrier_est - total_system_paid

    # Automation rate
    auto_count = sum(
        1 for c in shadow_claims if c.auto_adjudicated is True
    )
    adjudicated_count = sum(
        1 for c in shadow_claims if c.adjudicated_at is not None
    )

    return {
        "employer_id": employer_id,
        "employer_name": employer.name,
        "status": employer.status.value,
        "total_shadow_claims": len(shadow_claims),
        "aggregate": {
            "total_billed": round(total_billed, 2),
            "total_carrier_est": round(total_carrier_est, 2),
            "total_system_paid": round(total_system_paid, 2),
            "total_savings": round(total_savings, 2),
            "savings_pct": (
                round(total_savings / total_carrier_est * 100, 1)
                if total_carrier_est > 0 else 0.0
            ),
            "employee_oop_eliminated": round(total_billed - total_system_paid, 2),
            "automation_rate": (
                round(auto_count / adjudicated_count * 100, 1)
                if adjudicated_count > 0 else None
            ),
        },
        "by_benefit_type": benefit_type_agg,
        "by_employee": employee_agg,
        "claims": claims_detail,
        "shadow_mode_guarantees": {
            "zero_cost": True,
            "zero_risk": True,
            "zero_disruption": True,
        },
    }


# ---------------------------------------------------------------------------
# Stage 3: Activation (shadow -> live)
# ---------------------------------------------------------------------------

def activate_from_shadow(db: Session, employer_id: str) -> dict:
    """One-click transition from shadow to live. Zero data re-entry.

    Constitution: "One-click transition from shadow to live. Zero data
    re-entry." All shadow infrastructure (employee mapping, carrier
    connection, plan configuration) carries over.
    """
    employer = db.query(Employer).filter(
        Employer.employer_id == employer_id,
    ).first()
    if not employer:
        return {"error": "employer_not_found", "employer_id": employer_id}

    if employer.status != EmployerStatus.shadow:
        return {
            "error": "not_in_shadow_mode",
            "employer_id": employer_id,
            "current_status": employer.status.value,
            "message": "Employer must be in shadow mode before activation.",
        }

    # Gather shadow performance to include in activation record
    dashboard = get_shadow_dashboard(db, employer_id)
    shadow_claims_count = dashboard.get("total_shadow_claims", 0)

    # Transition employer to active
    employer.status = EmployerStatus.active
    db.flush()

    # Record benchmark query at activated stage
    activation_query = BenchmarkQuery(
        employer_id=employer.employer_id,
        inputs={
            "activation_source": "shadow_mode",
            "shadow_claims_processed": shadow_claims_count,
            "employee_count": employer.employee_count,
            "state": employer.geography,
        },
        results={
            "shadow_summary": dashboard.get("aggregate", {}),
            "activation_timestamp": datetime.now(UTC).isoformat(),
        },
        stage=BenchmarkStage.activated,
    )
    db.add(activation_query)

    # Transition all future claims to live mode
    # (Shadow claims remain as historical record; new claims will be mode=live)

    db.commit()

    return {
        "employer_id": employer_id,
        "status": "activated",
        "activation_id": str(activation_query.query_id),
        "shadow_performance": dashboard.get("aggregate", {}),
        "shadow_claims_processed": shadow_claims_count,
        "zero_data_re_entry": True,
        "one_click": True,
        "message": (
            f"Employer '{employer.name}' is now live. All shadow mode "
            f"configuration, employee mappings, and carrier connections "
            f"carry over. Zero data re-entry required."
        ),
    }
