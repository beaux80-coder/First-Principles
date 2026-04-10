"""Tests for the orchestrator-first care engine.

Covers:
  • Quality floor filtering in provider_selection
  • Convenience floor filtering (travel distance + appointment urgency)
  • Emergent care detection (prudent layperson standard)
  • Preventive scheduler rule eligibility and due detection
  • Claims verification (routed / emergent / unrouted / eligibility)
  • Population-health dashboard layers
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, UTC, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# Global test setup — ensure encryption key is available so EncryptedString
# columns (on Employee.demographics_encrypted, etc.) can round-trip.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _ensure_encryption_key():
    os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-orchestrator-tests")
    from app.config import settings as _settings
    if not _settings.encryption_key:
        _settings.encryption_key = "test-key-for-orchestrator-tests"
    yield


# ---------------------------------------------------------------------------
# Shared fixtures — in-memory SQLite with the full schema loaded
# ---------------------------------------------------------------------------
@pytest.fixture
def db_session():
    """In-memory SQLite with all models loaded for a single test."""
    # Import models before creating tables so metadata is populated.
    import app.database as db_module
    from app.database import Base
    from app.models import (  # noqa: F401  (side-effect: model registration)
        employer, employee, service, provider, claim,
        clinical_guideline, clinical_determination, price_data,
        price_comparison, care_episode, audit_log, benchmark_query,
    )

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    # Swap the module-level SessionLocal so code that builds its own
    # session (e.g., subagents) uses this engine.
    original_session_local = db_module.SessionLocal
    db_module.SessionLocal = SessionLocal
    try:
        yield session
    finally:
        session.close()
        db_module.SessionLocal = original_session_local
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _make_employer(db, name="TestCo", status="active"):
    from app.models.employer import Employer, EmployerStatus
    emp = Employer(name=name, status=EmployerStatus(status))
    db.add(emp)
    db.flush()
    return emp


def _make_employee(
    db,
    employer,
    age=45,
    sex="female",
    status="active",
    enrolled_days_ago=60,
    risk_factors=None,
    lat=None,
    lon=None,
    terminated_days_ago=None,
):
    from app.models.employee import Employee, EmployeeStatus
    now = datetime.now(UTC)
    demographics = {
        "age": age,
        "sex": sex,
        "risk_factors": risk_factors or [],
    }
    if lat is not None and lon is not None:
        demographics["latitude"] = lat
        demographics["longitude"] = lon
    # Also provide a date_of_birth so preventive_scheduler's age calc works
    demographics["date_of_birth"] = (now - timedelta(days=age * 365)).date().isoformat()
    emp = Employee(
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
    db.add(emp)
    db.flush()
    return emp


def _make_provider(
    db,
    name,
    quality_score,
    outcome_data_points=100,
    lat=None,
    lon=None,
    provider_type="physician",
    state="ID",
):
    from app.models.provider import Provider, ProviderType
    p = Provider(
        npi=str(uuid.uuid4().int)[:10],
        name=name,
        provider_type=ProviderType(provider_type),
        state=state,
        latitude=lat,
        longitude=lon,
        quality_score=quality_score,
        outcome_data_points=outcome_data_points,
    )
    db.add(p)
    db.flush()
    return p


# ---------------------------------------------------------------------------
# Certification filter — only gates the provider selection pipeline
# ---------------------------------------------------------------------------
class TestCertificationFilter:
    def test_physician_is_certified_for_health(self, db_session):
        from app.services.provider_selection import _certification_filter
        _make_provider(db_session, "Dr Alice", 80, provider_type="physician")
        _make_provider(db_session, "Smile Dental", 92, provider_type="dental")
        result = _certification_filter(db_session, benefit_type="health")
        assert result["providers_certified"] == 1
        assert result["certified_providers"][0]["provider_name"] == "Dr Alice"

    def test_dentist_is_certified_for_dental_not_health(self, db_session):
        from app.services.provider_selection import _certification_filter
        _make_provider(db_session, "Smile Dental", 92, provider_type="dental")
        health = _certification_filter(db_session, benefit_type="health")
        assert health["providers_certified"] == 0
        dental = _certification_filter(db_session, benefit_type="dental")
        assert dental["providers_certified"] == 1

    def test_provider_without_npi_is_not_certified(self, db_session):
        from app.services.provider_selection import _certification_filter
        from app.models.provider import Provider, ProviderType
        bad = Provider(
            npi="",  # empty NPI — not certified
            name="Bogus Clinic",
            provider_type=ProviderType.physician,
            state="ID",
            latitude=43.6, longitude=-116.2,
        )
        db_session.add(bad)
        db_session.flush()
        result = _certification_filter(db_session, benefit_type="health")
        assert result["providers_certified"] == 0

    def test_state_filter_drops_out_of_state_providers(self, db_session):
        from app.services.provider_selection import _certification_filter
        _make_provider(db_session, "Boise Doc", 85, provider_type="physician", state="ID")
        _make_provider(db_session, "Seattle Doc", 85, provider_type="physician", state="WA")
        result = _certification_filter(
            db_session, benefit_type="health", state="ID"
        )
        assert result["providers_certified"] == 1
        assert result["certified_providers"][0]["provider_name"] == "Boise Doc"

    def test_required_specialty_filter(self, db_session):
        from app.services.provider_selection import _certification_filter
        from app.models.provider import Provider, ProviderType
        cardio = Provider(
            npi="1111111111",
            name="Dr Heart",
            provider_type=ProviderType.physician,
            specialties=["cardiology"],
            state="ID",
        )
        general = Provider(
            npi="2222222222",
            name="Dr General",
            provider_type=ProviderType.physician,
            specialties=["family_medicine"],
            state="ID",
        )
        db_session.add_all([cardio, general])
        db_session.flush()

        result = _certification_filter(
            db_session, benefit_type="health", required_specialty="cardiology"
        )
        assert result["providers_certified"] == 1
        assert result["certified_providers"][0]["provider_name"] == "Dr Heart"

    def test_reasoning_describes_filter(self, db_session):
        from app.services.provider_selection import _certification_filter
        _make_provider(db_session, "Dr A", 80, provider_type="physician")
        result = _certification_filter(db_session, benefit_type="health")
        assert "benefit_type='health'" in result["reasoning"]
        assert "physician" in result["reasoning"]
        assert "candidates examined" in result["reasoning"]


# ---------------------------------------------------------------------------
# Convenience floor
# ---------------------------------------------------------------------------
class TestConvenienceFloor:
    def test_haversine_boise_to_meridian_about_ten_miles(self):
        from app.services.provider_selection import _haversine_miles
        # Boise, ID: 43.6150, -116.2023
        # Meridian, ID: 43.6121, -116.3915
        distance = _haversine_miles(43.6150, -116.2023, 43.6121, -116.3915)
        assert 8 < distance < 12

    def test_convenience_filter_excludes_far_provider(self, db_session):
        from app.services.provider_selection import _apply_convenience_floor
        close_p = _make_provider(db_session, "Close", 90, lat=43.6150, lon=-116.2023)
        # San Diego — hundreds of miles away
        far_p = _make_provider(db_session, "Far", 92, lat=32.7157, lon=-117.1611)

        qualified = [
            {"provider_id": str(close_p.provider_id), "quality_score": 90},
            {"provider_id": str(far_p.provider_id), "quality_score": 92},
        ]
        result = _apply_convenience_floor(
            db_session, qualified,
            employee_latitude=43.6150, employee_longitude=-116.2023,
            clinical_urgency="routine",
        )
        kept = {p["provider_id"] for p in result["qualified_providers"]}
        assert str(close_p.provider_id) in kept
        assert str(far_p.provider_id) not in kept

    def test_convenience_filter_emergent_keeps_all(self, db_session):
        from app.services.provider_selection import _apply_convenience_floor
        close_p = _make_provider(db_session, "Close", 90, lat=43.6150, lon=-116.2023)
        far_p = _make_provider(db_session, "Far", 92, lat=32.7157, lon=-117.1611)
        qualified = [
            {"provider_id": str(close_p.provider_id), "quality_score": 90},
            {"provider_id": str(far_p.provider_id), "quality_score": 92},
        ]
        result = _apply_convenience_floor(
            db_session, qualified, 43.6150, -116.2023, "emergent",
        )
        # Emergent bypasses the distance cap.
        assert len(result["qualified_providers"]) == 2
        assert result["fallback_applied"] is False

    def test_convenience_filter_fallback_when_no_one_close(self, db_session):
        from app.services.provider_selection import _apply_convenience_floor
        far_p = _make_provider(db_session, "Far", 92, lat=32.7157, lon=-117.1611)
        qualified = [{"provider_id": str(far_p.provider_id), "quality_score": 92}]
        result = _apply_convenience_floor(
            db_session, qualified, 43.6150, -116.2023, "routine",
        )
        # No one meets the threshold, but we fall back to the closest rather
        # than denying care.
        assert result["fallback_applied"] is True
        assert len(result["qualified_providers"]) == 1

    def test_convenience_filter_no_employee_location(self, db_session):
        from app.services.provider_selection import _apply_convenience_floor
        p = _make_provider(db_session, "Any", 90, lat=43.6, lon=-116.2)
        qualified = [{"provider_id": str(p.provider_id), "quality_score": 90}]
        result = _apply_convenience_floor(
            db_session, qualified, None, None, "routine",
        )
        # Missing coords → keep everyone, flag fallback.
        assert result["fallback_applied"] is True
        assert len(result["qualified_providers"]) == 1


# ---------------------------------------------------------------------------
# Emergent detection
# ---------------------------------------------------------------------------
class TestEmergentDetection:
    def test_emergency_room_place_of_service(self):
        from app.services.emergent_detection import is_emergent
        emergent, signals = is_emergent(place_of_service="23")
        assert emergent is True
        assert "place_of_service" in signals["rules_fired"]

    def test_er_procedure_code(self):
        from app.services.emergent_detection import is_emergent
        emergent, signals = is_emergent(procedure_codes=["99284"])
        assert emergent is True
        assert "procedure_codes" in signals["rules_fired"]

    def test_mi_diagnosis_code(self):
        from app.services.emergent_detection import is_emergent
        emergent, signals = is_emergent(diagnosis_codes=["I21.4"])
        assert emergent is True
        assert "I21.4" in signals["diagnosis_codes"]

    def test_suicidal_ideation_diagnosis(self):
        from app.services.emergent_detection import is_emergent
        emergent, _ = is_emergent(diagnosis_codes=["R45.81"])
        assert emergent is True

    def test_routine_visit_is_not_emergent(self):
        from app.services.emergent_detection import is_emergent
        emergent, signals = is_emergent(
            place_of_service="11",  # office
            procedure_codes=["99213"],
            diagnosis_codes=["M54.5"],  # low back pain, NOT a red flag
        )
        assert emergent is False
        assert signals["rules_fired"] == []

    def test_patient_self_attestation_triggers_emergent(self):
        from app.services.emergent_detection import is_emergent
        emergent, signals = is_emergent(patient_attested_emergency=True)
        assert emergent is True
        assert "patient_attestation" in signals["rules_fired"]

    def test_icd_prefix_match_is_permissive(self):
        """Any ICD code whose prefix matches our red-flag list should trigger."""
        from app.services.emergent_detection import is_emergent
        # I21.01 (STEMI anterior wall) — full code including subtype
        emergent, _ = is_emergent(diagnosis_codes=["I21.01"])
        assert emergent is True
        # Case insensitivity
        emergent, _ = is_emergent(diagnosis_codes=["i21.01"])
        assert emergent is True


# ---------------------------------------------------------------------------
# Preventive scheduler
# ---------------------------------------------------------------------------
class TestPreventiveScheduler:
    def test_mammography_rule_matches_female_45(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer, age=45, sex="female")
        due = find_due_rules(db_session, employee)
        rule_ids = {r.rule_id for r in due}
        assert "uspstf_mammography" in rule_ids
        assert "uspstf_cervical_screening" in rule_ids
        assert "uspstf_bp_screening" in rule_ids

    def test_mammography_rule_excludes_male(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer, age=45, sex="male")
        due = find_due_rules(db_session, employee)
        rule_ids = {r.rule_id for r in due}
        assert "uspstf_mammography" not in rule_ids

    def test_terminated_employee_gets_nothing(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = _make_employer(db_session)
        employee = _make_employee(
            db_session, employer, age=50, sex="female",
            status="terminated", terminated_days_ago=10,
        )
        due = find_due_rules(db_session, employee)
        assert due == []

    def test_colorectal_screening_excluded_for_young_adult(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer, age=25, sex="any")
        due = find_due_rules(db_session, employee)
        rule_ids = {r.rule_id for r in due}
        assert "uspstf_colorectal_screening" not in rule_ids

    def test_lung_cancer_screening_requires_smoking_history(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = _make_employer(db_session)
        # No smoking history — rule should NOT fire
        employee = _make_employee(db_session, employer, age=60, sex="any", risk_factors=[])
        rules = find_due_rules(db_session, employee)
        assert "uspstf_lung_cancer_screening" not in {r.rule_id for r in rules}

        # With smoking history — rule SHOULD fire
        smoker = _make_employee(
            db_session, employer, age=60, sex="any", risk_factors=["smoking_history"]
        )
        rules = find_due_rules(db_session, smoker)
        assert "uspstf_lung_cancer_screening" in {r.rule_id for r in rules}

    def test_schedule_for_employee_creates_episodes(self, db_session):
        from app.services.preventive_scheduler import schedule_for_employee
        from app.models.care_episode import CareEpisode
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer, age=40, sex="female")
        result = schedule_for_employee(db_session, employee.employee_id)
        assert result["due_rules_count"] > 0
        created_episodes = db_session.query(CareEpisode).filter(
            CareEpisode.employee_id == employee.employee_id,
            CareEpisode.is_preventive.is_(True),
        ).all()
        assert len(created_episodes) == result["due_rules_count"]
        for ep in created_episodes:
            assert ep.preventive_rule_id is not None
            assert ep.routing_decision is not None

    def test_scheduler_does_not_duplicate_open_episodes(self, db_session):
        from app.services.preventive_scheduler import schedule_for_employee
        from app.models.care_episode import CareEpisode
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer, age=40, sex="female")
        schedule_for_employee(db_session, employee.employee_id)
        first_count = db_session.query(CareEpisode).filter(
            CareEpisode.employee_id == employee.employee_id,
        ).count()
        # Running a second time should not create new open episodes
        schedule_for_employee(db_session, employee.employee_id)
        second_count = db_session.query(CareEpisode).filter(
            CareEpisode.employee_id == employee.employee_id,
        ).count()
        assert first_count == second_count


# ---------------------------------------------------------------------------
# Claims verification
# ---------------------------------------------------------------------------
class TestClaimsVerification:
    def _make_claim(
        self,
        db,
        employee,
        *,
        amount=150.00,
        benefit_type="health",
        care_episode_id=None,
        routing_expected_price=None,
        days_ago=1,
        coding_validation=None,
    ):
        from app.models.claim import Claim, ClaimMode, ClaimStatus
        from app.models.service import BenefitType
        submitted = datetime.now(UTC) - timedelta(days=days_ago)
        claim = Claim(
            employer_id=employee.employer_id,
            employee_id=employee.employee_id,
            benefit_type=BenefitType(benefit_type),
            mode=ClaimMode.live,
            status=ClaimStatus.submitted,
            amount_billed=amount,
            submitted_at=submitted,
            date_of_service=submitted,
            care_episode_id=care_episode_id,
            routing_expected_price=routing_expected_price,
            coding_validation=coding_validation,
        )
        db.add(claim)
        db.flush()
        return claim

    def test_terminated_employee_before_service_is_denied(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = _make_employer(db_session)
        employee = _make_employee(
            db_session, employer, status="terminated",
            terminated_days_ago=60, enrolled_days_ago=365,
        )
        claim = self._make_claim(db_session, employee, days_ago=1)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "terminated" in result["denial_reason"].lower()

    def test_waiting_period_denial(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = _make_employer(db_session)
        # Enrolled 5 days ago — within the 30-day waiting period
        employee = _make_employee(db_session, employer, enrolled_days_ago=5)
        claim = self._make_claim(db_session, employee, days_ago=0)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "waiting" in result["denial_reason"].lower()

    def test_unrouted_nonemergent_claim_is_denied(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer)
        claim = self._make_claim(db_session, employee)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "not scheduled through the Beneflex platform" in result["denial_reason"]

    def test_emergent_unrouted_claim_is_paid(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer)
        # ER POS code triggers emergent classification
        claim = self._make_claim(
            db_session, employee,
            coding_validation={"place_of_service": "23"},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result.get("is_emergent") is True
        assert result["status"] in ("approved", "paid")

    def test_routed_claim_with_consistent_price_is_paid(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        from app.models.care_episode import CareEpisode, EpisodeStatus
        from app.models.service import BenefitType
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer)

        episode = CareEpisode(
            employee_id=employee.employee_id,
            benefit_type=BenefitType.health,
            status=EpisodeStatus.scheduled,
            issue_description="Routine visit",
            interpreted_condition="general",
            routing_decision={
                "selected_provider_id": "prov-1",
                "expected_price": 150.00,
                "clinical_urgency": "routine",
                "routed_at": datetime.now(UTC).isoformat(),
            },
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        db_session.add(episode)
        db_session.flush()

        claim = self._make_claim(
            db_session, employee,
            amount=150.00,
            care_episode_id=episode.episode_id,
            routing_expected_price=150.00,
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["routing_matched"] is True
        assert result["status"] in ("approved", "paid")
        assert result["price_drift"]["drift_exceeds_tolerance"] is False

    def test_routed_claim_with_excessive_price_drift_is_flagged(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        from app.models.care_episode import CareEpisode, EpisodeStatus
        from app.models.service import BenefitType
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer)
        episode = CareEpisode(
            employee_id=employee.employee_id,
            benefit_type=BenefitType.health,
            status=EpisodeStatus.scheduled,
            issue_description="Routine visit",
            routing_decision={
                "selected_provider_id": "prov-1",
                "expected_price": 150.00,
                "clinical_urgency": "routine",
                "routed_at": datetime.now(UTC).isoformat(),
            },
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        db_session.add(episode)
        db_session.flush()

        claim = self._make_claim(
            db_session, employee,
            amount=450.00,  # 3x expected
            care_episode_id=episode.episode_id,
            routing_expected_price=150.00,
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "flagged_for_review"
        assert result["price_drift"]["drift_exceeds_tolerance"] is True


# ---------------------------------------------------------------------------
# Population health dashboard
# ---------------------------------------------------------------------------
class TestPopulationHealth:
    def test_empty_population(self, db_session):
        from app.services.population_health import population_health_dashboard
        dashboard = population_health_dashboard(db_session)
        assert dashboard["active_employee_count"] == 0
        assert dashboard["layer_1_preventive"]["scheduled"] == 0
        assert dashboard["layer_2_routing"]["episodes_created"] == 0
        assert dashboard["layer_3_verification"]["total_claims"] == 0

    def test_preventive_metrics_reflect_created_episodes(self, db_session):
        from app.services.population_health import population_health_dashboard
        from app.services.preventive_scheduler import schedule_for_employee
        employer = _make_employer(db_session)
        employee = _make_employee(db_session, employer, age=50, sex="female")
        schedule_for_employee(db_session, employee.employee_id)

        dashboard = population_health_dashboard(db_session)
        assert dashboard["layer_1_preventive"]["scheduled"] > 0
        # Some specific rules should appear
        assert "uspstf_mammography" in dashboard["layer_1_preventive"]["by_rule"]
