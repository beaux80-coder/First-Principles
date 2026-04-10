"""Tests for the universal coverage policy.

The policy is ONE document, applied the same way for every employer:
the plan covers every service that is medically necessary per
evidence-based clinical guidelines, with exactly two narrow
exclusions (cosmetic-without-indication, experimental-without-
evidence). If uncertain, the policy approves.

These tests verify:
  • The policy module is a pure function (no DB, deterministic).
  • Known cosmetic procedures are excluded ONLY when no medical
    indication is present in the patient context.
  • Known cosmetic procedures are NOT excluded when ANY medical
    indication is present.
  • Category III CPT codes (experimental) are excluded ONLY when
    the clinical engine also found zero matching guidelines.
  • Unknown / ambiguous inputs always default to NOT excluded
    (uncertainty → approval).
  • The benefits_admin.configure_plan endpoint rejects any
    coverage-scoping keys.
  • The benefits_admin.configure_plan endpoint returns the
    universal policy statement in its response.
  • The clinical engine calls the universal check after matching
    guidelines, and gray-area cases now default to approved.
"""

from __future__ import annotations

import uuid
from datetime import datetime, UTC

import pytest


# ---------------------------------------------------------------------------
# Direct tests of evaluate_universal_exclusions (pure function, no fixtures)
# ---------------------------------------------------------------------------
class TestCosmeticExclusion:
    def test_cosmetic_procedure_with_no_indication_is_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15824",  # Rhytidectomy, forehead
            condition="aesthetic concerns",
            patient_symptoms=["wants younger appearance"],
            patient_history={"diagnoses": [], "risk_factors": []},
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is True
        assert result["category"] == "cosmetic_no_indication"
        assert "15824" in result["reasoning"]
        assert result["policy_version"] == "universal-coverage-policy-v1"

    def test_rhinoplasty_with_nasal_obstruction_is_not_excluded(self):
        """Medical indication (breathing problem) overrides cosmetic classification."""
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="30400",  # Rhinoplasty
            condition="nasal obstruction",
            patient_symptoms=["difficulty breathing through nose"],
            patient_history={
                "diagnoses": ["septal deviation", "nasal obstruction"],
                "risk_factors": [],
            },
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False
        assert result["category"] is None
        assert result["signals"]["cosmetic_classification"]["listed_as_cosmetic"] is True
        assert len(result["signals"]["cosmetic_classification"]["matched_indications"]) >= 1

    def test_breast_augmentation_post_mastectomy_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="19340",  # Immediate insertion breast prosthesis
            patient_history={
                "diagnoses": ["breast cancer", "post mastectomy status"],
            },
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False
        assert "mastectomy" in result["signals"]["cosmetic_classification"]["matched_indications"] \
            or "breast cancer" in result["signals"]["cosmetic_classification"]["matched_indications"] \
            or "reconstruction" in result["signals"]["cosmetic_classification"]["matched_indications"]

    def test_hair_transplant_with_alopecia_areata_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15775",
            patient_history={"diagnoses": ["alopecia areata"]},
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_blepharoplasty_with_visual_field_obstruction_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15823",  # Blepharoplasty upper with excessive skin
            patient_symptoms=["visual field obstruction from drooping eyelid"],
            patient_history={"diagnoses": ["dermatochalasis obstructing vision"]},
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_liposuction_for_lipedema_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15877",  # SAL, trunk
            patient_history={"diagnoses": ["lipedema"]},
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_liposuction_without_medical_indication_is_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15877",
            patient_symptoms=["wants a flatter stomach"],
            patient_history={"diagnoses": [], "risk_factors": []},
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is True
        assert result["category"] == "cosmetic_no_indication"

    def test_reasoning_cites_the_specific_service_code(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15780",
            patient_symptoms=[],
            patient_history={},
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is True
        assert "15780" in result["reasoning"]
        # Must point the patient toward the path to reversal
        assert (
            "medical indication" in result["reasoning"].lower()
            or "documentation" in result["reasoning"].lower()
        )


class TestExperimentalExclusion:
    def test_category_iii_code_with_no_guidelines_is_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="0019T",  # CPT Category III
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is True
        assert result["category"] == "experimental_no_evidence"
        assert "Category III" in result["reasoning"]

    def test_category_iii_code_with_matching_guideline_is_not_excluded(self):
        """If the clinical engine found a guideline for this Category III
        code, the evidence base exists and the exclusion does not fire."""
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="0019T",
            matched_guidelines_count=2,
        )
        assert result["is_excluded"] is False

    def test_experimental_keyword_in_description_with_no_guidelines(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="99999",  # not a known code
            service_description="Investigational treatment for X",
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is True
        assert result["category"] == "experimental_no_evidence"

    def test_normal_cpt_code_with_no_guidelines_is_not_excluded(self):
        """A normal CPT code with no matching guideline is NOT automatically
        experimental — uncertainty resolves to approval."""
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="99213",  # normal E&M code
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_experimental_classification_signal_is_recorded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="0019T",
            matched_guidelines_count=0,
        )
        assert result["signals"]["experimental_classification"]["category_iii_cpt"] is True

    def test_category_iii_format_detector_is_strict(self):
        from app.services.coverage_policy import _is_category_iii_cpt
        assert _is_category_iii_cpt("0019T") is True
        assert _is_category_iii_cpt("9999T") is True
        assert _is_category_iii_cpt("99213") is False
        assert _is_category_iii_cpt("T0019") is False
        assert _is_category_iii_cpt("19T") is False  # too short
        assert _is_category_iii_cpt("") is False
        assert _is_category_iii_cpt(None) is False  # defensive


