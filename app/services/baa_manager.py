"""Business Associate Agreement lifecycle management (F13).

Constitution: "Every vendor touching PHI has a BAA."

Tracks BAA execution, expiration, renewal, and coverage gaps for all entities
that access, store, or transmit protected health information.
"""

import logging
import uuid
from datetime import datetime, timedelta, UTC

from sqlalchemy.orm import Session

from app.models.security import BAARecord, RenewalStatus, VendorType

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Known BAA entities — pre-populated for core infrastructure                   #
# --------------------------------------------------------------------------- #
KNOWN_ENTITIES = [
    {
        "vendor_name": "AWS",
        "vendor_type": VendorType.cloud_infrastructure,
        "phi_categories": ["all_ephi", "claims_data", "demographics", "clinical_data"],
        "signed_at": datetime(2024, 1, 15, tzinfo=UTC),
        "expires_at": datetime(2027, 1, 15, tzinfo=UTC),
        "renewal_status": RenewalStatus.active,
        "baa_document_ref": "s3://beneflex-legal/baa/aws-baa-2024.pdf",
    },
    {
        "vendor_name": "Auth0",
        "vendor_type": VendorType.identity_provider,
        "phi_categories": ["user_identifiers", "email_addresses", "authentication_logs"],
        "signed_at": datetime(2024, 2, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 2, 1, tzinfo=UTC),
        "renewal_status": RenewalStatus.active,
        "baa_document_ref": "s3://beneflex-legal/baa/auth0-baa-2024.pdf",
    },
    {
        "vendor_name": "Stripe",
        "vendor_type": VendorType.payment_processor,
        "phi_categories": ["payment_data", "employer_identifiers"],
        "signed_at": datetime(2024, 3, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 3, 1, tzinfo=UTC),
        "renewal_status": RenewalStatus.active,
        "baa_document_ref": "s3://beneflex-legal/baa/stripe-baa-2024.pdf",
    },
    {
        "vendor_name": "SendGrid",
        "vendor_type": VendorType.email_service,
        "phi_categories": ["email_addresses", "notification_content"],
        "signed_at": None,
        "expires_at": None,
        "renewal_status": RenewalStatus.pending,
        "baa_document_ref": None,
    },
    {
        "vendor_name": "Datadog",
        "vendor_type": VendorType.monitoring,
        "phi_categories": ["system_logs", "performance_metrics"],
        "signed_at": datetime(2024, 4, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 4, 1, tzinfo=UTC),
        "renewal_status": RenewalStatus.active,
        "baa_document_ref": "s3://beneflex-legal/baa/datadog-baa-2024.pdf",
    },
]


def _ensure_known_entities(db: Session) -> None:
    """Seed known BAA entities if they don't exist yet."""
    for entity in KNOWN_ENTITIES:
        exists = (
            db.query(BAARecord)
            .filter(BAARecord.vendor_name == entity["vendor_name"])
            .first()
        )
        if not exists:
            record = BAARecord(
                vendor_name=entity["vendor_name"],
                vendor_type=entity["vendor_type"],
                phi_categories_accessed=entity["phi_categories"],
                signed_at=entity["signed_at"] or datetime.now(UTC),
                expires_at=entity["expires_at"] or datetime.now(UTC),
                renewal_status=entity["renewal_status"],
                baa_document_ref=entity["baa_document_ref"],
            )
            db.add(record)
    db.commit()


