"""Tests for the operational testing infrastructure.

Covers:
  • Shadow mode runner — parallel engine comparison without payment side effects
  • Clinical review exporter — deterministic sampling + reviewer aggregation
  • Outcomes feedback loop — inferred outcome recording + accuracy reporting
"""

from __future__ import annotations

import uuid
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
# Shadow runner
# ---------------------------------------------------------------------------
class TestShadowRunner:
    def test_empty_batch_returns_clean_report(self, db_session):
        from app.services.shadow_runner import run_shadow_batch
        report = run_shadow_batch(db_session, [])
        assert report["total_claims"] == 0
        assert report["agreement_rate_pct"] is None
        assert report["disagreements"] == []

    def test_agreeing_claim_is_counted(self, db_session):
        from app.services.shadow_runner import run_shadow_batch, ShadowReference
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(db_session, employee, expected_price=200.00)
        claim = make_claim(db_session, employee, amount=200.00)
        link_claim_to_episode(db_session, claim, episode)

        references = [
            ShadowReference(
                claim_id=claim.claim_id,
                reference_decision="approved",
                reference_amount=200.00,
                reference_reason="carrier paid",
            )
        ]
        report = run_shadow_batch(db_session, references)
        assert report["evaluated"] == 1
        assert report["agreements"] == 1
        assert report["agreement_rate_pct"] == 100.0
        assert report["disagreements_count"] == 0

    def test_disagreeing_decision_is_flagged(self, db_session):
        from app.services.shadow_runner import run_shadow_batch, ShadowReference
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        # Unrouted, non-emergent — engine will deny
        claim = make_claim(db_session, employee, amount=350.00)
        references = [
            ShadowReference(
                claim_id=claim.claim_id,
                reference_decision="approved",  # carrier approved
                reference_amount=350.00,
            )
        ]
        report = run_shadow_batch(db_session, references)
        assert report["disagreements_count"] == 1
        disagreement = report["disagreements"][0]
        assert disagreement["engine_decision"] == "denied"
        assert disagreement["reference_decision"] == "approved"

    def test_nonexistent_claim_returns_error_not_crash(self, db_session):
        from app.services.shadow_runner import run_shadow_batch, ShadowReference
        references = [ShadowReference(claim_id=uuid.uuid4(), reference_decision="approved")]
        report = run_shadow_batch(db_session, references)
        assert report["agreements"] == 0
        assert len(report["errors"]) == 1

    def test_shadow_run_does_not_mutate_claim(self, db_session):
        """Shadow mode must NEVER change the persisted claim state."""
        from app.services.shadow_runner import run_shadow_batch, ShadowReference
        from app.models.claim import Claim, ClaimStatus
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        # Unrouted claim (will be denied by the engine)
        claim = make_claim(db_session, employee, amount=500.00)
        original_status = claim.status
        original_reasoning = claim.adjudication_reasoning

        references = [
            ShadowReference(
                claim_id=claim.claim_id, reference_decision="approved",
            )
        ]
        run_shadow_batch(db_session, references)

        db_session.expire_all()
        refreshed = db_session.query(Claim).filter(
            Claim.claim_id == claim.claim_id
        ).one()
        assert refreshed.status == original_status
        assert refreshed.adjudication_reasoning == original_reasoning


