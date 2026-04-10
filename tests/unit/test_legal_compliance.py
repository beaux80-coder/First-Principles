"""Legal compliance test suite.

Every test in this file maps to a specific federal or state requirement.
Each test deliberately attempts to produce a compliance violation and
asserts that the engine prevents it. When a test fails, it means the
engine is exposing the plan to legal liability.

Legal basis references:
  • ACA §2713 / 42 USC §300gg-13    — preventive care at 100%
  • ACA §1557                       — non-discrimination
  • 42 USC §300gg-19a               — emergency services (prudent layperson)
  • MHPAEA 29 USC §1185a            — mental health parity
  • ERISA 29 CFR 2560.503-1         — claims procedure
  • Idaho Code §41-5903             — utilization review standards
  • Idaho Code §41-5904             — external review
"""

from __future__ import annotations

import time
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
# ACA §2713 — preventive care coverage
# ---------------------------------------------------------------------------
class TestACAPreventiveCare:
    def test_uspstf_a_grade_rules_populated(self):
        """ACA §2713 covers USPSTF Grade A/B. We must implement at least
        the highest-impact subset."""
        from app.services.preventive_scheduler import USPSTF_PREVENTIVE_RULES
        grades = {rule.grade for rule in USPSTF_PREVENTIVE_RULES}
        assert "A" in grades
        assert "B" in grades
        assert len(USPSTF_PREVENTIVE_RULES) >= 15  # minimum coverage

    @pytest.mark.parametrize("rule_id,age,sex,expected", [
        ("uspstf_colorectal_screening", 50, "any",    True),
        ("uspstf_mammography",          50, "female", True),
        ("uspstf_cervical_screening",   30, "female", True),
        ("uspstf_bp_screening",         25, "any",    True),
        ("uspstf_hiv_screening",        30, "any",    True),
        ("uspstf_depression_screening", 40, "any",    True),
    ])
    def test_every_core_uspstf_rule_applies_to_matching_employee(
        self, db_session, rule_id, age, sex, expected
    ):
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=age, sex=sex)
        due_ids = {r.rule_id for r in find_due_rules(db_session, employee)}
        assert (rule_id in due_ids) == expected

    def test_routed_preventive_claim_has_zero_oop(self, db_session):
        """ACA §2713 requires preventive services at 100% — no cost sharing."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=50, sex="female")
        episode = make_care_episode(
            db_session, employee,
            is_preventive=True,
            preventive_rule_id="uspstf_mammography",
            expected_price=280.00,
        )
        claim = make_claim(db_session, employee, amount=280.00)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["amount_employee_oop"] == 0.0


# ---------------------------------------------------------------------------
# 42 USC §300gg-19a — emergency services (prudent layperson)
# ---------------------------------------------------------------------------
class TestEmergencyServicesComprehensive:
    @pytest.mark.parametrize("icd_prefix_code", [
        "I21.4",    # acute MI
        "I63.9",    # ischemic stroke
        "I50.23",   # acute decompensated heart failure
        "I26.09",   # pulmonary embolism
        "A41.9",    # sepsis
        "R57.0",    # cardiogenic shock
        "T78.0",    # anaphylaxis
        "S06.5X0A", # traumatic brain injury
        "K35.80",   # acute appendicitis
        "O15.9",    # eclampsia
        "G03.9",    # meningitis
        "R45.81",   # suicidal ideation
        "R40.20",   # coma
        "J96.00",   # acute respiratory failure
    ])
    def test_every_red_flag_icd_triggers_emergent(self, db_session, icd_prefix_code):
        """Any red-flag ICD-10 code must produce an emergent classification
        and pay the claim, regardless of routing status."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=5000.00,
            coding_validation={"diagnosis_codes": [icd_prefix_code]},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result.get("is_emergent") is True, f"{icd_prefix_code} should trigger emergent"
        assert result["status"] in ("approved", "paid")

    def test_er_place_of_service_alone_is_sufficient(self, db_session):
        """POS 23 (ER) alone triggers emergent even without specific diagnosis."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=800.00,
            coding_validation={"place_of_service": "23"},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True

    def test_ambulance_place_of_service_is_emergent(self, db_session):
        """POS 41 (ambulance land) triggers emergent."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=1500.00,
            coding_validation={"place_of_service": "41"},
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True

    def test_emergent_signals_audit_recorded(self, db_session):
        """Every emergent classification must record the signals that fired
        (for audit defense)."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["I21.4"],
            },
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        signals = result["emergent_signals"]
        assert "place_of_service" in signals["rules_fired"]
        assert "diagnosis_codes" in signals["rules_fired"]


# ---------------------------------------------------------------------------
# MHPAEA — Mental Health Parity and Addiction Equity Act
# ---------------------------------------------------------------------------
class TestMHPAEAParity:
    def test_mental_health_and_medical_er_both_emergent(self, db_session):
        """Parallel claim test: a mental health ER visit and a medical ER
        visit must both be classified as emergent. If one is and the other
        isn't, it's a parity violation."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)

        medical_claim = make_claim(
            db_session, employee,
            amount=3000.00,
            benefit_type="health",
            coding_validation={"place_of_service": "23"},
        )
        medical_result = adjudicate_claim(db_session, medical_claim.claim_id)

        employee2 = make_employee(db_session, employer)
        mh_claim = make_claim(
            db_session, employee2,
            amount=3000.00,
            benefit_type="mental_health",
            coding_validation={"place_of_service": "23"},
        )
        mh_result = adjudicate_claim(db_session, mh_claim.claim_id)

        # Both must be classified the same way.
        assert medical_result["is_emergent"] == mh_result["is_emergent"]
        assert medical_result["status"] == mh_result["status"]

    def test_mental_health_routed_preventive_same_as_medical(self, db_session):
        """Depression screening (mental_health) and BP screening (health)
        are both USPSTF Grade B. They must both be scheduled by the
        preventive scheduler for an eligible employee."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=35)
        rules = {r.rule_id for r in find_due_rules(db_session, employee)}
        assert "uspstf_depression_screening" in rules  # mental_health
        assert "uspstf_bp_screening" in rules  # health

    def test_mental_health_claim_routed_paid_like_medical(self, db_session):
        """A routed mental health claim with consistent price must pay
        identically to a medical claim with the same structure."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)

        employee_a = make_employee(db_session, employer)
        ep_medical = make_care_episode(
            db_session, employee_a, benefit_type="health", expected_price=200.00,
        )
        claim_medical = make_claim(
            db_session, employee_a, amount=200.00, benefit_type="health",
        )
        link_claim_to_episode(db_session, claim_medical, ep_medical)
        medical = adjudicate_claim(db_session, claim_medical.claim_id)

        employee_b = make_employee(db_session, employer)
        ep_mh = make_care_episode(
            db_session, employee_b, benefit_type="mental_health", expected_price=200.00,
        )
        claim_mh = make_claim(
            db_session, employee_b, amount=200.00, benefit_type="mental_health",
        )
        link_claim_to_episode(db_session, claim_mh, ep_mh)
        mh = adjudicate_claim(db_session, claim_mh.claim_id)

        assert medical["status"] == mh["status"]
        assert medical["routing_matched"] == mh["routing_matched"]


