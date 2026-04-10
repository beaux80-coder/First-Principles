"""Outcomes feedback loop.

Level 5 of the testing pyramid: periodically poll for the actual
outcome of each determination the engine has made, compare it to what
the engine predicted, and compute accuracy metrics over time. This is
the slowest feedback loop but also the most important — it's the only
signal that tells us whether the engine's decisions were *right*, not
just *consistent*.

Inputs (already in the codebase):
  • ClinicalDetermination.outcome_feedback — one of:
      "correct", "incorrect", "partial", "unknown", None
  • ClinicalDetermination.outcome_recorded_at — set when feedback is filed
  • ClinicalDetermination.decision — what the engine decided
  • CareEpisode.status — resolved / abandoned / in_progress (proxy signal)

The daily scheduler job:
  1. Finds all determinations created 30+ days ago with
     outcome_feedback == None.
  2. For each determination, looks up the linked CareEpisode (via
     claim_id) and infers an outcome from the care episode state:
       • resolved → likely correct
       • abandoned / appointment_missed → unknown (no signal)
       • escalated to ER after determination → potentially incorrect
       • still open after 30 days → unknown
  3. Records the inferred outcome.
  4. Computes accuracy metrics and surfaces anomalies (sudden accuracy
     drops, specific rule types with low accuracy).

This produces the data for:
  • Accuracy dashboard (per benefit type, per decision type, per rule)
  • Alerting (accuracy below threshold triggers review)
  • Provider quality score updates (already wired in provider_selection.
    record_provider_outcome)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, UTC, timedelta
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.care_episode import CareEpisode, EpisodeStatus
from app.models.claim import Claim, ClaimStatus
from app.models.clinical_determination import ClinicalDetermination


logger = logging.getLogger(__name__)


# Minimum time after a determination before we try to infer an outcome.
# 30 days matches the standard post-care follow-up window.
DEFAULT_OUTCOME_WINDOW_DAYS = 30


# Sentinel strings persisted into ClinicalDetermination.outcome_feedback
OUTCOME_CORRECT = "correct"
OUTCOME_INCORRECT = "incorrect"
OUTCOME_PARTIAL = "partial"
OUTCOME_UNKNOWN = "unknown"


@dataclass
class OutcomeSignal:
    determination_id: str
    decision: str
    inferred_outcome: str
    reasoning: str


def _linked_episode(db: Session, det: ClinicalDetermination) -> Optional[CareEpisode]:
    """Find the CareEpisode associated with a determination via its claim."""
    if not det.claim_id:
        return None
    try:
        claim = db.query(Claim).filter(
            Claim.claim_id == det.claim_id
        ).first()
    except Exception:
        return None
    if claim is None or claim.care_episode_id is None:
        return None
    return db.query(CareEpisode).filter(
        CareEpisode.episode_id == claim.care_episode_id
    ).first()


def _find_escalation_after(
    db: Session, det: ClinicalDetermination, within_days: int = 30,
) -> Optional[Claim]:
    """Did the employee have an escalation (ER visit) in the `within_days`
    after the determination? Returns the escalation claim if found."""
    if not det.claim_id:
        return None
    det_claim = db.query(Claim).filter(
        Claim.claim_id == det.claim_id
    ).first()
    if det_claim is None:
        return None
    window_end = (det.created_at or datetime.now(UTC)) + timedelta(days=within_days)
    return (
        db.query(Claim)
        .filter(
            Claim.employee_id == det_claim.employee_id,
            Claim.claim_id != det_claim.claim_id,
            Claim.submitted_at >= det.created_at,
            Claim.submitted_at <= window_end,
            Claim.is_emergent.is_(True),
        )
        .first()
    )


def _infer_outcome(db: Session, det: ClinicalDetermination) -> OutcomeSignal:
    """Infer an outcome from the determination + linked episode + follow-on claims.

    Heuristics:
      • Episode resolved → decision was correct (the care worked)
      • Episode abandoned → unknown (employee dropped out, signal unreliable)
      • Escalation to ER within 30 days → possibly incorrect (denial that
        was wrong) or at minimum a red flag
      • Episode still open → unknown
      • No episode linked → unknown
    """
    decision = (
        det.decision.value if hasattr(det.decision, "value") else str(det.decision)
    )
    episode = _linked_episode(db, det)
    escalation = _find_escalation_after(db, det, within_days=DEFAULT_OUTCOME_WINDOW_DAYS)

    if escalation is not None and decision == "denied":
        return OutcomeSignal(
            determination_id=str(det.determination_id),
            decision=decision,
            inferred_outcome=OUTCOME_INCORRECT,
            reasoning=(
                f"Determination denied the service, but employee had an "
                f"emergent escalation (claim {escalation.claim_id}) within "
                f"{DEFAULT_OUTCOME_WINDOW_DAYS} days. This is a potential "
                f"false negative — flag for physician review."
            ),
        )

    if episode is None:
        return OutcomeSignal(
            determination_id=str(det.determination_id),
            decision=decision,
            inferred_outcome=OUTCOME_UNKNOWN,
            reasoning="No linked care episode — cannot infer outcome.",
        )

    if episode.status == EpisodeStatus.resolved:
        return OutcomeSignal(
            determination_id=str(det.determination_id),
            decision=decision,
            inferred_outcome=OUTCOME_CORRECT,
            reasoning=(
                f"Linked episode {episode.episode_id} is resolved. "
                f"Engine decision '{decision}' aligned with successful care."
            ),
        )

    if episode.status == EpisodeStatus.abandoned or episode.appointment_missed:
        return OutcomeSignal(
            determination_id=str(det.determination_id),
            decision=decision,
            inferred_outcome=OUTCOME_UNKNOWN,
            reasoning=(
                f"Episode {episode.episode_id} was abandoned or missed. "
                f"No reliable outcome signal."
            ),
        )

    if episode.follow_up_count and episode.follow_up_count >= 3:
        return OutcomeSignal(
            determination_id=str(det.determination_id),
            decision=decision,
            inferred_outcome=OUTCOME_PARTIAL,
            reasoning=(
                f"Episode required {episode.follow_up_count} follow-ups. "
                f"Decision may have been partial — review recommended."
            ),
        )

    return OutcomeSignal(
        determination_id=str(det.determination_id),
        decision=decision,
        inferred_outcome=OUTCOME_UNKNOWN,
        reasoning=(
            f"Episode {episode.episode_id} status is {episode.status.value}. "
            f"Insufficient signal to infer outcome."
        ),
    )


def run_outcomes_feedback(
    db: Session,
    *,
    window_days: int = DEFAULT_OUTCOME_WINDOW_DAYS,
    max_records: int = 500,
    as_of: Optional[datetime] = None,
) -> dict:
    """Poll recent determinations for outcomes and record inferred results.

    Registered as a daily job. Returns a summary with counts of each
    outcome classification produced in this run.
    """
    as_of = as_of or datetime.now(UTC)
    cutoff = as_of - timedelta(days=window_days)

    pending = (
        db.query(ClinicalDetermination)
        .filter(
            ClinicalDetermination.created_at <= cutoff,
            ClinicalDetermination.outcome_feedback.is_(None),
        )
        .limit(max_records)
        .all()
    )

    counts = {
        OUTCOME_CORRECT: 0,
        OUTCOME_INCORRECT: 0,
        OUTCOME_PARTIAL: 0,
        OUTCOME_UNKNOWN: 0,
    }
    flagged_for_review: list[dict] = []

    for det in pending:
        signal = _infer_outcome(db, det)
        det.outcome_feedback = signal.inferred_outcome
        det.outcome_recorded_at = as_of
        counts[signal.inferred_outcome] = counts.get(signal.inferred_outcome, 0) + 1
        if signal.inferred_outcome == OUTCOME_INCORRECT:
            flagged_for_review.append({
                "determination_id": signal.determination_id,
                "decision": signal.decision,
                "reasoning": signal.reasoning,
            })

    db.commit()

    summary = {
        "run_at": as_of.isoformat(),
        "window_days": window_days,
        "determinations_processed": len(pending),
        "counts_by_outcome": counts,
        "accuracy_pct": _compute_accuracy(counts),
        "flagged_for_review_count": len(flagged_for_review),
        "flagged_for_review": flagged_for_review[:20],
    }
    logger.info("Outcomes feedback run: %s", summary)
    return summary


def _compute_accuracy(counts: dict) -> Optional[float]:
    """Accuracy = correct / (correct + incorrect). Excludes unknown and
    partial because they're not binary outcomes."""
    correct = counts.get(OUTCOME_CORRECT, 0)
    incorrect = counts.get(OUTCOME_INCORRECT, 0)
    denominator = correct + incorrect
    if denominator == 0:
        return None
    return round(correct / denominator * 100, 1)


