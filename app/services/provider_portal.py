"""Provider Portal Service (Function 2, Questions 6-9).

Constitution: "The provider portal is web-based, requiring no software
installation. The provider receives a pre-service notification with the
confirmed amount and payment method."

Constitution: "Payment executed directly to the provider via direct
electronic payment at the earliest moment a verified charge is presented.
If direct electronic routing is physically impossible at that instant,
exact-amount virtual card."

Constitution: "If the provider disagrees, the documented price trail —
the provider's own published price, the pre-service confirmation, and
the payment — constitutes the factual record."

This module implements:
1. Pre-service notification (confirmed amount + payment method)
2. Charge submission with validation (1% tolerance)
3. Discrepancy resolution (programmatic -> human escalation)
4. Dispute filing with auto-populated price trail
5. Dispute resolution (factual error correction or price trail presentation)
6. Provider dashboard (web-based, no software installation)
7. Payment history

Every interaction is recorded as structured data feeding F8.
"""

import logging
import uuid
from datetime import datetime, UTC
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import func, and_, desc
from sqlalchemy.orm import Session

from app.models.provider_portal import (
    ProviderAuthorization,
    ProviderDispute,
    AuthorizationStatus,
    AuthPaymentMethod,
    DisputeStatus,
    DisputeResolutionMethod,
)
from app.models.claim import Claim
from app.models.provider import Provider
from app.models.price_data import PriceData

logger = logging.getLogger(__name__)

# ---- Payment timeline constants ----
# Constitution: "Payment executed at the earliest moment a verified charge
# is presented." These represent our commitments to the provider.
PAYMENT_TIMELINES = {
    AuthPaymentMethod.ach_direct: "same day",
    AuthPaymentMethod.virtual_card: "within 24 hours",
}

# ---- Charge validation ----
# 1% tolerance for charge vs. pre-authorized amount
CHARGE_TOLERANCE_PCT = Decimal("0.01")

# ---- Discrepancy resolution categories ----
FACTUAL_ERROR_KEYWORDS = [
    "wrong code",
    "wrong service",
    "incorrect code",
    "coding error",
    "wrong procedure",
    "wrong cpt",
    "wrong hcpcs",
    "misidentified",
    "wrong patient",
    "wrong date",
]

PRICE_DISAGREEMENT_KEYWORDS = [
    "price",
    "rate",
    "amount",
    "fee",
    "charge",
    "cost",
    "reimbursement",
    "underpaid",
    "underpayment",
    "below",
    "insufficient",
]


def create_pre_service_notification(
    db: Session,
    provider_id: uuid.UUID,
    claim_id: Optional[uuid.UUID],
    service_code: str,
    service_description: Optional[str],
    benefit_type: str,
    confirmed_amount: float,
    payment_method: str,
) -> dict:
    """Create a pre-service notification for a provider.

    Constitution: "The provider receives a pre-service notification with
    the confirmed amount and payment method."

    The provider portal is web-based — no software installation needed.
    This creates the authorization record and returns the notification
    data that is served via API to the provider's web portal.

    Args:
        db: Database session
        provider_id: Provider UUID
        claim_id: Associated claim UUID (if applicable)
        service_code: CPT/HCPCS/NDC code
        service_description: Human-readable service description
        benefit_type: Type of benefit (health, dental, vision, etc.)
        confirmed_amount: Pre-authorized amount
        payment_method: ach_direct or virtual_card

    Returns:
        Notification details including auth_id, confirmed amount,
        payment method, and estimated payment timeline.
    """
    # Validate provider exists
    provider = db.query(Provider).filter(
        Provider.provider_id == provider_id
    ).first()
    if not provider:
        return {
            "error": "provider_not_found",
            "provider_id": str(provider_id),
        }

    # Validate claim if provided
    if claim_id:
        claim = db.query(Claim).filter(Claim.claim_id == claim_id).first()
        if not claim:
            return {
                "error": "claim_not_found",
                "claim_id": str(claim_id),
            }

    # Resolve payment method enum
    try:
        pay_method = AuthPaymentMethod(payment_method)
    except ValueError:
        return {
            "error": "invalid_payment_method",
            "valid_methods": [m.value for m in AuthPaymentMethod],
        }

    # Determine estimated payment timeline based on payment method
    timeline = PAYMENT_TIMELINES.get(pay_method, "within 48 hours")

    now = datetime.now(UTC)

    authorization = ProviderAuthorization(
        provider_id=provider_id,
        claim_id=claim_id,
        service_code=service_code,
        service_description=service_description,
        benefit_type=benefit_type,
        confirmed_amount=confirmed_amount,
        payment_method=pay_method,
        estimated_payment_timeline=timeline,
        pre_service_notified_at=now,
        notification_channel="portal",
        status=AuthorizationStatus.notified,
    )

    db.add(authorization)
    db.commit()
    db.refresh(authorization)

    logger.info(
        "Pre-service notification created: auth_id=%s, provider=%s, "
        "amount=%.2f, method=%s, timeline=%s",
        authorization.auth_id,
        provider.name,
        confirmed_amount,
        pay_method.value,
        timeline,
    )

    return {
        "auth_id": str(authorization.auth_id),
        "provider_id": str(provider_id),
        "provider_name": provider.name,
        "claim_id": str(claim_id) if claim_id else None,
        "service_code": service_code,
        "service_description": service_description,
        "benefit_type": benefit_type,
        "confirmed_amount": confirmed_amount,
        "payment_method": pay_method.value,
        "estimated_payment_timeline": timeline,
        "notification_channel": "portal",
        "notified_at": now.isoformat(),
        "status": AuthorizationStatus.notified.value,
        "portal_note": (
            "Web-based portal — no software installation required. "
            "Provider can view this notification at any time via the portal."
        ),
    }


