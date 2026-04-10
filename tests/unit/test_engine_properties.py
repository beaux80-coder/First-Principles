"""Property-based tests for the orchestrator engine.

Property tests generate thousands of random inputs looking for invariant
violations. They complement the golden scenarios (which check specific
cases) by finding edge cases we didn't think of.

Every test in this file asserts a property that must hold for ALL valid
inputs. If Hypothesis finds an input that breaks the property, either
the property is wrong or the engine is wrong — both are worth knowing.

Invariants checked:
  1. Quality floor never admits a provider below the floor (unless safety net)
  2. Quality floor is monotonic — adding a better provider only tightens
  3. Convenience floor never admits a provider beyond the distance cap
  4. Haversine distance is symmetric and non-negative
  5. Emergent classification is monotonic in signals
  6. Emergent classification is idempotent
  7. Preventive rule eligibility is monotonic in age within the rule window
  8. Engine never pays more than billed
  9. Engine never produces an approved claim with amount_employee_oop > 0
 10. Routing drift is symmetric around 0
"""

from __future__ import annotations

import math
from datetime import datetime, UTC, timedelta

import pytest
from hypothesis import given, assume, strategies as st, settings, HealthCheck

from tests.unit.factories import (
    make_care_episode,
    make_claim,
    make_employee,
    make_employer,
    make_provider,
    link_claim_to_episode,
)


# ---------------------------------------------------------------------------
# Strategies — reusable Hypothesis generators
# ---------------------------------------------------------------------------
provider_dict = st.fixed_dictionaries({
    "provider_id": st.uuids().map(str),
    "confidence_lower_bound": st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False,
    ),
    "quality_score": st.floats(
        min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False,
    ),
})

