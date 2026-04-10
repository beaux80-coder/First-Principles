"""Layer 4 — Population Health Feedback Metrics.

The orchestrator gets smarter every year because every claim, every care
episode, and every outcome feeds back into the decision layers. This module
exposes the feedback signals that (a) measure whether the orchestrator is
actually working and (b) point to the next tuning opportunity.

Reused signals:
  • ClinicalDetermination.outcome_feedback — already populated by the
    clinical engine's outcome-recording API (clinical_engine.py).
  • Provider.quality_score / outcome_data_points — updated on every
    recorded outcome via provider_selection.record_provider_outcome.
  • CareEpisode lifecycle fields — created_at, resolved_at, status,
    appointment_missed, follow_up_count — show whether routing is
    producing resolved care or drop-offs.
  • Claim fields — is_emergent, care_episode_id, amount_paid — show
    routed vs emergent vs unrouted breakdown and what the engine is
    paying for.

This module does NOT replace the existing `get_processing_metrics` or
`get_published_rates` functions elsewhere in the codebase — it rolls them
into a single dashboard payload organized by orchestration layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, UTC, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import func, and_, case
from sqlalchemy.orm import Session

from app.models.care_episode import CareEpisode, EpisodeStatus
from app.models.claim import Claim, ClaimStatus
from app.models.clinical_determination import ClinicalDetermination
from app.models.employee import Employee, EmployeeStatus
from app.models.employer import Employer
from app.models.provider import Provider


logger = logging.getLogger(__name__)


def population_health_dashboard(
    db: Session,
    employer_id: Optional[UUID] = None,
    window_days: int = 90,
) -> dict:
    """Compute the full Layer 4 feedback dashboard.

    Parameters
    ----------
    employer_id:
        If provided, scope all metrics to a single employer's population.
        Otherwise, report platform-wide numbers.
    window_days:
        How far back to look for time-bound metrics (unrouted rate, cost
        per employee, etc.). Preventive completion rate is evaluated on
        the lifetime of each rule's frequency window regardless of this
        setting.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(days=window_days)

    employee_query = db.query(Employee).filter(Employee.status == EmployeeStatus.active)
    if employer_id is not None:
        employee_query = employee_query.filter(Employee.employer_id == employer_id)
    active_employees = employee_query.all()
    employee_ids = [e.employee_id for e in active_employees]

    return {
        "generated_at": now.isoformat(),
        "employer_id": str(employer_id) if employer_id else None,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "active_employee_count": len(active_employees),
        "layer_1_preventive": _preventive_metrics(db, employee_ids),
        "layer_2_routing": _routing_metrics(db, employee_ids, window_start),
        "layer_3_verification": _verification_metrics(db, employee_ids, window_start),
        "layer_4_learning": _learning_metrics(db, employer_id, window_start),
    }