def get_accuracy_report(
    db: Session, *, window_days: int = 90
) -> dict:
    """Return current accuracy metrics across all determinations with
    recorded outcomes in the window. Used by the dashboard."""
    window_start = datetime.now(UTC) - timedelta(days=window_days)
    rows = (
        db.query(ClinicalDetermination.benefit_type, ClinicalDetermination.outcome_feedback)
        .filter(
            ClinicalDetermination.outcome_recorded_at >= window_start,
            ClinicalDetermination.outcome_feedback.isnot(None),
        )
        .all()
    )

    by_benefit: dict[str, dict[str, int]] = {}
    overall: dict[str, int] = {
        OUTCOME_CORRECT: 0,
        OUTCOME_INCORRECT: 0,
        OUTCOME_PARTIAL: 0,
        OUTCOME_UNKNOWN: 0,
    }
    for benefit_type, outcome in rows:
        key = benefit_type or "unknown"
        bucket = by_benefit.setdefault(key, dict(overall))
        bucket[outcome] = bucket.get(outcome, 0) + 1
        overall[outcome] = overall.get(outcome, 0) + 1

    return {
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "overall_counts": overall,
        "overall_accuracy_pct": _compute_accuracy(overall),
        "by_benefit_type": {
            bt: {
                "counts": counts,
                "accuracy_pct": _compute_accuracy(counts),
            }
            for bt, counts in by_benefit.items()
        },
    }
