"""Care Execution Coordination API endpoints (Function 9).

Constitution: "Employee describes issue in plain language. System executes
entire care process. Zero copays, zero deductibles, zero out-of-pocket."

Constitution: "Pre-authorization routing, appointment scheduling coordination,
care navigation, referral management, real-time eligibility verification,
post-care follow-up feeding F4 provider scoring. All actions logged
immutably, feeding F8."
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/care", tags=["F9 Care Execution Coordination"])


# ── Request / Response Models ───────────────────────────────────────────────


class PreAuthRequest(BaseModel):
    """Pre-authorization routing request."""
    claim_id: str
    service_code: str
    benefit_type: str  # health, dental, vision, mental_health, life, std, ltd
    patient_history: dict  # age, sex, diagnoses, risk_factors, medications, symptoms, condition


class CareNavigationRequest(BaseModel):
    """Care navigation request — guide employee through benefit options."""
    employee_id: str
    condition: str
    benefit_type: str  # health, dental, vision, mental_health, life, std, ltd


class ReferralRequest(BaseModel):
    """Referral management request."""
    employee_id: str
    referring_provider_id: str
    specialist_type: str  # orthopedic, cardiology, psychiatry, etc.
    condition: str


class EligibilityParams(BaseModel):
    """Eligibility verification query parameters."""
    service_code: str
    benefit_type: str


class FollowUpRequest(BaseModel):
    """Schedule post-care follow-up."""
    determination_id: str
    followup_type: str  # outcome_check, symptom_review, medication_review, etc.
    days_out: int  # Number of days from now to schedule the follow-up


class CareOutcomeRequest(BaseModel):
    """Record care outcome — feeds back to F4 provider scoring."""
    determination_id: str
    resolved: bool
    clinical_criteria: dict  # condition, resolution_criteria, notes


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/preauth")
def preauthorization(
    request: PreAuthRequest,
    db: Session = Depends(get_db),
):
    """Pre-authorization routing — auto-approve if F1 clinical engine approves.

    Constitution F9: "Pre-authorization routing — automated approval for
    guideline-matched services."

    Process:
    1. Submits to F1 Clinical Quality Engine for determination
    2. Auto-approves if F1 approves (zero delay for guideline-matched services)
    3. Queues for clinical review if F1 does not approve
    4. All actions logged immutably, feeding F8
    """
    from app.services.care_execution import route_preauthorization

    result = route_preauthorization(
        db=db,
        claim_id=request.claim_id,
        service_code=request.service_code,
        benefit_type=request.benefit_type,
        patient_history=request.patient_history,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return result


@router.post("/navigate")
def care_navigation(
    request: CareNavigationRequest,
    db: Session = Depends(get_db),
):
    """Care navigation — guide employees through benefit options across all 7 types.

    Constitution F9: "Care navigation — guide employees through benefit
    options across all benefit types."

    Returns a recommended care path with provider recommendations (F4),
    pricing estimates (F2), and cross-benefit opportunities.
    """
    import uuid as uuid_mod
    from app.services.care_execution import navigate_care

    try:
        employee_uuid = uuid_mod.UUID(request.employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = navigate_care(
        db=db,
        employee_id=employee_uuid,
        condition=request.condition,
        benefit_type=request.benefit_type,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@router.post("/referral")
def referral_management(
    request: ReferralRequest,
    db: Session = Depends(get_db),
):
    """Referral management — create referral, find specialist via F4.

    Constitution F9: "Referral management — track specialist referrals.
    If a referral is generated, the chain continues automatically."

    Creates a referral, finds the best specialist via F4 provider selection,
    auto-schedules the appointment, and tracks the entire chain.
    """
    import uuid as uuid_mod
    from app.services.care_execution import manage_referral

    try:
        employee_uuid = uuid_mod.UUID(request.employee_id)
        provider_uuid = uuid_mod.UUID(request.referring_provider_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID format for employee_id or referring_provider_id")

    result = manage_referral(
        db=db,
        employee_id=employee_uuid,
        referring_provider_id=provider_uuid,
        specialist_type=request.specialist_type,
        condition=request.condition,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@router.get("/eligibility/{employee_id}")
def eligibility_check(
    employee_id: str,
    service_code: str,
    benefit_type: str,
    db: Session = Depends(get_db),
):
    """Real-time eligibility verification.

    Constitution F9: "Real-time eligibility verification — check against
    employer plan."

    Verifies employee enrollment status, employer plan coverage, benefit
    type validity, and waiting period compliance.
    """
    import uuid as uuid_mod
    from app.services.care_execution import verify_eligibility

    try:
        employee_uuid = uuid_mod.UUID(employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = verify_eligibility(
        db=db,
        employee_id=employee_uuid,
        service_code=service_code,
        benefit_type=benefit_type,
    )

    return result


@router.post("/followup")
def schedule_follow_up(
    request: FollowUpRequest,
    db: Session = Depends(get_db),
):
    """Schedule post-care follow-up — outcome tracking feeds back to F4.

    Constitution F9: "Post-care follow-up — outcome tracking feeds back
    to F4 provider scoring."

    Schedules a follow-up check at a specified number of days after the
    clinical determination, to verify whether the care was effective.
    """
    from app.services.care_execution import schedule_followup

    result = schedule_followup(
        db=db,
        determination_id=request.determination_id,
        followup_type=request.followup_type,
        days_out=request.days_out,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@router.post("/outcome")
def record_outcome(
    request: CareOutcomeRequest,
    db: Session = Depends(get_db),
):
    """Record care outcome — feeds back to F4 provider scoring.

    Constitution F9: "Post-care follow-up — outcome tracking feeds back
    to F4 provider scoring."

    Records whether care was clinically successful and updates the
    provider's quality score via F4. Uses clinical resolution criteria
    from peer-reviewed literature, not satisfaction surveys.
    """
    from app.services.care_execution import record_care_outcome

    result = record_care_outcome(
        db=db,
        determination_id=request.determination_id,
        resolved=request.resolved,
        clinical_criteria=request.clinical_criteria,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@router.get("/status/{employee_id}")
def employee_care_status(
    employee_id: str,
    db: Session = Depends(get_db),
):
    """Get all active care episodes for an employee.

    Returns a consolidated view of all open and recently resolved care
    episodes, including referrals, follow-ups, and scheduling status.
    """
    import uuid as uuid_mod
    from app.services.care_execution import get_employee_care_status

    try:
        employee_uuid = uuid_mod.UUID(employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = get_employee_care_status(
        db=db,
        employee_id=employee_uuid,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result
