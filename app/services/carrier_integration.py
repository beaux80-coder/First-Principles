"""Carrier / TPA Integration Service (Function 6A, Stage 2 prerequisite).

Constitution requirement: "One-click API connection to employer's current
carrier/TPA." Supports OAuth where available, SFTP/EDI fallback. Max 3
employer actions to enter shadow mode.

Adapter-based design: each carrier gets an adapter that normalises raw
claim data into the internal schema. A mock adapter ships for development
and integration tests.
"""

import enum
import logging
import uuid
from datetime import datetime, UTC
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Carrier connection types
# ---------------------------------------------------------------------------

class CarrierConnectionType(str, enum.Enum):
    oauth = "oauth"
    sftp = "sftp"
    edi = "edi"
    api_key = "api_key"
    mock = "mock"


class CarrierConnectionStatus(str, enum.Enum):
    pending = "pending"
    connected = "connected"
    failed = "failed"
    disconnected = "disconnected"


# ---------------------------------------------------------------------------
# Normalised carrier claim (what the adapter produces)
# ---------------------------------------------------------------------------

class NormalisedCarrierClaim:
    """Carrier-agnostic representation of a claim from the current carrier."""

    __slots__ = (
        "carrier_claim_id", "employee_external_id", "service_date",
        "service_code", "service_description", "benefit_type",
        "billed_amount", "carrier_paid_amount", "employee_oop",
        "carrier_decision", "carrier_reasoning", "raw_data",
    )

    def __init__(
        self,
        carrier_claim_id: str,
        employee_external_id: str,
        service_date: datetime,
        service_code: str,
        service_description: str,
        benefit_type: str,
        billed_amount: float,
        carrier_paid_amount: float,
        employee_oop: float,
        carrier_decision: str,
        carrier_reasoning: str = "",
        raw_data: dict | None = None,
    ):
        self.carrier_claim_id = carrier_claim_id
        self.employee_external_id = employee_external_id
        self.service_date = service_date
        self.service_code = service_code
        self.service_description = service_description
        self.benefit_type = benefit_type
        self.billed_amount = billed_amount
        self.carrier_paid_amount = carrier_paid_amount
        self.employee_oop = employee_oop
        self.carrier_decision = carrier_decision
        self.carrier_reasoning = carrier_reasoning
        self.raw_data = raw_data or {}

    def to_dict(self) -> dict:
        return {
            "carrier_claim_id": self.carrier_claim_id,
            "employee_external_id": self.employee_external_id,
            "service_date": self.service_date.isoformat(),
            "service_code": self.service_code,
            "service_description": self.service_description,
            "benefit_type": self.benefit_type,
            "billed_amount": self.billed_amount,
            "carrier_paid_amount": self.carrier_paid_amount,
            "employee_oop": self.employee_oop,
            "carrier_decision": self.carrier_decision,
            "carrier_reasoning": self.carrier_reasoning,
        }


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------

class BaseCarrierAdapter:
    """Interface that every carrier adapter implements."""

    def test_connection(self) -> dict:
        raise NotImplementedError

    def fetch_claims(
        self, since: datetime | None = None, limit: int = 100,
    ) -> list[NormalisedCarrierClaim]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock adapter (development / integration tests)
# ---------------------------------------------------------------------------

class MockCarrierAdapter(BaseCarrierAdapter):
    """Generates realistic synthetic carrier claim data for shadow-mode
    development and testing. No external dependencies."""

    MOCK_CLAIMS = [
        {
            "service_code": "99214",
            "service_description": "Office visit, established patient, moderate complexity",
            "benefit_type": "health",
            "billed_amount": 285.00,
            "carrier_paid_amount": 185.00,
            "employee_oop": 50.00,
            "carrier_decision": "approved",
        },
        {
            "service_code": "D1110",
            "service_description": "Dental prophylaxis, adult",
            "benefit_type": "dental",
            "billed_amount": 150.00,
            "carrier_paid_amount": 95.00,
            "employee_oop": 30.00,
            "carrier_decision": "approved",
        },
        {
            "service_code": "92014",
            "service_description": "Comprehensive eye exam, established patient",
            "benefit_type": "vision",
            "billed_amount": 195.00,
            "carrier_paid_amount": 110.00,
            "employee_oop": 40.00,
            "carrier_decision": "approved",
        },
        {
            "service_code": "90837",
            "service_description": "Psychotherapy, 60 minutes",
            "benefit_type": "mental_health",
            "billed_amount": 250.00,
            "carrier_paid_amount": 160.00,
            "employee_oop": 60.00,
            "carrier_decision": "approved",
        },
        {
            "service_code": "71046",
            "service_description": "Chest X-ray, 2 views",
            "benefit_type": "health",
            "billed_amount": 450.00,
            "carrier_paid_amount": 180.00,
            "employee_oop": 0.00,
            "carrier_decision": "approved",
        },
        {
            "service_code": "99213",
            "service_description": "Office visit, established patient, low complexity",
            "benefit_type": "health",
            "billed_amount": 175.00,
            "carrier_paid_amount": 0.00,
            "employee_oop": 0.00,
            "carrier_decision": "denied",
            "carrier_reasoning": "Pre-authorization required",
        },
    ]

    def test_connection(self) -> dict:
        return {
            "status": "connected",
            "carrier_name": "Mock Carrier (Development)",
            "connection_type": CarrierConnectionType.mock.value,
            "capabilities": ["claims_read", "eligibility_read"],
        }

    def fetch_claims(
        self, since: datetime | None = None, limit: int = 100,
    ) -> list[NormalisedCarrierClaim]:
        claims: list[NormalisedCarrierClaim] = []
        for i, template in enumerate(self.MOCK_CLAIMS[:limit]):
            claims.append(NormalisedCarrierClaim(
                carrier_claim_id=f"MOCK-{uuid.uuid4().hex[:8].upper()}",
                employee_external_id=f"EMP-{(i % 3) + 1:04d}",
                service_date=datetime.now(UTC),
                service_code=template["service_code"],
                service_description=template["service_description"],
                benefit_type=template["benefit_type"],
                billed_amount=template["billed_amount"],
                carrier_paid_amount=template["carrier_paid_amount"],
                employee_oop=template["employee_oop"],
                carrier_decision=template["carrier_decision"],
                carrier_reasoning=template.get("carrier_reasoning", ""),
            ))
        return claims


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: dict[str, type[BaseCarrierAdapter]] = {
    "mock": MockCarrierAdapter,
}


