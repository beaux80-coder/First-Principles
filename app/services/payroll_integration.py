"""Payroll/HRIS Integration Framework (Function 11).

Constitution: "Integrates with employer's payroll or HRIS via API.
New hires auto-enrolled upon detection. One plan, zero cost-sharing,
no plan selection. Open enrollment eliminated entirely."

This module provides:
1. Abstract PayrollAdapter with standard methods for any HRIS/payroll system
2. Concrete adapters: ADP Workforce Now, Workday HCM, Paychex, Generic CSV
3. PayrollIntegrationManager for orchestrating sync, auto-enrollment, and
   continuous enrollment without open enrollment windows

All adapters follow the same contract: connect, pull census, push deductions,
detect new hires and terminations. The manager handles auto-enrollment
across all 7 benefit types with zero cost-sharing.
"""

import csv
import io
import json
import logging
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, UTC, timedelta

from sqlalchemy.orm import Session

from app.models.employee import Employee, EmployeeStatus
from app.models.service import BenefitType

logger = logging.getLogger(__name__)

ALL_BENEFIT_TYPES = [bt.value for bt in BenefitType]


# ── Abstract adapter ─────────────────────────────────────────────────────────


class PayrollAdapter(ABC):
    """Abstract base class for payroll/HRIS integrations.

    Every adapter must implement these six methods to enable:
    - Connection handshake and credential validation
    - Full census pull (all active employees)
    - Incremental new-hire and termination detection
    - Deduction push-back to payroll (for employer cost tracking)
    - Employee sync (bidirectional state reconciliation)
    """

    def __init__(self, employer_id: uuid.UUID, config: dict):
        self.employer_id = employer_id
        self.config = config
        self.connected = False
        self.last_sync_at: datetime | None = None

    @abstractmethod
    def connect(self) -> dict:
        """Establish connection to payroll system.

        Returns:
            Connection status dict with keys:
            - connected: bool
            - system_name: str
            - api_version: str
            - employer_payroll_id: str
        """

    @abstractmethod
    def sync_employees(self) -> dict:
        """Full bidirectional employee sync.

        Returns:
            Sync result dict with keys:
            - total_in_payroll: int
            - new_hires: list[dict]
            - terminations: list[dict]
            - updates: list[dict]
            - synced_at: str (ISO 8601)
        """

    @abstractmethod
    def pull_census(self) -> list[dict]:
        """Pull complete employee census from payroll.

        Returns:
            List of employee dicts with keys:
            - payroll_id: str
            - first_name: str
            - last_name: str
            - date_of_birth: str
            - hire_date: str
            - termination_date: str | None
            - status: str (active, terminated, leave)
            - zip_code: str
            - dependents: list[dict]
            - salary: float
        """

    @abstractmethod
    def push_deductions(self, deductions: list[dict]) -> dict:
        """Push benefit deduction records to payroll.

        Under beneflex, employee deductions are $0 (zero cost-sharing).
        This pushes employer-side cost allocations for accounting.

        Args:
            deductions: List of deduction dicts with keys:
                - employee_payroll_id: str
                - deduction_code: str
                - amount: float (always 0.00 for employee)
                - employer_amount: float
                - effective_date: str

        Returns:
            Push result dict with accepted/rejected counts.
        """

    @abstractmethod
    def detect_new_hires(self) -> list[dict]:
        """Detect employees added since last sync.

        Returns:
            List of new hire dicts (same schema as pull_census items).
        """

    @abstractmethod
    def detect_terminations(self) -> list[dict]:
        """Detect employees terminated since last sync.

        Returns:
            List of termination dicts with:
            - payroll_id: str
            - termination_date: str
            - termination_reason: str
        """


# ── ADP Workforce Now ─────────────────────────────────────────────────────────