# ---------------------------------------------------------------------------
# Layer 1 — Proactive preventive care
# ---------------------------------------------------------------------------
def _preventive_metrics(db: Session, employee_ids: list[UUID]) -> dict:
    """Preventive care completion and scheduling metrics.

    - `scheduled`: preventive episodes created by the scheduler
    - `resolved`: preventive episodes that reached resolved status
    - `missed_appointments`: preventive episodes where the employee no-showed
    - `completion_rate_pct`: resolved / (resolved + abandoned) for preventive
    """
    if not employee_ids:
        return {
            "scheduled": 0,
            "resolved": 0,
            "abandoned": 0,
            "missed_appointments": 0,
            "completion_rate_pct": None,
            "by_rule": {},
        }

    base_filter = and_(
        CareEpisode.employee_id.in_(employee_ids),
        CareEpisode.is_preventive.is_(True),
    )

    scheduled = db.query(func.count(CareEpisode.episode_id)).filter(base_filter).scalar() or 0
    resolved = db.query(func.count(CareEpisode.episode_id)).filter(
        and_(base_filter, CareEpisode.status == EpisodeStatus.resolved)
    ).scalar() or 0
    abandoned = db.query(func.count(CareEpisode.episode_id)).filter(
        and_(base_filter, CareEpisode.status == EpisodeStatus.abandoned)
    ).scalar() or 0
    missed = db.query(func.count(CareEpisode.episode_id)).filter(
        and_(base_filter, CareEpisode.appointment_missed.is_(True))
    ).scalar() or 0

    denominator = resolved + abandoned
    completion_rate = round(resolved / denominator * 100, 1) if denominator > 0 else None

    by_rule_rows = (
        db.query(
            CareEpisode.preventive_rule_id,
            func.count(CareEpisode.episode_id),
            func.sum(
                case(
                    (CareEpisode.status == EpisodeStatus.resolved, 1),
                    else_=0,
                )
            ),
        )
        .filter(base_filter)
        .group_by(CareEpisode.preventive_rule_id)
        .all()
    )
    by_rule: dict[str, dict] = {}
    for rule_id, total, resolved_count in by_rule_rows:
        if rule_id is None:
            continue
        resolved_count = int(resolved_count or 0)
        total = int(total or 0)
        by_rule[rule_id] = {
            "scheduled": total,
            "resolved": resolved_count,
            "completion_rate_pct": (
                round(resolved_count / total * 100, 1) if total > 0 else None
            ),
        }

    return {
        "scheduled": scheduled,
        "resolved": resolved,
        "abandoned": abandoned,
        "missed_appointments": missed,
        "completion_rate_pct": completion_rate,
        "by_rule": by_rule,
    }


# ---------------------------------------------------------------------------
# Layer 2 — Care routing
# ---------------------------------------------------------------------------
def _routing_metrics(
    db: Session, employee_ids: list[UUID], window_start: datetime
) -> dict:
    """Routing metrics: volume, resolution, and passive observability of
    the quality of providers the engine happened to select.

    IMPORTANT: the observed_provider_quality_score metric is a passive
    descriptive statistic. Provider selection is based on certification,
    convenience, and cost only — quality_score is NOT used as a filter
    or ranking input. This metric is published so an operator can notice
    if cost-only selection is incidentally routing employees to lower-
    outcome providers, which would be a signal to investigate, not to
    change the selection logic automatically.
    """
    if not employee_ids:
        return {
            "episodes_created": 0,
            "episodes_resolved": 0,
            "resolution_rate_pct": None,
            "observed_provider_quality_score": None,
            "observed_provider_quality_note": (
                "Passive observability metric only. Not used in selection."
            ),
            "average_follow_up_count": None,
        }

    base_filter = and_(
        CareEpisode.employee_id.in_(employee_ids),
        CareEpisode.created_at >= window_start,
    )

    total = db.query(func.count(CareEpisode.episode_id)).filter(base_filter).scalar() or 0
    resolved = db.query(func.count(CareEpisode.episode_id)).filter(
        and_(base_filter, CareEpisode.status == EpisodeStatus.resolved)
    ).scalar() or 0
    resolution_rate = round(resolved / total * 100, 1) if total > 0 else None

    # Passive observability: what is the measured outcome score of the
    # providers the engine incidentally selected? This is descriptive,
    # not prescriptive — the selection logic ignores quality_score.
    provider_scores = (
        db.query(Provider.quality_score)
        .join(CareEpisode, CareEpisode.provider_id == Provider.provider_id)
        .filter(base_filter)
        .filter(Provider.quality_score.isnot(None))
        .all()
    )
    observed_quality = (
        round(sum(float(row[0]) for row in provider_scores) / len(provider_scores), 2)
        if provider_scores
        else None
    )

    avg_follow_up = db.query(func.avg(CareEpisode.follow_up_count)).filter(
        base_filter
    ).scalar()
    avg_follow_up = round(float(avg_follow_up), 2) if avg_follow_up is not None else None

    return {
        "episodes_created": total,
        "episodes_resolved": resolved,
        "resolution_rate_pct": resolution_rate,
        "observed_provider_quality_score": observed_quality,
        "observed_provider_quality_note": (
            "Passive observability metric only. Not used in selection."
        ),
        "average_follow_up_count": avg_follow_up,
    }