def submit_provider_charge(
    db: Session,
    auth_id: uuid.UUID,
    provider_id: uuid.UUID,
    charge_amount: float,
) -> dict:
    """Submit a charge against a pre-service authorization.

    Constitution: "Payment executed directly to the provider via direct
    electronic payment at the earliest moment a verified charge is presented."

    Validation rules:
    1. If charge matches pre-authorized amount (within 1% tolerance):
       validate and proceed to payment immediately.
    2. If discrepancy exists:
       a. Compare against provider's published prices in price_data
       b. Compare against pre-service confirmation
       c. Compare against service actually rendered
    3. If programmatic resolution succeeds: adjust and proceed.
    4. If programmatic resolution fails: escalate to human review.

    Args:
        db: Database session
        auth_id: Authorization UUID
        provider_id: Provider UUID (must match authorization)
        charge_amount: Amount the provider is charging

    Returns:
        Validation result with status, any discrepancy details,
        and resolution path.
    """
    authorization = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.auth_id == auth_id,
    ).first()
    if not authorization:
        return {"error": "authorization_not_found", "auth_id": str(auth_id)}

    # Verify provider owns this authorization
    if authorization.provider_id != provider_id:
        return {
            "error": "provider_mismatch",
            "detail": "This authorization belongs to a different provider.",
        }

    # Verify authorization is in a valid state for charge submission
    valid_states = {
        AuthorizationStatus.notified,
        AuthorizationStatus.charge_received,
    }
    if authorization.status not in valid_states:
        return {
            "error": "invalid_authorization_state",
            "current_status": authorization.status.value,
            "detail": (
                f"Authorization is in '{authorization.status.value}' state. "
                f"Charges can only be submitted when status is "
                f"'notified' or 'charge_received'."
            ),
        }

    now = datetime.now(UTC)
    authorization.charge_submitted_at = now
    authorization.charge_amount = charge_amount
    authorization.status = AuthorizationStatus.charge_received

    # ---- Step 1: Check 1% tolerance ----
    confirmed = Decimal(str(authorization.confirmed_amount))
    charged = Decimal(str(charge_amount))
    tolerance = confirmed * CHARGE_TOLERANCE_PCT
    difference = abs(charged - confirmed)
    difference_pct = (
        (difference / confirmed * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if confirmed > 0
        else Decimal("0")
    )

    if difference <= tolerance:
        # Amounts match within tolerance — validate and proceed
        authorization.charge_validated = True
        authorization.discrepancy_flag = False
        authorization.status = AuthorizationStatus.validated
        db.commit()
        db.refresh(authorization)

        logger.info(
            "Charge validated: auth_id=%s, confirmed=%.2f, charged=%.2f, "
            "diff=%.2f (%.2f%% — within tolerance)",
            auth_id, float(confirmed), float(charged),
            float(difference), float(difference_pct),
        )

        return {
            "auth_id": str(auth_id),
            "validation": "approved",
            "confirmed_amount": float(confirmed),
            "charge_amount": float(charged),
            "difference": float(difference),
            "difference_pct": float(difference_pct),
            "within_tolerance": True,
            "status": AuthorizationStatus.validated.value,
            "payment_proceeds": True,
            "estimated_payment_timeline": authorization.estimated_payment_timeline,
        }

    # ---- Step 2: Discrepancy detected — attempt programmatic resolution ----
    authorization.discrepancy_flag = True
    discrepancy_details = {
        "confirmed_amount": float(confirmed),
        "charge_amount": float(charged),
        "difference": float(difference),
        "difference_pct": float(difference_pct),
        "tolerance_pct": float(CHARGE_TOLERANCE_PCT * 100),
    }

    logger.info(
        "Charge discrepancy detected: auth_id=%s, confirmed=%.2f, "
        "charged=%.2f, diff=%.2f (%.2f%%)",
        auth_id, float(confirmed), float(charged),
        float(difference), float(difference_pct),
    )

    # ---- Step 2a: Compare against provider's published prices ----
    provider = db.query(Provider).filter(
        Provider.provider_id == provider_id
    ).first()
    published_prices = _get_provider_published_prices(
        db, provider, authorization.service_code
    )

    published_price_match = None
    if published_prices:
        # Find the closest published price to the charged amount
        closest = min(
            published_prices,
            key=lambda p: abs(Decimal(str(p["price"])) - charged),
        )
        closest_price = Decimal(str(closest["price"]))
        if abs(closest_price - charged) <= closest_price * CHARGE_TOLERANCE_PCT:
            published_price_match = closest

    discrepancy_details["published_prices"] = published_prices
    discrepancy_details["published_price_match"] = published_price_match

    # ---- Step 2b: Compare against pre-service confirmation ----
    # The pre-service confirmation IS the confirmed_amount already checked above

    # ---- Step 2c: Determine if programmatic resolution is possible ----
    resolution_result = _attempt_programmatic_resolution(
        confirmed=confirmed,
        charged=charged,
        published_prices=published_prices,
        published_price_match=published_price_match,
        service_code=authorization.service_code,
    )

    if resolution_result["resolved"]:
        # Programmatic resolution succeeded
        resolved_amount = resolution_result["resolved_amount"]
        authorization.charge_validated = True
        authorization.charge_amount = float(resolved_amount)
        authorization.discrepancy_details = (
            f"Discrepancy resolved programmatically: "
            f"charged={float(charged)}, "
            f"resolved_to={float(resolved_amount)}. "
            f"Reason: {resolution_result['reason']}"
        )
        authorization.status = AuthorizationStatus.validated
        db.commit()
        db.refresh(authorization)

        logger.info(
            "Discrepancy resolved programmatically: auth_id=%s, "
            "original_charge=%.2f, resolved_to=%.2f, reason=%s",
            auth_id, float(charged), float(resolved_amount),
            resolution_result["reason"],
        )

        return {
            "auth_id": str(auth_id),
            "validation": "approved_with_adjustment",
            "confirmed_amount": float(confirmed),
            "original_charge": float(charged),
            "adjusted_amount": float(resolved_amount),
            "resolution": "programmatic",
            "resolution_reason": resolution_result["reason"],
            "discrepancy": discrepancy_details,
            "status": AuthorizationStatus.validated.value,
            "payment_proceeds": True,
            "estimated_payment_timeline": authorization.estimated_payment_timeline,
        }
    else:
        # Programmatic resolution failed — escalate to human review
        authorization.charge_validated = False
        authorization.discrepancy_details = (
            f"Discrepancy requires human review: "
            f"confirmed={float(confirmed)}, "
            f"charged={float(charged)}, "
            f"diff={float(difference)} ({float(difference_pct)}%). "
            f"Reason: {resolution_result['reason']}"
        )
        authorization.status = AuthorizationStatus.charge_received
        db.commit()
        db.refresh(authorization)

        logger.info(
            "Discrepancy escalated to human review: auth_id=%s, "
            "confirmed=%.2f, charged=%.2f, reason=%s",
            auth_id, float(confirmed), float(charged),
            resolution_result["reason"],
        )

        return {
            "auth_id": str(auth_id),
            "validation": "escalated_to_human_review",
            "confirmed_amount": float(confirmed),
            "charge_amount": float(charged),
            "difference": float(difference),
            "difference_pct": float(difference_pct),
            "resolution": "pending_human_review",
            "resolution_reason": resolution_result["reason"],
            "discrepancy": discrepancy_details,
            "status": AuthorizationStatus.charge_received.value,
            "payment_proceeds": False,
            "next_steps": (
                "A human reviewer will evaluate the discrepancy. "
                "The provider may also file a formal dispute."
            ),
        }


def file_provider_dispute(
    db: Session,
    claim_id: uuid.UUID,
    provider_id: uuid.UUID,
    dispute_reason: str,
    disputed_amount: float,
) -> dict:
    """File a provider dispute with auto-populated price trail.

    Constitution: "If the provider disagrees, the documented price trail —
    the provider's own published price, the pre-service confirmation, and
    the payment — constitutes the factual record."

    Auto-populates:
    1. published_price_reference: Provider's own published prices from price_data
    2. pre_service_confirmation_reference: From ProviderAuthorization
    3. service_rendered_reference: Service details from the claim/authorization

    Attempts immediate resolution:
    a. Factual error (wrong code, wrong service) -> correct automatically
    b. Price disagreement -> present documented price trail

    Everything is recorded as structured data feeding F8.

    Args:
        db: Database session
        claim_id: Claim UUID
        provider_id: Provider UUID
        dispute_reason: Textual reason for dispute
        disputed_amount: Amount the provider believes they are owed

    Returns:
        Dispute record with resolution or pending status.
    """
    # Validate claim
    claim = db.query(Claim).filter(Claim.claim_id == claim_id).first()
    if not claim:
        return {"error": "claim_not_found", "claim_id": str(claim_id)}

    # Validate provider
    provider = db.query(Provider).filter(
        Provider.provider_id == provider_id
    ).first()
    if not provider:
        return {"error": "provider_not_found", "provider_id": str(provider_id)}

    # Find related authorization
    authorization = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.claim_id == claim_id,
        ProviderAuthorization.provider_id == provider_id,
    ).order_by(desc(ProviderAuthorization.created_at)).first()

    # ---- Auto-populate published_price_reference ----
    service_code = authorization.service_code if authorization else None
    published_prices = _get_provider_published_prices(
        db, provider, service_code
    )
    published_price_reference = {
        "provider_name": provider.name,
        "provider_npi": provider.npi,
        "service_code": service_code,
        "published_prices": published_prices,
        "source": "price_data table — provider's own published prices",
        "note": (
            "Constitution: Provider's own published price is the first "
            "reference point in any dispute."
        ),
    }

    # ---- Auto-populate pre_service_confirmation_reference ----
    pre_service_confirmation_reference = None
    if authorization:
        pre_service_confirmation_reference = {
            "auth_id": str(authorization.auth_id),
            "confirmed_amount": float(authorization.confirmed_amount),
            "payment_method": authorization.payment_method.value,
            "estimated_payment_timeline": authorization.estimated_payment_timeline,
            "notified_at": (
                authorization.pre_service_notified_at.isoformat()
                if authorization.pre_service_notified_at
                else None
            ),
            "notification_channel": authorization.notification_channel,
            "service_code": authorization.service_code,
            "service_description": authorization.service_description,
            "benefit_type": authorization.benefit_type,
            "note": (
                "Constitution: Pre-service confirmation is the second "
                "reference point in any dispute."
            ),
        }

    # ---- Auto-populate service_rendered_reference ----
    service_rendered_reference = {
        "claim_id": str(claim_id),
        "amount_billed": float(claim.amount_billed),
        "amount_paid": float(claim.amount_paid) if claim.amount_paid else None,
        "benefit_type": claim.benefit_type.value,
        "submitted_at": claim.submitted_at.isoformat(),
        "status": claim.status.value,
    }
    if authorization:
        service_rendered_reference["charge_submitted_at"] = (
            authorization.charge_submitted_at.isoformat()
            if authorization.charge_submitted_at
            else None
        )
        service_rendered_reference["charge_amount"] = (
            float(authorization.charge_amount)
            if authorization.charge_amount
            else None
        )
        service_rendered_reference["note"] = (
            "Constitution: Service actually rendered is the third "
            "reference point in any dispute."
        )

    # Create dispute record
    dispute = ProviderDispute(
        claim_id=claim_id,
        provider_id=provider_id,
        authorization_id=authorization.auth_id if authorization else None,
        dispute_reason=dispute_reason,
        disputed_amount=disputed_amount,
        published_price_reference=published_price_reference,
        pre_service_confirmation_reference=pre_service_confirmation_reference,
        service_rendered_reference=service_rendered_reference,
        status=DisputeStatus.filed,
        feeding_f8=True,
    )

    # ---- Attempt immediate resolution ----
    resolution_result = _attempt_dispute_resolution(
        dispute_reason=dispute_reason,
        disputed_amount=disputed_amount,
        published_prices=published_prices,
        authorization=authorization,
        claim=claim,
    )

    if resolution_result["resolved"]:
        dispute.resolution_method = resolution_result["method"]
        dispute.resolution = resolution_result["resolution_text"]
        dispute.resolution_amount = resolution_result["resolution_amount"]
        dispute.resolved_at = datetime.now(UTC)

        if resolution_result["method"] == DisputeResolutionMethod.programmatic:
            dispute.status = DisputeStatus.resolved_programmatic
        else:
            dispute.status = DisputeStatus.resolved_human

        # Update authorization if exists
        if authorization and resolution_result.get("update_authorization"):
            authorization.charge_amount = resolution_result["resolution_amount"]
            authorization.charge_validated = True
            authorization.discrepancy_flag = True
            authorization.discrepancy_details = (
                f"Adjusted via dispute resolution: {resolution_result['resolution_text']}"
            )
            authorization.status = AuthorizationStatus.validated
    else:
        dispute.status = DisputeStatus.under_review

    db.add(dispute)
    db.commit()
    db.refresh(dispute)

    # If authorization was updated, mark it as disputed
    if authorization and not resolution_result["resolved"]:
        authorization.status = AuthorizationStatus.disputed
        db.commit()

    logger.info(
        "Dispute filed: dispute_id=%s, provider=%s, claim=%s, "
        "amount=%.2f, resolved=%s",
        dispute.dispute_id, provider.name, claim_id,
        disputed_amount, resolution_result["resolved"],
    )

    result = {
        "dispute_id": str(dispute.dispute_id),
        "claim_id": str(claim_id),
        "provider_id": str(provider_id),
        "provider_name": provider.name,
        "dispute_reason": dispute_reason,
        "disputed_amount": disputed_amount,
        "status": dispute.status.value,
        "filed_at": dispute.filed_at.isoformat(),
        "feeding_f8": True,
        "documented_price_trail": {
            "published_price_reference": published_price_reference,
            "pre_service_confirmation_reference": pre_service_confirmation_reference,
            "service_rendered_reference": service_rendered_reference,
        },
    }

    if resolution_result["resolved"]:
        result["resolution"] = {
            "method": dispute.resolution_method.value,
            "resolution_text": dispute.resolution,
            "resolution_amount": float(dispute.resolution_amount)
            if dispute.resolution_amount
            else None,
            "resolved_at": dispute.resolved_at.isoformat()
            if dispute.resolved_at
            else None,
        }
    else:
        result["next_steps"] = resolution_result.get("next_steps", (
            "Dispute is under review. The documented price trail has been "
            "assembled. A human reviewer will evaluate the dispute."
        ))

    return result


