"""Shadow mode runner for parallel engine comparison.

Lets you run the orchestrator engine against a batch of real (or
backfilled) claims WITHOUT affecting any payment, then compare the
decisions to a reference set (e.g. the carrier's actual decisions, or
the previous engine version). The output is a disagreement report that
engineers and clinicians can review before cutting over.

Usage (Python):

    from app.services.shadow_runner import run_shadow_batch, ShadowReference
    references = [
        ShadowReference(
            claim_id=claim_id,
            reference_decision="approved",
            reference_amount=150.0,
            reference_reason="Carrier paid",
        ),
        ...
    ]
    report = run_shadow_batch(db, references)
    print(report["agreement_rate"])
    print(report["disagreements"][:10])

The engine is called on every claim in the list. Each claim has its
mode set to `shadow` before running so the payment side effects (Stripe
transfer, etc.) are skipped by the existing payment execution code.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimMode, ClaimStatus


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowReference:
    """Reference decision for a single claim.

    `reference_decision` is one of: "approved" | "denied" | "paid" |
    "flagged_for_review". `reference_amount` is what the reference
    system paid. `reference_reason` is a short audit string."""
    claim_id: uuid.UUID
    reference_decision: str
    reference_amount: Optional[float] = None
    reference_reason: Optional[str] = None


@dataclass
class Disagreement:
    claim_id: str
    engine_decision: str
    reference_decision: str
    engine_amount: Optional[float]
    reference_amount: Optional[float]
    amount_delta: Optional[float]
    engine_reasoning: Optional[str]
    reference_reason: Optional[str]


def _is_approved(decision: str) -> bool:
    return decision in ("approved", "paid")


def _normalize_decision(decision: str) -> str:
    if decision in ("approved", "paid"):
        return "approved"
    return decision


def _run_single_shadow(db: Session, reference: ShadowReference) -> dict:
    """Run the orchestrator engine on a single claim in shadow mode and
    return the decision payload along with the reference for
    comparison."""
    from app.services.claims_adjudication import adjudicate_claim

    claim = db.query(Claim).filter(
        Claim.claim_id == reference.claim_id
    ).first()
    if claim is None:
        return {
            "claim_id": str(reference.claim_id),
            "error": "claim_not_found",
            "reference_decision": reference.reference_decision,
        }

    # Snapshot the original state so we can restore it after shadowing.
    original_mode = claim.mode
    original_status = claim.status
    original_amount_paid = claim.amount_paid
    original_paid_at = claim.paid_at
    original_adjudicated_at = claim.adjudicated_at
    original_reasoning = claim.adjudication_reasoning
    original_error_flags = claim.error_flags
    original_denial_reason = claim.denial_reason
    original_eligibility = claim.eligibility_check
    original_duplicate = claim.duplicate_check
    original_latency = claim.processing_latency_ms

    try:
        # Force shadow mode so any downstream payment action is a no-op
        # in the existing payment path.
        claim.mode = ClaimMode.shadow
        claim.status = ClaimStatus.submitted
        db.flush()

        engine_result = adjudicate_claim(db, claim.claim_id)

        engine_decision = engine_result.get("status", "unknown")
        engine_amount = engine_result.get("amount_paid")
        engine_reasoning = engine_result.get(
            "adjudication_reasoning"
        ) or engine_result.get("denial_reason")

        return {
            "claim_id": str(reference.claim_id),
            "engine_decision": engine_decision,
            "engine_amount": engine_amount,
            "engine_reasoning": engine_reasoning,
            "reference_decision": reference.reference_decision,
            "reference_amount": reference.reference_amount,
            "reference_reason": reference.reference_reason,
            "engine_full_result": engine_result,
        }
    finally:
        # Restore the claim to its pre-shadow state so no production
        # data is mutated. This is critical — shadow must be read-only.
        db.expire(claim)
        claim = db.query(Claim).filter(
            Claim.claim_id == reference.claim_id
        ).first()
        if claim is not None:
            claim.mode = original_mode
            claim.status = original_status
            claim.amount_paid = original_amount_paid
            claim.paid_at = original_paid_at
            claim.adjudicated_at = original_adjudicated_at
            claim.adjudication_reasoning = original_reasoning
            claim.error_flags = original_error_flags
            claim.denial_reason = original_denial_reason
            claim.eligibility_check = original_eligibility
            claim.duplicate_check = original_duplicate
            claim.processing_latency_ms = original_latency
            db.commit()


def run_shadow_batch(
    db: Session,
    references: list[ShadowReference],
    *,
    tolerance_pct: float = 0.05,
) -> dict:
    """Run the engine against every claim in `references` and return a
    comparison report.

    `tolerance_pct` is the allowed difference in paid amount between the
    engine and the reference before the claim is flagged as a
    disagreement. For decisions (approved vs denied), any mismatch
    counts as a disagreement.
    """
    if not references:
        return {
            "run_at": datetime.now(UTC).isoformat(),
            "total_claims": 0,
            "evaluated": 0,
            "agreements": 0,
            "disagreements_count": 0,
            "agreement_rate_pct": None,
            "amount_tolerance_pct": tolerance_pct,
            "decision_matrix": {},
            "disagreements": [],
            "errors": [],
        }

    agreements = 0
    disagreements: list[Disagreement] = []
    errors: list[dict] = []
    decision_matrix: dict[tuple[str, str], int] = {}

    for reference in references:
        result = _run_single_shadow(db, reference)
        if "error" in result:
            errors.append(result)
            continue

        engine_decision_norm = _normalize_decision(result["engine_decision"])
        reference_decision_norm = _normalize_decision(result["reference_decision"])
        key = (engine_decision_norm, reference_decision_norm)
        decision_matrix[key] = decision_matrix.get(key, 0) + 1

        decisions_agree = engine_decision_norm == reference_decision_norm

        amount_delta = None
        amounts_agree = True
        if (
            _is_approved(engine_decision_norm)
            and _is_approved(reference_decision_norm)
            and result.get("engine_amount") is not None
            and result.get("reference_amount") is not None
        ):
            engine_amt = float(result["engine_amount"] or 0)
            reference_amt = float(result["reference_amount"] or 0)
            amount_delta = engine_amt - reference_amt
            if reference_amt > 0:
                rel_delta = abs(amount_delta) / reference_amt
                amounts_agree = rel_delta <= tolerance_pct

        if decisions_agree and amounts_agree:
            agreements += 1
        else:
            disagreements.append(
                Disagreement(
                    claim_id=result["claim_id"],
                    engine_decision=engine_decision_norm,
                    reference_decision=reference_decision_norm,
                    engine_amount=result.get("engine_amount"),
                    reference_amount=result.get("reference_amount"),
                    amount_delta=amount_delta,
                    engine_reasoning=result.get("engine_reasoning"),
                    reference_reason=result.get("reference_reason"),
                )
            )

    total = len(references) - len(errors)
    agreement_rate = round(agreements / total * 100, 2) if total > 0 else None

    return {
        "run_at": datetime.now(UTC).isoformat(),
        "total_claims": len(references),
        "evaluated": total,
        "agreements": agreements,
        "disagreements_count": len(disagreements),
        "agreement_rate_pct": agreement_rate,
        "amount_tolerance_pct": tolerance_pct,
        "decision_matrix": {
            f"{engine}→{reference}": count
            for (engine, reference), count in decision_matrix.items()
        },
        "disagreements": [
            {
                "claim_id": d.claim_id,
                "engine_decision": d.engine_decision,
                "reference_decision": d.reference_decision,
                "engine_amount": d.engine_amount,
                "reference_amount": d.reference_amount,
                "amount_delta": d.amount_delta,
                "engine_reasoning": d.engine_reasoning,
                "reference_reason": d.reference_reason,
            }
            for d in disagreements
        ],
        "errors": errors,
    }


def summarize_disagreements(report: dict) -> str:
    """Human-readable summary of a shadow report, suitable for logging
    or dashboards."""
    lines = [
        f"Shadow run {report['run_at']}:",
        f"  Total:            {report['total_claims']}",
        f"  Evaluated:        {report['evaluated']}",
        f"  Agreements:       {report['agreements']}",
        f"  Disagreements:    {report['disagreements_count']}",
        f"  Agreement rate:   {report['agreement_rate_pct']}%",
        "",
        "Decision matrix (engine → reference):",
    ]
    for key, count in sorted(
        report["decision_matrix"].items(), key=lambda x: -x[1]
    ):
        lines.append(f"  {key}: {count}")
    return "\n".join(lines)
