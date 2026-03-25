"""Security, Privacy & Compliance models (Function 13).

Constitution: "HIPAA compliance is non-negotiable. Every vendor touching PHI has a BAA.
Every breach is detected, reported, and remediated per federal and state law."

Models:
- BAARecord: Business Associate Agreement lifecycle tracking
- BreachIncident: Breach detection, investigation, and notification tracking
- SecurityAssessment: Vendor and infrastructure security assessment results
"""

import uuid
from datetime import datetime, UTC
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    Enum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class VendorType(str, PyEnum):
    cloud_infrastructure = "cloud_infrastructure"
    identity_provider = "identity_provider"
    payment_processor = "payment_processor"
    email_service = "email_service"
    monitoring = "monitoring"
    analytics = "analytics"
    other = "other"


class RenewalStatus(str, PyEnum):
    active = "active"
    expiring_soon = "expiring_soon"
    expired = "expired"
    pending = "pending"


class IncidentType(str, PyEnum):
    unauthorized_access = "unauthorized_access"
    data_exfiltration = "data_exfiltration"
    brute_force = "brute_force"
    anomalous_access = "anomalous_access"
    system_compromise = "system_compromise"
    vendor_breach = "vendor_breach"


class Severity(str, PyEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IncidentStatus(str, PyEnum):
    detected = "detected"
    investigating = "investigating"
    contained = "contained"
    remediated = "remediated"
    reported = "reported"
    closed = "closed"


class EntityType(str, PyEnum):
    vendor = "vendor"
    integration = "integration"
    infrastructure = "infrastructure"
    application = "application"


class AssessmentType(str, PyEnum):
    initial = "initial"
    periodic = "periodic"
    incident_triggered = "incident_triggered"


class BAARecord(Base):
    __tablename__ = "baa_records"

    baa_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    vendor_name: Mapped[str] = mapped_column(String(255), index=True)
    vendor_type: Mapped[str] = mapped_column(
        Enum(VendorType, native_enum=False, length=50)
    )
    phi_categories_accessed: Mapped[list] = mapped_column(JSON, default=list)
    signed_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    renewal_status: Mapped[str] = mapped_column(
        Enum(RenewalStatus, native_enum=False, length=30), default=RenewalStatus.active
    )
    contacts: Mapped[str | None] = mapped_column(Text, nullable=True)
    baa_document_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class BreachIncident(Base):
    __tablename__ = "breach_incidents"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    incident_type: Mapped[str] = mapped_column(
        Enum(IncidentType, native_enum=False, length=50)
    )
    severity: Mapped[str] = mapped_column(
        Enum(Severity, native_enum=False, length=20)
    )
    affected_records_count: Mapped[int] = mapped_column(Integer, default=0)
    affected_employers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str] = mapped_column(Text)
    detection_method: Mapped[str] = mapped_column(String(100))
    hhs_notification_required: Mapped[bool] = mapped_column(Boolean, default=False)
    hhs_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_ag_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    individuals_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notification_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remediation_steps: Mapped[list] = mapped_column(JSON, default=list)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(IncidentStatus, native_enum=False, length=30), default=IncidentStatus.detected
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class SecurityAssessment(Base):
    __tablename__ = "security_assessments"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    entity_name: Mapped[str] = mapped_column(String(255), index=True)
    entity_type: Mapped[str] = mapped_column(
        Enum(EntityType, native_enum=False, length=30)
    )
    assessment_type: Mapped[str] = mapped_column(
        Enum(AssessmentType, native_enum=False, length=30)
    )
    overall_score: Mapped[float] = mapped_column(Numeric(5, 2))
    findings: Mapped[list] = mapped_column(JSON, default=list)
    recommendations: Mapped[list] = mapped_column(JSON, default=list)
    assessed_at: Mapped[datetime] = mapped_column(DateTime)
    next_assessment_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assessor: Mapped[str] = mapped_column(String(255))