class ADPAdapter(PayrollAdapter):
    """ADP Workforce Now API integration.

    Uses ADP's REST API (api.adp.com) with OAuth2 client credentials.
    Endpoints modeled on ADP Workforce Now v2 API structure.
    """

    API_BASE = "https://api.adp.com"
    TOKEN_URL = f"{API_BASE}/auth/oauth/v2/token"
    WORKERS_URL = f"{API_BASE}/hr/v2/workers"
    EVENTS_URL = f"{API_BASE}/events/hr/v1/worker"

    def __init__(self, employer_id: uuid.UUID, config: dict):
        super().__init__(employer_id, config)
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self.certificate_path = config.get("certificate_path", "")
        self.access_token: str | None = None
        self.token_expires_at: datetime | None = None

    def connect(self) -> dict:
        """Authenticate with ADP via OAuth2 + SSL certificate."""
        # In production, this would make an httpx POST to TOKEN_URL
        # with client credentials and mutual TLS certificate
        logger.info("ADP: Authenticating with OAuth2 client credentials")

        # Mock successful connection
        self.access_token = f"adp_token_{uuid.uuid4().hex[:16]}"
        self.token_expires_at = datetime.now(UTC) + timedelta(hours=1)
        self.connected = True

        return {
            "connected": True,
            "system_name": "ADP Workforce Now",
            "api_version": "v2",
            "employer_payroll_id": self.config.get("company_code", ""),
            "token_expires_at": self.token_expires_at.isoformat(),
            "features": [
                "worker_demographics", "worker_status_events",
                "payroll_deductions", "new_hire_reporting",
            ],
        }

    def sync_employees(self) -> dict:
        """Sync employees via ADP Workers API."""
        if not self.connected:
            self.connect()

        census = self.pull_census()
        new_hires = self.detect_new_hires()
        terminations = self.detect_terminations()

        self.last_sync_at = datetime.now(UTC)
        return {
            "total_in_payroll": len(census),
            "new_hires": new_hires,
            "terminations": terminations,
            "updates": [],
            "synced_at": self.last_sync_at.isoformat(),
            "source": "ADP Workforce Now",
        }

    def pull_census(self) -> list[dict]:
        """Pull census via GET /hr/v2/workers."""
        if not self.connected:
            self.connect()

        # In production: httpx.get(self.WORKERS_URL, headers=auth_headers)
        # ADP returns workers in their proprietary JSON format;
        # we normalize to our standard schema
        logger.info("ADP: Pulling census from /hr/v2/workers")
        return []

    def push_deductions(self, deductions: list[dict]) -> dict:
        """Push deductions via ADP payroll API."""
        if not self.connected:
            self.connect()

        # In production: POST to ADP's payroll deduction endpoint
        # Under beneflex, employee deductions are always $0
        logger.info(
            "ADP: Pushing %d deduction records (employee amount: $0.00)",
            len(deductions),
        )
        return {
            "accepted": len(deductions),
            "rejected": 0,
            "employee_deduction_total": 0.00,
            "employer_deduction_total": sum(
                d.get("employer_amount", 0) for d in deductions
            ),
            "pushed_at": datetime.now(UTC).isoformat(),
        }

    def detect_new_hires(self) -> list[dict]:
        """Detect new hires via ADP event notifications.

        ADP provides webhook-based event notifications for worker.hire
        events. We also poll /events/hr/v1/worker.hire as fallback.
        """
        if not self.connected:
            self.connect()

        # In production: GET /events/hr/v1/worker.hire?since=last_sync
        logger.info("ADP: Checking for new hire events since %s", self.last_sync_at)
        return []

    def detect_terminations(self) -> list[dict]:
        """Detect terminations via ADP event notifications."""
        if not self.connected:
            self.connect()

        # In production: GET /events/hr/v1/worker.terminate?since=last_sync
        logger.info(
            "ADP: Checking for termination events since %s", self.last_sync_at
        )
        return []


# ── Workday HCM ───────────────────────────────────────────────────────────────


class WorkdayAdapter(PayrollAdapter):
    """Workday HCM integration.

    Uses Workday's REST API (formerly SOAP/RaaS) for worker data.
    Connects via OAuth2 with Workday-issued API client credentials.
    """

    def __init__(self, employer_id: uuid.UUID, config: dict):
        super().__init__(employer_id, config)
        self.tenant_url = config.get(
            "tenant_url", "https://wd5-impl-services1.workday.com"
        )
        self.tenant_name = config.get("tenant_name", "")
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self.refresh_token = config.get("refresh_token", "")
        self.access_token: str | None = None

    def connect(self) -> dict:
        """Authenticate with Workday OAuth2."""
        logger.info(
            "Workday: Authenticating with tenant %s", self.tenant_name
        )

        self.access_token = f"wd_token_{uuid.uuid4().hex[:16]}"
        self.connected = True

        return {
            "connected": True,
            "system_name": "Workday HCM",
            "api_version": "v40.1",
            "employer_payroll_id": self.tenant_name,
            "features": [
                "workers", "staffing_events", "payroll",
                "benefits_enrollment", "compensation",
            ],
        }

    def sync_employees(self) -> dict:
        """Sync via Workday Workers REST resource."""
        if not self.connected:
            self.connect()

        census = self.pull_census()
        new_hires = self.detect_new_hires()
        terminations = self.detect_terminations()

        self.last_sync_at = datetime.now(UTC)
        return {
            "total_in_payroll": len(census),
            "new_hires": new_hires,
            "terminations": terminations,
            "updates": [],
            "synced_at": self.last_sync_at.isoformat(),
            "source": "Workday HCM",
        }

    def pull_census(self) -> list[dict]:
        """Pull census via Workday Workers resource.

        Endpoint: GET /ccx/api/v1/{tenant}/workers
        Workday returns paginated results; we iterate all pages.
        """
        if not self.connected:
            self.connect()

        # In production: paginate through Workday Workers API
        # Normalize Workday's deeply nested JSON to our flat schema
        logger.info(
            "Workday: Pulling census from /ccx/api/v1/%s/workers",
            self.tenant_name,
        )
        return []

    def push_deductions(self, deductions: list[dict]) -> dict:
        """Push deductions via Workday Payroll Input web service."""
        if not self.connected:
            self.connect()

        logger.info(
            "Workday: Pushing %d deduction records to payroll inputs",
            len(deductions),
        )
        return {
            "accepted": len(deductions),
            "rejected": 0,
            "employee_deduction_total": 0.00,
            "employer_deduction_total": sum(
                d.get("employer_amount", 0) for d in deductions
            ),
            "pushed_at": datetime.now(UTC).isoformat(),
        }

    def detect_new_hires(self) -> list[dict]:
        """Detect new hires via Workday Staffing Events.

        Uses Workday's business process events to detect Hire events.
        """
        if not self.connected:
            self.connect()

        logger.info("Workday: Checking staffing events for new hires")
        return []

    def detect_terminations(self) -> list[dict]:
        """Detect terminations via Workday Staffing Events."""
        if not self.connected:
            self.connect()

        logger.info("Workday: Checking staffing events for terminations")
        return []


# ── Paychex ───────────────────────────────────────────────────────────────────