# ---------------------------------------------------------------------------
# Clinical review exporter
# ---------------------------------------------------------------------------
class TestClinicalReviewExport:
    def test_empty_database_returns_empty_package(self, db_session):
        from app.services.clinical_review_export import export_review_package
        package = export_review_package(db_session, sample_size=20)
        assert package["case_count"] == 0
        assert package["cases"] == []

    def test_deterministic_sampling_with_seed(self, db_session):
        """Same seed + same data → same case order."""
        from app.services.clinical_review_export import export_review_package
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        # Create a bunch of denied claims
        for _ in range(10):
            employee = make_employee(db_session, employer)
            claim = make_claim(db_session, employee)
            adjudicate_claim(db_session, claim.claim_id)

        p1 = export_review_package(db_session, sample_size=5, seed=42)
        p2 = export_review_package(db_session, sample_size=5, seed=42)
        case_ids_1 = [c["case_id"] for c in p1["cases"]]
        case_ids_2 = [c["case_id"] for c in p2["cases"]]
        assert case_ids_1 == case_ids_2

    def test_every_case_has_complete_question_template(self, db_session):
        from app.services.clinical_review_export import export_review_package
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        claim = make_claim(db_session, employee)
        adjudicate_claim(db_session, claim.claim_id)

        package = export_review_package(db_session, sample_size=5, seed=1)
        for case in package["cases"]:
            assert "questions" in case
            ids = {q["id"] for q in case["questions"]}
            assert ids == {
                "agree_with_decision",
                "evidence_appropriate",
                "reasoning_accurate",
                "severity_concern",
                "free_text_notes",
            }

    def test_patient_summary_is_deidentified(self, db_session):
        """The exported patient summary must not include raw age,
        real birth date, or raw name."""
        from app.services.clinical_review_export import export_review_package
        from app.services.claims_adjudication import adjudicate_claim
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer, age=47, sex="female")
        claim = make_claim(db_session, employee)
        adjudicate_claim(db_session, claim.claim_id)
        package = export_review_package(db_session, sample_size=5, seed=1)
        assert package["case_count"] > 0
        for case in package["cases"]:
            if case["source"] != "claim":
                continue
            summary = case["patient_summary"]
            # Must have an age BAND, not a raw age
            assert "age_band" in summary
            assert summary["age_band"] in [
                "<18", "18-24", "25-34", "35-49", "50-64", "65+", "unknown"
            ]
            # Must NOT leak raw numeric age
            assert "age" not in summary

    def test_aggregate_reviewer_ratings(self):
        """Aggregating filled-in reviews produces correct accuracy metrics."""
        from app.services.clinical_review_export import aggregate_reviewer_ratings
        package = {
            "cases": [
                {
                    "case_id": "c1",
                    "decision": "approved",
                    "reviewer_rating": {
                        "reviewer_id": "dr-a",
                        "responses": {
                            "agree_with_decision": "yes",
                            "evidence_appropriate": "yes",
                            "reasoning_accurate": "yes",
                            "severity_concern": 1,
                        },
                    },
                },
                {
                    "case_id": "c2",
                    "decision": "denied",
                    "reviewer_rating": {
                        "reviewer_id": "dr-a",
                        "responses": {
                            "agree_with_decision": "no",
                            "evidence_appropriate": "partial",
                            "reasoning_accurate": "misleading",
                            "severity_concern": 5,
                            "free_text_notes": "This denial was wrong.",
                        },
                    },
                },
                {
                    "case_id": "c3",
                    "decision": "approved",
                    "reviewer_rating": {
                        "reviewer_id": "dr-b",
                        "responses": {
                            "agree_with_decision": "yes",
                            "evidence_appropriate": "yes",
                            "reasoning_accurate": "yes",
                            "severity_concern": 2,
                        },
                    },
                },
            ]
        }
        summary = aggregate_reviewer_ratings(package)
        assert summary["reviewed_count"] == 3
        assert summary["agreement_rate_pct"] == pytest.approx(66.7, abs=0.5)
        assert summary["disagreement_rate_pct"] == pytest.approx(33.3, abs=0.5)
        assert summary["mean_severity"] == pytest.approx(2.67, abs=0.1)
        assert len(summary["high_concern_cases"]) == 1
        assert summary["high_concern_cases"][0]["case_id"] == "c2"