def resolve_dispute(
    db: Session,
    dispute_id: uuid.UUID,
    resolution: str,
    resolution_amount: Optional[float],
    resolution_method: str,
) -> dict:
    """Resolve a provider dispute.

    Constitution: "If the provider cannot accept these terms, they may
    decline future patients from this plan."

    Records the resolution and updates the authorization/claim as needed.
    If the provider declines future patients, that status is recorded.

    Args:
        db: Database session
        dispute_id: Dispute UUID
        resolution: Resolution text
        resolution_amount: Adjusted payment amount (if any)
        resolution_method: programmatic or human_review

    Returns:
        Updated dispute record.
    """
    dispute = db.query(ProviderDispute).filter(
        ProviderDispute.dispute_id == dispute_id
    ).first()
    if not dispute:
        return {"error": "dispute_not_found", "dispute_id": str(dispute_id)}

    # Validate resolution method
    try:
        method = DisputeResolutionMethod(resolution_method)
    except ValueError:
        return {
            "error": "invalid_resolution_method",
            "valid_methods": [m.value for m in DisputeResolutionMethod],
        }

    # Check dispute is in a resolvable state
    resolvable_states = {DisputeStatus.filed, DisputeStatus.under_review}
    if dispute.status not in resolvable_states:
        return {
            "error": "dispute_not_resolvable",
            "current_status": dispute.status.value,
            "detail": (
                f"Dispute is in '{dispute.status.value}' state. "
                f"Only 'filed' or 'under_review' disputes can be resolved."
            ),
        }

    now = datetime.now(UTC)
    dispute.resolution_method = method
    dispute.resolution = resolution
    dispute.resolution_amount = resolution_amount
    dispute.resolved_at = now

    if method == DisputeResolutionMethod.programmatic:
        dispute.status = DisputeStatus.resolved_programmatic
    else:
        dispute.status = DisputeStatus.resolved_human

    # Update associated authorization if exists
    if dispute.authorization_id and resolution_amount is not None:
        authorization = db.query(ProviderAuthorization).filter(
            ProviderAuthorization.auth_id == dispute.authorization_id
        ).first()
        if authorization:
            authorization.charge_amount = resolution_amount
            authorization.charge_validated = True
            authorization.discrepancy_flag = True
            authorization.discrepancy_details = (
                f"Adjusted via dispute resolution ({dispute_id}): {resolution}"
            )
            authorization.status = AuthorizationStatus.validated

    # Update claim amount_paid if resolution adjusts the amount
    if resolution_amount is not None:
        claim = db.query(Claim).filter(
            Claim.claim_id == dispute.claim_id
        ).first()
        if claim:
            claim.amount_paid = resolution_amount

    db.commit()
    db.refresh(dispute)

    logger.info(
        "Dispute resolved: dispute_id=%s, method=%s, amount=%s, "
        "resolution=%s",
        dispute_id, method.value,
        resolution_amount, resolution,
    )

    return {
        "dispute_id": str(dispute.dispute_id),
        "claim_id": str(dispute.claim_id),
        "provider_id": str(dispute.provider_id),
        "dispute_reason": dispute.dispute_reason,
        "disputed_amount": float(dispute.disputed_amount),
        "resolution_method": method.value,
        "resolution": resolution,
        "resolution_amount": resolution_amount,
        "resolved_at": now.isoformat(),
        "status": dispute.status.value,
        "provider_accepted": dispute.provider_accepted,
        "feeding_f8": dispute.feeding_f8,
    }


