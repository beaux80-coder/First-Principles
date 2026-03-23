"""Audit logging middleware — HIPAA requirement.

Automatically logs every API request with actor, action, resource, and
response status. No PHI in the log itself — only request metadata.

Constitution: "Every read/write of PII/PHI is logged here."
"""

import logging
import time
from datetime import datetime, UTC

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.database import SessionLocal
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every API request for HIPAA audit trail."""

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = round((time.time() - start) * 1000)

        # Skip logging for static files and health checks
        path = request.url.path
        if path.startswith("/static") or path == "/health" or path == "/favicon.ico":
            return response

        # Extract actor from auth header or mark as anonymous
        actor = "anonymous"
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            # Don't log the token — just note that auth was provided
            actor = "authenticated_user"

        # Log the request — no PHI, only metadata
        try:
            db = SessionLocal()
            db.add(AuditLog(
                actor=actor,
                action=request.method,
                resource_type=path.split("/")[3] if len(path.split("/")) > 3 else path,
                resource_id=path,
                details={
                    "method": request.method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "client_ip": request.client.host if request.client else "unknown",
                },
            ))
            db.commit()
            db.close()
        except Exception as e:
            logger.warning(f"Audit log write failed: {e}")

        return response
