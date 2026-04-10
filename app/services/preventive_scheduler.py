"""Proactive Preventive Care Scheduler — Layer 1 of the Care Orchestrator.

Constitution: The engine optimizes for employee health + cost effectiveness.
The single biggest lever for BOTH is prevention: catching disease before it
becomes expensive to treat, and delivering services the ACA already requires
us to cover at 100% (USPSTF A/B, ACIP immunizations, HRSA preventive care).

This module continuously scans the active employee population and creates
`CareEpisode` records with `is_preventive=True` for any due preventive
service. The existing Layer 2 care routing machinery then schedules the
visit with a cost-optimized provider and notifies the employee.

The ruleset is a curated subset of the highest-impact USPSTF Grade A/B
recommendations. The list is designed to be expanded over time from the
existing `clinical_guidelines_ingester.py` pipeline.

Legal basis:
  • ACA §2713 requires coverage of USPSTF A/B rated services at 100% with
    no cost-sharing. Proactively scheduling them guarantees compliance.
  • HRSA-supported preventive care for women must be covered the same way.
  • ACIP immunizations are covered under the same rule.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.care_episode import CareEpisode, EpisodeStatus
from app.models.claim import Claim, ClaimStatus
from app.models.employee import Employee, EmployeeStatus
from app.models.service import BenefitType


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreventiveRule:
    """A single preventive care recommendation.

    Encodes a USPSTF or ACIP recommendation as executable data:
      - rule_id: stable identifier used in CareEpisode.preventive_rule_id
      - service: human-readable service name
      - condition: short label for the NLP / routing engines
      - cpt_code: billing code to use when the episode is created
      - benefit_type: maps to the BenefitType enum
      - min_age / max_age: eligibility window
      - sex: "any" | "female" | "male"
      - frequency_days: how often the service recurs
      - grade: USPSTF grade (A or B)
      - source: citation string written into the episode audit trail
    """
    rule_id: str
    service: str
    condition: str
    cpt_code: Optional[str]
    benefit_type: str
    min_age: int
    max_age: int
    sex: str
    frequency_days: int
    grade: str
    source: str
    risk_factor_triggers: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Curated USPSTF Grade A/B preventive care ruleset
#
# This is the MVP set. The full USPSTF catalog has ~50+ A/B recommendations;
# we start with the highest-impact ones and expand as the ingester feeds
# the ClinicalGuideline table.
# ---------------------------------------------------------------------------
USPSTF_PREVENTIVE_RULES: tuple[PreventiveRule, ...] = (
    PreventiveRule(
        rule_id="uspstf_colorectal_screening",
        service="Colorectal cancer screening",
        condition="colorectal_cancer_screening",
        cpt_code="45378",  # Diagnostic colonoscopy — placeholder, actual
                           # code depends on the modality chosen by routing
        benefit_type="health",
        min_age=45, max_age=75, sex="any",
        frequency_days=365 * 10,  # colonoscopy every 10 years
        grade="A",
        source="USPSTF 2021: Screening for Colorectal Cancer",
    ),
    PreventiveRule(
        rule_id="uspstf_mammography",
        service="Breast cancer screening (mammography)",
        condition="breast_cancer_screening",
        cpt_code="77067",
        benefit_type="health",
        min_age=40, max_age=74, sex="female",
        frequency_days=365 * 2,
        grade="B",
        source="USPSTF 2024: Screening for Breast Cancer",
    ),
    PreventiveRule(
        rule_id="uspstf_cervical_screening",
        service="Cervical cancer screening",
        condition="cervical_cancer_screening",
        cpt_code="88175",
        benefit_type="health",
        min_age=21, max_age=65, sex="female",
        frequency_days=365 * 3,
        grade="A",
        source="USPSTF 2018: Screening for Cervical Cancer",
    ),
    PreventiveRule(
        rule_id="uspstf_lung_cancer_screening",
        service="Lung cancer screening (LDCT)",
        condition="lung_cancer_screening",
        cpt_code="71271",
        benefit_type="health",
        min_age=50, max_age=80, sex="any",
        frequency_days=365,
        grade="B",
        source="USPSTF 2021: Screening for Lung Cancer",
        risk_factor_triggers=("smoking_history",),
    ),
    PreventiveRule(
        rule_id="uspstf_bp_screening",
        service="Blood pressure screening",
        condition="hypertension_screening",
        cpt_code="99401",
        benefit_type="health",
        min_age=18, max_age=120, sex="any",
        frequency_days=365,
        grade="A",
        source="USPSTF 2021: Screening for Hypertension",
    ),
    PreventiveRule(
        rule_id="uspstf_cholesterol_screening",
        service="Statin use for primary prevention — lipid screening",
        condition="cholesterol_screening",
        cpt_code="80061",
        benefit_type="health",
        min_age=40, max_age=75, sex="any",
        frequency_days=365 * 5,
        grade="B",
        source="USPSTF 2022: Statin Use for CVD Prevention in Adults",
    ),
    PreventiveRule(
        rule_id="uspstf_diabetes_screening",
        service="Prediabetes and Type 2 diabetes screening",
        condition="diabetes_screening",
        cpt_code="83036",
        benefit_type="health",
        min_age=35, max_age=70, sex="any",
        frequency_days=365 * 3,
        grade="B",
        source="USPSTF 2021: Screening for Prediabetes and Type 2 Diabetes",
    ),
    PreventiveRule(
        rule_id="uspstf_hepatitis_c",
        service="Hepatitis C screening",
        condition="hepatitis_c_screening",
        cpt_code="86803",
        benefit_type="health",
        min_age=18, max_age=79, sex="any",
        frequency_days=365 * 10,  # once in adult life for most adults
        grade="B",
        source="USPSTF 2020: Screening for Hepatitis C Virus Infection",
    ),
    PreventiveRule(
        rule_id="uspstf_hiv_screening",
        service="HIV screening",
        condition="hiv_screening",
        cpt_code="86703",
        benefit_type="health",
        min_age=15, max_age=65, sex="any",
        frequency_days=365 * 3,
        grade="A",
        source="USPSTF 2019: Screening for HIV Infection",
    ),
    PreventiveRule(
        rule_id="uspstf_depression_screening",
        service="Depression screening",
        condition="depression_screening",
        cpt_code="96127",
        benefit_type="mental_health",
        min_age=12, max_age=120, sex="any",
        frequency_days=365,
        grade="B",
        source="USPSTF 2023: Screening for Depression in Adults",
    ),
    PreventiveRule(
        rule_id="uspstf_obesity_counseling",
        service="Intensive behavioral counseling for obesity",
        condition="obesity_counseling",
        cpt_code="G0447",
        benefit_type="health",
        min_age=18, max_age=120, sex="any",
        frequency_days=365,
        grade="B",
        source="USPSTF 2018: Behavioral Weight Loss Interventions",
    ),
    PreventiveRule(
        rule_id="uspstf_tobacco_cessation",
        service="Tobacco cessation counseling",
        condition="tobacco_cessation",
        cpt_code="99406",
        benefit_type="health",
        min_age=18, max_age=120, sex="any",
        frequency_days=365,
        grade="A",
        source="USPSTF 2021: Tobacco Smoking Cessation in Adults",
        risk_factor_triggers=("tobacco_use",),
    ),
    PreventiveRule(
        rule_id="acip_influenza",
        service="Influenza vaccine",
        condition="influenza_vaccination",
        cpt_code="90658",
        benefit_type="health",
        min_age=6, max_age=120, sex="any",
        frequency_days=365,
        grade="A",
        source="ACIP 2024: Seasonal Influenza Vaccination",
    ),
    PreventiveRule(
        rule_id="acip_tdap",
        service="Tdap / Td booster",
        condition="tetanus_diphtheria_vaccination",
        cpt_code="90715",
        benefit_type="health",
        min_age=11, max_age=120, sex="any",
        frequency_days=365 * 10,
        grade="A",
        source="ACIP 2020: Tetanus, Diphtheria, and Pertussis Vaccination",
    ),
    PreventiveRule(
        rule_id="acip_covid19",
        service="COVID-19 vaccine (updated formulation)",
        condition="covid19_vaccination",
        cpt_code="91318",
        benefit_type="health",
        min_age=6, max_age=120, sex="any",
        frequency_days=365,
        grade="A",
        source="ACIP 2023: COVID-19 Vaccination Recommendations",
    ),
    PreventiveRule(
        rule_id="acip_zoster",
        service="Zoster (shingles) vaccine",
        condition="herpes_zoster_vaccination",
        cpt_code="90750",
        benefit_type="health",
        min_age=50, max_age=120, sex="any",
        frequency_days=365 * 80,  # lifetime, two-dose series
        grade="A",
        source="ACIP 2018: Recombinant Zoster Vaccine",
    ),
    PreventiveRule(
        rule_id="uspstf_osteoporosis_screening",
        service="Osteoporosis screening (DEXA)",
        condition="osteoporosis_screening",
        cpt_code="77080",
        benefit_type="health",
        min_age=65, max_age=120, sex="female",
        frequency_days=365 * 2,
        grade="B",
        source="USPSTF 2018: Screening for Osteoporosis to Prevent Fractures",
    ),
    PreventiveRule(
        rule_id="uspstf_aaa_screening",
        service="Abdominal aortic aneurysm screening",
        condition="aaa_screening",
        cpt_code="76706",
        benefit_type="health",
        min_age=65, max_age=75, sex="male",
        frequency_days=365 * 80,  # one-time
        grade="B",
        source="USPSTF 2019: Screening for Abdominal Aortic Aneurysm",
        risk_factor_triggers=("smoking_history",),
    ),
    PreventiveRule(
        rule_id="uspstf_unhealthy_alcohol",
        service="Unhealthy alcohol use screening and counseling",
        condition="alcohol_use_screening",
        cpt_code="99408",
        benefit_type="health",
        min_age=18, max_age=120, sex="any",
        frequency_days=365,
        grade="B",
        source="USPSTF 2018: Unhealthy Alcohol Use in Adolescents and Adults",
    ),
    PreventiveRule(
        rule_id="uspstf_preventive_dental_cleaning",
        service="Routine dental prophylaxis",
        condition="dental_prophylaxis",
        cpt_code="D1110",
        benefit_type="dental",
        min_age=18, max_age=120, sex="any",
        frequency_days=182,  # twice a year
        grade="B",
        source="ADA Preventive Care: Oral Prophylaxis",
    ),
)


# ---------------------------------------------------------------------------
# Demographics extraction — the Employee model stores demographics as an
# encrypted JSON blob. Extract the fields we need for rule matching.
# ---------------------------------------------------------------------------
def _extract_demographics(employee: Employee) -> dict:
    raw = employee.demographics_encrypted
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _employee_age(employee: Employee, as_of: datetime) -> Optional[int]:
    demographics = _extract_demographics(employee)
    dob_str = (
        demographics.get("date_of_birth")
        or demographics.get("dob")
        or demographics.get("birth_date")
    )
    if dob_str:
        try:
            dob = datetime.fromisoformat(str(dob_str).replace("Z", "+00:00"))
            if dob.tzinfo is None:
                dob = dob.replace(tzinfo=UTC)
            years = as_of.year - dob.year - (
                (as_of.month, as_of.day) < (dob.month, dob.day)
            )
            return max(years, 0)
        except (TypeError, ValueError):
            pass
    # Fallback: caller stored age directly.
    age = demographics.get("age")
    if isinstance(age, (int, float)):
        return int(age)
    return None


def _employee_sex(employee: Employee) -> str:
    demographics = _extract_demographics(employee)
    sex = str(demographics.get("sex", "")).lower() or str(demographics.get("gender", "")).lower()
    if sex in ("f", "female", "woman"):
        return "female"
    if sex in ("m", "male", "man"):
        return "male"
    return "any"


def _employee_risk_factors(employee: Employee) -> set[str]:
    demographics = _extract_demographics(employee)
    raw = demographics.get("risk_factors") or []
    return {str(r).lower() for r in raw}


def _employee_coordinates(employee: Employee) -> tuple[Optional[float], Optional[float]]:
    demographics = _extract_demographics(employee)
    lat = demographics.get("latitude")
    lon = demographics.get("longitude")
    try:
        return (float(lat), float(lon)) if lat is not None and lon is not None else (None, None)
    except (TypeError, ValueError):
        return (None, None)


# ---------------------------------------------------------------------------
# Rule eligibility — does this employee match a given preventive rule?
# ---------------------------------------------------------------------------
def _employee_matches_rule(
    employee: Employee, rule: PreventiveRule, as_of: datetime
) -> bool:
    if employee.status != EmployeeStatus.active:
        return False
    age = _employee_age(employee, as_of)
    if age is None:
        return False
    if age < rule.min_age or age > rule.max_age:
        return False
    if rule.sex != "any" and _employee_sex(employee) != rule.sex:
        return False
    if rule.risk_factor_triggers:
        employee_risks = _employee_risk_factors(employee)
        if not any(rf in employee_risks for rf in rule.risk_factor_triggers):
            return False
    return True


# ---------------------------------------------------------------------------
# Due-date logic — check when this service was last delivered and whether
# it is due again. Combines three signals:
#   1. Prior CareEpisode with the same preventive_rule_id
#   2. Prior claim with the same CPT code (care may have happened off
#      platform before enrollment)
#   3. The rule's frequency window
# ---------------------------------------------------------------------------
def _last_completed(
    db: Session, employee: Employee, rule: PreventiveRule
) -> Optional[datetime]:
    last_episode = (
        db.query(CareEpisode)
        .filter(
            CareEpisode.employee_id == employee.employee_id,
            CareEpisode.preventive_rule_id == rule.rule_id,
            CareEpisode.status.in_([EpisodeStatus.resolved, EpisodeStatus.in_progress, EpisodeStatus.scheduled]),
        )
        .order_by(CareEpisode.created_at.desc())
        .first()
    )
    last_ts: Optional[datetime] = None
    if last_episode is not None:
        last_ts = last_episode.resolved_at or last_episode.created_at

    if rule.cpt_code:
        # Find the most recent paid claim with the matching CPT code.
        last_claim = (
            db.query(Claim)
            .filter(
                Claim.employee_id == employee.employee_id,
                Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
            )
            .order_by(Claim.date_of_service.desc().nullslast(), Claim.submitted_at.desc())
            .first()
        )
        if last_claim is not None:
            claim_service = getattr(last_claim, "service", None)
            claim_cpt = (
                getattr(claim_service, "cpt_code", None)
                or getattr(claim_service, "code", None)
                if claim_service is not None
                else None
            )
            if claim_cpt and str(claim_cpt) == rule.cpt_code:
                claim_ts = last_claim.date_of_service or last_claim.submitted_at
                if last_ts is None or (claim_ts and claim_ts > last_ts):
                    last_ts = claim_ts

    return last_ts


def _is_due(
    db: Session, employee: Employee, rule: PreventiveRule, as_of: datetime
) -> bool:
    # Never schedule the same rule twice at the same time.
    open_episode = (
        db.query(CareEpisode)
        .filter(
            CareEpisode.employee_id == employee.employee_id,
            CareEpisode.preventive_rule_id == rule.rule_id,
            CareEpisode.status.in_(
                [EpisodeStatus.open, EpisodeStatus.scheduled, EpisodeStatus.in_progress]
            ),
        )
        .first()
    )
    if open_episode is not None:
        return False

    last_done = _last_completed(db, employee, rule)
    if last_done is None:
        return True
    # Normalize tz for SQLite-safe comparison.
    if last_done.tzinfo is None:
        last_done = last_done.replace(tzinfo=UTC)
    due_after = last_done + timedelta(days=rule.frequency_days)
    return as_of >= due_after


# ---------------------------------------------------------------------------
# Episode creation — wraps CareEpisode construction with all the fields
# Layer 2 routing needs to pick up the episode and schedule it.
# ---------------------------------------------------------------------------
def _benefit_type_enum(value: str) -> BenefitType:
    try:
        return BenefitType(value)
    except ValueError:
        return BenefitType.health


def _create_preventive_episode(
    db: Session, employee: Employee, rule: PreventiveRule, as_of: datetime
) -> CareEpisode:
    lat, lon = _employee_coordinates(employee)
    episode = CareEpisode(
        employee_id=employee.employee_id,
        benefit_type=_benefit_type_enum(rule.benefit_type),
        status=EpisodeStatus.open,
        issue_description=(
            f"Preventive care due: {rule.service}. "
            f"Auto-scheduled by the preventive scheduler under rule "
            f"'{rule.rule_id}' ({rule.source})."
        ),
        interpreted_condition=rule.condition,
        interpreted_benefit_type=rule.benefit_type,
        nlp_confidence=1.0,  # deterministic — no NLP needed
        employee_actions_required=1,
        resolution_criteria=(
            f"Service {rule.service} completed per {rule.source}."
        ),
        is_preventive=True,
        preventive_rule_id=rule.rule_id,
        created_at=as_of,
        last_updated_at=as_of,
        routing_decision={
            "scheduled_by": "preventive_scheduler",
            "rule_id": rule.rule_id,
            "service": rule.service,
            "cpt_code": rule.cpt_code,
            "grade": rule.grade,
            "source": rule.source,
            "clinical_urgency": "preventive",
            "employee_latitude": lat,
            "employee_longitude": lon,
            "routed_at": as_of.isoformat(),
        },
    )
    db.add(episode)
    db.flush()
    logger.info(
        "Preventive episode created: employee=%s rule=%s service=%s",
        employee.employee_id, rule.rule_id, rule.service,
    )
    return episode


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def find_due_rules(
    db: Session, employee: Employee, as_of: Optional[datetime] = None
) -> list[PreventiveRule]:
    """Return every preventive rule that is currently due for the employee."""
    as_of = as_of or datetime.now(UTC)
    due: list[PreventiveRule] = []
    for rule in USPSTF_PREVENTIVE_RULES:
        if not _employee_matches_rule(employee, rule, as_of):
            continue
        if _is_due(db, employee, rule, as_of):
            due.append(rule)
    return due


def schedule_for_employee(
    db: Session, employee_id: uuid.UUID, as_of: Optional[datetime] = None
) -> dict:
    """Create episodes for every due preventive rule for a single employee."""
    as_of = as_of or datetime.now(UTC)
    employee = db.query(Employee).filter(Employee.employee_id == employee_id).first()
    if employee is None:
        return {"error": "employee_not_found", "employee_id": str(employee_id)}

    due_rules = find_due_rules(db, employee, as_of)
    created: list[dict] = []
    for rule in due_rules:
        episode = _create_preventive_episode(db, employee, rule, as_of)
        created.append({
            "episode_id": str(episode.episode_id),
            "rule_id": rule.rule_id,
            "service": rule.service,
            "grade": rule.grade,
            "source": rule.source,
        })
    db.commit()
    return {
        "employee_id": str(employee_id),
        "as_of": as_of.isoformat(),
        "due_rules_count": len(due_rules),
        "episodes_created": created,
    }


def run_daily_scheduler(db: Session, as_of: Optional[datetime] = None) -> dict:
    """Scan the full active employee population and schedule due preventive care.

    Registered as a daily job on the existing APScheduler worker.
    Returns a summary suitable for logging / dashboards.
    """
    as_of = as_of or datetime.now(UTC)
    active_employees = (
        db.query(Employee).filter(Employee.status == EmployeeStatus.active).all()
    )

    total_episodes_created = 0
    employees_touched = 0
    by_rule: dict[str, int] = {}

    for employee in active_employees:
        due_rules = find_due_rules(db, employee, as_of)
        if not due_rules:
            continue
        employees_touched += 1
        for rule in due_rules:
            _create_preventive_episode(db, employee, rule, as_of)
            total_episodes_created += 1
            by_rule[rule.rule_id] = by_rule.get(rule.rule_id, 0) + 1
    db.commit()

    summary = {
        "run_at": as_of.isoformat(),
        "active_employees_scanned": len(active_employees),
        "employees_with_due_care": employees_touched,
        "episodes_created": total_episodes_created,
        "by_rule": by_rule,
        "layer": "1_preventive",
    }
    logger.info("Preventive scheduler run: %s", summary)
    return summary