# ---------------------------------------------------------------------------
# ERISA 29 CFR 2560.503-1 — claims procedure requirements
# ---------------------------------------------------------------------------
class TestERISAClaimsProcedure:
    def test_denial_notice_contains_specific_reason(self, db_session):
        """§2560.503-1(g)(1)(i): denial must state the specific reason."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(db_session, employee, amount=350.00)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert result["denial_reason"]
        assert len(result["denial_reason"]) > 50  # not empty/placeholder

    def test_unrouted_denial_explains_appeal_path(self, db_session):
        """§2560.503-1(g)(1)(iv): denial must describe appeal procedures."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(db_session, employee)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["status"] == "denied"
        assert "appeal" in result["denial_reason"].lower()

    def test_every_denial_includes_steps_audit_trail(self, db_session):
        """§2560.503-1(g)(1)(v): denial must reference the rule/protocol
        used. Our 'steps' array serves this purpose by listing every
        stage the claim passed through."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(db_session, employee)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert "steps" in result
        assert len(result["steps"]) > 0

    def test_processing_latency_recorded(self, db_session):
        """§2560.503-1(f): claim decision timelines are enforced. We track
        processing latency so we can prove compliance with the windows."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=200.00)
        claim = make_claim(db_session, employee, amount=200.00)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result.get("processing_latency_ms") is not None
        assert result["processing_latency_ms"] > 0


# ---------------------------------------------------------------------------
# Idaho Code §41-5903 — utilization review timing
# ---------------------------------------------------------------------------
class TestIdahoUtilizationReview:
    def test_routine_determinations_are_near_instantaneous(self, db_session):
        """§41-5903 requires prospective review within 2 business days.
        Our orchestrator-first engine produces decisions in sub-second
        time, which trivially complies. We set an aggressive budget."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=300.00)
        claim = make_claim(db_session, employee, amount=300.00)
        link_claim_to_episode(db_session, claim, episode)
        start = time.perf_counter()
        adjudicate_claim(db_session, claim.claim_id)
        elapsed_s = time.perf_counter() - start
        # 2 business days is the legal ceiling; our target is <5s.
        assert elapsed_s < 5.0

    def test_emergent_decisions_near_instantaneous(self, db_session):
        """Idaho does not set a specific timing rule for emergency claim
        payment, but the prudent layperson standard plus our own
        orchestrator principles demand near-instant."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(
            db_session, employee,
            amount=5000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["I21.4"],
            },
        )
        start = time.perf_counter()
        result = adjudicate_claim(db_session, claim.claim_id)
        elapsed_s = time.perf_counter() - start
        assert result.get("is_emergent") is True
        assert elapsed_s < 5.0


# ---------------------------------------------------------------------------
# Immutability — audit chain integrity
# ---------------------------------------------------------------------------
class TestImmutabilityRequirements:
    def test_claim_eligibility_check_persisted(self, db_session):
        """Every verified claim must persist the eligibility check result
        for audit reconstruction."""
        from app.services.claims_adjudication import adjudicate_claim
        from app.models.claim import Claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=200.00)
        claim = make_claim(db_session, employee, amount=200.00)
        link_claim_to_episode(db_session, claim, episode)
        adjudicate_claim(db_session, claim.claim_id)
        db_session.expire_all()
        refreshed = db_session.query(Claim).filter(
            Claim.claim_id == claim.claim_id
        ).one()
        assert refreshed.eligibility_check is not None
        assert refreshed.eligibility_check.get("eligible") is True