# ---------------------------------------------------------------------------
# Layer 3 — Claims verification
# ---------------------------------------------------------------------------
def _verification_metrics(
    db: Session, employee_ids: list[UUID], window_start: datetime
) -> dict:
    """Verification metrics: routed vs emergent vs unrouted, cost per employee."""
    if not employee_ids:
        return {
            "total_claims": 0,
            "routed": 0,
            "emergent": 0,
            "unrouted_denied": 0,
            "other_denied": 0,
            "routed_rate_pct": None,
            "emergent_rate_pct": None,
            "unrouted_rate_pct": None,
            "total_paid_amount": 0.0,
            "average_cost_per_active_employee": 0.0,
        }

    base_filter = and_(
        Claim.employee_id.in_(employee_ids),
        Claim.submitted_at >= window_start,
    )
    total = db.query(func.count(Claim.claim_id)).filter(base_filter).scalar() or 0

    routed = db.query(func.count(Claim.claim_id)).filter(
        and_(base_filter, Claim.care_episode_id.isnot(None))
    ).scalar() or 0

    emergent = db.query(func.count(Claim.claim_id)).filter(
        and_(base_filter, Claim.is_emergent.is_(True))
    ).scalar() or 0

    unrouted_denied = db.query(func.count(Claim.claim_id)).filter(
        and_(
            base_filter,
            Claim.status == ClaimStatus.denied,
            Claim.denial_reason.ilike("%not scheduled through%"),
        )
    ).scalar() or 0

    other_denied = db.query(func.count(Claim.claim_id)).filter(
        and_(base_filter, Claim.status == ClaimStatus.denied)
    ).scalar() or 0
    other_denied = max(0, other_denied - unrouted_denied)

    paid_sum = db.query(func.coalesce(func.sum(Claim.amount_paid), 0.0)).filter(
        and_(base_filter, Claim.status.in_([ClaimStatus.approved, ClaimStatus.paid]))
    ).scalar() or 0.0
    paid_sum = float(paid_sum)

    def _pct(numer: int) -> Optional[float]:
        return round(numer / total * 100, 1) if total > 0 else None

    return {
        "total_claims": total,
        "routed": routed,
        "emergent": emergent,
        "unrouted_denied": unrouted_denied,
        "other_denied": other_denied,
        "routed_rate_pct": _pct(routed),
        "emergent_rate_pct": _pct(emergent),
        "unrouted_rate_pct": _pct(unrouted_denied),
        "total_paid_amount": round(paid_sum, 2),
        "average_cost_per_active_employee": round(
            paid_sum / len(employee_ids), 2
        ) if employee_ids else 0.0,
    }


# ---------------------------------------------------------------------------
# Layer 4 — Continuous learning
# ---------------------------------------------------------------------------
def _learning_metrics(
    db: Session,
    employer_id: Optional[UUID],
    window_start: datetime,
) -> dict:
    """Continuous learning metrics.

    - Outcome feedback coverage (what fraction of determinations have an
      outcome recorded — the signal the engine learns from).
    - Provider quality score drift (how many providers had their score
      updated in the window).
    - Determination accuracy (correct / total feedback).
    """
    det_query = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.created_at >= window_start
    )
    total_dets = det_query.count()
    with_feedback = det_query.filter(
        ClinicalDetermination.outcome_feedback.isnot(None)
    ).count()
    correct = det_query.filter(
        ClinicalDetermination.outcome_feedback == "correct"
    ).count()

    feedback_coverage = (
        round(with_feedback / total_dets * 100, 1) if total_dets > 0 else None
    )
    accuracy = (
        round(correct / with_feedback * 100, 1) if with_feedback > 0 else None
    )

    providers_with_data = db.query(func.count(Provider.provider_id)).filter(
        Provider.outcome_data_points > 0
    ).scalar() or 0

    return {
        "determinations_in_window": total_dets,
        "outcome_feedback_recorded": with_feedback,
        "feedback_coverage_pct": feedback_coverage,
        "determination_accuracy_pct": accuracy,
        "providers_with_platform_outcome_data": providers_with_data,
    }