class TestUncertaintyResolvesToApproval:
    def test_unknown_service_code_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="99999",
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_empty_service_code_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="",
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_none_service_code_is_not_excluded(self):
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code=None,
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is False

    def test_no_patient_context_on_cosmetic_code_still_excludes(self):
        """A known cosmetic code with zero patient context has no
        indication to match — exclusion fires."""
        from app.services.coverage_policy import evaluate_universal_exclusions
        result = evaluate_universal_exclusions(
            service_code="15824",
            patient_symptoms=None,
            patient_history=None,
            matched_guidelines_count=0,
        )
        assert result["is_excluded"] is True


class TestPolicyStatement:
    def test_statement_includes_both_exclusions(self):
        from app.services.coverage_policy import get_universal_policy_statement
        stmt = get_universal_policy_statement()
        categories = {e["category"] for e in stmt["exclusions"]}
        assert categories == {"cosmetic_no_indication", "experimental_no_evidence"}

    def test_statement_mentions_uncertainty_rule(self):
        from app.services.coverage_policy import get_universal_policy_statement
        stmt = get_universal_policy_statement()
        rule_lc = stmt["uncertainty_rule"].lower()
        assert "uncertain" in rule_lc
        assert "approved" in rule_lc

    def test_no_employer_specific_toggles(self):
        from app.services.coverage_policy import get_universal_policy_statement
        stmt = get_universal_policy_statement()
        assert stmt["employer_specific_toggles"] is None
        assert "universal" in stmt["employer_specific_toggles_note"].lower()

    def test_policy_version_is_stable(self):
        from app.services.coverage_policy import POLICY_VERSION
        assert POLICY_VERSION == "universal-coverage-policy-v1"


# ---------------------------------------------------------------------------
# Tests of benefits_admin.configure_plan (now rejects coverage toggles)
# ---------------------------------------------------------------------------
class TestConfigurePlanRejectsCoverageToggles:
    def _make_employer(self, db):
        from app.models.employer import Employer, EmployerStatus
        emp = Employer(name="TestCo", status=EmployerStatus.active)
        db.add(emp)
        db.commit()
        db.refresh(emp)
        return emp

    @pytest.mark.parametrize("forbidden_key", [
        "benefit_types_enabled",
        "covered_benefit_types",
        "opted_out_benefits",
        "coverage_exclusions",
        "excluded_services",
        "excluded_categories",
        "service_exclusions",
        "exclusion_list",
        "plan_tier",
        "coverage_level",
        "coverage_options",
        "fertility_coverage",
        "bariatric_coverage",
        "gender_affirming_coverage",
        "glp1_coverage",
        "mental_health_toggle",
        "optional_benefits",
    ])
    def test_forbidden_key_raises_value_error(self, db_session, forbidden_key):
        from app.services.benefits_admin import configure_plan
        employer = self._make_employer(db_session)
        with pytest.raises(ValueError) as excinfo:
            configure_plan(
                db_session,
                employer_id=employer.employer_id,
                plan_config={forbidden_key: ["anything"]},
            )
        assert forbidden_key in str(excinfo.value)
        assert "universal" in str(excinfo.value).lower()

    def test_admin_only_config_is_accepted(self, db_session):
        from app.services.benefits_admin import configure_plan
        employer = self._make_employer(db_session)
        result = configure_plan(
            db_session,
            employer_id=employer.employer_id,
            plan_config={
                "plan_year_start": "2026-01-01",
                "waiting_period_days": 30,
                "eligibility_rules": {
                    "min_hours_per_week": 30,
                    "employee_classes": ["full_time"],
                },
            },
        )
        assert result["status"] == "configured"
        assert result["plan_administration"]["waiting_period_days"] == 30
        assert result["plan_administration"]["plan_year_start"] == "2026-01-01"

    def test_response_includes_universal_policy_statement(self, db_session):
        from app.services.benefits_admin import configure_plan
        employer = self._make_employer(db_session)
        result = configure_plan(
            db_session,
            employer_id=employer.employer_id,
            plan_config={},
        )
        policy = result["coverage_policy"]
        assert policy["policy_version"] == "universal-coverage-policy-v1"
        assert policy["employer_specific_toggles"] is None
        categories = {e["category"] for e in policy["exclusions"]}
        assert categories == {"cosmetic_no_indication", "experimental_no_evidence"}

    def test_response_declares_all_7_benefit_types_covered(self, db_session):
        from app.services.benefits_admin import configure_plan
        employer = self._make_employer(db_session)
        result = configure_plan(
            db_session,
            employer_id=employer.employer_id,
            plan_config={},
        )
        covered = set(result["covered_benefit_types"])
        assert covered == {
            "health", "dental", "vision", "life",
            "std", "ltd", "mental_health",
        }