class PaychexAdapter(PayrollAdapter):
    """Paychex Flex API integration.

    Uses Paychex's REST API with OAuth2 for payroll and worker data.
    """

    API_BASE = "https://api.paychex.com"

    def __init__(self, employer_id: uuid.UUID, config: dict):
        super().__init__(employer_id, config)
        self.api_key = config.get("api_key", "")
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self.company_id = config.get("company_id", "")
        self.access_token: str | None = None

    def connect(self) -> dict:
        """Authenticate with Paychex OAuth2."""
        logger.info("Paychex: Authenticating with company %s", self.company_id)

        self.access_token = f"pcx_token_{uuid.uuid4().hex[:16]}"
        self.connected = True

        return {
            "connected": True,
            "system_name": "Paychex Flex",
            "api_version": "v1",
            "employer_payroll_id": self.company_id,
            "features": [
                "workers", "payroll", "company", "labor",
            ],
        }

    def sync_employees(self) -> dict:
        """Sync via Paychex Workers API."""
        if not self.connected:
            self.connect()

        census = self.pull_census()
        new_hires = self.detect_new_hires()
        terminations = self.detect_terminations()

        self.last_sync_at = datetime.now(UTC)
        return {
            "total_in_payroll": len(census),
            "new_hires": new_hires,
            "terminations": terminations,
            "updates": [],
            "synced_at": self.last_sync_at.isoformat(),
            "source": "Paychex Flex",
        }

    def pull_census(self) -> list[dict]:
        """Pull census via GET /companies/{companyId}/workers."""
        if not self.connected:
            self.connect()

        logger.info(
            "Paychex: Pulling census from /companies/%s/workers",
            self.company_id,
        )
        return []

    def push_deductions(self, deductions: list[dict]) -> dict:
        """Push deductions via Paychex Payroll API."""
        if not self.connected:
            self.connect()

        logger.info(
            "Paychex: Pushing %d deduction records", len(deductions)
        )
        return {
            "accepted": len(deductions),
            "rejected": 0,
            "employee_deduction_total": 0.00,
            "employer_deduction_total": sum(
                d.get("employer_amount", 0) for d in deductions
            ),
            "pushed_at": datetime.now(UTC).isoformat(),
        }

    def detect_new_hires(self) -> list[dict]:
        """Detect new hires via Paychex worker status changes."""
        if not self.connected:
            self.connect()

        logger.info("Paychex: Checking for new hire events")
        return []

    def detect_terminations(self) -> list[dict]:
        """Detect terminations via Paychex worker status changes."""
        if not self.connected:
            self.connect()

        logger.info("Paychex: Checking for termination events")
        return []


# ── Generic CSV fallback ──────────────────────────────────────────────────────


class GenericCSVAdapter(PayrollAdapter):
    """CSV file upload fallback for employers without API-capable payroll.

    Accepts standard census CSV files with columns:
    payroll_id, first_name, last_name, date_of_birth, hire_date,
    termination_date, status, zip_code, salary
    """

    REQUIRED_COLUMNS = {
        "payroll_id", "first_name", "last_name", "hire_date", "status",
    }

    def __init__(self, employer_id: uuid.UUID, config: dict):
        super().__init__(employer_id, config)
        self.csv_content: str | None = config.get("csv_content")
        self.csv_file_path: str | None = config.get("csv_file_path")
        self._parsed_rows: list[dict] = []
        self._previous_census: list[dict] = []

    def connect(self) -> dict:
        """Validate CSV is parseable and has required columns."""
        logger.info("CSV: Validating census file")

        raw = self.csv_content or ""
        if not raw and self.csv_file_path:
            # In production, read from secure upload storage
            raw = ""

        if not raw:
            return {
                "connected": False,
                "system_name": "Generic CSV",
                "api_version": "n/a",
                "employer_payroll_id": str(self.employer_id),
                "error": "No CSV content provided",
            }

        reader = csv.DictReader(io.StringIO(raw))
        columns = set(reader.fieldnames or [])
        missing = self.REQUIRED_COLUMNS - columns
        if missing:
            return {
                "connected": False,
                "system_name": "Generic CSV",
                "api_version": "n/a",
                "employer_payroll_id": str(self.employer_id),
                "error": f"Missing required columns: {missing}",
            }

        self._parsed_rows = list(reader)
        self.connected = True

        return {
            "connected": True,
            "system_name": "Generic CSV",
            "api_version": "n/a",
            "employer_payroll_id": str(self.employer_id),
            "rows_parsed": len(self._parsed_rows),
        }

    def sync_employees(self) -> dict:
        """Sync from parsed CSV data."""
        if not self.connected:
            self.connect()

        census = self.pull_census()
        new_hires = self.detect_new_hires()
        terminations = self.detect_terminations()

        # Store current census as previous for next diff
        self._previous_census = census
        self.last_sync_at = datetime.now(UTC)

        return {
            "total_in_payroll": len(census),
            "new_hires": new_hires,
            "terminations": terminations,
            "updates": [],
            "synced_at": self.last_sync_at.isoformat(),
            "source": "Generic CSV",
        }

    def pull_census(self) -> list[dict]:
        """Return parsed CSV rows as census."""
        if not self.connected:
            self.connect()
        return self._parsed_rows

    def push_deductions(self, deductions: list[dict]) -> dict:
        """CSV adapter cannot push deductions; generate report instead."""
        logger.info(
            "CSV: Generating deduction report for %d records "
            "(manual payroll entry required)",
            len(deductions),
        )
        return {
            "accepted": len(deductions),
            "rejected": 0,
            "employee_deduction_total": 0.00,
            "employer_deduction_total": sum(
                d.get("employer_amount", 0) for d in deductions
            ),
            "pushed_at": datetime.now(UTC).isoformat(),
            "note": (
                "CSV adapter: deductions exported as report. "
                "Manual entry into payroll system required."
            ),
        }

    def detect_new_hires(self) -> list[dict]:
        """Detect new hires by diffing current vs previous census."""
        if not self._previous_census:
            # First sync: everyone with status=active is a "new hire"
            return [
                row for row in self._parsed_rows
                if row.get("status", "").lower() == "active"
            ]

        previous_ids = {r["payroll_id"] for r in self._previous_census}
        return [
            row for row in self._parsed_rows
            if row["payroll_id"] not in previous_ids
            and row.get("status", "").lower() == "active"
        ]

    def detect_terminations(self) -> list[dict]:
        """Detect terminations by diffing current vs previous census."""
        if not self._previous_census:
            return [
                row for row in self._parsed_rows
                if row.get("status", "").lower() == "terminated"
            ]

        previous_active = {
            r["payroll_id"] for r in self._previous_census
            if r.get("status", "").lower() == "active"
        }
        return [
            row for row in self._parsed_rows
            if row["payroll_id"] in previous_active
            and row.get("status", "").lower() == "terminated"
        ]


