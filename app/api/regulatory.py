"""Regulatory Filing API (Function 11).

Constitution: "Generates, files, and maintains all required regulatory documents."
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/regulatory", tags=["F11 Regulatory Filing"])


# -- Request Models ----------------------------------------------------------


class Form5500Request(BaseModel):
    """Request to generate Form 5500."""
    plan_year: int


class ACAFormsRequest(BaseModel):
    """Request to generate ACA 1094-C/1095-C forms."""
    tax_year: int


# -- Endpoints ---------------------------------------------------------------


@router.post("/form5500/{employer_id}")
def generate_form_5500(
    employer_id: str,
    request: Form5500Request,
    db: Session = Depends(get_db),
):
    """Generate DOL Form 5500 annual reporting data.

    Constitution F11: "Generates, files, and maintains all required
    regulatory documents" including Form 5500 annual reporting.
    """
    import uuid as uuid_mod
    from app.services.regulatory_filing import generate_form_5500 as _generate

    try:
        employer_uuid = uuid_mod.UUID(employer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employer_id format")

    try:
        result = _generate(db, employer_id=employer_uuid, plan_year=request.plan_year)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result


@router.post("/aca-forms/{employer_id}")
def generate_aca_forms(
    employer_id: str,
    request: ACAFormsRequest,
    db: Session = Depends(get_db),
):
    """Generate ACA 1094-C transmittal and 1095-C employee forms.

    Constitution F11: "ACA reporting (1094-C, 1095-C)" generation
    for IRS compliance.
    """
    import uuid as uuid_mod
    from app.services.regulatory_filing import (
        generate_aca_1094c,
        generate_aca_1095c,
    )
    from app.models.employee import Employee, EmployeeStatus

    try:
        employer_uuid = uuid_mod.UUID(employer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employer_id format")

    try:
        transmittal = generate_aca_1094c(
            db, employer_id=employer_uuid, tax_year=request.tax_year
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Generate individual 1095-C for each active employee
    employees = (
        db.query(Employee)
        .filter(
            Employee.employer_id == employer_uuid,
            Employee.status == EmployeeStatus.active,
        )
        .all()
    )

    individual_forms = []
    for emp in employees:
        try:
            form = generate_aca_1095c(
                db, employee_id=emp.employee_id, tax_year=request.tax_year
            )
            individual_forms.append(form)
        except ValueError:
            continue

    return {
        "employer_id": employer_id,
        "tax_year": request.tax_year,
        "transmittal_1094c": transmittal,
        "individual_1095c_count": len(individual_forms),
        "individual_1095c_forms": individual_forms,
    }


@router.get("/compliance/{employer_id}")
def run_compliance_checklist(
    employer_id: str,
    db: Session = Depends(get_db),
):
    """Run full compliance check across ACA, ERISA, HIPAA, and state requirements.

    Constitution F11: "Generates, files, and maintains all required
    regulatory documents. Continuously monitors regulatory changes."
    """
    import uuid as uuid_mod
    from app.services.regulatory_filing import run_compliance_checklist as _check

    try:
        employer_uuid = uuid_mod.UUID(employer_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid employer_id format")

    try:
        result = _check(db, employer_id=employer_uuid)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return result


@router.get("/monitor")
def monitor_regulatory_changes(
    db: Session = Depends(get_db),
):
    """Monitor Federal Register for ERISA/ACA/HIPAA rule changes.

    Constitution F11: "Continuously monitors regulatory changes."
    Checks the Federal Register API and returns recent relevant updates.
    """
    from app.services.regulatory_filing import check_regulatory_updates

    return check_regulatory_updates(db)


@router.get("/tpa-licenses")
def get_tpa_license_status():
    """Get TPA license status across all tracked states.

    Constitution F11: "State-mandated filings." Tracks TPA licensing
    requirements and renewal deadlines for every state where the
    platform operates.
    """
    from app.services.regulatory_filing import get_all_license_statuses

    return get_all_license_statuses()
