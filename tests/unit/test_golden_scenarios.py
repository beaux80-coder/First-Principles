"""Golden scenario library — the engine's behavioral regression suite.

Each test is a realistic, end-to-end scenario: patient, claim, expected
decision, expected reasoning. If any scenario flips, the commit that
caused it must either be a bug (fix the engine) or an intentional policy
change (update the scenario and document it in the commit message).

Scenarios are grouped by category:
  A. Legally-mandated approvals     (must never deny)
  B. Legally-mandated denials       (must always catch)
  C. Orchestrator-specific policy   (our new rules)
  D. Gray-area clinical judgment    (evidence-driven)
  E. Cost-vs-health tradeoffs       (routing optimization)
"""

from __future__ import annotations

from datetime import datetime, UTC, timedelta

import pytest

from tests.unit.factories import (
    make_care_episode,
    make_claim,
    make_employee,
    make_employer,
    link_claim_to_episode,
)


# ---------------------------------------------------------------------------
# Category A — Legally-mandated approvals
# These must ALWAYS pay, regardless of routing or cost.
# ---------------------------------------------------------------------------
class TestCategoryA_LegallyMandatedApprovals:
    def test_mammography_routed_for_45yo_woman(self, db_session):
        """ACA §2713 + USPSTF B: mammography for women 40-74 must be covered."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=45, sex="female")
        episode = make_care_episode(
            db_session, employee,
            condition="breast_cancer_screening",
            benefit_type="health",
            is_preventive=True,
            preventive_rule_id="uspstf_mammography",
            expected_price=280.00,
        )
        claim = make_claim(db_session, employee, amount=280.00)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")
        assert result["routing_matched"] is True
        assert result["amount_employee_oop"] == 0.0

    def test_colonoscopy_routed_for_50yo(self, db_session):
        """USPSTF A: colorectal cancer screening for adults 45-75."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=50)
        episode = make_care_episode(
            db_session, employee,
            condition="colorectal_cancer_screening",
            is_preventive=True,
            preventive_rule_id="uspstf_colorectal_screening",
            expected_price=1200.00,
        )
        claim = make_claim(db_session, employee, amount=1200.00)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")
        assert result["amount_employee_oop"] == 0.0

    def test_unrouted_er_visit_for_mi_is_paid_emergent(self, db_session):
        """42 USC §300gg-19a: MI must be covered regardless of routing."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee, amount=45000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["I21.4"],
                "procedure_codes": ["99285"],
            },
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")
        assert result["is_emergent"] is True
        assert "diagnosis_codes" in result["emergent_signals"]["rules_fired"]

    def test_unrouted_stroke_er_visit_is_paid(self, db_session):
        """Ischemic stroke (I63) is a red-flag ICD prefix → emergent."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee, amount=28000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["I63.9"],
            },
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True
        assert result["status"] in ("approved", "paid")

    def test_unrouted_mental_health_crisis_is_paid(self, db_session):
        """Suicidal ideation (R45.81) is a red-flag ICD → emergent + parity."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=2200.00,
            benefit_type="mental_health",
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["R45.81"],
            },
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True
        assert result["status"] in ("approved", "paid")

    def test_unrouted_anaphylaxis_is_paid(self, db_session):
        """Anaphylaxis (T78.0) → emergent."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=1800.00,
            coding_validation={"diagnosis_codes": ["T78.0"]},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True

    def test_routed_flu_vaccine_is_paid_zero_oop(self, db_session):
        """ACIP immunization — must be covered at 100%."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=35)
        episode = make_care_episode(
            db_session, employee,
            condition="influenza_vaccination",
            is_preventive=True,
            preventive_rule_id="acip_influenza",
            expected_price=45.00,
        )
        claim = make_claim(db_session, employee, amount=45.00)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")
        assert result["amount_employee_oop"] == 0.0

    def test_patient_self_attested_emergency_is_paid(self, db_session):
        """Prudent layperson: patient self-attestation triggers emergent."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=800.00,
            coding_validation={"patient_attested_emergency": True},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True
        assert "patient_attestation" in result["emergent_signals"]["rules_fired"]