# ── Finch / Merge Aggregator ─────────────────────────────────────────────────


class FinchAggregatorAdapter(PayrollAdapter):
    """Payroll aggregator integration via Finch (or Merge) unified API.

    Constitution F11 Item 1: "Payroll aggregator + photo/OCR fallback."

    Finch/Merge provide a single API that normalises access to 200+
    payroll and HRIS systems (ADP, Gusto, Paychex, Rippling, etc.).
    This adapter connects to the aggregator rather than each vendor
    directly, dramatically expanding coverage for small/mid employers
    who may use niche payroll providers.

    Flow:
    1. Employer authorises via Finch Connect (embedded widget)
    2. Finch returns an access_token scoped to the employer's payroll
    3. We call Finch's normalised /directory, /individual, /employment,
       /payment, and /pay-statement endpoints
    """

    API_BASE = "https://api.tryfinch.com"
    DIRECTORY_URL = f"{API_BASE}/employer/directory"
    INDIVIDUAL_URL = f"{API_BASE}/employer/individual"
    EMPLOYMENT_URL = f"{API_BASE}/employer/employment"
    PAYMENT_URL = f"{API_BASE}/employer/payment"
    PAY_STATEMENT_URL = f"{API_BASE}/employer/pay-statement"

    def __init__(self, employer_id: uuid.UUID, config: dict):
        super().__init__(employer_id, config)
        self.access_token: str | None = config.get("access_token")
        self.client_id = config.get("client_id", "")
        self.client_secret = config.get("client_secret", "")
        self.provider_id: str | None = config.get("provider_id")
        self.sandbox = config.get("sandbox", False)

    def connect(self) -> dict:
        """Authenticate via Finch Connect access token.

        In production the employer completes the Finch Connect widget
        which returns an authorization code; we exchange it for an
        access_token via POST /auth/token.
        """
        logger.info(
            "Finch: Connecting to aggregator (provider: %s, sandbox: %s)",
            self.provider_id,
            self.sandbox,
        )

        if not self.access_token:
            # Mock token exchange for development
            self.access_token = f"finch_token_{uuid.uuid4().hex[:16]}"

        self.connected = True

        return {
            "connected": True,
            "system_name": "Finch Aggregator",
            "api_version": "2023-06-01",
            "employer_payroll_id": str(self.employer_id),
            "underlying_provider": self.provider_id or "unknown",
            "sandbox": self.sandbox,
            "features": [
                "directory", "individual", "employment",
                "payment", "pay_statement", "benefits",
            ],
            "supported_payroll_systems": [
                "ADP", "Gusto", "Paychex", "Rippling", "Justworks",
                "BambooHR", "Paylocity", "Paycom", "TriNet", "Zenefits",
                "Square Payroll", "OnPay", "200+ others",
            ],
        }

    def sync_employees(self) -> dict:
        """Sync employees via Finch /employer/directory."""
        if not self.connected:
            self.connect()

        census = self.pull_census()
        new_hires = self.detect_new_hires()
        terminations = self.detect_terminations()

        self.last_sync_at = datetime.now(UTC)
        return {
            "total_in_payroll": len(census),
            "new_hires": new_hires,
            "terminations": terminations,
            "updates": [],
            "synced_at": self.last_sync_at.isoformat(),
            "source": f"Finch Aggregator ({self.provider_id or 'unknown'})",
        }

    def pull_census(self) -> list[dict]:
        """Pull census via GET /employer/directory + POST /employer/individual.

        Finch returns a directory of employee IDs first, then we batch
        request individual details for each.
        """
        if not self.connected:
            self.connect()

        # In production:
        # 1. GET /employer/directory -> list of {id, first_name, last_name, ...}
        # 2. POST /employer/individual with {requests: [{individual_id: ...}]}
        # 3. POST /employer/employment with {requests: [{individual_id: ...}]}
        # All responses are normalised across 200+ providers.
        logger.info(
            "Finch: Pulling census via /employer/directory + /employer/individual"
        )
        return []

    def push_deductions(self, deductions: list[dict]) -> dict:
        """Push deductions via Finch Benefits API (where supported).

        Not all underlying providers support deduction push; for those
        that don't, we generate a manual-entry report (same as CSV adapter).
        """
        if not self.connected:
            self.connect()

        logger.info(
            "Finch: Pushing %d deduction records (employee amount: $0.00)",
            len(deductions),
        )
        return {
            "accepted": len(deductions),
            "rejected": 0,
            "employee_deduction_total": 0.00,
            "employer_deduction_total": sum(
                d.get("employer_amount", 0) for d in deductions
            ),
            "pushed_at": datetime.now(UTC).isoformat(),
            "note": (
                "Finch aggregator: deductions pushed where underlying "
                "provider supports write access. Manual fallback for others."
            ),
        }

    def detect_new_hires(self) -> list[dict]:
        """Detect new hires via Finch directory diff.

        Finch does not provide event-based notifications for all
        providers, so we diff the current directory against the last
        sync snapshot.
        """
        if not self.connected:
            self.connect()

        logger.info(
            "Finch: Checking for new hires since %s", self.last_sync_at
        )
        return []

    def detect_terminations(self) -> list[dict]:
        """Detect terminations via Finch directory diff."""
        if not self.connected:
            self.connect()

        logger.info(
            "Finch: Checking for terminations since %s", self.last_sync_at
        )
        return []


