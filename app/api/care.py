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


@router.get("/overdue")
def get_overdue_episodes(
    days_threshold: int = 14,
    db: Session = Depends(get_db),
):
    """Proactive issue tracking — find all overdue care episodes.

    Constitution F9: "Tracks resolution status of every open issue.
    Proactively follows up on missed appointments and overdue results."

    Returns all open/scheduled episodes that need follow-up:
    - Episodes open longer than `days_threshold` without resolution
    - Missed appointments not yet rescheduled
    - Episodes with no follow-up activity in the last 7 days

    This powers the proactive follow-up system that ensures no employee
    care issue falls through the cracks.
    """
    from app.services.care_execution import find_overdue_episodes

    result = find_overdue_episodes(db=db, days_threshold=days_threshold)
    return result


@router.post("/depart/{employee_id}")
def departing_employee_recommendation(
    employee_id: str,
    db: Session = Depends(get_db),
):
    """Departing employee recommendation path.

    Constitution F9: "When an employee leaves an employer on the platform,
    the system provides a simple mechanism for the employee to recommend
    the product to their new employer. The mechanism includes anonymized
    statistics from the employee's own care experience and links to the
    benchmark tool (Function 6A)."

    Returns a shareable recommendation with:
    - Anonymized stats (episodes managed, time saved, OOP=$0)
    - Link to benchmark tool (/benchmark/)
    - No PII
    """
    import uuid as uuid_mod
    from app.services.care_execution import generate_departure_recommendation

    try:
        employee_uuid = uuid_mod.UUID(employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = generate_departure_recommendation(db=db, employee_id=employee_uuid)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Build the shareable recommendation with anonymized stats and benchmark link.
    # Strip any fields that could contain PII — return only aggregate stats.
    experience = result.get("verified_experience_data", {})
    shareable = {
        "recommendation_id": result.get("recommendation_id"),
        "generated_at": result.get("generated_at"),
        "anonymized_care_stats": {
            "episodes_managed": experience.get("total_care_episodes", 0),
            "episodes_resolved": experience.get("resolved_episodes", 0),
            "resolution_rate": experience.get("resolution_rate"),
            "avg_resolution_days": experience.get("avg_resolution_days"),
            "benefit_types_used_count": len(experience.get("benefit_types_used", [])),
            "unique_conditions_treated": experience.get("unique_conditions_treated", 0),
            "total_out_of_pocket": experience.get("total_employee_out_of_pocket", 0.0),
            "time_saved_note": (
                "Zero employee admin burden: scheduling, referrals, prescriptions, "
                "and payments handled automatically by the system."
            ),
        },
        "system_performance_summary": {
            "zero_copays": True,
            "zero_deductibles": True,
            "zero_out_of_pocket": True,
            "auto_scheduling": True,
            "auto_referral_chaining": result.get("system_performance", {}).get(
                "auto_referral_chaining_used", False
            ),
        },
        "benchmark_link": {
            "url": "/api/v1/benchmark/",
            "description": (
                "Use the benchmark tool (Function 6A) to compare your current "
                "benefits costs against the First Principles platform. See "
                "projected savings with zero employee cost-sharing."
            ),
            "action": "Share this link with your new employer's HR or benefits team.",
        },
        "shareable": True,
        "contains_pii": False,
        "message": (
            "This recommendation is based on verified clinical outcomes, not "
            "satisfaction surveys. All statistics are anonymized. Share the "
            "benchmark link with your new employer to see projected savings."
        ),
    }

    return shareable


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


# ── F9: Prescription routing to lowest-price pharmacy ─────────────────────


class PrescriptionRoutingRequest(BaseModel):
    """Route a prescription to the lowest-price pharmacy channel."""
    employee_id: str
    drug_name: str
    quantity: int = 30
    state: Optional[str] = None


@router.post("/prescription")
def route_prescription_endpoint(
    request: PrescriptionRoutingRequest,
    db: Session = Depends(get_db),
):
    """Route prescription to lowest-price pharmacy via F2 price discovery.

    Constitution: "If prescription generated: system identifies lowest-price
    channel via Function 2, routes prescription, notifies employee of
    pickup/delivery."

    Process:
    1. Queries NADAC data in price_data table for the drug
    2. Compares against all pharmacy channels (NADAC, GoodRx, ASP)
    3. Selects the cheapest pharmacy in the employee's state
    4. Returns: selected pharmacy, price, drug info, pickup instructions
    """
    import uuid as uuid_mod
    from app.services.care_execution import route_prescription_to_pharmacy

    try:
        employee_uuid = uuid_mod.UUID(request.employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = route_prescription_to_pharmacy(
        db=db,
        employee_id=employee_uuid,
        drug_name=request.drug_name,
        quantity=request.quantity,
        state=request.state,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


# ── F9: Automatic referral/imaging/lab chain ──────────────────────────────


class ReferralChainRequest(BaseModel):
    """Trigger automatic referral chain processing."""
    episode_id: str
    referral_type: str  # imaging, lab, specialist, follow_up
    referral_reason: str


@router.post("/chain")
def referral_chain_endpoint(
    request: ReferralChainRequest,
    db: Session = Depends(get_db),
):
    """Automatic referral/imaging/lab chain processing.

    Constitution: "If visit results in referral, imaging, lab, or follow-up:
    system automatically selects optimal follow-up provider/facility,
    schedules it, notifies employee, transmits clinical information.
    Chain continues until issue resolved."

    Creates a new linked care episode, selects provider via F4,
    auto-schedules, and returns the full episode chain.
    """
    import uuid as uuid_mod
    from app.services.care_execution import process_referral_chain

    try:
        episode_uuid = uuid_mod.UUID(request.episode_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid episode_id format")

    result = process_referral_chain(
        db=db,
        episode_id=episode_uuid,
        referral_type=request.referral_type,
        referral_reason=request.referral_reason,
    )

    if "error" in result:
        raise HTTPException(
            status_code=400 if "Invalid referral_type" in result.get("error", "") else 404,
            detail=result["error"],
        )

    return result


# ── F9: FHIR clinical context generation ──────────────────────────────────


@router.get("/fhir/{episode_id}")
def fhir_bundle_endpoint(
    episode_id: str,
    db: Session = Depends(get_db),
):
    """Generate FHIR R4 Bundle for clinical context transmission.

    Constitution: "Transmits medical history and clinical context to provider
    in advance" and "Is clinical context transmitted using every method
    physically available and legally permitted?"

    Returns a FHIR R4 Bundle containing:
    - Patient resource (demographics from employee)
    - Condition resource (from the care episode)
    - MedicationStatement (if any prescription history)
    - AllergyIntolerance (if in history)
    """
    import uuid as uuid_mod
    from app.services.care_execution import generate_fhir_bundle

    try:
        episode_uuid = uuid_mod.UUID(episode_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid episode_id format")

    result = generate_fhir_bundle(
        db=db,
        episode_id=episode_uuid,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


# -- F9: Reactivate departing employee recommendation -----------------------


class ReactivateRequest(BaseModel):
    """Request to reactivate a departing employee recommendation."""
    employee_id: str
    new_employer_name: str


@router.post("/reactivate")
def reactivate_recommendation(
    request: ReactivateRequest,
    db: Session = Depends(get_db),
):
    """Reactivate departing employee recommendation. F9 Q10.

    Constitution F9: generate a portable recommendation for a former
    employee to share with their new employer, including anonymized
    care statistics and a link to the benchmark tool.
    """
    import uuid as uuid_mod
    from app.services.care_execution import reactivate_recommendation as _reactivate

    try:
        employee_uuid = uuid_mod.UUID(request.employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = _reactivate(
        db,
        former_employee_id=employee_uuid,
        new_employer_name=request.new_employer_name,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result