# --------------------------------------------------------------------------- #
# BAA registry operations                                                      #
# --------------------------------------------------------------------------- #
def get_baa_registry(db: Session) -> dict:
    """Return all BAA records with current status.

    Ensures known entities are seeded, then refreshes renewal_status based
    on current dates before returning the registry.
    """
    _ensure_known_entities(db)
    _refresh_renewal_statuses(db)

    records = db.query(BAARecord).order_by(BAARecord.vendor_name).all()

    return {
        "total_baas": len(records),
        "status_summary": {
            "active": sum(1 for r in records if r.renewal_status == RenewalStatus.active),
            "expiring_soon": sum(1 for r in records if r.renewal_status == RenewalStatus.expiring_soon),
            "expired": sum(1 for r in records if r.renewal_status == RenewalStatus.expired),
            "pending": sum(1 for r in records if r.renewal_status == RenewalStatus.pending),
        },
        "records": [
            {
                "baa_id": str(r.baa_id),
                "vendor_name": r.vendor_name,
                "vendor_type": r.vendor_type,
                "phi_categories_accessed": r.phi_categories_accessed,
                "signed_at": r.signed_at.isoformat() if r.signed_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "renewal_status": r.renewal_status,
                "baa_document_ref": r.baa_document_ref,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    }


def check_baa_coverage(db: Session) -> dict:
    """Verify every PHI-touching entity has a current, active BAA.

    Returns coverage report with gaps identified.
    """
    _ensure_known_entities(db)
    _refresh_renewal_statuses(db)

    records = db.query(BAARecord).all()
    gaps = []

    for record in records:
        if record.renewal_status == RenewalStatus.pending:
            gaps.append({
                "vendor_name": record.vendor_name,
                "vendor_type": record.vendor_type,
                "issue": "BAA not yet signed",
                "phi_at_risk": record.phi_categories_accessed,
                "severity": "critical",
                "remediation": f"Execute BAA with {record.vendor_name} before transmitting PHI",
            })
        elif record.renewal_status == RenewalStatus.expired:
            gaps.append({
                "vendor_name": record.vendor_name,
                "vendor_type": record.vendor_type,
                "issue": "BAA expired",
                "expired_at": record.expires_at.isoformat(),
                "phi_at_risk": record.phi_categories_accessed,
                "severity": "critical",
                "remediation": f"Immediately renew BAA with {record.vendor_name} or cease PHI transmission",
            })
        elif record.renewal_status == RenewalStatus.expiring_soon:
            gaps.append({
                "vendor_name": record.vendor_name,
                "vendor_type": record.vendor_type,
                "issue": "BAA expiring soon",
                "expires_at": record.expires_at.isoformat(),
                "phi_at_risk": record.phi_categories_accessed,
                "severity": "warning",
                "remediation": f"Initiate BAA renewal with {record.vendor_name}",
            })

    fully_covered = len(gaps) == 0
    critical_gaps = [g for g in gaps if g["severity"] == "critical"]

    return {
        "fully_covered": fully_covered,
        "total_entities": len(records),
        "covered_entities": len(records) - len(critical_gaps),
        "gaps": gaps,
        "critical_gap_count": len(critical_gaps),
        "recommendation": (
            "All PHI-touching entities have active BAAs"
            if fully_covered
            else f"ACTION REQUIRED: {len(critical_gaps)} critical BAA gaps. "
                 f"PHI transmission to uncovered entities must cease immediately per 45 CFR 164.502(e)."
        ),
    }


def check_expiring_baas(db: Session, days_ahead: int = 90) -> dict:
    """Alert on BAAs expiring within the specified window.

    Returns list of BAAs needing renewal attention.
    """
    _ensure_known_entities(db)
    now = datetime.now(UTC)
    threshold = now + timedelta(days=days_ahead)

    expiring = (
        db.query(BAARecord)
        .filter(
            BAARecord.expires_at != None,  # noqa: E711
            BAARecord.expires_at <= threshold,
            BAARecord.renewal_status != RenewalStatus.expired,
        )
        .order_by(BAARecord.expires_at)
        .all()
    )

    return {
        "window_days": days_ahead,
        "checked_at": now.isoformat(),
        "expiring_count": len(expiring),
        "expiring_baas": [
            {
                "baa_id": str(r.baa_id),
                "vendor_name": r.vendor_name,
                "vendor_type": r.vendor_type,
                "expires_at": r.expires_at.isoformat(),
                "days_until_expiry": (r.expires_at - now).days,
                "phi_categories_accessed": r.phi_categories_accessed,
                "action_required": (
                    "URGENT: Expired" if r.expires_at <= now
                    else f"Renew within {(r.expires_at - now).days} days"
                ),
            }
            for r in expiring
        ],
    }


def create_baa_record(
    db: Session,
    vendor_name: str,
    vendor_type: str,
    signed_at: datetime,
    expires_at: datetime,
    phi_categories: list[str],
    baa_document_ref: str | None = None,
    contacts: str | None = None,
) -> dict:
    """Register a new Business Associate Agreement.

    Returns the created BAA record.
    """
    # Check for existing BAA with same vendor
    existing = (
        db.query(BAARecord)
        .filter(BAARecord.vendor_name == vendor_name)
        .first()
    )
    if existing:
        raise ValueError(
            f"BAA already exists for {vendor_name} (ID: {existing.baa_id}). "
            f"Use renew_baa() to update an existing BAA."
        )

    now = datetime.now(UTC)
    renewal_status = RenewalStatus.active
    if expires_at <= now:
        renewal_status = RenewalStatus.expired
    elif expires_at <= now + timedelta(days=90):
        renewal_status = RenewalStatus.expiring_soon

    record = BAARecord(
        vendor_name=vendor_name,
        vendor_type=vendor_type,
        phi_categories_accessed=phi_categories,
        signed_at=signed_at,
        expires_at=expires_at,
        renewal_status=renewal_status,
        baa_document_ref=baa_document_ref,
        contacts=contacts,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    logger.info("Created BAA record for %s (ID: %s)", vendor_name, record.baa_id)

    return {
        "baa_id": str(record.baa_id),
        "vendor_name": record.vendor_name,
        "vendor_type": record.vendor_type,
        "signed_at": record.signed_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "renewal_status": record.renewal_status,
        "phi_categories_accessed": record.phi_categories_accessed,
    }


def renew_baa(db: Session, baa_id: str, new_expires_at: datetime) -> dict:
    """Renew an existing BAA by extending its expiration date.

    Resets renewal_status to active and logs the renewal.
    """
    record = (
        db.query(BAARecord)
        .filter(BAARecord.baa_id == baa_id)
        .first()
    )
    if not record:
        raise ValueError(f"BAA {baa_id} not found")

    old_expires = record.expires_at
    record.expires_at = new_expires_at
    record.renewal_status = RenewalStatus.active
    db.commit()

    logger.info(
        "Renewed BAA for %s: %s -> %s",
        record.vendor_name,
        old_expires.isoformat(),
        new_expires_at.isoformat(),
    )

    return {
        "baa_id": str(record.baa_id),
        "vendor_name": record.vendor_name,
        "previous_expiry": old_expires.isoformat(),
        "new_expiry": new_expires_at.isoformat(),
        "renewal_status": record.renewal_status,
    }


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #
def _refresh_renewal_statuses(db: Session) -> None:
    """Update renewal_status for all BAAs based on current date."""
    now = datetime.now(UTC)
    threshold_90 = now + timedelta(days=90)

    records = db.query(BAARecord).filter(BAARecord.renewal_status != RenewalStatus.pending).all()
    for record in records:
        if record.expires_at <= now:
            record.renewal_status = RenewalStatus.expired
        elif record.expires_at <= threshold_90:
            record.renewal_status = RenewalStatus.expiring_soon
        else:
            record.renewal_status = RenewalStatus.active
    db.commit()
