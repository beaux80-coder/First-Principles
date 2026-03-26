"""Provider-initiated price offers — Constitution F2 Q16-Q17.

Providers can proactively submit price offers for platform patients.
The system does not counter-offer. The provider sets their own price.
Offers operate OUTSIDE the TEE — no financial data enters clinical filtering.
"""

import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy.orm import Session

from app.models.provider_offer import ProviderOffer, OfferStatus

logger = logging.getLogger(__name__)


def submit_offer(
    db: Session,
    provider_npi: str,
    service_code: str,
    offered_price: float,
    volume_capacity: int | None = None,
    valid_days: int = 90,
) -> dict:
    """Provider submits a price offer for platform patients.

    The provider sets their price. The system includes this offer in
    price comparison alongside every other channel (cash, published,
    reference-based, transparency-file). If the offered price is the
    lowest among clinically-sufficient options, the provider gets the
    patients. Same-day guaranteed payment as incentive.
    """
    now = datetime.now(UTC)
    offer = ProviderOffer(
        provider_npi=provider_npi,
        service_code=service_code,
        offered_price=offered_price,
        volume_capacity=volume_capacity,
        valid_from=now,
        valid_until=now + timedelta(days=valid_days),
        status=OfferStatus.active,
        created_at=now,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)

    logger.info(
        "Provider offer submitted: NPI=%s, service=%s, price=%.2f",
        provider_npi, service_code, offered_price,
    )

    return {
        "offer_id": str(offer.offer_id),
        "provider_npi": provider_npi,
        "service_code": service_code,
        "offered_price": offered_price,
        "volume_capacity": volume_capacity,
        "valid_from": offer.valid_from.isoformat(),
        "valid_until": offer.valid_until.isoformat(),
        "status": OfferStatus.active.value,
        "tee_isolation": (
            "This offer operates entirely in the price optimization layer "
            "OUTSIDE the TEE. No provider-initiated price data enters the "
            "clinical filtering process."
        ),
        "competitive_note": (
            "Your offer will be compared alongside every other pricing channel. "
            "If your price is the lowest among clinically-sufficient options, "
            "you receive the patients with same-day guaranteed payment."
        ),
        "feeding_f8": True,
    }


def get_active_offers(
    db: Session,
    provider_npi: str | None = None,
    service_code: str | None = None,
) -> list[dict]:
    """Get active provider offers, optionally filtered."""
    query = db.query(ProviderOffer).filter(
        ProviderOffer.status == OfferStatus.active,
    )
    if provider_npi:
        query = query.filter(ProviderOffer.provider_npi == provider_npi)
    if service_code:
        query = query.filter(ProviderOffer.service_code == service_code)

    offers = query.all()
    return [
        {
            "offer_id": str(o.offer_id),
            "provider_npi": o.provider_npi,
            "service_code": o.service_code,
            "offered_price": float(o.offered_price),
            "volume_capacity": o.volume_capacity,
            "valid_until": o.valid_until.isoformat() if o.valid_until else None,
            "status": o.status.value,
        }
        for o in offers
    ]


def update_offer(
    db: Session,
    offer_id: str,
    offered_price: float | None = None,
    volume_capacity: int | None = None,
    valid_days: int | None = None,
) -> dict:
    """Provider updates their offer. Can change price at any time."""
    offer = db.query(ProviderOffer).filter(ProviderOffer.offer_id == offer_id).first()
    if not offer:
        return {"error": "offer_not_found"}

    if offered_price is not None:
        offer.offered_price = offered_price
    if volume_capacity is not None:
        offer.volume_capacity = volume_capacity
    if valid_days is not None:
        offer.valid_until = datetime.now(UTC) + timedelta(days=valid_days)

    db.commit()

    return {
        "offer_id": str(offer.offer_id),
        "updated": True,
        "offered_price": float(offer.offered_price),
        "volume_capacity": offer.volume_capacity,
        "valid_until": offer.valid_until.isoformat() if offer.valid_until else None,
        "feeding_f8": True,
    }


def withdraw_offer(db: Session, offer_id: str) -> dict:
    """Provider withdraws their offer."""
    offer = db.query(ProviderOffer).filter(ProviderOffer.offer_id == offer_id).first()
    if not offer:
        return {"error": "offer_not_found"}

    offer.status = OfferStatus.withdrawn
    db.commit()

    return {"offer_id": str(offer.offer_id), "status": "withdrawn", "feeding_f8": True}


def expire_stale_offers(db: Session) -> int:
    """Auto-expire offers past their valid_until date."""
    now = datetime.now(UTC)
    expired = (
        db.query(ProviderOffer)
        .filter(
            ProviderOffer.status == OfferStatus.active,
            ProviderOffer.valid_until is not None,
            ProviderOffer.valid_until < now,
        )
        .all()
    )
    count = 0
    for offer in expired:
        offer.status = OfferStatus.expired
        count += 1
    if count:
        db.commit()
    return count