def record_provider_decline(
    db: Session,
    dispute_id: uuid.UUID,
) -> dict:
    """Record that a provider has declined future patients from this plan.

    Constitution: "If the provider cannot accept these terms, they may
    decline future patients from this plan."

    This does not affect existing authorizations or payments — it only
    records that the provider will not accept future patients under
    these payment terms.

    Args:
        db: Database session
        dispute_id: Dispute UUID

    Returns:
        Updated dispute record with decline status.
    """
    dispute = db.query(ProviderDispute).filter(
        ProviderDispute.dispute_id == dispute_id
    ).first()
    if not dispute:
        return {"error": "dispute_not_found", "dispute_id": str(dispute_id)}

    dispute.provider_accepted = False
    dispute.status = DisputeStatus.provider_declined_future

    db.commit()
    db.refresh(dispute)

    logger.info(
        "Provider declined future patients: dispute_id=%s, provider_id=%s",
        dispute_id, dispute.provider_id,
    )

    return {
        "dispute_id": str(dispute.dispute_id),
        "provider_id": str(dispute.provider_id),
        "status": dispute.status.value,
        "provider_accepted": False,
        "detail": (
            "Provider has declined future patients from this plan. "
            "Existing authorizations and payments are not affected."
        ),
        "feeding_f8": dispute.feeding_f8,
    }