# ── Adapter registry ─────────────────────────────────────────────────────────

ADAPTER_REGISTRY: dict[str, type[PayrollAdapter]] = {
    "adp": ADPAdapter,
    "workday": WorkdayAdapter,
    "paychex": PaychexAdapter,
    "csv": GenericCSVAdapter,
    "finch": FinchAggregatorAdapter,
}


# ── PayrollIntegrationManager ─────────────────────────────────────────────────


class PayrollIntegrationManager:
    """Orchestrates payroll integration, auto-enrollment, and continuous
    enrollment across all adapter types.

    Constitution: "New hires auto-enrolled upon detection. One plan,
    zero cost-sharing, no plan selection. Open enrollment eliminated entirely."
    """

    def get_adapter(
        self, employer_id: uuid.UUID, integration_type: str, config: dict | None = None
    ) -> PayrollAdapter:
        """Get the appropriate payroll adapter for an employer.

        Args:
            employer_id: Employer UUID
            integration_type: One of 'adp', 'workday', 'paychex', 'csv'
            config: Integration-specific configuration (credentials, etc.)

        Returns:
            Configured PayrollAdapter instance.

        Raises:
            ValueError: If integration_type is not recognized.
        """
        adapter_cls = ADAPTER_REGISTRY.get(integration_type.lower())
        if adapter_cls is None:
            raise ValueError(
                f"Unknown integration type: {integration_type}. "
                f"Supported: {list(ADAPTER_REGISTRY.keys())}"
            )
        return adapter_cls(employer_id, config or {})

    def sync_and_enroll(
        self,
        db: Session,
        employer_id: uuid.UUID,
        integration_type: str = "csv",
        config: dict | None = None,
    ) -> dict:
        """Full sync cycle: detect new hires, auto-enroll, detect terminations.

        Constitution: "New hires auto-enrolled upon detection."

        This method:
        1. Connects to payroll via the appropriate adapter
        2. Syncs employee data
        3. Auto-enrolls all new hires with all 7 benefit types, zero cost-sharing
        4. Detects terminations and initiates COBRA processing

        Args:
            db: Database session
            employer_id: Employer UUID
            integration_type: Payroll system type
            config: Integration-specific configuration

        Returns:
            Comprehensive sync result including enrollment and COBRA details.
        """
        adapter = self.get_adapter(employer_id, integration_type, config)
        connection = adapter.connect()

        if not connection.get("connected"):
            return {
                "status": "connection_failed",
                "error": connection.get("error", "Unable to connect"),
                "employer_id": str(employer_id),
            }

        sync_result = adapter.sync_employees()

        # Auto-enroll each new hire with all 7 benefit types
        enrolled = []
        for hire in sync_result.get("new_hires", []):
            enrollment = self.continuous_enrollment(db, employer_id, hire)
            enrolled.append(enrollment)

        # Process terminations: initiate COBRA
        cobra_initiated = []
        for term in sync_result.get("terminations", []):
            cobra_result = self._initiate_cobra_for_termination(
                db, employer_id, term
            )
            cobra_initiated.append(cobra_result)

        # Push $0 employee deductions back to payroll
        deductions = []
        for hire in sync_result.get("new_hires", []):
            deductions.append({
                "employee_payroll_id": hire.get("payroll_id", ""),
                "deduction_code": "BFX_BENEFIT",
                "amount": 0.00,
                "employer_amount": 0.00,  # Actual cost set after pricing
                "effective_date": datetime.now(UTC).strftime("%Y-%m-%d"),
            })
        deduction_result = adapter.push_deductions(deductions) if deductions else {}

        return {
            "status": "synced",
            "employer_id": str(employer_id),
            "connection": connection,
            "sync_summary": {
                "total_in_payroll": sync_result.get("total_in_payroll", 0),
                "new_hires_detected": len(sync_result.get("new_hires", [])),
                "terminations_detected": len(sync_result.get("terminations", [])),
                "synced_at": sync_result.get("synced_at"),
            },
            "auto_enrollment": {
                "employees_enrolled": len(enrolled),
                "enrollments": enrolled,
                "benefit_types_per_employee": ALL_BENEFIT_TYPES,
                "cost_sharing": "zero",
                "open_enrollment_required": False,
            },
            "cobra_processing": {
                "terminations_processed": len(cobra_initiated),
                "cobra_notices": cobra_initiated,
            },
            "deduction_push": deduction_result,
            "constitutional_compliance": {
                "auto_enrollment_on_detection": True,
                "one_plan": True,
                "zero_cost_sharing": True,
                "no_plan_selection_required": True,
                "open_enrollment_eliminated": True,
            },
        }

    def continuous_enrollment(
        self,
        db: Session,
        employer_id: uuid.UUID,
        employee_data: dict,
    ) -> dict:
        """Enroll an employee immediately with all 7 benefit types.

        Constitution: "No plan selection. Open enrollment eliminated entirely."

        Under beneflex there is one plan with zero cost-sharing.
        No elections needed. No waiting for open enrollment windows.
        The employee is enrolled in all 7 benefit types the moment
        they are detected in payroll.

        Args:
            db: Database session
            employer_id: Employer UUID
            employee_data: Employee info from payroll (name, DOB, etc.)

        Returns:
            Enrollment confirmation dict.
        """
        demographics = {
            "first_name": employee_data.get("first_name", ""),
            "last_name": employee_data.get("last_name", ""),
            "date_of_birth": employee_data.get("date_of_birth", ""),
            "zip_code": employee_data.get("zip_code", ""),
            "hire_date": employee_data.get("hire_date", ""),
            "payroll_id": employee_data.get("payroll_id", ""),
            "dependents": employee_data.get("dependents", []),
            "enrollment_date": datetime.now(UTC).isoformat(),
            "enrollment_method": "continuous_auto",
            "benefit_elections": {
                bt: {"elected": True, "auto_enrolled": True}
                for bt in ALL_BENEFIT_TYPES
            },
        }

        employee = Employee(
            employer_id=employer_id,
            status=EmployeeStatus.active,
            demographics_encrypted=json.dumps(demographics),
        )
        db.add(employee)
        db.commit()
        db.refresh(employee)

        logger.info(
            "Auto-enrolled employee %s with all 7 benefit types "
            "(zero cost-sharing, no plan selection)",
            employee.employee_id,
        )

        return {
            "employee_id": str(employee.employee_id),
            "employer_id": str(employer_id),
            "status": "enrolled",
            "enrolled_at": employee.enrolled_at.isoformat(),
            "enrollment_method": "continuous_auto",
            "benefit_types_enrolled": ALL_BENEFIT_TYPES,
            "elections_required": False,
            "open_enrollment_required": False,
            "zero_cost_sharing": {
                "employee_premium_contribution": 0.00,
                "deductible": 0.00,
                "copays": 0.00,
                "coinsurance": 0.00,
                "out_of_pocket_max": 0.00,
            },
        }

    def eliminate_open_enrollment(self) -> dict:
        """Explain the plan structure that eliminates open enrollment.

        Constitution: "One plan, zero cost-sharing, no plan selection.
        Open enrollment eliminated entirely."

        Returns:
            Plan structure explanation showing why OE is unnecessary.
        """
        return {
            "open_enrollment_status": "eliminated",
            "explanation": (
                "Under beneflex, open enrollment is eliminated entirely "
                "because there is exactly one plan with zero cost-sharing. "
                "There are no plan choices for employees to make, no "
                "premium tiers to select, no deductible levels to compare, "
                "and no contribution amounts to elect. Every employee "
                "is automatically enrolled in all 7 benefit types the "
                "moment they appear in payroll. The concept of an annual "
                "enrollment window is unnecessary and does not exist."
            ),
            "plan_structure": {
                "number_of_plans": 1,
                "plan_name": "beneflex Universal",
                "benefit_types_included": ALL_BENEFIT_TYPES,
                "employee_cost_sharing": {
                    "premium_contribution": "$0.00",
                    "deductible": "$0.00",
                    "copays": "$0.00",
                    "coinsurance": "0%",
                    "out_of_pocket_maximum": "$0.00",
                },
                "plan_selection_required": False,
                "enrollment_method": "automatic_on_hire",
                "waiting_period": "0 days",
            },
            "traditional_comparison": {
                "typical_employer_plans": "3-5 plan options",
                "typical_enrollment_window": "2-4 weeks annually",
                "typical_employee_decisions": [
                    "Which plan tier (Bronze/Silver/Gold/Platinum)?",
                    "Which premium level (employee only/family)?",
                    "HSA vs FSA vs HRA?",
                    "Supplemental life insurance amount?",
                    "STD/LTD voluntary elections?",
                    "Dental and vision bundle or standalone?",
                ],
                "beneflex_employee_decisions": [],
                "admin_burden_eliminated": [
                    "Annual open enrollment communications",
                    "Plan comparison tools and decision support",
                    "Election deadline tracking and reminders",
                    "Late enrollment exception processing",
                    "Default enrollment rules and auto-assignment",
                    "Benefits fair coordination",
                    "Enrollment system configuration",
                ],
            },
        }

    def _initiate_cobra_for_termination(
        self,
        db: Session,
        employer_id: uuid.UUID,
        termination_data: dict,
    ) -> dict:
        """Initiate COBRA processing for a terminated employee.

        Called automatically when a termination is detected during payroll sync.
        """
        payroll_id = termination_data.get("payroll_id", "")
        termination_date = termination_data.get(
            "termination_date", datetime.now(UTC).strftime("%Y-%m-%d")
        )
        termination_reason = termination_data.get(
            "termination_reason", "voluntary_termination"
        )

        # Map payroll termination reasons to COBRA qualifying events
        cobra_event_map = {
            "voluntary_termination": "voluntary_termination",
            "involuntary_termination": "involuntary_termination",
            "layoff": "involuntary_termination",
            "reduction_in_force": "involuntary_termination",
            "resignation": "voluntary_termination",
            "hours_reduction": "reduction_in_hours",
            "retirement": "voluntary_termination",
        }

        cobra_event = cobra_event_map.get(
            termination_reason, "voluntary_termination"
        )

        # Determine continuation period
        max_months = 18
        if cobra_event in ("divorce", "death_of_employee", "dependent_aging_out"):
            max_months = 36

        try:
            term_dt = datetime.fromisoformat(termination_date)
        except ValueError:
            term_dt = datetime.now(UTC)

        election_deadline = term_dt + timedelta(days=60)
        continuation_end = term_dt + timedelta(days=max_months * 30)

        return {
            "payroll_id": payroll_id,
            "cobra_qualifying_event": cobra_event,
            "termination_date": termination_date,
            "election_deadline": election_deadline.isoformat(),
            "continuation_end": continuation_end.isoformat(),
            "max_continuation_months": max_months,
            "benefit_types_eligible": ALL_BENEFIT_TYPES,
            "notice_status": "generated",
            "premium_basis": "pass_through_cost_plus_2pct_admin",
        }


