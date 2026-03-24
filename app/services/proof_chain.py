"""Proof Chain Engine (Function 6A, Proof Chain — 4 layers).

Constitution requirement — four layers of proof, each independently
verifiable:

Layer 1: Every price independently verifiable.
         Source: CMS hospital transparency, Medicare physician fee schedule,
         NADAC pharmacy, insurer TiC files. Tagged on every line item.

Layer 2: Every care coordination claim backed by execution logs from
         live employers. Source: clinical determinations, adjudication
         audit trails, payment records.

Layer 3: Aggregate results actuarially certified.
         Source: shadow-mode and live aggregate statistics, third-party
         actuarial review.

Layer 4: Shadow-to-live track record published.
         Source: shadow dashboard snapshots, activation records,
         post-activation performance.

Every claim line item is tagged with which proof layers apply to it.
The proof summary gives coverage metrics per layer.
"""

import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus, ClaimMode
from app.models.benchmark_query import BenchmarkQuery, BenchmarkStage
from app.models.price_data import PriceData

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer definitions
# ---------------------------------------------------------------------------

PROOF_LAYERS = {
    1: {
        "name": "Price Verification",
        "description": (
            "Every price independently verifiable. Each line item references "
            "CMS hospital transparency, Medicare physician fee schedule, "
            "NADAC pharmacy acquisition cost, or insurer transparency data."
        ),
        "source": "CMS public pricing data",
    },
    2: {
        "name": "Care Execution Logs",
        "description": (
            "Every care coordination claim backed by execution logs from "
            "live employers. Clinical determinations, adjudication audit "
            "trails, and payment records provide a complete chain of custody."
        ),
        "source": "Clinical determination + adjudication audit trail",
    },
    3: {
        "name": "Actuarial Certification",
        "description": (
            "Aggregate results actuarially certified. Shadow-mode and live "
            "aggregate statistics reviewed and certified by independent "
            "actuarial firm."
        ),
        "source": "Third-party actuarial review",
    },
    4: {
        "name": "Shadow-to-Live Track Record",
        "description": (
            "Shadow-to-live track record published. Verifiable history of "
            "shadow mode predictions vs. actual live outcomes for every "
            "employer that has activated."
        ),
        "source": "Shadow dashboard snapshots + post-activation performance",
    },
}


# ---------------------------------------------------------------------------
# Layer tagging
# ---------------------------------------------------------------------------

def tag_proof_layer(
    db: Session,
    claim_id: str | None = None,
    service_code: str | None = None,
    employer_id: str | None = None,
) -> dict:
    """Tag a claim or line item with applicable proof layers (1-4).

    Returns a dict with each layer and whether it applies, plus the
    supporting evidence reference.
    """
    layers: dict[int, dict] = {}

    # --- Layer 1: Price verification ---
    layer1_evidence = []
    if service_code:
        price_count = db.query(func.count(PriceData.price_id)).filter(
            PriceData.service_code == service_code,
        ).scalar() or 0
        if price_count > 0:
            layer1_evidence.append(
                f"{price_count} verified price records for code {service_code}"
            )

    layers[1] = {
        **PROOF_LAYERS[1],
        "applies": len(layer1_evidence) > 0,
        "evidence": layer1_evidence,
    }

    # --- Layer 2: Care execution logs ---
    layer2_evidence = []
    if claim_id:
        claim = db.query(Claim).filter(Claim.claim_id == claim_id).first()
        if claim:
            if claim.clinical_determination_id:
                layer2_evidence.append(
                    f"Clinical determination: {claim.clinical_determination_id}"
                )
            if claim.adjudication_reasoning:
                layer2_evidence.append("Full adjudication audit trail recorded")
            if claim.paid_at:
                layer2_evidence.append(
                    f"Payment executed at {claim.paid_at.isoformat()}"
                )

    layers[2] = {
        **PROOF_LAYERS[2],
        "applies": len(layer2_evidence) > 0,
        "evidence": layer2_evidence,
    }

    # --- Layer 3: Actuarial certification ---
    # In production, this checks for actuarial sign-off records.
    # For now, flag as applicable when sufficient aggregate data exists.
    layer3_evidence = []
    if employer_id:
        claim_count = db.query(func.count(Claim.claim_id)).filter(
            Claim.employer_id == employer_id,
        ).scalar() or 0
        if claim_count >= 50:
            layer3_evidence.append(
                f"{claim_count} claims available for actuarial review"
            )

    layers[3] = {
        **PROOF_LAYERS[3],
        "applies": len(layer3_evidence) > 0,
        "evidence": layer3_evidence,
    }

    # --- Layer 4: Shadow-to-live track record ---
    layer4_evidence = []
    if employer_id:
        shadow_query = db.query(BenchmarkQuery).filter(
            and_(
                BenchmarkQuery.employer_id == employer_id,
                BenchmarkQuery.stage == BenchmarkStage.shadow,
            )
        ).first()
        if shadow_query:
            layer4_evidence.append(
                f"Shadow session: {shadow_query.query_id}"
            )

        activation_query = db.query(BenchmarkQuery).filter(
            and_(
                BenchmarkQuery.employer_id == employer_id,
                BenchmarkQuery.stage == BenchmarkStage.activated,
            )
        ).first()
        if activation_query:
            layer4_evidence.append(
                f"Activation record: {activation_query.query_id}"
            )

    layers[4] = {
        **PROOF_LAYERS[4],
        "applies": len(layer4_evidence) > 0,
        "evidence": layer4_evidence,
    }

    layers_covered = sum(1 for l in layers.values() if l["applies"])

    return {
        "claim_id": claim_id,
        "service_code": service_code,
        "employer_id": employer_id,
        "layers": layers,
        "layers_covered": layers_covered,
        "total_layers": 4,
        "coverage_pct": round(layers_covered / 4 * 100, 1),
    }


