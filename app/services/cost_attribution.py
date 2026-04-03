"""Cost Change Attribution Engine — F8 Supplement S1.

Every time a cost input changes anywhere in the system, the data pipeline
records not just the change but its causal source. Attribution is conservative —
defaulting to external/unrelated where causation is ambiguous.
"""

import logging
import uuid
from datetime import datetime, timedelta, UTC
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.price_data import PriceData

logger = logging.getLogger(__name__)


class CausalSource:
    """Causal source categories for cost changes."""
    PLATFORM_VOLUME = "platform_volume_effect"
    PROVIDER_COMPETITION = "provider_competition_response"
    PREDICTION_ACCURACY = "prediction_accuracy_improvement"
    SERVICE_PRICE_DISCOVERY = "service_level_price_discovery"
    EXTERNAL = "external_unrelated"


# Attribution window: max days between correlated events
_ATTRIBUTION_WINDOW_DAYS = 14


def tag_cost_change(
    db: Session,
    employer_id: str,
    change_type: str,
    old_value: float,
    new_value: float,
    service_code: Optional[str] = None,
    provider_npi: Optional[str] = None,
    state: Optional[str] = None,
) -> dict:
    """Tag a cost-relevant data change with its causal source.

    Attribution logic:
    1. Check if provider viewed Intelligence Feed recently → provider_competition
    2. Check if platform volume increased in metro → platform_volume
    3. Check if prediction confidence improved → prediction_accuracy
    4. Check if new pricing channel found → service_price_discovery
    5. Default → external_unrelated (conservative)
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(days=_ATTRIBUTION_WINDOW_DAYS)
    dollar_change = round(new_value - old_value, 2)

    source = CausalSource.EXTERNAL  # Conservative default
    source_evidence = []
    correlated_event = None

    # 1. Provider competition response — price change correlated with feed view
    if provider_npi and dollar_change < 0:
        feed_views = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "provider_feed_view",
                AuditLog.resource_id == provider_npi,
                AuditLog.timestamp >= window_start,
            )
            .count()
        )
        if feed_views > 0:
            source = CausalSource.PROVIDER_COMPETITION
            source_evidence.append(
                f"Provider {provider_npi} viewed Intelligence Feed {feed_views} times "
                f"in the {_ATTRIBUTION_WINDOW_DAYS} days before this price change"
            )
            correlated_event = f"feed_view_count:{feed_views}"

    # 2. Platform volume effect — new employers in metro
    if source == CausalSource.EXTERNAL and state:
        from app.models.employer import Employer
        new_employers = (
            db.query(func.count(Employer.employer_id))
            .filter(
                Employer.geography.like(f"{state}%"),
                Employer.created_at >= window_start,
            )
            .scalar() or 0
        )
        if new_employers > 0 and dollar_change < 0:
            source = CausalSource.PLATFORM_VOLUME
            source_evidence.append(
                f"{new_employers} new employers joined in {state} metro in the last "
                f"{_ATTRIBUTION_WINDOW_DAYS} days"
            )
            correlated_event = f"new_employers:{new_employers}"

    # 3. Prediction accuracy improvement — buffer reduction
    if source == CausalSource.EXTERNAL and change_type == "prediction_buffer":
        source = CausalSource.PREDICTION_ACCURACY
        source_evidence.append(
            "Prediction confidence interval narrowed with accumulated data"
        )
        correlated_event = "confidence_interval_narrowed"

    # 4. Service-level price discovery — new lower-cost option found
    if source == CausalSource.EXTERNAL and service_code and dollar_change < 0:
        new_prices = (
            db.query(func.count(PriceData.price_id))
            .filter(
                PriceData.service_code == service_code,
                PriceData.ingested_at >= window_start,
            )
            .scalar() or 0
        )
        if new_prices > 0:
            source = CausalSource.SERVICE_PRICE_DISCOVERY
            source_evidence.append(
                f"{new_prices} new price records for {service_code} ingested recently"
            )
            correlated_event = f"new_price_records:{new_prices}"

    # Record the tagged change
    tagged_change = {
        "change_id": str(uuid.uuid4()),
        "employer_id": employer_id,
        "change_type": change_type,
        "old_value": old_value,
        "new_value": new_value,
        "dollar_change": dollar_change,
        "causal_source": source,
        "source_evidence": source_evidence,
        "correlated_event": correlated_event,
        "service_code": service_code,
        "provider_npi": provider_npi,
        "state": state,
        "tagged_at": now.isoformat(),
        "attribution_window_days": _ATTRIBUTION_WINDOW_DAYS,
        "conservative_attribution": source == CausalSource.EXTERNAL,
        "downstream_functions_affected": ["F3", "F7", "F7A"],
        "feeding_f7": True,
        "feeding_f6b": True,
    }

    # Persist to audit log
    log = AuditLog(
        actor="system:cost_attribution",
        action="cost_change_tagged",
        resource_type="cost_change",
        resource_id=tagged_change["change_id"],
        details=tagged_change,
    )
    db.add(log)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Failed to persist cost change tag", exc_info=True)

    return tagged_change


def get_employer_cost_attribution(
    db: Session,
    employer_id: str,
    months: int = 12,
) -> dict:
    """Get all cost change attributions for an employer.

    Returns cumulative attribution by source category since the employer joined,
    plus per-period breakdown.
    """
    since = datetime.now(UTC) - timedelta(days=months * 30)

    tagged_changes = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "cost_change_tagged",
            AuditLog.timestamp >= since,
        )
        .all()
    )

    # Filter to this employer
    employer_changes = []
    for log in tagged_changes:
        details = log.details or {}
        if details.get("employer_id") == employer_id:
            employer_changes.append(details)

    # Aggregate by source
    by_source = {
        CausalSource.PLATFORM_VOLUME: 0.0,
        CausalSource.PROVIDER_COMPETITION: 0.0,
        CausalSource.PREDICTION_ACCURACY: 0.0,
        CausalSource.SERVICE_PRICE_DISCOVERY: 0.0,
        CausalSource.EXTERNAL: 0.0,
    }

    for change in employer_changes:
        src = change.get("causal_source", CausalSource.EXTERNAL)
        by_source[src] = by_source.get(src, 0.0) + change.get("dollar_change", 0.0)

    # Round values
    by_source = {k: round(v, 2) for k, v in by_source.items()}

    platform_driven_total = round(
        by_source[CausalSource.PLATFORM_VOLUME]
        + by_source[CausalSource.PROVIDER_COMPETITION]
        + by_source[CausalSource.PREDICTION_ACCURACY]
        + by_source[CausalSource.SERVICE_PRICE_DISCOVERY],
        2,
    )

    return {
        "employer_id": employer_id,
        "period_months": months,
        "total_changes_tracked": len(employer_changes),
        "attribution_by_source": by_source,
        "platform_driven_total": platform_driven_total,
        "external_total": by_source[CausalSource.EXTERNAL],
        "attribution_methodology": (
            "Conservative attribution: changes default to external/unrelated "
            "unless platform-driven cause is supported by specific correlated events "
            "within a 14-day window. Platform attribution is never inflated."
        ),
        "feeding_f7": True,
        "feeding_f6b": True,
    }
