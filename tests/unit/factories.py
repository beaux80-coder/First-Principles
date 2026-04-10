"""Factory helpers for orchestrator-engine tests.

Builds realistic ORM instances for Employer, Employee, Provider, Service,
CareEpisode, Claim, and ClinicalGuideline. Each factory returns a flushed
row so subsequent relationships can reference its primary key.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, UTC, timedelta
from typing import Optional


def make_employer(db, name="TestCo", status="active"):
    from app.models.employer import Employer, EmployerStatus
    row = Employer(name=name, status=EmployerStatus(status))
    db.add(row)
    db.flush()
    return row


def make_employee(
    db,
    employer,
    *,
    age: int = 45,
    sex: str = "female",
    status: str = "active",
    enrolled_days_ago: int = 60,
    risk_factors: Optional[list[str]] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    terminated_days_ago: Optional[int] = None,
    zip_code: Optional[str] = None,
    race: Optional[str] = None,
):
    """Create an Employee with demographics stored as encrypted JSON.

    `race` is never read by the engine but is recorded for fairness-test
    bookkeeping — tests can group synthetic populations on it without
    leaking the attribute into the decision path.
    """
    from app.models.employee import Employee, EmployeeStatus
    now = datetime.now(UTC)
    # Exact age: step back `age` calendar years, not `age*365` days, so
    # the preventive-scheduler's year-based age computation agrees with
    # what the caller asked for.
    try:
        dob = now.replace(year=now.year - age)
    except ValueError:
        # Feb 29 birthday in a non-leap-year target — step back one day.
        dob = now.replace(year=now.year - age, day=28)
    demographics: dict = {
        "age": age,
        "sex": sex,
        "risk_factors": risk_factors or [],
        "date_of_birth": dob.date().isoformat(),
    }
    if lat is not None and lon is not None:
        demographics["latitude"] = lat
        demographics["longitude"] = lon
    if zip_code:
        demographics["zip_code"] = zip_code
    if race:
        demographics["race"] = race

    row = Employee(
        employer_id=employer.employer_id,
        status=EmployeeStatus(status),
        enrolled_at=now - timedelta(days=enrolled_days_ago),
        terminated_at=(
            now - timedelta(days=terminated_days_ago)
            if terminated_days_ago is not None
            else None
        ),
        demographics_encrypted=json.dumps(demographics),
    )
    db.add(row)
    db.flush()
    return row


def make_provider(
    db,
    name,
    *,
    quality_score: float = 85.0,
    outcome_data_points: int = 100,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    provider_type: str = "physician",
    state: str = "ID",
    specialties: Optional[list[str]] = None,
):
    from app.models.provider import Provider, ProviderType
    row = Provider(
        npi=str(uuid.uuid4().int)[:10],
        name=name,
        provider_type=ProviderType(provider_type),
        specialties=specialties,
        state=state,
        latitude=lat,
        longitude=lon,
        quality_score=quality_score,
        outcome_data_points=outcome_data_points,
    )
    db.add(row)
    db.flush()
    return row


def make_service(db, code: str, benefit_type: str = "health", description: str = ""):
    from app.models.service import Service, BenefitType, CodeType
    ct = CodeType.cdt if code.startswith("D") else (
        CodeType.hcpcs if code[0].isalpha() else CodeType.cpt
    )
    row = Service(
        code=code,
        code_type=ct,
        benefit_type=BenefitType(benefit_type),
        description=description or f"Service {code}",
    )
    db.add(row)
    db.flush()
    return row


def make_care_episode(
    db,
    employee,
    *,
    condition: str = "general",
    benefit_type: str = "health",
    status: str = "scheduled",
    is_preventive: bool = False,
    preventive_rule_id: Optional[str] = None,
    provider_id: Optional[uuid.UUID] = None,
    expected_price: Optional[float] = None,
    clinical_urgency: str = "routine",
    created_days_ago: int = 2,
    issue_description: str = "Routine care",
):
    from app.models.care_episode import CareEpisode, EpisodeStatus
    from app.models.service import BenefitType
    created_at = datetime.now(UTC) - timedelta(days=created_days_ago)
    routing_decision = {
        "selected_provider_id": str(provider_id) if provider_id else None,
        "expected_price": expected_price,
        "clinical_urgency": clinical_urgency,
        "routed_at": created_at.isoformat(),
    }
    row = CareEpisode(
        employee_id=employee.employee_id,
        benefit_type=BenefitType(benefit_type),
        status=EpisodeStatus(status),
        issue_description=issue_description,
        interpreted_condition=condition,
        interpreted_benefit_type=benefit_type,
        provider_id=provider_id,
        routing_decision=routing_decision,
        is_preventive=is_preventive,
        preventive_rule_id=preventive_rule_id,
        created_at=created_at,
        last_updated_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def make_claim(
    db,
    employee,
    *,
    amount: float = 150.00,
    benefit_type: str = "health",
    care_episode_id: Optional[uuid.UUID] = None,
    routing_expected_price: Optional[float] = None,
    provider=None,
    service=None,
    days_ago: int = 1,
    coding_validation: Optional[dict] = None,
    date_of_service: Optional[datetime] = None,
):
    from app.models.claim import Claim, ClaimMode, ClaimStatus
    from app.models.service import BenefitType
    submitted = datetime.now(UTC) - timedelta(days=days_ago)
    if date_of_service is None:
        date_of_service = submitted
    row = Claim(
        employer_id=employee.employer_id,
        employee_id=employee.employee_id,
        provider_id=provider.provider_id if provider else None,
        service_id=service.service_id if service else None,
        benefit_type=BenefitType(benefit_type),
        mode=ClaimMode.live,
        status=ClaimStatus.submitted,
        amount_billed=amount,
        submitted_at=submitted,
        date_of_service=date_of_service,
        care_episode_id=care_episode_id,
        routing_expected_price=routing_expected_price,
        coding_validation=coding_validation,
    )
    db.add(row)
    db.flush()
    return row


def link_claim_to_episode(db, claim, episode):
    """Set both sides of the claim <-> episode routing linkage."""
    claim.care_episode_id = episode.episode_id
    if episode.routing_decision and "expected_price" in episode.routing_decision:
        claim.routing_expected_price = episode.routing_decision["expected_price"]
    db.flush()
    return claim
