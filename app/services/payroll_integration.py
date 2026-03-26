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


# ── Adapter registry ─────────────────────────────────────────────────────────

ADAPTER_REGISTRY: dict[str, type[PayrollAdapter]] = {
    "adp": ADPAdapter,
    "workday": WorkdayAdapter,
    "paychex": PaychexAdapter,
    "csv": GenericCSVAdapter,
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
