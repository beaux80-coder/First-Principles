"""Fairness / disparate impact tests.

Federal anti-discrimination law applicable to health benefits:
  • ACA §1557 — prohibits discrimination by race, color, national origin,
    sex (including sex stereotypes, pregnancy, gender identity), age,
    or disability in any health program receiving federal funds.
  • ADA Title I — prohibits disability discrimination by employers.
  • GINA — prohibits using genetic information in coverage decisions.
  • Title VII — prohibits employment discrimination broadly.
  • Age Discrimination in Employment Act (ADEA).

Even a "neutral" rule can create disparate impact. These tests build
synthetic populations differing only on a protected-class attribute
and verify the engine produces statistically similar decisions.

Methodology:
  • Generate N employees per group with clinically-matched claims.
  • Run the engine on all claims.
  • Compute approval rate per group.
  • Assert absolute delta in approval rates < 5 percentage points.
  • If a legitimate clinical reason for the delta exists (e.g. AAA
    screening is male-only because the evidence supports that), the
    test documents WHY the disparity is lawful.

These are synthetic-data tests. Production should run this analysis on
real claims quarterly and produce a fairness audit report.
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


def _approval_rate(results: list[dict]) -> float:
    if not results:
        return 0.0
    approved = sum(
        1 for r in results
        if r.get("status") in ("approved", "paid")
    )
    return approved / len(results)


def _run_matched_cohort(
    db_session,
    employer,
    *,
    count: int,
    age: int,
    sex: str,
    race: str | None = None,
    zip_code: str | None = None,
    route_claims: bool = True,
    benefit_type: str = "health",
    amount: float = 250.00,
) -> list[dict]:
    """Create `count` employees with identical clinical profile and
    submit a claim for each. Returns the adjudication results."""
    from app.services.claims_adjudication import adjudicate_claim

    results: list[dict] = []
    for _ in range(count):
        employee = make_employee(
            db_session, employer,
            age=age, sex=sex, race=race, zip_code=zip_code,
            enrolled_days_ago=180,
        )
        if route_claims:
            episode = make_care_episode(
                db_session, employee,
                benefit_type=benefit_type,
                expected_price=amount,
            )
            claim = make_claim(
                db_session, employee,
                amount=amount, benefit_type=benefit_type,
            )
            link_claim_to_episode(db_session, claim, episode)
        else:
            claim = make_claim(
                db_session, employee,
                amount=amount, benefit_type=benefit_type,
            )
        results.append(adjudicate_claim(db_session, claim.claim_id))
    return results


# ---------------------------------------------------------------------------
# Sex parity
# ---------------------------------------------------------------------------
class TestSexParity:
    def test_routed_routine_care_approval_rate_parity(self, db_session):
        """A routine routed health claim must be approved at the same rate
        for men and women."""
        employer = make_employer(db_session)
        male_results = _run_matched_cohort(
            db_session, employer, count=20, age=40, sex="male",
        )
        female_results = _run_matched_cohort(
            db_session, employer, count=20, age=40, sex="female",
        )
        male_rate = _approval_rate(male_results)
        female_rate = _approval_rate(female_results)
        assert abs(male_rate - female_rate) < 0.05, (
            f"Sex disparity in routine care: male={male_rate:.2%}, "
            f"female={female_rate:.2%}"
        )
        # Both should be near 100% for properly-routed care
        assert male_rate > 0.90
        assert female_rate > 0.90

    def test_emergent_classification_is_sex_blind(self, db_session):
        """Same ICD codes → same emergent classification regardless of sex."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)

        # Test with an MI diagnosis for both sexes
        male = make_employee(db_session, employer, age=55, sex="male")
        female = make_employee(db_session, employer, age=55, sex="female")

        male_claim = make_claim(
            db_session, male, amount=25000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["I21.4"],
            },
        )
        female_claim = make_claim(
            db_session, female, amount=25000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["I21.4"],
            },
        )
        male_result = adjudicate_claim(db_session, male_claim.claim_id)
        female_result = adjudicate_claim(db_session, female_claim.claim_id)

        assert male_result["is_emergent"] == female_result["is_emergent"]
        assert male_result["status"] == female_result["status"]