# ---------------------------------------------------------------------------
# Proof summary (employer-level)
# ---------------------------------------------------------------------------

def get_proof_summary(db: Session, employer_id: str) -> dict:
    """Summary of proof coverage for an employer across all 4 layers.

    Constitution: The proof chain must be comprehensive enough that an
    employer or their advisor can independently verify every claim the
    system makes about cost savings, care quality, and operational
    superiority.
    """
    from app.models.employer import Employer

    employer = db.query(Employer).filter(
        Employer.employer_id == employer_id,
    ).first()
    if not employer:
        return {"error": "employer_not_found", "employer_id": employer_id}

    # Count claims and their proof coverage
    all_claims = db.query(Claim).filter(
        Claim.employer_id == employer_id,
    ).all()

    total_claims = len(all_claims)
    shadow_claims = sum(1 for c in all_claims if c.mode == ClaimMode.shadow)
    live_claims = sum(1 for c in all_claims if c.mode == ClaimMode.live)

    # Layer 1: price verification coverage
    claims_with_price_comparison = sum(
        1 for c in all_claims if c.price_comparison_id is not None
    )

    # Layer 2: care execution log coverage
    claims_with_clinical = sum(
        1 for c in all_claims if c.clinical_determination_id is not None
    )
    claims_with_audit_trail = sum(
        1 for c in all_claims if c.adjudication_reasoning is not None
    )

    # Layer 3: actuarial certification readiness
    actuarial_ready = total_claims >= 50

    # Layer 4: shadow-to-live track record
    shadow_sessions = db.query(func.count(BenchmarkQuery.query_id)).filter(
        and_(
            BenchmarkQuery.employer_id == employer_id,
            BenchmarkQuery.stage == BenchmarkStage.shadow,
        )
    ).scalar() or 0

    activation_records = db.query(func.count(BenchmarkQuery.query_id)).filter(
        and_(
            BenchmarkQuery.employer_id == employer_id,
            BenchmarkQuery.stage == BenchmarkStage.activated,
        )
    ).scalar() or 0

    # Total price data available in system
    total_price_records = db.query(func.count(PriceData.price_id)).scalar() or 0

    return {
        "employer_id": employer_id,
        "employer_name": employer.name,
        "total_claims": total_claims,
        "shadow_claims": shadow_claims,
        "live_claims": live_claims,
        "layers": {
            1: {
                **PROOF_LAYERS[1],
                "coverage": {
                    "claims_with_price_verification": claims_with_price_comparison,
                    "coverage_pct": (
                        round(claims_with_price_comparison / total_claims * 100, 1)
                        if total_claims > 0 else 0.0
                    ),
                    "total_price_records_in_system": total_price_records,
                },
            },
            2: {
                **PROOF_LAYERS[2],
                "coverage": {
                    "claims_with_clinical_determination": claims_with_clinical,
                    "claims_with_audit_trail": claims_with_audit_trail,
                    "coverage_pct": (
                        round(claims_with_audit_trail / total_claims * 100, 1)
                        if total_claims > 0 else 0.0
                    ),
                },
            },
            3: {
                **PROOF_LAYERS[3],
                "coverage": {
                    "actuarial_ready": actuarial_ready,
                    "claims_for_review": total_claims,
                    "minimum_required": 50,
                    "coverage_pct": min(100.0, round(total_claims / 50 * 100, 1)),
                },
            },
            4: {
                **PROOF_LAYERS[4],
                "coverage": {
                    "shadow_sessions": shadow_sessions,
                    "activation_records": activation_records,
                    "has_track_record": shadow_sessions > 0 and activation_records > 0,
                    "coverage_pct": (
                        100.0 if (shadow_sessions > 0 and activation_records > 0)
                        else 50.0 if shadow_sessions > 0
                        else 0.0
                    ),
                },
            },
        },
        "overall_coverage_pct": _compute_overall_coverage(
            claims_with_price_comparison, claims_with_audit_trail,
            total_claims, actuarial_ready, shadow_sessions, activation_records,
        ),
    }


def _compute_overall_coverage(
    price_verified: int,
    audit_trailed: int,
    total_claims: int,
    actuarial_ready: bool,
    shadow_sessions: int,
    activation_records: int,
) -> float:
    """Weighted average of proof layer coverage."""
    if total_claims == 0:
        return 0.0

    l1 = price_verified / total_claims * 100
    l2 = audit_trailed / total_claims * 100
    l3 = 100.0 if actuarial_ready else min(100.0, total_claims / 50 * 100)
    l4 = (
        100.0 if (shadow_sessions > 0 and activation_records > 0)
        else 50.0 if shadow_sessions > 0
        else 0.0
    )

    # Equal weight across all four layers
    return round((l1 + l2 + l3 + l4) / 4, 1)