def get_provider_dashboard(
    db: Session,
    provider_id: uuid.UUID,
) -> dict:
    """Get the provider dashboard — web-based, no software installation.

    Constitution: "The provider portal is web-based, requiring no software
    installation."

    Returns all pending authorizations, submitted charges, active disputes,
    and payment history — everything the provider needs in a single API
    response that powers the web portal.

    Args:
        db: Database session
        provider_id: Provider UUID

    Returns:
        Complete provider dashboard data.
    """
    provider = db.query(Provider).filter(
        Provider.provider_id == provider_id
    ).first()
    if not provider:
        return {"error": "provider_not_found", "provider_id": str(provider_id)}

    # ---- Pre-service notifications (pending and notified) ----
    pending_auths = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.provider_id == provider_id,
        ProviderAuthorization.status.in_([
            AuthorizationStatus.pending,
            AuthorizationStatus.notified,
        ]),
    ).order_by(desc(ProviderAuthorization.created_at)).all()

    # ---- Submitted charges (awaiting validation/payment) ----
    submitted_charges = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.provider_id == provider_id,
        ProviderAuthorization.status.in_([
            AuthorizationStatus.charge_received,
            AuthorizationStatus.validated,
        ]),
    ).order_by(desc(ProviderAuthorization.charge_submitted_at)).all()

    # ---- Active disputes ----
    active_disputes = db.query(ProviderDispute).filter(
        ProviderDispute.provider_id == provider_id,
        ProviderDispute.status.in_([
            DisputeStatus.filed,
            DisputeStatus.under_review,
        ]),
    ).order_by(desc(ProviderDispute.filed_at)).all()

    # ---- Paid authorizations (recent payment history) ----
    paid_auths = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.provider_id == provider_id,
        ProviderAuthorization.status == AuthorizationStatus.paid,
    ).order_by(desc(ProviderAuthorization.created_at)).limit(50).all()

    # ---- Resolved disputes ----
    resolved_disputes = db.query(ProviderDispute).filter(
        ProviderDispute.provider_id == provider_id,
        ProviderDispute.status.in_([
            DisputeStatus.resolved_programmatic,
            DisputeStatus.resolved_human,
            DisputeStatus.provider_declined_future,
        ]),
    ).order_by(desc(ProviderDispute.resolved_at)).limit(50).all()

    # ---- Summary metrics ----
    total_authorizations = db.query(func.count(ProviderAuthorization.auth_id)).filter(
        ProviderAuthorization.provider_id == provider_id
    ).scalar() or 0

    total_paid = db.query(func.sum(ProviderAuthorization.confirmed_amount)).filter(
        ProviderAuthorization.provider_id == provider_id,
        ProviderAuthorization.status == AuthorizationStatus.paid,
    ).scalar() or 0

    total_disputes = db.query(func.count(ProviderDispute.dispute_id)).filter(
        ProviderDispute.provider_id == provider_id
    ).scalar() or 0

    return {
        "provider_id": str(provider_id),
        "provider_name": provider.name,
        "provider_npi": provider.npi,
        "portal_type": "web-based — no software installation required",
        "summary": {
            "total_authorizations": total_authorizations,
            "total_paid_amount": float(total_paid),
            "total_disputes": total_disputes,
            "pending_notifications": len(pending_auths),
            "pending_charges": len(submitted_charges),
            "active_disputes": len(active_disputes),
        },
        "pre_service_notifications": [
            _serialize_authorization(auth) for auth in pending_auths
        ],
        "submitted_charges": [
            _serialize_authorization(auth) for auth in submitted_charges
        ],
        "active_disputes": [
            _serialize_dispute(d) for d in active_disputes
        ],
        "recent_payments": [
            _serialize_authorization(auth) for auth in paid_auths
        ],
        "resolved_disputes": [
            _serialize_dispute(d) for d in resolved_disputes
        ],
    }


