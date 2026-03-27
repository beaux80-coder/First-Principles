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
from datetime import datetime, UTC

from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus, ClaimMode
from app.models.benchmark_query import BenchmarkQuery, BenchmarkStage
from app.models.price_data import PriceData
from app.models.employer import Employer

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
    # Constitution: "Every price is independently verifiable"
    # Must validate that the claim's ACTUAL paid amount matches a published price
    layer1_evidence = []
    price_verified = False
    if service_code:
        price_count = db.query(func.count(PriceData.price_id)).filter(
            PriceData.service_code == service_code,
        ).scalar() or 0
        if price_count > 0:
            layer1_evidence.append(
                f"{price_count} published price records for code {service_code}"
            )

    # Validate the claim's paid amount against published prices
    if claim_id:
        claim = db.query(Claim).filter(Claim.claim_id == claim_id).first()
        if claim and claim.amount_paid and claim.amount_paid > 0:
            # Check if this exact price exists in public sources
            from app.models.provider import Provider
            provider_npi = None
            if claim.provider_id:
                provider = db.query(Provider).filter(Provider.provider_id == claim.provider_id).first()
                if provider:
                    provider_npi = provider.npi

            # Look for matching published price (within $0.01 tolerance)
            match_query = db.query(PriceData).filter(
                PriceData.service_code == (service_code or ""),
            )
            if provider_npi:
                match_query = match_query.filter(PriceData.provider_npi == provider_npi)

            matching_prices = match_query.limit(20).all()
            for mp in matching_prices:
                if abs(float(mp.price) - float(claim.amount_paid)) < 0.01:
                    price_verified = True
                    layer1_evidence.append(
                        f"VERIFIED: Paid ${float(claim.amount_paid):.2f} matches "
                        f"{mp.source.value} published price from "
                        f"{mp.ingested_at.strftime('%Y-%m-%d') if mp.ingested_at else 'unknown'}"
                    )
                    break

            if not price_verified and matching_prices:
                lowest = min(float(p.price) for p in matching_prices if p.price > 0)
                layer1_evidence.append(
                    f"UNVERIFIED: Paid ${float(claim.amount_paid):.2f} does not match "
                    f"any published price (lowest published: ${lowest:.2f})"
                )
            elif not price_verified:
                layer1_evidence.append(
                    "UNVERIFIED: No published prices found for this provider/service combination"
                )

    layers[1] = {
        **PROOF_LAYERS[1],
        "applies": len(layer1_evidence) > 0,
        "verified": price_verified,
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

    layers_covered = sum(1 for layer in layers.values() if layer["applies"])

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


# ---------------------------------------------------------------------------
# Layer 2 population — care execution proof chain
# ---------------------------------------------------------------------------

def populate_layer_2(db: Session) -> dict:
    """Query clinical_determinations + claims + payments to build Layer 2 proof.

    Layer 2: Every care coordination claim backed by execution logs.
    Source: clinical determinations, adjudication audit trails, payment records.

    Scans all claims and populates their Layer 2 evidence by verifying that
    each claim has:
    - A clinical determination ID (from F1 TEE)
    - An adjudication reasoning trace (from F5)
    - A payment record with timestamp (from F7)
    """
    now = datetime.now(UTC)

    # Query all claims with their Layer 2 fields
    all_claims = db.query(Claim).filter(
        Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
    ).all()

    total_claims = len(all_claims)
    layer2_complete = 0
    layer2_partial = 0
    layer2_missing = 0

    evidence_details = {
        "clinical_determination_present": 0,
        "adjudication_trail_present": 0,
        "payment_record_present": 0,
    }

    claims_needing_attention = []

    for claim in all_claims:
        has_clinical = claim.clinical_determination_id is not None
        has_audit = claim.adjudication_reasoning is not None
        has_payment = claim.paid_at is not None

        if has_clinical:
            evidence_details["clinical_determination_present"] += 1
        if has_audit:
            evidence_details["adjudication_trail_present"] += 1
        if has_payment:
            evidence_details["payment_record_present"] += 1

        evidence_count = sum([has_clinical, has_audit, has_payment])
        if evidence_count == 3:
            layer2_complete += 1
        elif evidence_count > 0:
            layer2_partial += 1
        else:
            layer2_missing += 1
            claims_needing_attention.append({
                "claim_id": str(claim.claim_id),
                "employer_id": str(claim.employer_id),
                "status": claim.status.value,
                "missing": [
                    k for k, present in [
                        ("clinical_determination", has_clinical),
                        ("adjudication_trail", has_audit),
                        ("payment_record", has_payment),
                    ]
                    if not present
                ],
            })

    return {
        "layer": 2,
        "layer_name": PROOF_LAYERS[2]["name"],
        "populated_at": now.isoformat(),
        "total_claims_assessed": total_claims,
        "coverage": {
            "complete": layer2_complete,
            "partial": layer2_partial,
            "missing": layer2_missing,
            "complete_pct": round(layer2_complete / total_claims * 100, 1) if total_claims > 0 else 0.0,
        },
        "evidence_breakdown": evidence_details,
        "claims_needing_attention": claims_needing_attention[:20],
        "recommendation": (
            "All approved/paid claims should have all three Layer 2 evidence "
            "elements. Review claims with missing elements to ensure complete "
            "care execution proof chain."
        ),
    }


# ---------------------------------------------------------------------------
# Layer 3 population — actuarial certification input
# ---------------------------------------------------------------------------

def populate_layer_3(db: Session) -> dict:
    """Compute aggregate statistics for actuarial certification (Layer 3).

    Layer 3: Aggregate results actuarially certified.
    Source: shadow-mode and live aggregate statistics, third-party actuarial review.

    Produces the aggregate data package an independent actuarial firm needs
    to certify the system's results. Includes:
    - Claim volume and cost statistics per employer
    - Shadow vs live mode comparisons
    - Savings distribution statistics
    - Loss ratio computations
    """
    now = datetime.now(UTC)

    # Per-employer aggregate statistics
    employer_stats = (
        db.query(
            Claim.employer_id,
            Claim.mode,
            func.count(Claim.claim_id).label("claim_count"),
            func.sum(Claim.amount_billed).label("total_billed"),
            func.sum(Claim.amount_paid).label("total_paid"),
            func.avg(Claim.amount_billed).label("avg_billed"),
            func.avg(Claim.amount_paid).label("avg_paid"),
            func.min(Claim.submitted_at).label("first_claim"),
            func.max(Claim.submitted_at).label("last_claim"),
        )
        .filter(Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]))
        .group_by(Claim.employer_id, Claim.mode)
        .all()
    )

    # Build employer-level actuarial summaries
    employer_summaries = {}
    for stat in employer_stats:
        emp_id = str(stat.employer_id)
        if emp_id not in employer_summaries:
            employer_summaries[emp_id] = {"shadow": None, "live": None}

        mode_key = stat.mode.value if stat.mode else "live"
        employer_summaries[emp_id][mode_key] = {
            "claim_count": stat.claim_count,
            "total_billed": round(float(stat.total_billed or 0), 2),
            "total_paid": round(float(stat.total_paid or 0), 2),
            "avg_billed": round(float(stat.avg_billed or 0), 2),
            "avg_paid": round(float(stat.avg_paid or 0), 2),
            "first_claim": stat.first_claim.isoformat() if stat.first_claim else None,
            "last_claim": stat.last_claim.isoformat() if stat.last_claim else None,
            "loss_ratio": round(
                float(stat.total_paid or 0) / float(stat.total_billed or 1), 4
            ) if stat.total_billed else None,
        }

    # Platform-wide aggregate statistics
    platform_total_billed = db.query(func.sum(Claim.amount_billed)).filter(
        Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
    ).scalar() or 0

    platform_total_paid = db.query(func.sum(Claim.amount_paid)).filter(
        Claim.status == ClaimStatus.paid,
    ).scalar() or 0

    platform_claim_count = db.query(func.count(Claim.claim_id)).filter(
        Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
    ).scalar() or 0

    shadow_claim_count = db.query(func.count(Claim.claim_id)).filter(
        Claim.mode == ClaimMode.shadow,
        Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
    ).scalar() or 0

    live_claim_count = db.query(func.count(Claim.claim_id)).filter(
        Claim.mode == ClaimMode.live,
        Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
    ).scalar() or 0

    # Employers with sufficient data for actuarial review (>= 50 claims)
    actuarial_ready_employers = [
        emp_id for emp_id, modes in employer_summaries.items()
        if sum(
            (modes.get("shadow", {}) or {}).get("claim_count", 0) +
            (modes.get("live", {}) or {}).get("claim_count", 0)
            for _ in [1]
        ) >= 50
    ]

    return {
        "layer": 3,
        "layer_name": PROOF_LAYERS[3]["name"],
        "populated_at": now.isoformat(),
        "platform_aggregates": {
            "total_claims": platform_claim_count,
            "total_billed": round(float(platform_total_billed), 2),
            "total_paid": round(float(platform_total_paid), 2),
            "platform_loss_ratio": round(
                float(platform_total_paid) / float(platform_total_billed), 4
            ) if platform_total_billed else None,
            "shadow_claims": shadow_claim_count,
            "live_claims": live_claim_count,
        },
        "employer_count": len(employer_summaries),
        "actuarial_ready_employers": len(actuarial_ready_employers),
        "minimum_claims_for_certification": 50,
        "employer_summaries": employer_summaries,
        "actuarial_package_contents": [
            "Per-employer claim count, total billed, total paid, loss ratio",
            "Shadow vs live mode split per employer",
            "Claim date range per employer for exposure period calculation",
            "Platform-wide aggregate loss ratio",
            "Sufficient volume assessment (50+ claims threshold)",
        ],
        "certification_readiness": (
            "ready" if len(actuarial_ready_employers) > 0
            else "insufficient_data"
        ),
    }


