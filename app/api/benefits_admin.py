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
    """Plan configuration request."""
    employer_id: str = Field(description="Employer UUID")
    plan_config: dict = Field(
        description=(
            "Plan configuration: benefit_types_enabled, plan_year_start, "
            "waiting_period_days, eligibility_rules, contribution_strategy"
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
    """Employer plan configuration.

    Constitution: "Standard benefits admin: plan configuration."

    Configure benefit plan parameters including enabled benefit types,
    plan year, waiting periods, and eligibility rules.
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


# -- Payroll Integration -----------------------------------------------------


class PayrollConnectRequest(BaseModel):
    """Request to connect employer payroll/HRIS integration."""
    employer_id: str
    integration_type: str = Field(
        description="Payroll system: adp, workday, paychex, csv"
    )
    config: dict = Field(
        default_factory=dict,
        description="Integration-specific config (credentials, company code, etc.)",
    )


@router.post("/payroll/connect")
def connect_payroll(
    request: PayrollConnectRequest,
    db: Session = Depends(get_db),
):
    """Connect to employer's payroll/HRIS and sync employees.

    Constitution F11: "Integrates with employer's payroll or HRIS via API.
    New hires auto-enrolled upon detection."
    """
    import uuid as uuid_mod
    from app.services.payroll_integration import PayrollIntegrationManager

    try:
        employer_uuid = uuid_mod.UUID(request.employer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employer_id format")

    manager = PayrollIntegrationManager()

    try:
        result = manager.sync_and_enroll(
            db,
            employer_id=employer_uuid,
            integration_type=request.integration_type,
            config=request.config,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


# -- Employee Q&A ------------------------------------------------------------


class EmployeeQARequest(BaseModel):
    """Employee question in plain language."""
    employee_id: str
    question: str


@router.post("/qa")
def employee_qa(
    request: EmployeeQARequest,
    db: Session = Depends(get_db),
):
    """Answer employee benefits question in plain language.

    Constitution: "Employees ask in plain language. System answers
    immediately with full context on the employee's history and plan
    terms. Not a chatbot reading an FAQ."
    """
    import uuid as uuid_mod
    from app.services.employee_qa import answer_employee_question

    try:
        employee_uuid = uuid_mod.UUID(request.employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = answer_employee_question(
        db,
        employee_id=employee_uuid,
        question=request.question,
    )

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


# -- Shadow Admin Burden Display ---------------------------------------------


@router.get("/shadow-burden/{employer_id}")
def shadow_admin_burden(
    employer_id: str,
    db: Session = Depends(get_db),
):
    """Display current admin burden and projected savings upon activation.

    Constitution: "Zero cost, zero risk, zero disruption." Shows shadow
    mode employers what admin burden disappears when beneflex activates.
    """
    import uuid as uuid_mod
    from app.services.benefits_admin import display_shadow_admin_burden

    try:
        employer_uuid = uuid_mod.UUID(employer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employer_id format")

    try:
        result = display_shadow_admin_burden(db, employer_id=employer_uuid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result


# -- Welcome Message ---------------------------------------------------------


@router.post("/welcome/{employee_id}")
def send_welcome(
    employee_id: str,
    db: Session = Depends(get_db),
):
    """Send welcome message to newly enrolled employee.

    Constitution F11: "Welcome to [company]. Your health, dental, vision,
    and all other benefits are active now. When you need care, text this
    number. Everything is covered. You will never receive a bill."
    """
    import uuid as uuid_mod
    from app.services.benefits_admin import send_welcome_message

    try:
        employee_uuid = uuid_mod.UUID(employee_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employee_id format")

    result = send_welcome_message(db, employee_id=employee_uuid)

    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result