def get_provider_payment_history(
    db: Session,
    provider_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Get payment history for a provider.

    Returns all payments with amounts, dates, methods, and speed.

    Args:
        db: Database session
        provider_id: Provider UUID
        limit: Maximum number of records to return
        offset: Offset for pagination

    Returns:
        Payment history with totals and per-payment details.
    """
    provider = db.query(Provider).filter(
        Provider.provider_id == provider_id
    ).first()
    if not provider:
        return {"error": "provider_not_found", "provider_id": str(provider_id)}

    # All paid authorizations (our payment records)
    query = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.provider_id == provider_id,
        ProviderAuthorization.status == AuthorizationStatus.paid,
    ).order_by(desc(ProviderAuthorization.created_at))

    total_count = query.count()
    payments = query.offset(offset).limit(limit).all()

    # Aggregate stats
    total_paid = db.query(func.sum(ProviderAuthorization.confirmed_amount)).filter(
        ProviderAuthorization.provider_id == provider_id,
        ProviderAuthorization.status == AuthorizationStatus.paid,
    ).scalar() or 0

    # Payment method breakdown
    method_counts = dict(
        db.query(
            ProviderAuthorization.payment_method,
            func.count(ProviderAuthorization.auth_id),
        ).filter(
            ProviderAuthorization.provider_id == provider_id,
            ProviderAuthorization.status == AuthorizationStatus.paid,
        ).group_by(ProviderAuthorization.payment_method).all()
    )

    return {
        "provider_id": str(provider_id),
        "provider_name": provider.name,
        "total_payments": total_count,
        "total_paid_amount": float(total_paid),
        "payment_method_breakdown": {
            str(k): v for k, v in method_counts.items()
        },
        "payments": [
            {
                "auth_id": str(p.auth_id),
                "claim_id": str(p.claim_id) if p.claim_id else None,
                "service_code": p.service_code,
                "service_description": p.service_description,
                "benefit_type": p.benefit_type,
                "confirmed_amount": float(p.confirmed_amount),
                "charge_amount": float(p.charge_amount) if p.charge_amount else None,
                "payment_method": p.payment_method.value,
                "estimated_payment_timeline": p.estimated_payment_timeline,
                "notified_at": (
                    p.pre_service_notified_at.isoformat()
                    if p.pre_service_notified_at
                    else None
                ),
                "charge_submitted_at": (
                    p.charge_submitted_at.isoformat()
                    if p.charge_submitted_at
                    else None
                ),
                "created_at": p.created_at.isoformat(),
                "discrepancy_flag": p.discrepancy_flag,
            }
            for p in payments
        ],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total_count,
            "has_more": (offset + limit) < total_count,
        },
    }


def get_provider_authorizations(
    db: Session,
    provider_id: uuid.UUID,
    status_filter: Optional[str] = None,
) -> dict:
    """Get all authorizations for a provider, optionally filtered by status.

    Args:
        db: Database session
        provider_id: Provider UUID
        status_filter: Optional status to filter by

    Returns:
        List of authorizations.
    """
    provider = db.query(Provider).filter(
        Provider.provider_id == provider_id
    ).first()
    if not provider:
        return {"error": "provider_not_found", "provider_id": str(provider_id)}

    query = db.query(ProviderAuthorization).filter(
        ProviderAuthorization.provider_id == provider_id,
    )

    if status_filter:
        try:
            status_enum = AuthorizationStatus(status_filter)
            query = query.filter(
                ProviderAuthorization.status == status_enum
            )
        except ValueError:
            return {
                "error": "invalid_status_filter",
                "valid_statuses": [s.value for s in AuthorizationStatus],
            }

    authorizations = query.order_by(
        desc(ProviderAuthorization.created_at)
    ).all()

    return {
        "provider_id": str(provider_id),
        "provider_name": provider.name,
        "status_filter": status_filter,
        "count": len(authorizations),
        "authorizations": [
            _serialize_authorization(auth) for auth in authorizations
        ],
    }


# ===========================================================================
# Internal helpers
# ===========================================================================


def _get_provider_published_prices(
    db: Session,
    provider: Optional[Provider],
    service_code: Optional[str],
) -> list[dict]:
    """Query price_data for a provider's published prices.

    Returns all matching price records for the provider + service code
    combination, sorted by most recent first.
    """
    if not provider or not service_code:
        return []

    # Query by NPI first (most specific), then by name
    filters = [PriceData.service_code == service_code]

    if provider.npi:
        npi_prices = (
            db.query(PriceData)
            .filter(
                PriceData.provider_npi == provider.npi,
                PriceData.service_code == service_code,
            )
            .order_by(desc(PriceData.ingested_at))
            .limit(10)
            .all()
        )
        if npi_prices:
            return [
                {
                    "price_id": str(p.price_id),
                    "price": float(p.price),
                    "channel": p.channel,
                    "source": p.source.value,
                    "source_url": p.source_url,
                    "state": p.state,
                    "ingested_at": p.ingested_at.isoformat(),
                }
                for p in npi_prices
            ]

    # Fallback: query by provider name
    name_prices = (
        db.query(PriceData)
        .filter(
            PriceData.provider_name == provider.name,
            PriceData.service_code == service_code,
        )
        .order_by(desc(PriceData.ingested_at))
        .limit(10)
        .all()
    )
    return [
        {
            "price_id": str(p.price_id),
            "price": float(p.price),
            "channel": p.channel,
            "source": p.source.value,
            "source_url": p.source_url,
            "state": p.state,
            "ingested_at": p.ingested_at.isoformat(),
        }
        for p in name_prices
    ]


def _attempt_programmatic_resolution(
    confirmed: Decimal,
    charged: Decimal,
    published_prices: list[dict],
    published_price_match: Optional[dict],
    service_code: str,
) -> dict:
    """Attempt to resolve a charge discrepancy programmatically.

    Resolution logic:
    1. If the charged amount matches a published price: use confirmed amount
       (provider published one price but was pre-authorized at our rate).
    2. If charge is lower than confirmed: accept the lower charge (provider
       benefit).
    3. If charge is higher but published prices support the confirmed amount:
       use confirmed amount with documentation.
    4. If no published prices exist and charge is modestly higher (<5%):
       accept with flag.
    5. Otherwise: cannot resolve programmatically.

    Returns:
        dict with 'resolved' bool, 'resolved_amount', 'reason'
    """
    # Case 1: Charged amount matches a published price but differs from confirmed
    if published_price_match:
        published = Decimal(str(published_price_match["price"]))
        if abs(published - charged) <= published * CHARGE_TOLERANCE_PCT:
            # Provider charged their published price. Our confirmed amount
            # was based on price comparison. Use the confirmed amount.
            return {
                "resolved": True,
                "resolved_amount": float(confirmed),
                "reason": (
                    f"Provider charged their published price "
                    f"(${float(charged):.2f}), which matches price_data record. "
                    f"Pre-service confirmation was ${float(confirmed):.2f}. "
                    f"Resolving to confirmed amount per Constitution: "
                    f"pre-service confirmation is binding."
                ),
            }

    # Case 2: Provider charged less than confirmed — accept the lower amount
    if charged < confirmed:
        return {
            "resolved": True,
            "resolved_amount": float(charged),
            "reason": (
                f"Provider charged ${float(charged):.2f}, which is less "
                f"than the confirmed amount of ${float(confirmed):.2f}. "
                f"Accepting the lower charge."
            ),
        }

    # Case 3: Charge is higher, but published prices support our confirmed amount
    if published_prices:
        avg_published = Decimal(str(
            sum(p["price"] for p in published_prices) / len(published_prices)
        ))
        if abs(avg_published - confirmed) <= confirmed * Decimal("0.10"):
            return {
                "resolved": True,
                "resolved_amount": float(confirmed),
                "reason": (
                    f"Provider charged ${float(charged):.2f}, above confirmed "
                    f"${float(confirmed):.2f}. Average published price is "
                    f"${float(avg_published):.2f}, which supports the confirmed "
                    f"amount. Resolving to confirmed amount."
                ),
            }

    # Case 4: Modest overage (<5%) with no published prices — accept with flag
    overage_pct = (
        ((charged - confirmed) / confirmed * Decimal("100"))
        if confirmed > 0
        else Decimal("0")
    )
    if overage_pct <= Decimal("5") and not published_prices:
        return {
            "resolved": True,
            "resolved_amount": float(charged),
            "reason": (
                f"Provider charged ${float(charged):.2f}, "
                f"{float(overage_pct):.1f}% above confirmed "
                f"${float(confirmed):.2f}. No published prices available. "
                f"Accepting modest overage with discrepancy flag."
            ),
        }

    # Case 5: Cannot resolve programmatically
    return {
        "resolved": False,
        "resolved_amount": None,
        "reason": (
            f"Cannot resolve programmatically. Charge ${float(charged):.2f} "
            f"exceeds confirmed ${float(confirmed):.2f} by "
            f"{float(overage_pct):.1f}%. "
            f"{'Published prices available but do not support the charge.' if published_prices else 'No published prices available for comparison.'} "
            f"Escalating to human review."
        ),
    }


def _attempt_dispute_resolution(
    dispute_reason: str,
    disputed_amount: float,
    published_prices: list[dict],
    authorization: Optional[ProviderAuthorization],
    claim: Optional[Claim],
) -> dict:
    """Attempt immediate resolution of a dispute.

    Resolution hierarchy:
    a. Factual error (wrong code, wrong service) -> correct automatically
    b. Price disagreement -> present documented price trail

    Returns:
        dict with 'resolved' bool, 'method', 'resolution_text',
        'resolution_amount', 'update_authorization', 'next_steps'
    """
    reason_lower = dispute_reason.lower()

    # ---- Case A: Factual error ----
    is_factual_error = any(
        keyword in reason_lower for keyword in FACTUAL_ERROR_KEYWORDS
    )
    if is_factual_error:
        # Factual errors are correctable: adjust to the disputed amount
        # if the provider identifies a coding or service error
        return {
            "resolved": True,
            "method": DisputeResolutionMethod.programmatic,
            "resolution_text": (
                f"Factual error identified in dispute reason. "
                f"Adjusting payment to disputed amount of "
                f"${disputed_amount:.2f} pending verification of the "
                f"corrected service code/description."
            ),
            "resolution_amount": disputed_amount,
            "update_authorization": True,
        }

    # ---- Case B: Price disagreement ----
    is_price_disagreement = any(
        keyword in reason_lower for keyword in PRICE_DISAGREEMENT_KEYWORDS
    )
    if is_price_disagreement and authorization:
        confirmed = float(authorization.confirmed_amount)

        # Build price trail summary
        price_trail_parts = [
            f"Pre-service confirmation: ${confirmed:.2f}",
        ]
        if published_prices:
            avg_pub = sum(p["price"] for p in published_prices) / len(published_prices)
            price_trail_parts.append(
                f"Average published price ({len(published_prices)} records): "
                f"${avg_pub:.2f}"
            )
        if claim and claim.amount_paid is not None:
            price_trail_parts.append(
                f"Amount paid on claim: ${float(claim.amount_paid):.2f}"
            )

        price_trail_summary = "; ".join(price_trail_parts)

        # If disputed amount is close to confirmed, resolve at confirmed
        if abs(disputed_amount - confirmed) <= confirmed * 0.05:
            return {
                "resolved": True,
                "method": DisputeResolutionMethod.programmatic,
                "resolution_text": (
                    f"Price disagreement resolved via documented price trail. "
                    f"{price_trail_summary}. "
                    f"Disputed amount ${disputed_amount:.2f} is within 5% of "
                    f"confirmed amount. Resolving at confirmed amount "
                    f"${confirmed:.2f}."
                ),
                "resolution_amount": confirmed,
                "update_authorization": False,
            }

        # If disputed amount is significantly different, present trail
        # but cannot auto-resolve
        return {
            "resolved": False,
            "method": None,
            "resolution_text": None,
            "resolution_amount": None,
            "update_authorization": False,
            "next_steps": (
                f"Price disagreement. Documented price trail: "
                f"{price_trail_summary}. "
                f"Disputed amount ${disputed_amount:.2f} differs significantly "
                f"from confirmed ${confirmed:.2f}. Requires human review. "
                f"Constitution: 'If the provider cannot accept these terms, "
                f"they may decline future patients from this plan.'"
            ),
        }

    # ---- Default: Cannot auto-resolve ----
    return {
        "resolved": False,
        "method": None,
        "resolution_text": None,
        "resolution_amount": None,
        "update_authorization": False,
        "next_steps": (
            "Dispute requires human review. The documented price trail "
            "has been assembled and recorded for the reviewer."
        ),
    }


def _serialize_authorization(auth: ProviderAuthorization) -> dict:
    """Serialize a ProviderAuthorization to a dict for API responses."""
    return {
        "auth_id": str(auth.auth_id),
        "provider_id": str(auth.provider_id),
        "claim_id": str(auth.claim_id) if auth.claim_id else None,
        "service_code": auth.service_code,
        "service_description": auth.service_description,
        "benefit_type": auth.benefit_type,
        "confirmed_amount": float(auth.confirmed_amount),
        "payment_method": auth.payment_method.value,
        "estimated_payment_timeline": auth.estimated_payment_timeline,
        "notified_at": (
            auth.pre_service_notified_at.isoformat()
            if auth.pre_service_notified_at
            else None
        ),
        "notification_channel": auth.notification_channel,
        "charge_submitted_at": (
            auth.charge_submitted_at.isoformat()
            if auth.charge_submitted_at
            else None
        ),
        "charge_amount": (
            float(auth.charge_amount) if auth.charge_amount else None
        ),
        "charge_validated": auth.charge_validated,
        "discrepancy_flag": auth.discrepancy_flag,
        "discrepancy_details": auth.discrepancy_details,
        "status": auth.status.value,
        "created_at": auth.created_at.isoformat(),
    }


def _serialize_dispute(dispute: ProviderDispute) -> dict:
    """Serialize a ProviderDispute to a dict for API responses."""
    return {
        "dispute_id": str(dispute.dispute_id),
        "claim_id": str(dispute.claim_id),
        "provider_id": str(dispute.provider_id),
        "authorization_id": (
            str(dispute.authorization_id)
            if dispute.authorization_id
            else None
        ),
        "dispute_reason": dispute.dispute_reason,
        "disputed_amount": float(dispute.disputed_amount),
        "resolution_method": (
            dispute.resolution_method.value
            if dispute.resolution_method
            else None
        ),
        "resolution": dispute.resolution,
        "resolution_amount": (
            float(dispute.resolution_amount)
            if dispute.resolution_amount
            else None
        ),
        "provider_accepted": dispute.provider_accepted,
        "filed_at": dispute.filed_at.isoformat(),
        "resolved_at": (
            dispute.resolved_at.isoformat()
            if dispute.resolved_at
            else None
        ),
        "status": dispute.status.value,
        "feeding_f8": dispute.feeding_f8,
    }