# ---------------------------------------------------------------------------
# Outcomes feedback loop
# ---------------------------------------------------------------------------
class TestOutcomesFeedback:
    def _create_determination(
        self, db_session, decision="approved", created_days_ago=40,
        claim_id=None, benefit_type="health",
    ):
        from app.models.clinical_determination import (
            ClinicalDetermination, DeterminationDecision,
        )
        det = ClinicalDetermination(
            claim_id=str(claim_id) if claim_id else None,
            benefit_type=benefit_type,
            decision=DeterminationDecision(decision),
            reasoning="Test reasoning",
            created_at=datetime.now(UTC) - timedelta(days=created_days_ago),
            audit_hash="test-hash-" + str(uuid.uuid4()),
        )
        db_session.add(det)
        db_session.flush()
        return det

    def test_run_outcomes_with_no_pending_determinations(self, db_session):
        from app.services.outcomes_feedback import run_outcomes_feedback
        summary = run_outcomes_feedback(db_session)
        assert summary["determinations_processed"] == 0
        assert summary["accuracy_pct"] is None

    def test_resolved_episode_infers_correct_outcome(self, db_session):
        from app.services.outcomes_feedback import (
            run_outcomes_feedback, OUTCOME_CORRECT,
        )
        from app.models.care_episode import EpisodeStatus
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)
        episode = make_care_episode(
            db_session, employee, expected_price=200.00,
            status="resolved",  # resolved
        )
        episode.resolved_at = datetime.now(UTC) - timedelta(days=35)
        claim = make_claim(db_session, employee, amount=200.00)
        link_claim_to_episode(db_session, claim, episode)

        self._create_determination(
            db_session, decision="approved",
            claim_id=claim.claim_id, created_days_ago=40,
        )

        summary = run_outcomes_feedback(db_session)
        assert summary["determinations_processed"] == 1
        assert summary["counts_by_outcome"][OUTCOME_CORRECT] == 1

    def test_escalation_after_denial_is_flagged_incorrect(self, db_session):
        from app.services.outcomes_feedback import (
            run_outcomes_feedback, OUTCOME_INCORRECT,
        )
        employer = make_employer(db_session)
        employee = make_employee(db_session, employer)

        # Earlier denied claim + determination
        denied_claim = make_claim(db_session, employee, amount=150.00, days_ago=45)
        det = self._create_determination(
            db_session, decision="denied",
            claim_id=denied_claim.claim_id, created_days_ago=45,
        )

        # Later emergent claim — the escalation
        from app.models.claim import Claim, ClaimMode, ClaimStatus
        from app.models.service import BenefitType
        escalation = Claim(
            employer_id=employer.employer_id,
            employee_id=employee.employee_id,
            benefit_type=BenefitType.health,
            mode=ClaimMode.live,
            status=ClaimStatus.approved,
            amount_billed=25000.00,
            submitted_at=datetime.now(UTC) - timedelta(days=35),
            date_of_service=datetime.now(UTC) - timedelta(days=35),
            is_emergent=True,
        )
        db_session.add(escalation)
        db_session.flush()

        summary = run_outcomes_feedback(db_session)
        assert summary["determinations_processed"] >= 1
        assert summary["counts_by_outcome"].get(OUTCOME_INCORRECT, 0) >= 1
        assert summary["flagged_for_review_count"] >= 1

    def test_accuracy_report_aggregates_by_benefit_type(self, db_session):
        from app.services.outcomes_feedback import (
            get_accuracy_report, OUTCOME_CORRECT, OUTCOME_INCORRECT,
        )
        # Seed a handful of determinations with recorded outcomes
        for _ in range(3):
            det = self._create_determination(
                db_session, decision="approved", benefit_type="health",
                created_days_ago=20,
            )
            det.outcome_feedback = OUTCOME_CORRECT
            det.outcome_recorded_at = datetime.now(UTC)
        det_bad = self._create_determination(
            db_session, decision="denied", benefit_type="health",
            created_days_ago=20,
        )
        det_bad.outcome_feedback = OUTCOME_INCORRECT
        det_bad.outcome_recorded_at = datetime.now(UTC)
        db_session.commit()

        report = get_accuracy_report(db_session)
        assert report["overall_counts"][OUTCOME_CORRECT] == 3
        assert report["overall_counts"][OUTCOME_INCORRECT] == 1
        assert report["overall_accuracy_pct"] == 75.0
        assert "health" in report["by_benefit_type"]
        assert report["by_benefit_type"]["health"]["accuracy_pct"] == 75.0