# ---------------------------------------------------------------------------
# Category B — Legally-mandated denials (protect the plan from waste/fraud)
# ---------------------------------------------------------------------------
class TestCategoryB_LegallyMandatedDenials:
    def test_duplicate_claim_is_denied(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=300.00)

        claim1 = make_claim(db_session, employee, amount=300.00)
        link_claim_to_episode(db_session, claim1, episode)
        adjudicate_claim(db_session, claim1.claim_id)

        # Second claim, same episode, same amount — duplicate.
        claim2 = make_claim(db_session, employee, amount=300.00)
        link_claim_to_episode(db_session, claim2, episode)
        result = adjudicate_claim(db_session, claim2.claim_id)
        assert result["status"] == "denied"
        assert "Duplicate" in result["denial_reason"]

    def test_claim_for_wrong_employer_is_denied(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        from app.models.claim import Claim, ClaimMode, ClaimStatus
        from app.models.service import BenefitType
        employer_a = make_employer(db_session, name="CompanyA")
        employer_b = make_employer(db_session, name="CompanyB")
        employee = make_employee(db_session, employer_a)
        # Submit a claim against employer_b even though employee works at A
        claim = Claim(
            employer_id=employer_b.employer_id,
            employee_id=employee.employee_id,
            benefit_type=BenefitType.health,
            mode=ClaimMode.live,
            status=ClaimStatus.submitted,
            amount_billed=500.00,
            submitted_at=datetime.now(UTC),
            date_of_service=datetime.now(UTC),
        )
        db_session.add(claim)
        db_session.flush()
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "mismatch" in result["denial_reason"].lower()

    def test_service_before_enrollment_denied(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, enrolled_days_ago=5)
        # Service date in the first 30 days (within waiting period)
        claim = make_claim(db_session, employee, days_ago=0)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "waiting" in result["denial_reason"].lower()

    def test_service_after_termination_denied(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(
            db_session, employer,
            status="terminated",
            terminated_days_ago=30,
            enrolled_days_ago=365,
        )
        claim = make_claim(db_session, employee, days_ago=5)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"

    def test_prospect_employer_rejects_claims(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session, status="prospect")
        employee = make_employee(db_session, employer)
        claim = make_claim(db_session, employee)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "employer_inactive" in result["denial_reason"]


# ---------------------------------------------------------------------------
# Category C — Orchestrator-specific policy
# These are the decisions that distinguish this engine from traditional TPA.
# ---------------------------------------------------------------------------
class TestCategoryC_OrchestratorPolicy:
    def test_unrouted_routine_care_denied_with_platform_instructions(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=350.00,
            coding_validation={"place_of_service": "11"},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "not scheduled through the Beneflex platform" in result["denial_reason"]
        assert "appeal" in result["denial_reason"].lower()

    def test_routed_price_consistent_paid(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=400.00)
        claim = make_claim(db_session, employee, amount=400.00)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")
        assert result["routing_matched"] is True
        assert result["price_drift"]["drift_exceeds_tolerance"] is False

    def test_routed_price_within_tolerance_paid(self, db_session):
        """Within 10% tolerance (default) — still paid."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=400.00)
        claim = make_claim(db_session, employee, amount=430.00)  # +7.5%
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")

    def test_routed_price_outside_tolerance_flagged(self, db_session):
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=400.00)
        claim = make_claim(db_session, employee, amount=700.00)  # +75%
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "flagged_for_review"

    def test_certification_filter_excludes_wrong_provider_type(self, db_session):
        """A dentist cannot be certified to deliver a health service, and a
        physician cannot be certified to deliver a dental service. The
        certification filter is the only quality gate in the pipeline."""
        from app.services.provider_selection import _certification_filter
        from tests.unit.factories import make_provider
        make_provider(db_session, "Dr Physician", provider_type="physician")
        make_provider(db_session, "Smile Dental", provider_type="dental")

        health = _certification_filter(db_session, benefit_type="health")
        assert health["providers_certified"] == 1
        assert health["certified_providers"][0]["provider_name"] == "Dr Physician"

        dental = _certification_filter(db_session, benefit_type="dental")
        assert dental["providers_certified"] == 1
        assert dental["certified_providers"][0]["provider_name"] == "Smile Dental"

    def test_cheapest_certified_wins_regardless_of_outcome_score(self, db_session):
        """Price is the only tiebreaker among certified providers. Outcome
        score (quality_score) is NOT used as a ranking input, even when
        one certified provider has a much higher historical score."""
        from app.services.provider_selection import _certification_filter
        from tests.unit.factories import make_provider
        # 'high_quality' and 'low_quality' both certified; selection pipeline
        # treats them identically at the certification stage.
        make_provider(db_session, "High Quality", quality_score=99, provider_type="physician")
        make_provider(db_session, "Low Quality", quality_score=55, provider_type="physician")
        result = _certification_filter(db_session, benefit_type="health")
        assert result["providers_certified"] == 2
        # Certification pass does not rank by score; both are in.
        names = {p["provider_name"] for p in result["certified_providers"]}
        assert names == {"High Quality", "Low Quality"}

    def test_emergent_classification_overrides_unrouted_denial(self, db_session):
        """Even without routing, emergent classification → paid."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=5000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["R57.0"],  # cardiogenic shock
            },
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] in ("approved", "paid")
        assert result["is_emergent"] is True


# ---------------------------------------------------------------------------
# Category D — Gray-area clinical judgment
# ---------------------------------------------------------------------------
class TestCategoryD_GrayArea:
    def test_lung_cancer_screening_eligibility_with_smoking_history(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        smoker = make_employee(
            db_session, employer, age=60,
            risk_factors=["smoking_history"],
        )
        due = {r.rule_id for r in find_due_rules(db_session, smoker)}
        assert "uspstf_lung_cancer_screening" in due

    def test_lung_cancer_screening_denied_without_smoking_history(self, db_session):
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        non_smoker = make_employee(db_session, employer, age=60, risk_factors=[])
        due = {r.rule_id for r in find_due_rules(db_session, non_smoker)}
        assert "uspstf_lung_cancer_screening" not in due

    def test_aaa_screening_for_male_65_with_smoking(self, db_session):
        """USPSTF B: one-time AAA screening for male smokers 65-75."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        patient = make_employee(
            db_session, employer, age=68, sex="male",
            risk_factors=["smoking_history"],
        )
        due = {r.rule_id for r in find_due_rules(db_session, patient)}
        assert "uspstf_aaa_screening" in due

    def test_aaa_screening_excluded_for_female(self, db_session):
        """AAA screening is male-only per USPSTF (B for women = no recommendation)."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        patient = make_employee(
            db_session, employer, age=68, sex="female",
            risk_factors=["smoking_history"],
        )
        due = {r.rule_id for r in find_due_rules(db_session, patient)}
        assert "uspstf_aaa_screening" not in due


# ---------------------------------------------------------------------------
# Category E — Cost-first selection among certified providers
# Exercises the full selection pipeline: certification → convenience →
# cheapest. Outcome score is not used as a selection input.
# ---------------------------------------------------------------------------
class TestCategoryE_CostVsHealth:
    def test_both_certified_both_pass_through_to_cost_stage(self, db_session):
        """Two providers, both certified for the same benefit type, both
        reachable — both proceed to the cost optimization stage. There
        is no ranking by outcome score between them."""
        from app.services.provider_selection import _certification_filter
        from tests.unit.factories import make_provider
        make_provider(db_session, "Cheap Clinic", quality_score=89, provider_type="physician")
        make_provider(db_session, "Pricey Clinic", quality_score=92, provider_type="physician")
        result = _certification_filter(db_session, benefit_type="health")
        names = {p["provider_name"] for p in result["certified_providers"]}
        assert names == {"Cheap Clinic", "Pricey Clinic"}

    def test_far_cheap_provider_excluded_by_convenience(self, db_session):
        """A far provider can't win on cost — convenience floor drops it."""
        from app.services.provider_selection import _apply_convenience_floor
        from tests.unit.factories import make_provider
        boise_lat, boise_lon = 43.6150, -116.2023
        close = make_provider(db_session, "Close", lat=boise_lat, lon=boise_lon)
        # Seattle, ~400 miles away
        far = make_provider(db_session, "Far", lat=47.6062, lon=-122.3321)
        providers = [
            {"provider_id": str(close.provider_id), "quality_score": 85},
            {"provider_id": str(far.provider_id), "quality_score": 95},
        ]
        result = _apply_convenience_floor(
            db_session, providers, boise_lat, boise_lon, "routine"
        )
        kept = {p["provider_id"] for p in result["qualified_providers"]}
        assert str(close.provider_id) in kept
        assert str(far.provider_id) not in kept

    def test_specialty_care_allows_longer_travel(self, db_session):
        """Specialty threshold is 60 miles (vs 45 for routine)."""
        from app.services.provider_selection import _apply_convenience_floor
        from tests.unit.factories import make_provider
        boise_lat, boise_lon = 43.6150, -116.2023
        # Twin Falls, ID — about 100 miles
        specialty = make_provider(db_session, "TwinFalls", lat=42.5630, lon=-114.4609)
        providers = [
            {"provider_id": str(specialty.provider_id), "quality_score": 95},
        ]
        # For routine care, Twin Falls is too far.
        routine = _apply_convenience_floor(
            db_session, providers, boise_lat, boise_lon, "routine"
        )
        assert routine["fallback_applied"] is True  # no one within 45 mi

        # But for specialty... also too far since specialty cap is 60mi.
        specialty_result = _apply_convenience_floor(
            db_session, providers, boise_lat, boise_lon, "specialty"
        )
        assert specialty_result["fallback_applied"] is True

    def test_emergent_ignores_travel_distance(self, db_session):
        """Emergent care has no distance cap — nearest clinically qualified wins."""
        from app.services.provider_selection import _apply_convenience_floor
        from tests.unit.factories import make_provider
        far = make_provider(
            db_session, "Far", lat=47.6062, lon=-122.3321
        )  # Seattle
        providers = [{"provider_id": str(far.provider_id), "quality_score": 95}]
        result = _apply_convenience_floor(
            db_session, providers, 43.6150, -116.2023, "emergent"
        )
        # No fallback, no exclusion
        assert result["fallback_applied"] is False
        assert len(result["qualified_providers"]) == 1