# ---------------------------------------------------------------------------
# Layer 4 population — shadow-to-live track record
# ---------------------------------------------------------------------------

def populate_layer_4(db: Session) -> dict:
    """Capture shadow-to-live track record for Layer 4 proof.

    Layer 4: Shadow-to-live track record published.
    Source: shadow dashboard snapshots, activation records, post-activation
    performance.

    For each employer that transitioned from shadow to live, capture:
    - Shadow mode predictions (projected savings)
    - Actual live results (realized savings)
    - Predicted vs actual comparison
    """
    now = datetime.now(UTC)

    # Find employers with both shadow and activated benchmark records
    shadow_queries = (
        db.query(BenchmarkQuery)
        .filter(BenchmarkQuery.stage == BenchmarkStage.shadow)
        .all()
    )

    activation_queries = (
        db.query(BenchmarkQuery)
        .filter(BenchmarkQuery.stage == BenchmarkStage.activated)
        .all()
    )

    # Map by employer_id for matching
    shadow_by_employer = {}
    for sq in shadow_queries:
        if sq.employer_id:
            shadow_by_employer[str(sq.employer_id)] = sq

    activation_by_employer = {}
    for aq in activation_queries:
        if aq.employer_id:
            activation_by_employer[str(aq.employer_id)] = aq

    # Build track records for employers that have both shadow and activation
    track_records = []
    for emp_id, shadow_bq in shadow_by_employer.items():
        activation_bq = activation_by_employer.get(emp_id)

        # Get employer info
        employer = db.query(Employer).filter(
            Employer.employer_id == shadow_bq.employer_id,
        ).first()

        # Shadow predictions (from benchmark results)
        shadow_results = shadow_bq.results or {}
        shadow_comparison = shadow_results.get("comparison", {})
        predicted_savings_pct = shadow_comparison.get("savings_pct", 0)
        predicted_savings_annual = shadow_comparison.get("annual_savings", 0)

        # Actual live results (from claims data)
        shadow_claims = db.query(Claim).filter(
            Claim.employer_id == shadow_bq.employer_id,
            Claim.mode == ClaimMode.shadow,
            Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
        ).all()

        live_claims = db.query(Claim).filter(
            Claim.employer_id == shadow_bq.employer_id,
            Claim.mode == ClaimMode.live,
            Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]),
        ).all()

        shadow_total_paid = sum(float(c.amount_paid or 0) for c in shadow_claims)
        shadow_total_billed = sum(float(c.amount_billed or 0) for c in shadow_claims)
        live_total_paid = sum(float(c.amount_paid or 0) for c in live_claims)
        live_total_billed = sum(float(c.amount_billed or 0) for c in live_claims)

        # Compute actual savings if both shadow and live data exist
        actual_savings_pct = None
        prediction_accuracy = None
        if shadow_total_billed > 0 and live_total_paid > 0:
            actual_savings_pct = round(
                (shadow_total_billed - live_total_paid) / shadow_total_billed * 100, 1
            )
            if predicted_savings_pct > 0:
                prediction_accuracy = round(
                    actual_savings_pct / predicted_savings_pct * 100, 1
                )

        record = {
            "employer_id": emp_id,
            "employer_name": employer.name if employer else "Unknown",
            "shadow_period": {
                "start": shadow_bq.created_at.isoformat() if shadow_bq.created_at else None,
                "claims_processed": len(shadow_claims),
                "total_billed": round(shadow_total_billed, 2),
                "total_paid": round(shadow_total_paid, 2),
            },
            "activation": {
                "activated_at": activation_bq.created_at.isoformat() if activation_bq and activation_bq.created_at else None,
                "is_activated": activation_bq is not None,
            },
            "live_performance": {
                "claims_processed": len(live_claims),
                "total_billed": round(live_total_billed, 2),
                "total_paid": round(live_total_paid, 2),
            },
            "predicted_vs_actual": {
                "predicted_savings_pct": predicted_savings_pct,
                "predicted_savings_annual": predicted_savings_annual,
                "actual_savings_pct": actual_savings_pct,
                "prediction_accuracy_pct": prediction_accuracy,
            },
        }
        track_records.append(record)

    # Compute aggregate prediction accuracy
    predictions_with_actuals = [
        r for r in track_records
        if r["predicted_vs_actual"]["prediction_accuracy_pct"] is not None
    ]
    avg_accuracy = (
        round(
            sum(r["predicted_vs_actual"]["prediction_accuracy_pct"] for r in predictions_with_actuals)
            / len(predictions_with_actuals),
            1,
        )
        if predictions_with_actuals
        else None
    )

    return {
        "layer": 4,
        "layer_name": PROOF_LAYERS[4]["name"],
        "populated_at": now.isoformat(),
        "total_shadow_employers": len(shadow_by_employer),
        "total_activated_employers": len(activation_by_employer),
        "employers_with_track_record": len(track_records),
        "track_records": track_records,
        "aggregate_metrics": {
            "predictions_with_actuals": len(predictions_with_actuals),
            "average_prediction_accuracy_pct": avg_accuracy,
            "note": (
                "Prediction accuracy measures how closely shadow-mode savings "
                "projections match actual live results. 100% = perfect prediction."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Line item tagging — tag each claim with applicable proof layers
# ---------------------------------------------------------------------------

def tag_line_items(db: Session, claim_id) -> dict:
    """Tag each claim line item with which proof layers apply.

    Combines claim-level, service-level, and employer-level proof layer
    assessment into a single tagged result for a specific claim.

    Args:
        db: Database session.
        claim_id: The claim to tag.
    """
    claim = db.query(Claim).filter(Claim.claim_id == claim_id).first()
    if not claim:
        return {"error": f"Claim {claim_id} not found"}

    # Get service code from claim's service relationship
    service_code = None
    if claim.service_id:
        from app.models.service import Service
        service = db.query(Service).filter(
            Service.service_id == claim.service_id,
        ).first()
        if service:
            service_code = service.service_code

    # Run the full proof layer tagging
    proof_result = tag_proof_layer(
        db,
        claim_id=str(claim.claim_id),
        service_code=service_code,
        employer_id=str(claim.employer_id),
    )

    # Enrich with line-item specific details
    line_item_tags = {
        "claim_id": str(claim.claim_id),
        "employer_id": str(claim.employer_id),
        "benefit_type": claim.benefit_type.value if claim.benefit_type else None,
        "claim_status": claim.status.value,
        "claim_mode": claim.mode.value if claim.mode else None,
        "amount_billed": round(float(claim.amount_billed or 0), 2),
        "amount_paid": round(float(claim.amount_paid or 0), 2),
        "service_code": service_code,
        "layers": proof_result["layers"],
        "layers_covered": proof_result["layers_covered"],
        "total_layers": proof_result["total_layers"],
        "coverage_pct": proof_result["coverage_pct"],
        "tagging_summary": [],
    }

    # Build human-readable tagging summary
    for layer_num, layer_data in proof_result["layers"].items():
        tag_entry = {
            "layer": layer_num,
            "name": layer_data["name"],
            "applies": layer_data["applies"],
            "evidence_count": len(layer_data.get("evidence", [])),
        }
        if layer_data["applies"]:
            tag_entry["evidence"] = layer_data["evidence"]
        line_item_tags["tagging_summary"].append(tag_entry)

    return line_item_tags
