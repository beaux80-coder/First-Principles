"""Benefits Administration API (Function 11).

Constitution: "Standard benefits admin: enrollment, eligibility, plan
configuration, COBRA, life events. Must work for all 7 benefit types."

Endpoints:
  POST /admin/enroll                     -- Employee enrollment
  POST /admin/life-event                 -- Life event processing
  POST /admin/cobra                      -- COBRA management
  POST /admin/plan-config                -- Plan configuration
  GET  /admin/enrollment/{employer_id}   -- Enrollment summary
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/admin", tags=["F11 Benefits Administration"])


# ── Request Models ───────────────────────────────────────────────────────────


class EnrollRequest(BaseModel):
    """Employee enrollment request."""
    employer_id: str = Field(description="Employer UUID")
    employee_data: dict = Field(
        description=(
            "Employee information: first_name, last_name, date_of_birth, "
            "zip_code, dependents"
        )
    )
    benefit_elections: dict = Field(
        description=(
            "Benefit elections by type: {health: {elected: true}, "
            "dental: {elected: true}, ...}"
        )
    )


class LifeEventRequest(BaseModel):
    """Life event processing request."""
    employee_id: str = Field(description="Employee UUID")
    event_type: str = Field(
        description=(
            "Type of life event: marriage, birth, adoption, divorce, "
            "death_of_dependent, loss_of_coverage, relocation, "
            "employment_status_change"
        )
    )
    event_date: str = Field(description="Date of the event (ISO 8601)")
    changes: dict = Field(
        description=(
            "Requested changes: add_dependents, remove_dependents, "
            "benefit_changes"
        )
    )


class CobraRequest(BaseModel):
    """COBRA management request."""
    employee_id: str = Field(description="Employee UUID")
    qualifying_event: dict = Field(
        description=(
            "Qualifying event details: event_type, event_date, "
            "covered_individuals"
        )
    )


class PlanConfigRequest(BaseModel):
    """Plan configuration request — administrative parameters only.

    The Beneflex coverage policy is universal. Employers cannot toggle
    individual benefit types, add per-plan exclusions, or change the
    scope of covered services. This endpoint accepts only administrative
    parameters (plan year, waiting period, eligibility rules). Any
    attempt to pass coverage-scoping keys (benefit_types_enabled,
    coverage_exclusions, fertility_coverage, etc.) is rejected with a
    400 error.
    """
    employer_id: str = Field(description="Employer UUID")
    plan_config: dict = Field(
        description=(
            "Administrative plan configuration. Supported keys: "
            "plan_year_start (str), waiting_period_days (int), "
            "eligibility_rules (dict with min_hours_per_week and "
            "employee_classes). Coverage scope is the universal "
            "policy and cannot be configured here."
        )
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/enroll")
def enroll_employee(request: EnrollRequest, db: Session = Depends(get_db)):
    """Employee enrollment with elections across all 7 benefit types.

    Constitution: "Standard benefits admin: enrollment. Must work for
    all 7 benefit types."

    Enrolls an employee with benefit elections. All 7 benefit types
    (health, dental, vision, life, STD, LTD, mental health) are
    available from day one. Zero employee cost-sharing.
    """
    from app.services.benefits_admin import enroll_employee as _enroll

    try:
        return _enroll(
            db,
            employer_id=request.employer_id,
            employee_data=request.employee_data,
            benefit_elections=request.benefit_elections,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/life-event")
def process_life_event(request: LifeEventRequest, db: Session = Depends(get_db)):
    """Process a qualifying life event (marriage, birth, etc.).

    Constitution: "Standard benefits admin: life events."

    Life events allow mid-year benefit election changes within the
    qualifying period. Changes can apply to any of the 7 benefit types.
    """
    from app.services.benefits_admin import process_life_event as _process

    try:
        return _process(
            db,
            employee_id=request.employee_id,
            event_type=request.event_type,
            event_date=request.event_date,
            changes=request.changes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/cobra")
def manage_cobra(request: CobraRequest, db: Session = Depends(get_db)):
    """COBRA continuation management.

    Constitution: "Standard benefits admin: COBRA."

    Manages COBRA continuation coverage for employees and dependents
    who experience a qualifying event. Continues all 7 benefit types.
    """
    from app.services.benefits_admin import manage_cobra as _cobra

    try:
        return _cobra(
            db,
            employee_id=request.employee_id,
            qualifying_event=request.qualifying_event,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/plan-config")
def configure_plan(request: PlanConfigRequest, db: Session = Depends(get_db)):
    """Employer plan configuration — administrative parameters only.

    Configure administrative plan parameters: plan year start,
    waiting period, and eligibility rules (min hours per week and
    employee classes).

    Coverage scope is NOT configurable. Every employer is bound by
    the universal Beneflex coverage policy: all services that are
    medically necessary per evidence-based clinical guidelines are
    covered, with only two narrow exclusions (cosmetic with no
    medical indication, experimental with no evidence base). The
    response body includes the full universal policy statement for
    transparency.
    """
    from app.services.benefits_admin import configure_plan as _configure

    try:
        return _configure(
            db,
            employer_id=request.employer_id,
            plan_config=request.plan_config,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/export/{employer_id}")
def export_employer_data(employer_id: str, db: Session = Depends(get_db)):
    """Export all employer data — employers own their raw data.

    Constitution F11: "Employers own their raw data and may export it."

    Returns all data for the specified employer in structured JSON:
    - Claims and determinations
    - Care episodes
    - Enrollment data
    - No PII from other employers included (strict employer_id filtering)
    """
    from app.services.benefits_admin import export_employer_data as _export

    try:
        return _export(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/enrollment/{employer_id}")
def get_enrollment_summary(employer_id: str, db: Session = Depends(get_db)):
    """Enrollment summary and statistics.

    Constitution: "Standard benefits admin: enrollment, eligibility."

    Returns enrollment counts, status breakdown, and benefit type
    participation rates for an employer.
    """
    from app.services.benefits_admin import get_enrollment_summary as _summary

    try:
        return _summary(db, employer_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