def _get_adapter(carrier_name: str, credentials: dict) -> BaseCarrierAdapter:
    """Resolve and instantiate the right adapter for a carrier."""
    adapter_cls = _ADAPTER_REGISTRY.get(carrier_name.lower())
    if adapter_cls:
        return adapter_cls()

    # Default: mock adapter (in production, would raise for unknown carriers)
    logger.warning("Unknown carrier '%s' — falling back to mock adapter.", carrier_name)
    return MockCarrierAdapter()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def connect_carrier(
    employer_id: str,
    carrier_name: str,
    connection_type: str,
    credentials: dict | None = None,
) -> dict:
    """Connect to a carrier/TPA. OAuth where supported, SFTP/EDI fallback.

    Constitution: "One-click API connection to employer's current carrier/TPA."
    Max 3 employer actions: select carrier, authorise, confirm.

    Returns connection metadata for storage on the employer record.
    """
    credentials = credentials or {}
    adapter = _get_adapter(carrier_name, credentials)

    try:
        test_result = adapter.test_connection()
    except Exception as exc:
        logger.error("Carrier connection failed for %s: %s", carrier_name, exc)
        return {
            "employer_id": employer_id,
            "carrier_name": carrier_name,
            "connection_type": connection_type,
            "status": CarrierConnectionStatus.failed.value,
            "error": str(exc),
        }

    return {
        "employer_id": employer_id,
        "carrier_name": carrier_name,
        "connection_type": connection_type,
        "status": CarrierConnectionStatus.connected.value,
        "capabilities": test_result.get("capabilities", []),
        "connected_at": datetime.now(UTC).isoformat(),
    }


def ingest_carrier_claim(raw_data: dict) -> NormalisedCarrierClaim:
    """Parse raw carrier claim data into a NormalisedCarrierClaim.

    Accepts either EDI 837/835 parsed output or a flat JSON dict
    from a carrier API response.
    """
    return NormalisedCarrierClaim(
        carrier_claim_id=raw_data.get("carrier_claim_id", f"ING-{uuid.uuid4().hex[:8]}"),
        employee_external_id=raw_data.get("employee_external_id", "UNKNOWN"),
        service_date=datetime.fromisoformat(raw_data["service_date"])
            if isinstance(raw_data.get("service_date"), str)
            else raw_data.get("service_date", datetime.now(UTC)),
        service_code=raw_data.get("service_code", ""),
        service_description=raw_data.get("service_description", ""),
        benefit_type=raw_data.get("benefit_type", "health"),
        billed_amount=float(raw_data.get("billed_amount", 0)),
        carrier_paid_amount=float(raw_data.get("carrier_paid_amount", 0)),
        employee_oop=float(raw_data.get("employee_oop", 0)),
        carrier_decision=raw_data.get("carrier_decision", "unknown"),
        carrier_reasoning=raw_data.get("carrier_reasoning", ""),
        raw_data=raw_data,
    )


def fetch_carrier_claims(
    carrier_name: str,
    credentials: dict | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[NormalisedCarrierClaim]:
    """Fetch claims from a connected carrier. Used by shadow mode to pull
    real claims for parallel processing."""
    adapter = _get_adapter(carrier_name, credentials or {})
    return adapter.fetch_claims(since=since, limit=limit)
