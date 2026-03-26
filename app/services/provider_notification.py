"""Pre-service provider notification — Constitution F2 Q6.

For every authorized service, the system notifies the provider in advance
that the service is approved and payment is guaranteed. Notification includes
the confirmed payment amount, payment method, and expected timeline.
"""

import logging
from datetime import datetime, UTC

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.provider_notification import ProviderNotification, NotificationStatus

logger = logging.getLogger(__name__)


def send_pre_service_notification(
    db: Session,
    provider_npi: str,
    claim_id: str,
    confirmed_amount: float,
    payment_method: str = "direct_electronic",
    expected_timeline: str = "Same-day payment upon service completion",
) -> dict:
    """Send pre-service authorization notification to provider.

    Constitution F2: The system notifies the provider in advance that
    the service is approved and payment is guaranteed. The notification
    includes the confirmed payment amount, the payment method, and the
    expected payment timeline.
    """
    notification = ProviderNotification(
        provider_npi=provider_npi,
        claim_id=claim_id,
        notification_type="pre_service_auth",
        confirmed_amount=confirmed_amount,
        payment_method=payment_method,
        expected_payment_timeline=expected_timeline,
        status=NotificationStatus.sent,
        sent_at=datetime.now(UTC),
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)

    logger.info(
        "Pre-service notification sent: provider=%s, claim=%s, amount=%.2f",
        provider_npi, claim_id, confirmed_amount,
    )

    return {
        "notification_id": str(notification.notification_id),
        "provider_npi": provider_npi,
        "claim_id": claim_id,
        "confirmed_amount": confirmed_amount,
        "payment_method": payment_method,
        "expected_payment_timeline": expected_timeline,
        "status": NotificationStatus.sent.value,
        "message_to_provider": (
            f"Service approved. Payment of ${confirmed_amount:.2f} is guaranteed "
            f"via {payment_method}. {expected_timeline}. "
            "No new processes, portals, or software required from your practice."
        ),
        "feeding_f8": True,
    }


def confirm_notification(db: Session, notification_id: str) -> dict:
    """Provider confirms receipt of notification."""
    notification = db.query(ProviderNotification).filter(
        ProviderNotification.notification_id == notification_id
    ).first()
    if not notification:
        return {"error": "notification_not_found"}

    notification.status = NotificationStatus.confirmed
    notification.confirmed_at = datetime.now(UTC)
    db.commit()

    return {
        "notification_id": str(notification.notification_id),
        "status": NotificationStatus.confirmed.value,
        "confirmed_at": notification.confirmed_at.isoformat(),
    }


def get_provider_notifications(db: Session, provider_npi: str) -> list[dict]:
    """List notifications for a provider."""
    notifications = (
        db.query(ProviderNotification)
        .filter(ProviderNotification.provider_npi == provider_npi)
        .order_by(ProviderNotification.sent_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "notification_id": str(n.notification_id),
            "claim_id": n.claim_id,
            "confirmed_amount": float(n.confirmed_amount),
            "payment_method": n.payment_method,
            "expected_payment_timeline": n.expected_payment_timeline,
            "status": n.status.value,
            "sent_at": n.sent_at.isoformat(),
            "confirmed_at": n.confirmed_at.isoformat() if n.confirmed_at else None,
        }
        for n in notifications
    ]