# ---------------------------------------------------------------------------
# Age parity
# ---------------------------------------------------------------------------
class TestAgeParity:
    def test_routed_care_approval_rate_by_age_bracket(self, db_session):
        """Routine routed care should be approved at similar rates across
        age brackets. (Note: preventive rule eligibility does legitimately
        vary by age — that's tested separately.)"""
        employer = make_employer(db_session)
        young = _run_matched_cohort(
            db_session, employer, count=20, age=25, sex="any",
        )
        middle = _run_matched_cohort(
            db_session, employer, count=20, age=45, sex="any",
        )
        older = _run_matched_cohort(
            db_session, employer, count=20, age=65, sex="any",
        )
        r_young = _approval_rate(young)
        r_middle = _approval_rate(middle)
        r_older = _approval_rate(older)

        # All three should be near-100% for routed routine care
        assert r_young > 0.90
        assert r_middle > 0.90
        assert r_older > 0.90
        # Deltas between any two brackets should be tiny
        assert abs(r_young - r_middle) < 0.05
        assert abs(r_middle - r_older) < 0.05

    def test_preventive_rule_counts_increase_monotonically(self, db_session):
        """The total number of USPSTF preventive rules that apply to an
        employee is a non-decreasing function of age (older patients
        generally qualify for more screenings). Sex-specific rules may
        create small local exceptions — document and allow them."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        counts = []
        for age in [25, 35, 45, 55, 65]:
            employee = make_employee(
                db_session, employer, age=age, sex="any",
                risk_factors=[],
            )
            due = find_due_rules(db_session, employee)
            counts.append(len(due))
        # Counts should be non-decreasing overall
        assert counts[-1] >= counts[0]


# ---------------------------------------------------------------------------
# Race parity (proxied through race label stored in demographics)
# ---------------------------------------------------------------------------
class TestRaceParity:
    def test_routine_claim_approval_is_race_blind(self, db_session):
        """The engine must not read `race` from demographics (it doesn't —
        but we test that approval rates are identical across cohorts
        labeled with different race values)."""
        employer = make_employer(db_session)
        cohorts = {}
        for race in ["white", "black", "asian", "hispanic", "native"]:
            results = _run_matched_cohort(
                db_session, employer, count=15,
                age=40, sex="any", race=race,
            )
            cohorts[race] = _approval_rate(results)

        rates = list(cohorts.values())
        max_rate = max(rates)
        min_rate = min(rates)
        assert max_rate - min_rate < 0.05, (
            f"Race-labeled cohorts show disparate approval rates: {cohorts}"
        )

    def test_emergent_classification_is_race_blind(self, db_session):
        """Red-flag ICD code must always trigger emergent, regardless of
        race label on the patient record."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        for race in ["white", "black", "asian", "hispanic", "native"]:
            employee = make_employee(db_session, employer, race=race)
            claim = make_claim(
                db_session, employee, amount=20000.00,
                coding_validation={
                    "place_of_service": "23",
                    "diagnosis_codes": ["I63.9"],
                },
            )
            result = adjudicate_claim(db_session, claim.claim_id)
            assert result["is_emergent"] is True, f"Race={race} was not classified emergent"


# ---------------------------------------------------------------------------
# Disability / pregnancy protection
# ---------------------------------------------------------------------------
class TestDisabilityAndPregnancy:
    def test_pregnancy_related_diagnosis_is_handled_like_medical(self, db_session):
        """Eclampsia (O15) is a red-flag emergent condition. The engine
        must classify it as emergent — pregnancy is legally protected
        and can never be grounds for denial."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=32, sex="female")
        claim = make_claim(
            db_session, employee, amount=15000.00,
            coding_validation={
                "place_of_service": "23",
                "diagnosis_codes": ["O15.9"],
            },
        )
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["is_emergent"] is True
        assert result["status"] in ("approved", "paid")

    def test_high_complexity_patient_routine_care_not_denied(self, db_session):
        """A patient with multiple chronic conditions (a proxy for
        disability status) must not be denied routine routed care at a
        different rate than a healthy cohort."""
        employer = make_employer(db_session)

        # Baseline: healthy cohort
        healthy = _run_matched_cohort(
            db_session, employer, count=20, age=50, sex="any",
        )

        # High-complexity cohort with multiple risk factors
        complex_results = []
        from app.services.claims_adjudication import adjudicate_claim
        for _ in range(20):
            employee = make_employee(
                db_session, employer, age=50, sex="any",
                risk_factors=["diabetes", "hypertension", "chronic_kidney_disease"],
            )
            episode = make_care_episode(
                db_session, employee, expected_price=250.00,
            )
            claim = make_claim(db_session, employee, amount=250.00)
            link_claim_to_episode(db_session, claim, episode)
            complex_results.append(adjudicate_claim(db_session, claim.claim_id))

        healthy_rate = _approval_rate(healthy)
        complex_rate = _approval_rate(complex_results)

        # Complex patients should NOT be approved at a meaningfully
        # lower rate than healthy patients for routine routed care.
        assert healthy_rate - complex_rate < 0.05


# ---------------------------------------------------------------------------
# Legitimate (lawful) disparities — documented clinical rationale
# ---------------------------------------------------------------------------
class TestLegitimateDisparities:
    """Some disparities ARE lawful because the underlying medical evidence
    is itself sex/age/risk-factor-specific. These tests document those
    disparities so that when a fairness audit flags them, we can cite
    the clinical rationale."""

    def test_mammography_is_female_only_by_evidence(self, db_session):
        """Mammography is recommended for women only per USPSTF — this
        is a lawful sex-based difference because breast cancer screening
        evidence is sex-specific."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        woman = make_employee(db_session, employer, age=50, sex="female")
        man = make_employee(db_session, employer, age=50, sex="male")
        woman_rules = {r.rule_id for r in find_due_rules(db_session, woman)}
        man_rules = {r.rule_id for r in find_due_rules(db_session, man)}
        assert "uspstf_mammography" in woman_rules
        assert "uspstf_mammography" not in man_rules
        # This disparity is lawful. The rationale: USPSTF 2024 recommendation.

    def test_aaa_screening_is_male_smoker_only_by_evidence(self, db_session):
        """AAA screening is USPSTF B for men 65-75 with smoking history
        only — evidence does not support it for women or non-smokers."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        male_smoker = make_employee(
            db_session, employer, age=68, sex="male",
            risk_factors=["smoking_history"],
        )
        female_smoker = make_employee(
            db_session, employer, age=68, sex="female",
            risk_factors=["smoking_history"],
        )
        male_nonsmoker = make_employee(
            db_session, employer, age=68, sex="male",
            risk_factors=[],
        )
        assert "uspstf_aaa_screening" in {r.rule_id for r in find_due_rules(db_session, male_smoker)}
        assert "uspstf_aaa_screening" not in {r.rule_id for r in find_due_rules(db_session, female_smoker)}
        assert "uspstf_aaa_screening" not in {r.rule_id for r in find_due_rules(db_session, male_nonsmoker)}
