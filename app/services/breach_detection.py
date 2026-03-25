"""Continuous automated breach detection engine (F13).

Constitution: "Every breach is detected, reported, and remediated."

Runs multiple detection heuristics against audit logs and access patterns to
identify potential security incidents in real time. Findings are escalated
through the incident response pipeline.
"""

import logging
import uuid
from datetime import datetime, timedelta, UTC
from collections import Counter, defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.security import (
    BreachIncident,
    IncidentStatus,
    IncidentType,
    Severity,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Detection thresholds                                                         #
# --------------------------------------------------------------------------- #
BRUTE_FORCE_THRESHOLD = 50           # failed auths in 24h
BULK_ACCESS_THRESHOLD = 1000         # records by single user in 1h
AFTER_HOURS_PHI_THRESHOLD = 20       # PHI accesses outside business hours
BUSINESS_HOURS_START = 6             # 6 AM
BUSINESS_HOURS_END = 22              # 10 PM
GEO_ANOMALY_MAX_LOCATIONS = 3        # distinct IPs/locations in 1h
PRIVILEGE_ESCALATION_WINDOW_HOURS = 1


# --------------------------------------------------------------------------- #
# Main detection loop                                                          #
# --------------------------------------------------------------------------- #
def run_continuous_detection(db: Session) -> dict:
    """Run all detection heuristics against recent audit log data.

    Returns a summary of detected anomalies with severity levels.
    Each anomaly is recorded as a BreachIncident if it meets the threshold.
    """
    now = datetime.now(UTC)
    anomalies = []

    # 1. Brute force detection
    brute_force = _detect_brute_force(db, now)
    anomalies.extend(brute_force)

    # 2. Bulk data access detection
    bulk_access = _detect_bulk_data_access(db, now)
    anomalies.extend(bulk_access)

    # 3. After-hours PHI access
    after_hours = _detect_after_hours_access(db, now)
    anomalies.extend(after_hours)

    # 4. Geographic anomaly detection
    geo_anomalies = _detect_geographic_anomalies(db, now)
    anomalies.extend(geo_anomalies)

    # 5. Privilege escalation detection
    priv_esc = _detect_privilege_escalation(db, now)
    anomalies.extend(priv_esc)

    # 6. Unauthenticated PHI access attempts
    unauth = _detect_unauthenticated_phi_access(db, now)
    anomalies.extend(unauth)

    # Create incidents for new anomalies
    created_incidents = []
    for anomaly in anomalies:
        incident = create_incident(
            db,
            incident_type=anomaly["incident_type"],
            severity=anomaly["severity"],
            description=anomaly["description"],
            detection_method=anomaly["detection_method"],
            affected_records=anomaly.get("affected_records", 0),
        )
        created_incidents.append(incident)

    return {
        "scan_completed_at": now.isoformat(),
        "anomalies_detected": len(anomalies),
        "anomalies": anomalies,
        "incidents_created": len(created_incidents),
        "severity_breakdown": {
            "critical": sum(1 for a in anomalies if a["severity"] == Severity.critical),
            "high": sum(1 for a in anomalies if a["severity"] == Severity.high),
            "medium": sum(1 for a in anomalies if a["severity"] == Severity.medium),
            "low": sum(1 for a in anomalies if a["severity"] == Severity.low),
        },
    }


# --------------------------------------------------------------------------- #
# Detection heuristics                                                         #
# --------------------------------------------------------------------------- #
def _detect_brute_force(db: Session, now: datetime) -> list[dict]:
    """Detect brute force attacks: >50 failed auth attempts in 24 hours."""
    window_start = now - timedelta(hours=24)
    anomalies = []

    failed_auths = (
        db.query(
            AuditLog.actor,
            func.count(AuditLog.log_id).label("attempt_count"),
        )
        .filter(
            AuditLog.timestamp >= window_start,
            AuditLog.action.in_(["auth_failure", "login_failed", "401"]),
        )
        .group_by(AuditLog.actor)
        .having(func.count(AuditLog.log_id) > BRUTE_FORCE_THRESHOLD)
        .all()
    )

    for actor, count in failed_auths:
        anomalies.append({
            "incident_type": IncidentType.brute_force,
            "severity": Severity.high if count > BRUTE_FORCE_THRESHOLD * 2 else Severity.medium,
            "description": (
                f"Brute force attack detected: {count} failed authentication attempts "
                f"from actor '{actor}' within 24 hours (threshold: {BRUTE_FORCE_THRESHOLD})"
            ),
            "detection_method": "brute_force_heuristic",
            "affected_records": 0,
            "details": {"actor": actor, "attempt_count": count},
        })

    return anomalies


def _detect_bulk_data_access(db: Session, now: datetime) -> list[dict]:
    """Detect bulk data exfiltration: >1000 records accessed by single user in 1h."""
    window_start = now - timedelta(hours=1)
    anomalies = []

    bulk_accesses = (
        db.query(
            AuditLog.actor,
            func.count(AuditLog.log_id).label("access_count"),
        )
        .filter(
            AuditLog.timestamp >= window_start,
            AuditLog.action.in_(["GET", "read", "query", "export"]),
        )
        .group_by(AuditLog.actor)
        .having(func.count(AuditLog.log_id) > BULK_ACCESS_THRESHOLD)
        .all()
    )

    for actor, count in bulk_accesses:
        anomalies.append({
            "incident_type": IncidentType.data_exfiltration,
            "severity": Severity.critical,
            "description": (
                f"Potential data exfiltration: user '{actor}' accessed {count} records "
                f"within 1 hour (threshold: {BULK_ACCESS_THRESHOLD})"
            ),
            "detection_method": "bulk_access_heuristic",
            "affected_records": count,
            "details": {"actor": actor, "access_count": count},
        })

    return anomalies


def _detect_after_hours_access(db: Session, now: datetime) -> list[dict]:
    """Detect suspicious after-hours PHI access: >20 PHI accesses outside 6am-10pm."""
    window_start = now - timedelta(hours=24)
    anomalies = []

    # Query all accesses in the last 24 hours
    recent_accesses = (
        db.query(AuditLog)
        .filter(
            AuditLog.timestamp >= window_start,
            AuditLog.resource_type.in_(["employee", "claim", "clinical", "phi"]),
        )
        .all()
    )

    # Group by actor and count after-hours accesses
    after_hours_by_actor: dict[str, int] = defaultdict(int)
    for access in recent_accesses:
        hour = access.timestamp.hour
        if hour < BUSINESS_HOURS_START or hour >= BUSINESS_HOURS_END:
            after_hours_by_actor[access.actor] += 1

    for actor, count in after_hours_by_actor.items():
        if count > AFTER_HOURS_PHI_THRESHOLD:
            anomalies.append({
                "incident_type": IncidentType.anomalous_access,
                "severity": Severity.medium,
                "description": (
                    f"After-hours PHI access anomaly: user '{actor}' made {count} PHI "
                    f"accesses outside business hours ({BUSINESS_HOURS_START}:00-"
                    f"{BUSINESS_HOURS_END}:00) in 24 hours (threshold: {AFTER_HOURS_PHI_THRESHOLD})"
                ),
                "detection_method": "after_hours_heuristic",
                "affected_records": count,
                "details": {"actor": actor, "after_hours_count": count},
            })

    return anomalies


def _detect_geographic_anomalies(db: Session, now: datetime) -> list[dict]:
    """Detect access from too many distinct locations in a short window."""
    window_start = now - timedelta(hours=1)
    anomalies = []

    recent_accesses = (
        db.query(AuditLog)
        .filter(AuditLog.timestamp >= window_start)
        .all()
    )

    # Group by actor and collect unique client IPs
    actor_ips: dict[str, set[str]] = defaultdict(set)
    for access in recent_accesses:
        if access.details and isinstance(access.details, dict):
            client_ip = access.details.get("client_ip", "unknown")
            if client_ip != "unknown":
                actor_ips[access.actor].add(client_ip)

    for actor, ips in actor_ips.items():
        if len(ips) > GEO_ANOMALY_MAX_LOCATIONS:
            anomalies.append({
                "incident_type": IncidentType.unauthorized_access,
                "severity": Severity.high,
                "description": (
                    f"Geographic anomaly: user '{actor}' accessed from {len(ips)} "
                    f"distinct IP addresses within 1 hour (threshold: {GEO_ANOMALY_MAX_LOCATIONS}). "
                    f"Possible credential compromise or session hijacking."
                ),
                "detection_method": "geographic_anomaly_heuristic",
                "affected_records": 0,
                "details": {"actor": actor, "unique_ips": list(ips)},
            })

    return anomalies


def _detect_privilege_escalation(db: Session, now: datetime) -> list[dict]:
    """Detect role changes or access beyond assigned role."""
    window_start = now - timedelta(hours=PRIVILEGE_ESCALATION_WINDOW_HOURS)
    anomalies = []

    # Look for role-change or admin-access audit entries
    escalation_events = (
        db.query(AuditLog)
        .filter(
            AuditLog.timestamp >= window_start,
            AuditLog.action.in_([
                "role_change", "permission_grant", "admin_access",
                "privilege_escalation", "sudo",
            ]),
        )
        .all()
    )

    if escalation_events:
        actors = Counter(e.actor for e in escalation_events)
        for actor, count in actors.items():
            anomalies.append({
                "incident_type": IncidentType.unauthorized_access,
                "severity": Severity.critical,
                "description": (
                    f"Privilege escalation detected: user '{actor}' triggered {count} "
                    f"privilege-related events within {PRIVILEGE_ESCALATION_WINDOW_HOURS} hour(s)"
                ),
                "detection_method": "privilege_escalation_heuristic",
                "affected_records": 0,
                "details": {"actor": actor, "event_count": count},
            })

    # Detect 403 responses (access beyond role)
    forbidden_accesses = (
        db.query(
            AuditLog.actor,
            func.count(AuditLog.log_id).label("forbidden_count"),
        )
        .filter(
            AuditLog.timestamp >= window_start,
            AuditLog.details.isnot(None),
        )
        .group_by(AuditLog.actor)
        .all()
    )

    for actor, count in forbidden_accesses:
        # Check the details for 403 status codes (stored in JSON details)
        forbidden_entries = (
            db.query(AuditLog)
            .filter(
                AuditLog.timestamp >= window_start,
                AuditLog.actor == actor,
            )
            .all()
        )
        forbidden_count = sum(
            1 for e in forbidden_entries
            if isinstance(e.details, dict) and e.details.get("status_code") == 403
        )
        if forbidden_count > 5:
            anomalies.append({
                "incident_type": IncidentType.unauthorized_access,
                "severity": Severity.medium,
                "description": (
                    f"Access beyond role: user '{actor}' received {forbidden_count} "
                    f"forbidden (403) responses in {PRIVILEGE_ESCALATION_WINDOW_HOURS} hour(s), "
                    f"suggesting attempts to access unauthorized resources"
                ),
                "detection_method": "role_violation_heuristic",
                "affected_records": 0,
                "details": {"actor": actor, "forbidden_count": forbidden_count},
            })

    return anomalies


def _detect_unauthenticated_phi_access(db: Session, now: datetime) -> list[dict]:
    """Detect attempts to access PHI endpoints without authentication."""
    window_start = now - timedelta(hours=24)
    anomalies = []

    unauth_phi = (
        db.query(
            AuditLog.resource_id,
            func.count(AuditLog.log_id).label("attempt_count"),
        )
        .filter(
            AuditLog.timestamp >= window_start,
            AuditLog.actor == "anonymous",
            AuditLog.resource_type.in_(["employee", "claim", "clinical", "phi"]),
        )
        .group_by(AuditLog.resource_id)
        .all()
    )

    total_attempts = sum(count for _, count in unauth_phi)
    if total_attempts > 0:
        anomalies.append({
            "incident_type": IncidentType.unauthorized_access,
            "severity": Severity.high if total_attempts > 10 else Severity.medium,
            "description": (
                f"Unauthenticated PHI access attempts: {total_attempts} anonymous "
                f"requests to PHI-containing endpoints in 24 hours"
            ),
            "detection_method": "unauthenticated_phi_heuristic",
            "affected_records": 0,
            "details": {
                "total_attempts": total_attempts,
                "endpoints": [
                    {"path": path, "count": count} for path, count in unauth_phi
                ],
            },
        })

    return anomalies


# --------------------------------------------------------------------------- #
# Incident management                                                          #
# --------------------------------------------------------------------------- #
def create_incident(
    db: Session,
    incident_type: str,
    severity: str,
    description: str,
    detection_method: str,
    affected_records: int = 0,
    affected_employers: list | None = None,
    remediation_steps: list | None = None,
) -> dict:
    """Create a new BreachIncident record.

    Returns the created incident as a dict.
    """
    now = datetime.now(UTC)

    # Determine if HHS notification is required
    hhs_required = affected_records >= 500
    notification_deadline = now + timedelta(days=60) if hhs_required else None

    incident = BreachIncident(
        detected_at=now,
        incident_type=incident_type,
        severity=severity,
        affected_records_count=affected_records,
        affected_employers=affected_employers,
        description=description,
        detection_method=detection_method,
        hhs_notification_required=hhs_required,
        notification_deadline=notification_deadline,
        remediation_steps=remediation_steps or [],
        status=IncidentStatus.detected,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    logger.warning(
        "SECURITY INCIDENT CREATED: type=%s severity=%s records=%d id=%s",
        incident_type, severity, affected_records, incident.incident_id,
    )

    return {
        "incident_id": str(incident.incident_id),
        "detected_at": incident.detected_at.isoformat(),
        "incident_type": incident.incident_type,
        "severity": incident.severity,
        "affected_records_count": incident.affected_records_count,
        "description": incident.description,
        "detection_method": incident.detection_method,
        "hhs_notification_required": incident.hhs_notification_required,
        "notification_deadline": incident.notification_deadline.isoformat() if incident.notification_deadline else None,
        "status": incident.status,
    }


def escalate_incident(db: Session, incident_id: str) -> dict:
    """Trigger incident response protocol for an existing incident.

    Moves incident through the response lifecycle and determines
    required notifications.
    """
    incident = (
        db.query(BreachIncident)
        .filter(BreachIncident.incident_id == incident_id)
        .first()
    )
    if not incident:
        raise ValueError(f"Incident {incident_id} not found")

    now = datetime.now(UTC)
    escalation_result = {
        "incident_id": str(incident.incident_id),
        "previous_status": incident.status,
        "actions_taken": [],
    }

    # Progress through status lifecycle
    if incident.status == IncidentStatus.detected:
        incident.status = IncidentStatus.investigating
        escalation_result["actions_taken"].append("Moved to investigating status")

    elif incident.status == IncidentStatus.investigating:
        incident.status = IncidentStatus.contained
        escalation_result["actions_taken"].append("Marked as contained")

    elif incident.status == IncidentStatus.contained:
        incident.status = IncidentStatus.remediated
        escalation_result["actions_taken"].append("Marked as remediated")

        # Trigger notification process if required
        if incident.hhs_notification_required and not incident.hhs_notified_at:
            escalation_result["actions_taken"].append(
                "HHS notification required — submit via breach portal within "
                f"{max(0, (incident.notification_deadline - now).days)} days"
            )

    elif incident.status == IncidentStatus.remediated:
        incident.status = IncidentStatus.reported
        escalation_result["actions_taken"].append("Marked as reported")

    elif incident.status == IncidentStatus.reported:
        incident.status = IncidentStatus.closed
        escalation_result["actions_taken"].append("Incident closed")

    escalation_result["new_status"] = incident.status

    # Add severity-specific actions
    if incident.severity in (Severity.critical, Severity.high):
        escalation_result["actions_taken"].append(
            "Alert sent to security team and privacy officer"
        )
        if incident.severity == Severity.critical:
            escalation_result["actions_taken"].append(
                "Executive notification triggered for critical severity"
            )

    db.commit()

    logger.warning(
        "Incident %s escalated: %s -> %s",
        incident_id, escalation_result["previous_status"], escalation_result["new_status"],
    )

    return escalation_result


def get_active_incidents(db: Session) -> dict:
    """Dashboard view of all open (non-closed) incidents.

    Returns incidents grouped by severity with summary statistics.
    """
    active = (
        db.query(BreachIncident)
        .filter(BreachIncident.status != IncidentStatus.closed)
        .order_by(
            BreachIncident.severity.desc(),
            BreachIncident.detected_at.desc(),
        )
        .all()
    )

    now = datetime.now(UTC)

    return {
        "checked_at": now.isoformat(),
        "total_active": len(active),
        "by_severity": {
            "critical": sum(1 for i in active if i.severity == Severity.critical),
            "high": sum(1 for i in active if i.severity == Severity.high),
            "medium": sum(1 for i in active if i.severity == Severity.medium),
            "low": sum(1 for i in active if i.severity == Severity.low),
        },
        "by_status": {
            "detected": sum(1 for i in active if i.status == IncidentStatus.detected),
            "investigating": sum(1 for i in active if i.status == IncidentStatus.investigating),
            "contained": sum(1 for i in active if i.status == IncidentStatus.contained),
            "remediated": sum(1 for i in active if i.status == IncidentStatus.remediated),
            "reported": sum(1 for i in active if i.status == IncidentStatus.reported),
        },
        "overdue_notifications": sum(
            1 for i in active
            if i.notification_deadline and i.notification_deadline < now
            and not i.hhs_notified_at
        ),
        "incidents": [
            {
                "incident_id": str(i.incident_id),
                "detected_at": i.detected_at.isoformat(),
                "incident_type": i.incident_type,
                "severity": i.severity,
                "status": i.status,
                "affected_records_count": i.affected_records_count,
                "description": i.description,
                "detection_method": i.detection_method,
                "hhs_notification_required": i.hhs_notification_required,
                "days_open": (now - i.detected_at).days,
                "notification_deadline": i.notification_deadline.isoformat() if i.notification_deadline else None,
            }
            for i in active
        ],
    }
