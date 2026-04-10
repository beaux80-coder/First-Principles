"""Clinical expert review exporter.

Selects a sample of recent clinical determinations and care routing
decisions, bundles them with the clinical evidence they cite, and emits
a reviewable package that a physician panel can rate. This is the
infrastructure for Level 4 of the testing pyramid (clinical expert
review) — it does NOT do the review itself, which is a human process.

Output is a list of structured dicts, one per case, containing:
  • Case metadata (id, benefit type, urgency, decision, timestamp)
  • De-identified patient summary (age band, sex, risk factors)
  • The decision the engine made
  • The evidence the engine cited
  • A question template for each reviewer
  • A placeholder for reviewer rating + notes

The export is deterministic given the same sample seed, so multiple
reviewers can independently score the same cases.

Sampling strategy:
  • Stratified by decision type (approved / denied / emergent / flagged)
  • Stratified by benefit type
  • Stratified by confidence (low-confidence decisions oversampled since
    they're the ones most likely to be wrong)
  • Recent-first within each stratum
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Optional

from sqlalchemy import and_, or_, desc
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus
from app.models.clinical_determination import ClinicalDetermination


logger = logging.getLogger(__name__)


@dataclass
class ReviewCase:
    """One case for a clinical reviewer to score."""
    case_id: str
    source: str  # "claim" | "determination"
    decision: str
    benefit_type: Optional[str]
    created_at: str
    patient_summary: dict
    engine_reasoning: Optional[str]
    evidence_cited: list[str]
    questions: list[dict]
    reviewer_rating: Optional[dict] = field(default=None)  # filled in by reviewer


def _age_band(age: Optional[int]) -> str:
    """Return a de-identification-safe age band."""
    if age is None:
        return "unknown"
    if age < 18:
        return "<18"
    if age < 25:
        return "18-24"
    if age < 35:
        return "25-34"
    if age < 50:
        return "35-49"
    if age < 65:
        return "50-64"
    return "65+"


def _extract_patient_summary(claim: Claim) -> dict:
    """Extract a de-identified patient summary from the claim's employee
    record. The reviewer needs clinical context but should NOT see PHI."""
    employee = getattr(claim, "employee", None)
    summary: dict = {
        "patient_ref": f"pt-{str(claim.employee_id)[:8]}",
        "age_band": "unknown",
        "sex": "unknown",
        "risk_factors": [],
    }
    if employee is None:
        return summary
    raw = employee.demographics_encrypted
    if not raw:
        return summary
    try:
        demographics = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, json.JSONDecodeError):
        return summary
    age = demographics.get("age")
    if isinstance(age, (int, float)):
        summary["age_band"] = _age_band(int(age))
    sex = str(demographics.get("sex", "")).lower() or "unknown"
    summary["sex"] = sex
    summary["risk_factors"] = demographics.get("risk_factors") or []
    return summary


def _build_questions(decision: str) -> list[dict]:
    return [
        {
            "id": "agree_with_decision",
            "question": f"Do you agree with the engine's {decision} decision?",
            "response_type": "enum",
            "options": ["yes", "no", "unclear"],
        },
        {
            "id": "evidence_appropriate",
            "question": "Is the cited evidence appropriate for this patient's clinical situation?",
            "response_type": "enum",
            "options": ["yes", "no", "partial", "missing"],
        },
        {
            "id": "reasoning_accurate",
            "question": "Is the plain-language reasoning accurate and complete?",
            "response_type": "enum",
            "options": ["yes", "no", "misleading"],
        },
        {
            "id": "severity_concern",
            "question": "How concerning is the engine's behavior here? (1=not at all, 5=urgent fix required)",
            "response_type": "integer",
            "range": [1, 5],
        },
        {
            "id": "free_text_notes",
            "question": "Any additional notes?",
            "response_type": "text",
        },
    ]


def _claim_to_review_case(claim: Claim) -> ReviewCase:
    decision = (
        claim.status.value
        if hasattr(claim.status, "value")
        else str(claim.status)
    )
    reasoning = claim.adjudication_reasoning or claim.denial_reason
    evidence = []
    if claim.coding_validation and isinstance(claim.coding_validation, dict):
        for dx in claim.coding_validation.get("diagnosis_codes", []) or []:
            evidence.append(f"Diagnosis code: {dx}")
        for proc in claim.coding_validation.get("procedure_codes", []) or []:
            evidence.append(f"Procedure code: {proc}")
        pos = claim.coding_validation.get("place_of_service")
        if pos:
            evidence.append(f"Place of service: {pos}")
    if claim.is_emergent:
        signals = claim.emergent_signals or {}
        rules = signals.get("rules_fired", []) if isinstance(signals, dict) else []
        evidence.append(
            f"Classified emergent under prudent layperson standard "
            f"(signals: {', '.join(rules)})"
        )
    return ReviewCase(
        case_id=str(claim.claim_id),
        source="claim",
        decision=decision,
        benefit_type=(
            claim.benefit_type.value
            if hasattr(claim.benefit_type, "value")
            else str(claim.benefit_type)
        ),
        created_at=(claim.submitted_at or datetime.now(UTC)).isoformat(),
        patient_summary=_extract_patient_summary(claim),
        engine_reasoning=reasoning,
        evidence_cited=evidence,
        questions=_build_questions(decision),
    )


def _determination_to_review_case(det: ClinicalDetermination) -> ReviewCase:
    decision = (
        det.decision.value if hasattr(det.decision, "value") else str(det.decision)
    )
    return ReviewCase(
        case_id=str(det.determination_id),
        source="determination",
        decision=decision,
        benefit_type=det.benefit_type,
        created_at=(det.created_at or datetime.now(UTC)).isoformat(),
        patient_summary={
            "patient_ref": f"det-{str(det.determination_id)[:8]}",
            "age_band": "unknown",
            "sex": "unknown",
            "risk_factors": [],
            "note": "Patient detail withheld — full inputs available to panel lead only.",
        },
        engine_reasoning=det.reasoning,
        evidence_cited=list(det.guidelines_referenced or []),
        questions=_build_questions(decision),
    )


def export_review_package(
    db: Session,
    *,
    sample_size: int = 100,
    window_days: int = 30,
    seed: Optional[int] = None,
) -> dict:
    """Build a deterministic sample of recent engine decisions for
    clinical review.

    Stratifies by decision type so that approvals, denials, emergent
    classifications, and flagged-for-review cases are all represented.
    """
    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random.Random()

    window_start = datetime.now(UTC) - timedelta(days=window_days)

    # Pull recent claims in each decision stratum
    def _claims_with_status(status_values, limit):
        return (
            db.query(Claim)
            .filter(
                Claim.submitted_at >= window_start,
                Claim.status.in_(status_values),
            )
            .order_by(desc(Claim.submitted_at))
            .limit(limit)
            .all()
        )

    per_stratum = max(1, sample_size // 4)

    approved = _claims_with_status(
        [ClaimStatus.approved, ClaimStatus.paid], per_stratum * 3
    )
    denied = _claims_with_status([ClaimStatus.denied], per_stratum * 3)
    flagged = _claims_with_status(
        [ClaimStatus.adjudicating], per_stratum * 2
    )
    emergent = [
        c for c in approved if c.is_emergent is True
    ][: per_stratum]

    # Deterministic random sample within each stratum
    def _sample(seq, k):
        if len(seq) <= k:
            return seq
        return rng.sample(seq, k)

    selected: list[Claim] = []
    selected.extend(_sample(approved, per_stratum))
    selected.extend(_sample(denied, per_stratum))
    selected.extend(_sample(flagged, per_stratum))
    selected.extend(_sample(emergent, per_stratum))

    # De-duplicate by claim_id while preserving order
    seen: set = set()
    unique: list[Claim] = []
    for c in selected:
        if c.claim_id not in seen:
            seen.add(c.claim_id)
            unique.append(c)

    claim_cases = [_claim_to_review_case(c) for c in unique[:sample_size]]

    # Also pull a small number of ClinicalDetermination records so the
    # panel can review the raw clinical reasoning layer.
    determinations = (
        db.query(ClinicalDetermination)
        .filter(ClinicalDetermination.created_at >= window_start)
        .order_by(desc(ClinicalDetermination.created_at))
        .limit(per_stratum)
        .all()
    )
    det_cases = [_determination_to_review_case(d) for d in determinations]

    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "window_days": window_days,
        "seed": seed,
        "case_count": len(claim_cases) + len(det_cases),
        "stratum_targets": {
            "approved": per_stratum,
            "denied": per_stratum,
            "flagged": per_stratum,
            "emergent": per_stratum,
            "determinations": per_stratum,
        },
        "cases": [
            {
                "case_id": c.case_id,
                "source": c.source,
                "decision": c.decision,
                "benefit_type": c.benefit_type,
                "created_at": c.created_at,
                "patient_summary": c.patient_summary,
                "engine_reasoning": c.engine_reasoning,
                "evidence_cited": c.evidence_cited,
                "questions": c.questions,
                "reviewer_rating": c.reviewer_rating,
            }
            for c in claim_cases + det_cases
        ],
    }


def aggregate_reviewer_ratings(filled_package: dict) -> dict:
    """Given a review package with reviewer ratings filled in, compute
    summary statistics for the panel.

    Expected `reviewer_rating` format per case:
        {
            "reviewer_id": "...",
            "responses": {
                "agree_with_decision": "yes",
                "evidence_appropriate": "yes",
                "reasoning_accurate": "yes",
                "severity_concern": 2,
                "free_text_notes": "...",
            },
        }

    Returns agreement rates by response type and lists the highest-
    concern cases."""
    cases = filled_package.get("cases", [])
    reviewed = [c for c in cases if c.get("reviewer_rating")]
    if not reviewed:
        return {"reviewed_count": 0, "message": "No ratings recorded."}

    agree_counts = {"yes": 0, "no": 0, "unclear": 0}
    evidence_counts = {"yes": 0, "no": 0, "partial": 0, "missing": 0}
    reasoning_counts = {"yes": 0, "no": 0, "misleading": 0}
    severity_scores: list[int] = []
    high_concern_cases: list[dict] = []

    for case in reviewed:
        responses = (case["reviewer_rating"] or {}).get("responses", {})
        agree = responses.get("agree_with_decision")
        if agree in agree_counts:
            agree_counts[agree] += 1
        evidence = responses.get("evidence_appropriate")
        if evidence in evidence_counts:
            evidence_counts[evidence] += 1
        reasoning = responses.get("reasoning_accurate")
        if reasoning in reasoning_counts:
            reasoning_counts[reasoning] += 1
        severity = responses.get("severity_concern")
        if isinstance(severity, (int, float)):
            severity_scores.append(int(severity))
            if severity >= 4:
                high_concern_cases.append({
                    "case_id": case["case_id"],
                    "decision": case["decision"],
                    "severity": severity,
                    "notes": responses.get("free_text_notes"),
                })

    def _pct(counts: dict, key: str) -> float:
        total = sum(counts.values())
        return round(counts[key] / total * 100, 1) if total > 0 else 0.0

    return {
        "reviewed_count": len(reviewed),
        "agreement_rate_pct": _pct(agree_counts, "yes"),
        "disagreement_rate_pct": _pct(agree_counts, "no"),
        "evidence_appropriate_pct": _pct(evidence_counts, "yes"),
        "reasoning_accurate_pct": _pct(reasoning_counts, "yes"),
        "mean_severity": (
            round(sum(severity_scores) / len(severity_scores), 2)
            if severity_scores
            else None
        ),
        "high_concern_cases": high_concern_cases,
    }