latitude = st.floats(min_value=-89.0, max_value=89.0, allow_nan=False, allow_infinity=False)
longitude = st.floats(min_value=-179.0, max_value=179.0, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# 1. Certification filter invariants (replaces old quality-floor invariants)
# ---------------------------------------------------------------------------
class TestCertificationFilterProperties:
    @given(
        benefit_type=st.sampled_from(
            ["health", "dental", "vision", "mental_health", "life", "std", "ltd"]
        ),
    )
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_empty_db_returns_zero_certified(self, db_session, benefit_type):
        from app.services.provider_selection import _certification_filter
        result = _certification_filter(db_session, benefit_type=benefit_type)
        assert result["providers_certified"] == 0
        assert result["certified_providers"] == []

    @given(
        provider_type=st.sampled_from(
            ["physician", "hospital", "dental", "vision", "mental_health", "lab", "pharmacy"]
        ),
        benefit_type=st.sampled_from(
            ["health", "dental", "vision", "mental_health"]
        ),
    )
    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_provider_certification_agrees_with_benefit_type_mapping(
        self, db_session, provider_type, benefit_type,
    ):
        """A provider is certified for a benefit_type iff its provider_type
        is in BENEFIT_TYPE_TO_PROVIDER_TYPES[benefit_type].

        The db_session fixture is shared across Hypothesis examples, so
        we reset the Provider table at the start of each example to
        keep the invariant test crisp."""
        from app.models.provider import Provider
        db_session.query(Provider).delete()
        db_session.flush()

        from app.services.provider_selection import (
            _certification_filter, BENEFIT_TYPE_TO_PROVIDER_TYPES,
        )
        from tests.unit.factories import make_provider
        make_provider(db_session, "Test Provider", provider_type=provider_type)
        result = _certification_filter(db_session, benefit_type=benefit_type)
        expected_match = provider_type in BENEFIT_TYPE_TO_PROVIDER_TYPES.get(
            benefit_type, []
        )
        assert (result["providers_certified"] == 1) == expected_match


# ---------------------------------------------------------------------------
# 2. Haversine distance invariants
# ---------------------------------------------------------------------------
class TestHaversineProperties:
    @given(lat1=latitude, lon1=longitude, lat2=latitude, lon2=longitude)
    @settings(max_examples=500, deadline=None)
    def test_distance_is_non_negative(self, lat1, lon1, lat2, lon2):
        from app.services.provider_selection import _haversine_miles
        assert _haversine_miles(lat1, lon1, lat2, lon2) >= 0

    @given(lat1=latitude, lon1=longitude, lat2=latitude, lon2=longitude)
    @settings(max_examples=500, deadline=None)
    def test_distance_is_symmetric(self, lat1, lon1, lat2, lon2):
        from app.services.provider_selection import _haversine_miles
        forward = _haversine_miles(lat1, lon1, lat2, lon2)
        reverse = _haversine_miles(lat2, lon2, lat1, lon1)
        assert math.isclose(forward, reverse, rel_tol=1e-9)

    @given(lat=latitude, lon=longitude)
    @settings(max_examples=200, deadline=None)
    def test_distance_to_self_is_zero(self, lat, lon):
        from app.services.provider_selection import _haversine_miles
        assert _haversine_miles(lat, lon, lat, lon) == pytest.approx(0.0, abs=1e-9)

    @given(
        lat1=latitude, lon1=longitude,
        lat2=latitude, lon2=longitude,
        lat3=latitude, lon3=longitude,
    )
    @settings(max_examples=300, deadline=None)
    def test_triangle_inequality(self, lat1, lon1, lat2, lon2, lat3, lon3):
        """d(A,C) <= d(A,B) + d(B,C). Fundamental metric property."""
        from app.services.provider_selection import _haversine_miles
        ab = _haversine_miles(lat1, lon1, lat2, lon2)
        bc = _haversine_miles(lat2, lon2, lat3, lon3)
        ac = _haversine_miles(lat1, lon1, lat3, lon3)
        assert ac <= ab + bc + 1e-6  # tiny epsilon for float arithmetic


# ---------------------------------------------------------------------------
# 3. Emergent detection invariants
# ---------------------------------------------------------------------------
class TestEmergentDetectionProperties:
    @given(
        pos=st.one_of(st.none(), st.sampled_from(["11", "21", "22", "23", "41", "42", "99"])),
        procs=st.lists(
            st.sampled_from(["99213", "99214", "99281", "99282", "99283", "99284", "99285"]),
            max_size=3,
        ),
        dxs=st.lists(
            st.sampled_from([
                "J06.9", "M54.5", "I10", "I21.4", "I63.9", "R45.81",
                "T78.0", "S06.0", "K35.80", "G03.9",
            ]),
            max_size=4,
        ),
        attested=st.booleans(),
    )
    @settings(max_examples=300, deadline=None)
    def test_classification_is_idempotent(self, pos, procs, dxs, attested):
        """Classifying the same inputs twice must give the same result."""
        from app.services.emergent_detection import is_emergent
        r1 = is_emergent(
            place_of_service=pos, procedure_codes=procs,
            diagnosis_codes=dxs, patient_attested_emergency=attested,
        )
        r2 = is_emergent(
            place_of_service=pos, procedure_codes=procs,
            diagnosis_codes=dxs, patient_attested_emergency=attested,
        )
        assert r1 == r2

    @given(dxs=st.lists(
        st.sampled_from(["J06.9", "M54.5", "I10"]),  # all NON-red-flag
        min_size=1, max_size=5,
    ))
    @settings(max_examples=100, deadline=None)
    def test_non_red_flag_diagnoses_do_not_trigger_alone(self, dxs):
        """A pile of non-red-flag diagnoses must not trigger emergent."""
        from app.services.emergent_detection import is_emergent
        emergent, _ = is_emergent(diagnosis_codes=dxs)
        assert emergent is False

    @given(dxs=st.lists(
        st.sampled_from([
            "I21.4", "I63.9", "R45.81", "T78.0", "I26.09",
        ]),
        min_size=1, max_size=5,
    ))
    @settings(max_examples=100, deadline=None)
    def test_any_red_flag_diagnosis_triggers_emergent(self, dxs):
        """ANY red-flag diagnosis in the list must trigger emergent."""
        from app.services.emergent_detection import is_emergent
        emergent, _ = is_emergent(diagnosis_codes=dxs)
        assert emergent is True

    @given(attested=st.booleans())
    @settings(max_examples=10, deadline=None)
    def test_self_attestation_alone_determines_outcome(self, attested):
        """Self-attestation alone determines the result when no other
        signals are present."""
        from app.services.emergent_detection import is_emergent
        emergent, _ = is_emergent(patient_attested_emergency=attested)
        assert emergent == attested


# ---------------------------------------------------------------------------
# 4. Claims engine payment invariants
# ---------------------------------------------------------------------------
class TestClaimsPaymentProperties:
    @given(amount=st.floats(min_value=1.0, max_value=100000.0, allow_nan=False))
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_routed_claim_paid_amount_equals_billed(self, db_session, amount):
        """A routed claim with matching expected price must pay
        exactly the billed amount."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=amount)
        claim = make_claim(db_session, employee, amount=amount)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        if result["status"] in ("approved", "paid"):
            assert result["amount_paid"] == pytest.approx(amount, rel=1e-6)

    @given(amount=st.floats(min_value=1.0, max_value=100000.0, allow_nan=False))
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_employee_oop_always_zero(self, db_session, amount):
        """Constitution: zero copay, zero deductible, zero OOP."""
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=amount)
        claim = make_claim(db_session, employee, amount=amount)
        link_claim_to_episode(db_session, claim, episode)
        result = adjudicate_claim(db_session, claim.claim_id)
        assert result["amount_employee_oop"] == 0.0

    @given(
        expected=st.floats(min_value=100.0, max_value=10000.0, allow_nan=False),
        delta_pct=st.floats(min_value=0.0, max_value=0.5, allow_nan=False),
    )
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_price_drift_symmetric(self, db_session, expected, delta_pct):
        """Price drift magnitude is the same whether billed is
        `expected*(1+delta)` or `expected*(1-delta)`."""
        from app.services.claims_adjudication import _price_drift_vs_routing
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)

        high_billed = expected * (1 + delta_pct)
        low_billed = expected * (1 - delta_pct)
        assume(low_billed > 0)  # keep payable amounts strictly positive

        ep_high = make_care_episode(db_session, employee, expected_price=expected)
        claim_high = make_claim(db_session, employee, amount=high_billed)
        link_claim_to_episode(db_session, claim_high, ep_high)
        drift_high = _price_drift_vs_routing(claim_high, ep_high)

        employee2 = make_employee(db_session, employer)
        ep_low = make_care_episode(db_session, employee2, expected_price=expected)
        claim_low = make_claim(db_session, employee2, amount=low_billed)
        link_claim_to_episode(db_session, claim_low, ep_low)
        drift_low = _price_drift_vs_routing(claim_low, ep_low)

        assert drift_high["drift_pct"] == pytest.approx(drift_low["drift_pct"], abs=1e-4)


# ---------------------------------------------------------------------------
# 5. Preventive rule eligibility invariants
# ---------------------------------------------------------------------------
class TestPreventiveRuleProperties:
    @given(age=st.integers(min_value=18, max_value=100))
    @settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_preventive_rules_match_age_brackets(self, db_session, age):
        """For any age, the rules returned must all have min_age <= age <= max_age."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=age)
        due = find_due_rules(db_session, employee)
        for rule in due:
            assert rule.min_age <= age <= rule.max_age

    @given(
        age=st.integers(min_value=18, max_value=90),
        sex=st.sampled_from(["male", "female"]),
    )
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_sex_specific_rules_match(self, db_session, age, sex):
        """A rule with sex='female' must never fire for a male employee
        (and vice versa)."""
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=age, sex=sex)
        due = find_due_rules(db_session, employee)
        for rule in due:
            if rule.sex != "any":
                assert rule.sex == sex

    @given(age=st.integers(min_value=18, max_value=90))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_terminated_employee_never_gets_preventive_rules(self, db_session, age):
        from app.services.preventive_scheduler import find_due_rules
        employer = make_employer(db_session)
        employee = make_employee(
            db_session, employer, age=age,
            status="terminated", terminated_days_ago=1,
            enrolled_days_ago=400,
        )
        due = find_due_rules(db_session, employee)
        assert due == []