# ── Photo / OCR Roster Fallback ─────────────────────────────────────────────


def process_roster_photo(
    db: Session,
    employer_id: uuid.UUID,
    image_data: bytes,
) -> dict:
    """Process a photo of an employee roster via OCR fallback.

    Constitution F11 Item 1: For employers without API-capable payroll,
    accept a photo of the employee roster (e.g., a printed list, whiteboard,
    spreadsheet screenshot). OCR extracts employee names and basic info,
    then creates Employee records for auto-enrollment.

    Args:
        db: Database session
        employer_id: Employer UUID
        image_data: Raw image bytes (JPEG, PNG, etc.)

    Returns:
        Extraction results with parsed employees and enrollment status.
    """
    if not image_data:
        return {
            "employer_id": str(employer_id),
            "error": "no_image_data",
            "message": "No image data provided. Please upload a photo of the roster.",
        }

    # In production, this would call an OCR service (e.g., AWS Textract,
    # Google Cloud Vision, or Azure Form Recognizer) to extract text
    # from the roster photo. The extracted text is then parsed into
    # structured employee records.
    #
    # OCR pipeline:
    # 1. Pre-process image (deskew, contrast enhancement)
    # 2. Run OCR to extract raw text
    # 3. Parse text into rows (table detection or line-by-line)
    # 4. Map columns to employee fields (name, hire date, etc.)
    # 5. Confidence scoring per field

    image_size_bytes = len(image_data)
    image_hash = uuid.uuid5(uuid.NAMESPACE_DNS, str(image_data[:256]))

    logger.info(
        "OCR: Processing roster photo for employer %s (%d bytes)",
        employer_id,
        image_size_bytes,
    )

    # Mock OCR extraction result — in production, replaced by real OCR output
    ocr_raw_text = ""
    extracted_employees: list[dict] = []

    # Parse OCR output into employee records
    # In production: use NLP + heuristics to identify names, dates, etc.
    # from the raw OCR text.

    # Auto-enroll any extracted employees
    enrolled = []
    for emp_data in extracted_employees:
        employee = Employee(
            employer_id=employer_id,
            status=EmployeeStatus.active,
            demographics_encrypted=json.dumps({
                "first_name": emp_data.get("first_name", ""),
                "last_name": emp_data.get("last_name", ""),
                "enrollment_date": datetime.now(UTC).isoformat(),
                "enrollment_method": "roster_photo_ocr",
                "benefit_elections": {
                    bt: {"elected": True, "auto_enrolled": True}
                    for bt in ALL_BENEFIT_TYPES
                },
            }),
        )
        db.add(employee)
        db.flush()
        enrolled.append({
            "employee_id": str(employee.employee_id),
            "name": f"{emp_data.get('first_name', '')} {emp_data.get('last_name', '')}",
            "ocr_confidence": emp_data.get("confidence", 0.0),
        })

    if enrolled:
        db.commit()

    return {
        "employer_id": str(employer_id),
        "method": "roster_photo_ocr",
        "image_size_bytes": image_size_bytes,
        "image_hash": str(image_hash),
        "ocr_result": {
            "raw_text_length": len(ocr_raw_text),
            "employees_extracted": len(extracted_employees),
            "extraction_confidence": 0.0,
        },
        "enrollment": {
            "employees_enrolled": len(enrolled),
            "enrollments": enrolled,
            "benefit_types_per_employee": ALL_BENEFIT_TYPES,
            "enrollment_method": "roster_photo_ocr",
        },
        "fallback_note": (
            "Photo/OCR is a fallback for employers without API-capable "
            "payroll. For best accuracy, use a payroll integration adapter "
            "(ADP, Workday, Paychex, Finch aggregator, or CSV upload)."
        ),
        "next_steps": [
            "Review extracted employee names for accuracy",
            "Confirm enrollment for each employee",
            "Provide additional demographics (DOB, zip) if available",
        ],
    }