# ---------------------------------------------------------------------------
# Tests of clinical_engine integration (gray-area default is APPROVE now)
# ---------------------------------------------------------------------------
class TestClinicalEngineGrayAreaDefaultApproval:
    def test_no_guidelines_no_risk_now_approves(self, db_session):
        """Previously: no guideline + low risk → denied.
        Now: no guideline + low risk → APPROVED (uncertainty → patient)."""
        from app.services.clinical_engine import make_determination
        det = make_determination(
            db_session,
            claim_id="test-claim-gray-area",
            service_code="99213",  # normal E&M
            benefit_type="health",
            patient_symptoms=["mild occasional fatigue"],
            patient_history={
                "age": 30,
                "sex": "any",
                "diagnoses": [],
                "medications": [],
                "risk_factors": [],
            },
            condition=None,
        )
        decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
        assert decision == "approved"
        assert "universal coverage policy" in det.reasoning.lower()

    def test_no_guidelines_high_risk_approves_with_risk_reasoning(self, db_session):
        from app.services.clinical_engine import make_determination
        det = make_determination(
            db_session,
            claim_id="test-claim-high-risk",
            service_code="99214",
            benefit_type="health",
            patient_symptoms=["chest pain", "shortness of breath"],
            patient_history={
                "age": 68,
                "sex": "male",
                "diagnoses": ["diabetes", "hypertension", "heart failure"],
                "medications": ["metformin", "lisinopril", "carvedilol"],
                "risk_factors": ["smoking"],
            },
            condition=None,
        )
        decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
        assert decision == "approved"

    def test_cosmetic_code_without_indication_is_denied_by_universal_policy(self, db_session):
        """This is the one case where the universal policy can flip an
        otherwise-approved determination into a denial. The gray-area
        engine would have approved 15824; the universal policy catches
        it."""
        from app.services.clinical_engine import make_determination
        det = make_determination(
            db_session,
            claim_id="test-claim-cosmetic",
            service_code="15824",  # Rhytidectomy forehead
            benefit_type="health",
            patient_symptoms=["wants to look younger"],
            patient_history={
                "age": 55,
                "sex": "female",
                "diagnoses": [],
                "medications": [],
                "risk_factors": [],
            },
            condition=None,
        )
        decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
        assert decision == "denied"
        assert "universal policy exclusion" in det.reasoning.lower() \
            or "cosmetic_no_indication" in det.reasoning.lower()

    def test_cosmetic_code_with_trauma_indication_is_approved(self, db_session):
        from app.services.clinical_engine import make_determination
        det = make_determination(
            db_session,
            claim_id="test-claim-trauma",
            service_code="15824",
            benefit_type="health",
            patient_symptoms=["severe facial trauma", "post-burn reconstruction"],
            patient_history={
                "age": 40,
                "sex": "any",
                "diagnoses": ["burn reconstruction", "trauma"],
                "medications": [],
                "risk_factors": [],
            },
            condition="trauma",
        )
        decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
        assert decision == "approved"

    def test_category_iii_without_guidelines_is_denied(self, db_session):
        from app.services.clinical_engine import make_determination
        det = make_determination(
            db_session,
            claim_id="test-claim-experimental",
            service_code="0019T",
            benefit_type="health",
            patient_symptoms=["back pain"],
            patient_history={
                "age": 45,
                "sex": "any",
                "diagnoses": [],
                "medications": [],
                "risk_factors": [],
            },
            condition=None,
        )
        decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
        assert decision == "denied"
        assert "experimental_no_evidence" in det.reasoning.lower() \
            or "universal policy exclusion" in det.reasoning.lower()

    def test_determination_reasoning_cites_policy_version(self, db_session):
        from app.services.clinical_engine import make_determination
        det = make_determination(
            db_session,
            claim_id="test-claim-reasoning",
            service_code="15824",
            benefit_type="health",
            patient_symptoms=[],
            patient_history={"age": 50},
            condition=None,
        )
        # The guidelines_referenced should include the universal policy
        # tag for audit traceability
        refs = det.guidelines_referenced or []
        joined = " ".join(refs)
        assert "universal-coverage-policy-v1" in joined \
            or "cosmetic_no_indication" in joined
