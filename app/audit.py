"""Audit logging service — HIPAA requirement.

Logs all data access events. No PHI in the log itself.
"""

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def log_access(
    db: Session,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict | None = None,
) -> None:
    entry = AuditLog(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )
    db.add(entry)
    db.commit()
