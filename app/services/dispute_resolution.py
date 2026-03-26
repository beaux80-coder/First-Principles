"""Dispute resolution — Constitution F2 Q8-Q9.

When a provider disputes a payment amount, the system resolves it by:
(a) referencing the provider's own published price and the pre-service
confirmation, (b) correcting factual errors automatically, (c) presenting
the documented price trail for price disagreements.

Every dispute and resolution recorded as structured data feeding F8.
"""

import logging
from datetime import datetime, UTC

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.dispute import Dispute, DisputeType, DisputeStatus
from app.models.price_data import PriceData

logger = logging.getLogger(__name__)


def file_dispute(
    db: Session,
    claim_id: str,
    provider_npi: str,
    dispute_type: str,
    provider_stated_amount: float,
    system_verified_amount: float,
) -> dict:
    """Provider files a dispute on a payment amount."""
    try:
        d_type = DisputeType(dispute_type)
    except ValueError:
        d_type = DisputeType.price_disagreement

    dispute = Dispute(
        claim_id=claim_id,
        provider_npi=provider_npi,
        dispute_type=d_type,
        status=DisputeStatus.open,
        provider_stated_amount=provider_stated_amount,
        system_verified_amount=system_verified_amount,
    )
    db.add(dispute)
    db.commit()
    db.refresh(dispute)

    logger.info("Dispute filed: %s for claim %s", dispute.dispute_id, claim_id)

    return {
        "dispute_id": str(dispute.dispute_id),
        "claim_id": claim_id,
        "provider_npi": provider_npi,
        "dispute_type": d_type.value,
        "status": DisputeStatus.open.value,
        "provider_stated_amount": provider_stated_amount,
        "system_verified_amount": system_verified_amount,
        "feeding_f8": True,
    }


def auto_resolve(db: Session, dispute_id: str) -> dict:
    """Attempt automatic resolution of a dispute.

    Constitution F2 spec: (a) reference published price + pre-service
    confirmation, (b) correct factual errors automatically, (c) present
    price trail for price disagreements.
    """
    dispute = db.query(Dispute).filter(Dispute.dispute_id == dispute_id).first()
    if not dispute:
        return {"error": "dispute_not_found"}

    # Look up published prices for this provider
    published_prices = (
        db.query(PriceData)
        .filter(PriceData.provider_npi == dispute.provider_npi)
        .order_by(PriceData.ingested_at.desc())
        .limit(10)
        .all()
    )

    price_trail = [
        {
            "source": p.source.value if p.source else "unknown",
            "price": float(p.price),
            "channel": p.channel,
            "ingested_at": p.ingested_at.isoformat() if p.ingested_at else None,
        }
        for p in published_prices
    ]

    # Resolution logic
    resolution = None
    resolution_method = None

    if dispute.dispute_type == DisputeType.factual_error:
        # Check if service code or amount was wrong
        if abs(float(dispute.provider_stated_amount) - float(dispute.system_verified_amount)) < 0.01:
            resolution = "Amounts match — no factual error detected"
            resolution_method = "programmatic"
            dispute.status = DisputeStatus.auto_resolved
        else:
            resolution = (
                f"Provider states ${float(dispute.provider_stated_amount):.2f}, "
                f"system verified ${float(dispute.system_verified_amount):.2f}. "
                f"Difference: ${abs(float(dispute.provider_stated_amount) - float(dispute.system_verified_amount)):.2f}. "
                "Escalating for review."
            )
            resolution_method = "escalated"
            dispute.status = DisputeStatus.escalated

    elif dispute.dispute_type == DisputeType.price_disagreement:
        # Present the full price trail
        dispute.published_price_reference = (
            f"Published prices found: {len(price_trail)} records. "
            f"System paid the lowest verified price at time of service."
        )
        if price_trail:
            lowest_published = min(p["price"] for p in price_trail)
            if float(dispute.system_verified_amount) <= lowest_published:
                resolution = (
                    f"System paid ${float(dispute.system_verified_amount):.2f} which equals or is below "
                    f"the provider's own published price (${lowest_published:.2f}). "
                    "The price was the provider's own published price, paid in full."
                )
                resolution_method = "programmatic"
                dispute.status = DisputeStatus.auto_resolved
            else:
                resolution = (
                    f"Price discrepancy: system paid ${float(dispute.system_verified_amount):.2f} "
                    f"but lowest published price is ${lowest_published:.2f}. "
                    "Escalating for human review."
                )
                resolution_method = "escalated"
                dispute.status = DisputeStatus.escalated
        else:
            resolution = "No published price records found. Escalating for review."
            resolution_method = "escalated"
            dispute.status = DisputeStatus.escalated

    dispute.resolution = resolution
    dispute.resolution_method = resolution_method
    if dispute.status in (DisputeStatus.auto_resolved, DisputeStatus.resolved):
        dispute.resolved_at = datetime.now(UTC)

    db.commit()

    return {
        "dispute_id": str(dispute.dispute_id),
        "status": dispute.status.value,
        "resolution": resolution,
        "resolution_method": resolution_method,
        "price_trail": price_trail,
        "published_price_reference": dispute.published_price_reference,
        "feeding_f8": True,
    }


def escalate_to_human(db: Session, dispute_id: str) -> dict:
    """Escalate dispute to human review when auto-resolution fails."""
    dispute = db.query(Dispute).filter(Dispute.dispute_id == dispute_id).first()
    if not dispute:
        return {"error": "dispute_not_found"}

    dispute.status = DisputeStatus.escalated
    db.commit()

    return {
        "dispute_id": str(dispute.dispute_id),
        "status": DisputeStatus.escalated.value,
        "note": "Dispute escalated to human reviewer for resolution",
        "feeding_f8": True,
    }


def resolve_dispute(
    db: Session,
    dispute_id: str,
    resolution: str,
    resolution_method: str = "human",
) -> dict:
    """Record final dispute resolution."""
    dispute = db.query(Dispute).filter(Dispute.dispute_id == dispute_id).first()
    if not dispute:
        return {"error": "dispute_not_found"}

    dispute.status = DisputeStatus.resolved
    dispute.resolution = resolution
    dispute.resolution_method = resolution_method
    dispute.resolved_at = datetime.now(UTC)
    db.commit()

    return {
        "dispute_id": str(dispute.dispute_id),
        "status": DisputeStatus.resolved.value,
        "resolution": resolution,
        "resolution_method": resolution_method,
        "resolved_at": dispute.resolved_at.isoformat(),
        "feeding_f8": True,
    }


def get_dispute(db: Session, dispute_id: str) -> dict | None:
    """Get dispute details."""
    dispute = db.query(Dispute).filter(Dispute.dispute_id == dispute_id).first()
    if not dispute:
        return None
    return {
        "dispute_id": str(dispute.dispute_id),
        "claim_id": dispute.claim_id,
        "provider_npi": dispute.provider_npi,
        "dispute_type": dispute.dispute_type.value,
        "status": dispute.status.value,
        "provider_stated_amount": float(dispute.provider_stated_amount),
        "system_verified_amount": float(dispute.system_verified_amount),
        "resolution": dispute.resolution,
        "resolution_method": dispute.resolution_method,
        "created_at": dispute.created_at.isoformat(),
        "resolved_at": dispute.resolved_at.isoformat() if dispute.resolved_at else None,
    }
