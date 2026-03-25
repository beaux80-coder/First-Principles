"""Security Operations API (Function 13).

Constitution: "HIPAA compliance is non-negotiable. Every vendor touching PHI has a BAA.
Every breach is detected, reported, and remediated per federal and state law."

Endpoints:
- GET  /security/hipaa-compliance — full HIPAA compliance check
- GET  /security/baa-registry — BAA status for all vendors
- POST /security/baa — create/update BAA record
- POST /security/privacy-request — handle patient privacy request
- GET  /security/breach-detection — run detection scan
- GET  /security/incidents — active breach incidents
- POST /security/incidents — create incident
- GET  /security/state-compliance/{state} — state-specific compliance
- GET  /security/cis-benchmarks — CIS benchmark check
- GET  /security/dependency-scan — vulnerability scan
- POST /security/third-party-assessment — assess a vendor
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/security", tags=["security-ops"])


# --------------------------------------------------------------------------- #
# Request / Response schemas                                                   #
# --------------------------------------------------------------------------- #
class BAACreateRequest(BaseModel):
    vendor_name: str
    vendor_type: str
    signed_at: datetime
    expires_at: datetime
    phi_categories: list[str]
    baa_document_ref: Optional[str] = None
    contacts: Optional[str] = None


class PrivacyRequestInput(BaseModel):
    request_type: str  # access, amendment, restriction, accounting_of_disclosures
    employee_id: str
    details: dict = {}


class IncidentCreateRequest(BaseModel):
    incident_type: str
    severity: str
    description: str
    detection_method: str
    affected_records: int = 0
    affected_employers: Optional[list[str]] = None
    remediation_steps: Optional[list[str]] = None


class ThirdPartyAssessmentRequest(BaseModel):
    entity_name: str
    entity_type: str = "vendor"
    assessment_type: str = "initial"
    findings: Optional[list[dict]] = None
    assessor: str = "security_team"


# --------------------------------------------------------------------------- #
# HIPAA compliance                                                             #
# --------------------------------------------------------------------------- #
@router.get("/hipaa-compliance")
def hipaa_compliance_check(db: Session = Depends(get_db)):
    """Full HIPAA Privacy Rule, Security Rule, and Breach Notification Rule compliance check."""
    from app.services.hipaa_compliance import verify_hipaa_compliance
    return verify_hipaa_compliance(db)


# --------------------------------------------------------------------------- #
# BAA management                                                               #
# --------------------------------------------------------------------------- #
@router.get("/baa-registry")
def baa_registry(db: Session = Depends(get_db)):
    """Return BAA status for all vendors with coverage gap analysis."""
    from app.services.baa_manager import get_baa_registry, check_baa_coverage
    registry = get_baa_registry(db)
    coverage = check_baa_coverage(db)
    return {
        "registry": registry,
        "coverage": coverage,
    }


@router.post("/baa")
def create_baa(request: BAACreateRequest, db: Session = Depends(get_db)):
    """Create a new BAA record for a vendor."""
    from app.services.baa_manager import create_baa_record
    try:
        return create_baa_record(
            db,
            vendor_name=request.vendor_name,
            vendor_type=request.vendor_type,
            signed_at=request.signed_at,
            expires_at=request.expires_at,
            phi_categories=request.phi_categories,
            baa_document_ref=request.baa_document_ref,
            contacts=request.contacts,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


# --------------------------------------------------------------------------- #
# Privacy requests                                                             #
# --------------------------------------------------------------------------- #
@router.post("/privacy-request")
def handle_privacy_request(request: PrivacyRequestInput, db: Session = Depends(get_db)):
    """Handle HIPAA patient privacy rights request (access, amendment, restriction, accounting)."""
    from app.services.hipaa_compliance import process_privacy_request
    try:
        return process_privacy_request(
            db,
            request_type=request.request_type,
            employee_id=request.employee_id,
            details=request.details,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------------------------------------------------- #
# Breach detection                                                             #
# --------------------------------------------------------------------------- #
@router.get("/breach-detection")
def run_breach_detection(db: Session = Depends(get_db)):
    """Run continuous breach detection scan across all heuristics."""
    from app.services.breach_detection import run_continuous_detection
    return run_continuous_detection(db)


# --------------------------------------------------------------------------- #
# Incident management                                                         #
# --------------------------------------------------------------------------- #
@router.get("/incidents")
def list_active_incidents(db: Session = Depends(get_db)):
    """Dashboard view of all active (non-closed) breach incidents."""
    from app.services.breach_detection import get_active_incidents
    return get_active_incidents(db)


@router.post("/incidents")
def create_incident_endpoint(request: IncidentCreateRequest, db: Session = Depends(get_db)):
    """Manually create a breach incident record."""
    from app.services.breach_detection import create_incident
    return create_incident(
        db,
        incident_type=request.incident_type,
        severity=request.severity,
        description=request.description,
        detection_method=request.detection_method,
        affected_records=request.affected_records,
        affected_employers=request.affected_employers,
        remediation_steps=request.remediation_steps,
    )


# --------------------------------------------------------------------------- #
# State compliance                                                             #
# --------------------------------------------------------------------------- #
@router.get("/state-compliance/{state}")
def state_compliance_check(state: str, db: Session = Depends(get_db)):
    """Check compliance with a specific state's privacy law."""
    from app.services.state_privacy import check_state_compliance
    return check_state_compliance(db, state)


# --------------------------------------------------------------------------- #
# Infrastructure security                                                      #
# --------------------------------------------------------------------------- #
@router.get("/cis-benchmarks")
def cis_benchmark_check():
    """Verify CIS benchmark compliance across all control categories."""
    from app.services.infrastructure_security import verify_cis_benchmarks
    return verify_cis_benchmarks()


@router.get("/dependency-scan")
def dependency_scan():
    """SBOM generation and CVE vulnerability scan of Python dependencies."""
    from app.services.infrastructure_security import scan_dependencies
    return scan_dependencies()


@router.post("/third-party-assessment")
def third_party_assessment(request: ThirdPartyAssessmentRequest, db: Session = Depends(get_db)):
    """Conduct a third-party vendor security assessment."""
    from app.services.infrastructure_security import assess_third_party
    return assess_third_party(
        db,
        entity_name=request.entity_name,
        entity_type=request.entity_type,
        assessment_type=request.assessment_type,
        findings=request.findings,
        assessor=request.assessor,
    )
