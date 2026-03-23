"""Audit logging for all data access — HIPAA requirement.

Every read/write of PII/PHI is logged here. No PHI in the log itself.
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    log_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))  # user ID or system identifier
    action: Mapped[str] = mapped_column(String(100))  # e.g., "read", "write", "delete"
    resource_type: Mapped[str] = mapped_column(String(100))  # e.g., "employee", "claim"
    resource_id: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