def process_text_update(
    db: Session,
    employer_id: uuid.UUID,
    text_message: str,
) -> dict:
    """Process a text-based roster update (e.g., "hired John Smith 3/15").

    Constitution F11 Item 1: Accept natural-language text messages for
    roster changes. Parses the message to detect hires, terminations,
    and other changes, then applies them to the employee roster.

    Supported formats:
    - "hired John Smith 3/15"
    - "John Smith started 2024-03-15"
    - "terminated Jane Doe 4/1"
    - "Jane Doe last day 2024-04-01"

    Args:
        db: Database session
        employer_id: Employer UUID
        text_message: Natural-language roster update message

    Returns:
        Parsed action and result of applying the change.
    """
    if not text_message or not text_message.strip():
        return {
            "employer_id": str(employer_id),
            "error": "empty_message",
            "message": "No text message provided.",
        }

    text = text_message.strip()

    logger.info(
        "Text update: Processing '%s' for employer %s",
        text,
        employer_id,
    )

    # Parse the text message to extract action, name, and date
    action = "unknown"
    employee_name = ""
    event_date = datetime.now(UTC).strftime("%Y-%m-%d")

    # Normalise to lowercase for pattern matching
    text_lower = text.lower()

    # Hire patterns
    hire_patterns = [
        r"hired?\s+(.+?)(?:\s+(?:on\s+)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?))?$",
        r"(.+?)\s+(?:started|start(?:ing)?|begins?|joining)\s*(?:on\s+)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?)?",
        r"new\s+(?:hire|employee)\s*:?\s*(.+?)(?:\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?))?$",
        r"hired?\s+(.+?)(?:\s+(?:on\s+)?(\d{4}-\d{2}-\d{2}))?$",
        r"(.+?)\s+(?:started|start)\s*(?:on\s+)?(\d{4}-\d{2}-\d{2})",
    ]

    # Termination patterns
    term_patterns = [
        r"(?:terminated?|fired?|let\s+go)\s+(.+?)(?:\s+(?:on\s+)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?))?$",
        r"(.+?)\s+(?:last\s+day|leaving|left|terminated?|quit)\s*(?:on\s+)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?)?",
        r"(?:terminated?|fired?)\s+(.+?)(?:\s+(?:on\s+)?(\d{4}-\d{2}-\d{2}))?$",
        r"(.+?)\s+(?:last\s+day|leaving|left)\s*(?:on\s+)?(\d{4}-\d{2}-\d{2})?",
    ]

    # Try hire patterns first
    for pattern in hire_patterns:
        match = re.search(pattern, text_lower)
        if match:
            action = "hire"
            employee_name = match.group(1).strip().title()
            if match.lastindex and match.lastindex >= 2 and match.group(2):
                event_date = _normalise_date(match.group(2))
            break

    # Try termination patterns if no hire matched
    if action == "unknown":
        for pattern in term_patterns:
            match = re.search(pattern, text_lower)
            if match:
                action = "termination"
                employee_name = match.group(1).strip().title()
                if match.lastindex and match.lastindex >= 2 and match.group(2):
                    event_date = _normalise_date(match.group(2))
                break

    if action == "unknown":
        return {
            "employer_id": str(employer_id),
            "original_message": text,
            "parsed_action": "unknown",
            "error": "unrecognised_format",
            "message": (
                "Could not parse the message. Supported formats: "
                "'hired [Name] [date]', 'terminated [Name] [date]', "
                "'[Name] started [date]', '[Name] last day [date]'."
            ),
        }

    # Split name into first/last
    name_parts = employee_name.split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    result: dict = {
        "employer_id": str(employer_id),
        "original_message": text,
        "parsed_action": action,
        "parsed_name": employee_name,
        "parsed_first_name": first_name,
        "parsed_last_name": last_name,
        "parsed_date": event_date,
    }

    if action == "hire":
        # Create new employee and auto-enroll
        employee = Employee(
            employer_id=employer_id,
            status=EmployeeStatus.active,
            demographics_encrypted=json.dumps({
                "first_name": first_name,
                "last_name": last_name,
                "hire_date": event_date,
                "enrollment_date": datetime.now(UTC).isoformat(),
                "enrollment_method": "text_message",
                "benefit_elections": {
                    bt: {"elected": True, "auto_enrolled": True}
                    for bt in ALL_BENEFIT_TYPES
                },
            }),
        )
        db.add(employee)
        db.commit()
        db.refresh(employee)

        result["employee_id"] = str(employee.employee_id)
        result["status"] = "enrolled"
        result["enrollment"] = {
            "employee_id": str(employee.employee_id),
            "enrolled_at": employee.enrolled_at.isoformat(),
            "benefit_types": ALL_BENEFIT_TYPES,
            "enrollment_method": "text_message",
            "zero_cost_sharing": True,
        }

    elif action == "termination":
        # Find employee by name and terminate
        employees = db.query(Employee).filter(
            Employee.employer_id == employer_id,
            Employee.status == EmployeeStatus.active,
        ).all()

        matched_employee = None
        for emp in employees:
            if emp.demographics_encrypted:
                try:
                    demographics = json.loads(emp.demographics_encrypted)
                    emp_first = demographics.get("first_name", "").lower()
                    emp_last = demographics.get("last_name", "").lower()
                    if (
                        emp_first == first_name.lower()
                        and emp_last == last_name.lower()
                    ):
                        matched_employee = emp
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

        if matched_employee:
            matched_employee.status = EmployeeStatus.terminated
            matched_employee.terminated_at = datetime.now(UTC)
            db.commit()

            result["employee_id"] = str(matched_employee.employee_id)
            result["status"] = "terminated"
            result["termination"] = {
                "employee_id": str(matched_employee.employee_id),
                "terminated_at": matched_employee.terminated_at.isoformat(),
                "termination_date": event_date,
                "cobra_eligible": True,
            }
        else:
            result["status"] = "employee_not_found"
            result["message"] = (
                f"No active employee named '{employee_name}' found. "
                f"Please verify the name and try again."
            )

    return result


def _normalise_date(date_str: str) -> str:
    """Normalise a date string (M/D, M/D/YY, M/D/YYYY, YYYY-MM-DD) to ISO format."""
    if not date_str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    # Already ISO format
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str

    # M/D or M/D/YY or M/D/YYYY
    parts = date_str.split("/")
    if len(parts) >= 2:
        month = int(parts[0])
        day = int(parts[1])
        if len(parts) == 3:
            year = int(parts[2])
            if year < 100:
                year += 2000
        else:
            year = datetime.now(UTC).year
        return f"{year:04d}-{month:02d}-{day:02d}"

    return datetime.now(UTC).strftime("%Y-%m-%d")
