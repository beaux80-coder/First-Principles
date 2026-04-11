"""Clinical Determination Testing Harness.

Generates 500 realistic patient scenarios via the Anthropic API, runs
each through the clinical determination engine (`make_determination`),
compares the engine's decisions to Claude's clinical judgment, and
produces a detailed report.

Usage:
    # Generate + run + report (requires ANTHROPIC_API_KEY env var)
    python -m tests.harness.run_harness

    # Reuse previously generated scenarios (no API call)
    python -m tests.harness.run_harness --no-generate

    # Custom file paths
    python -m tests.harness.run_harness --scenarios-file my_scenarios.json --report-file my_report.json

    # Fewer scenarios for a quick test
    python -m tests.harness.run_harness --total 50 --batch-size 25
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("harness")


HARNESS_DIR = Path(__file__).parent
DEFAULT_SCENARIOS_FILE = HARNESS_DIR / "scenarios.json"
DEFAULT_REPORT_FILE = HARNESS_DIR / "report.json"


# ---------------------------------------------------------------------------
# DB setup — mirrors tests/unit/conftest.py
# ---------------------------------------------------------------------------
def setup_db():
    """Create an in-memory SQLite database with all models loaded."""
    os.environ.setdefault("ENCRYPTION_KEY", "harness-test-key")

    from app.config import settings
    if not settings.encryption_key:
        settings.encryption_key = "harness-test-key"

    import app.database as db_module
    from app.database import Base
    from app.models import (  # noqa: F401 — side-effect model registration
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

    db_module.SessionLocal = SessionLocal
    return session


def seed_guidelines(db) -> int:
    """Seed the database with CMS NCD clinical guidelines."""
    from app.services.clinical_guidelines_ingester import ingest_cms_ncd_guidelines
    count = ingest_cms_ncd_guidelines(db)
    logger.info("Seeded %d clinical guidelines", count)
    return count


# ---------------------------------------------------------------------------
# Engine execution
# ---------------------------------------------------------------------------
def run_scenario(db, scenario: dict) -> dict:
    """Run a single scenario through make_determination and return the
    engine result alongside the scenario and comparison."""
    from app.services.clinical_engine import make_determination

    try:
        det = make_determination(
            db=db,
            claim_id=f"harness-{scenario.get('scenario_id', 'unknown')}",
            service_code=scenario.get("service_code", "99213"),
            benefit_type=scenario.get("benefit_type", "health"),
            patient_symptoms=scenario.get("symptoms", []),
            patient_history={
                "age": scenario.get("age", 30),
                "sex": scenario.get("sex", ""),
                "diagnoses": scenario.get("diagnoses", []),
                "risk_factors": scenario.get("risk_factors", []),
                "medications": scenario.get("medications", []),
            },
            condition=scenario.get("condition"),
        )

        engine_decision = (
            det.decision.value if hasattr(det.decision, "value") else str(det.decision)
        )

        return {
            "scenario": scenario,
            "engine_decision": engine_decision,
            "engine_reasoning": det.reasoning,
            "engine_guidelines_referenced": det.guidelines_referenced or [],
            "engine_risk_score": det.risk_score,
            "engine_latency_ms": det.latency_ms,
            "error": None,
        }

    except Exception as e:
        logger.error(
            "Engine error on scenario %s: %s",
            scenario.get("scenario_id", "?"), e,
        )
        return {
            "scenario": scenario,
            "engine_decision": "error",
            "engine_reasoning": str(e),
            "engine_guidelines_referenced": [],
            "engine_risk_score": None,
            "engine_latency_ms": None,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def _claude_to_engine_decision(claude_det: str) -> str:
    """Map Claude's determination to the engine's decision vocabulary."""
    if claude_det == "medically_necessary":
        return "approved"
    return "denied"


def compare_results(results: list[dict]) -> dict:
    """Build the full comparison report from scenario results."""
    now = datetime.now(UTC)

    total = len(results)
    agreements = 0
    false_denials: list[dict] = []
    false_approvals: list[dict] = []
    errors: list[dict] = []
    latencies: list[float] = []

    by_category: dict[str, dict] = {}
    by_benefit_type: dict[str, dict] = {}

    for r in results:
        scenario = r["scenario"]
        engine_dec = r["engine_decision"]
        claude_det = scenario.get("claude_determination", "")
        claude_dec = _claude_to_engine_decision(claude_det)

        category = scenario.get("category", "unknown")
        benefit_type = scenario.get("benefit_type", "unknown")

        # Initialize category and benefit_type buckets
        for bucket_key, bucket_map in [
            (category, by_category),
            (benefit_type, by_benefit_type),
        ]:
            if bucket_key not in bucket_map:
                bucket_map[bucket_key] = {
                    "total": 0,
                    "agreements": 0,
                    "false_denials": 0,
                    "false_approvals": 0,
                    "errors": 0,
                }
            bucket_map[bucket_key]["total"] += 1

        if r["error"]:
            errors.append(r)
            by_category[category]["errors"] += 1
            by_benefit_type[benefit_type]["errors"] += 1
            continue

        if r["engine_latency_ms"] is not None:
            latencies.append(r["engine_latency_ms"])

        # Compare
        if engine_dec == claude_dec:
            agreements += 1
            by_category[category]["agreements"] += 1
            by_benefit_type[benefit_type]["agreements"] += 1
        elif engine_dec == "denied" and claude_dec == "approved":
            # FALSE DENIAL — engine denied something Claude says is necessary
            disagreement = _build_disagreement(r, high_priority=True)
            false_denials.append(disagreement)
            by_category[category]["false_denials"] += 1
            by_benefit_type[benefit_type]["false_denials"] += 1
        elif engine_dec in ("approved", "modified") and claude_dec == "denied":
            # FALSE APPROVAL — engine approved something Claude says is not necessary
            disagreement = _build_disagreement(r, high_priority=False)
            false_approvals.append(disagreement)
            by_category[category]["false_approvals"] += 1
            by_benefit_type[benefit_type]["false_approvals"] += 1
        else:
            # Engine returned "modified" and Claude said approved, or other edge
            # Treat as agreement (modified is a partial approval)
            agreements += 1
            by_category[category]["agreements"] += 1
            by_benefit_type[benefit_type]["agreements"] += 1

    evaluated = total - len(errors)
    avg_latency = (
        round(sum(latencies) / len(latencies), 2) if latencies else None
    )

    return {
        "metadata": {
            "generated_at": now.isoformat(),
            "total_scenarios": total,
            "engine_version": "clinical_engine.make_determination",
            "model_used_for_answer_key": "claude-sonnet-4-20250514",
        },
        "summary": {
            "total": total,
            "evaluated": evaluated,
            "errors": len(errors),
            "agreements": agreements,
            "agreement_rate_pct": (
                round(agreements / evaluated * 100, 1) if evaluated > 0 else None
            ),
            "false_denials": len(false_denials),
            "false_denials_pct": (
                round(len(false_denials) / evaluated * 100, 1) if evaluated > 0 else None
            ),
            "false_approvals": len(false_approvals),
            "false_approvals_pct": (
                round(len(false_approvals) / evaluated * 100, 1) if evaluated > 0 else None
            ),
            "avg_engine_latency_ms": avg_latency,
        },
        "by_category": by_category,
        "by_benefit_type": by_benefit_type,
        "false_denials": false_denials,
        "false_approvals": false_approvals,
        "errors": [
            {
                "scenario_id": e["scenario"].get("scenario_id"),
                "error": e["error"],
            }
            for e in errors
        ],
        "all_results": results,
    }


def _build_disagreement(result: dict, high_priority: bool) -> dict:
    scenario = result["scenario"]
    return {
        "HIGH_PRIORITY": high_priority,
        "scenario_id": scenario.get("scenario_id"),
        "category": scenario.get("category"),
        "patient": {
            "age": scenario.get("age"),
            "sex": scenario.get("sex"),
            "diagnoses": scenario.get("diagnoses"),
            "symptoms": scenario.get("symptoms"),
            "risk_factors": scenario.get("risk_factors"),
            "medications": scenario.get("medications"),
            "condition": scenario.get("condition"),
        },
        "service_requested": scenario.get("service_requested"),
        "service_code": scenario.get("service_code"),
        "benefit_type": scenario.get("benefit_type"),
        "engine_decision": result["engine_decision"],
        "engine_reasoning": result["engine_reasoning"],
        "engine_risk_score": result["engine_risk_score"],
        "engine_guidelines_referenced": result["engine_guidelines_referenced"],
        "claude_determination": scenario.get("claude_determination"),
        "claude_rationale": scenario.get("claude_rationale"),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_summary(report: dict) -> None:
    """Print the summary to stdout."""
    s = report["summary"]
    print()
    print("=" * 72)
    print("  CLINICAL DETERMINATION TESTING HARNESS — RESULTS")
    print("=" * 72)
    print()
    print(f"  Total scenarios:      {s['total']}")
    print(f"  Evaluated:            {s['evaluated']}")
    print(f"  Errors:               {s['errors']}")
    print(f"  Avg engine latency:   {s['avg_engine_latency_ms']} ms")
    print()
    print(f"  Agreements:           {s['agreements']}  ({s['agreement_rate_pct']}%)")
    print(f"  False denials:        {s['false_denials']}  ({s['false_denials_pct']}%)")
    print(f"  False approvals:      {s['false_approvals']}  ({s['false_approvals_pct']}%)")
    print()

    # False denials are the dangerous ones
    if report["false_denials"]:
        print("-" * 72)
        print("  ⚠  HIGH PRIORITY FALSE DENIALS")
        print("  Engine denied these services, but clinical judgment says")
        print("  they are medically necessary. Each one is a potential")
        print("  case where a patient would be incorrectly denied care.")
        print("-" * 72)
        for i, fd in enumerate(report["false_denials"], 1):
            print()
            print(f"  [{i}] {fd['scenario_id']}")
            print(f"      Patient: {fd['patient']['age']}yo {fd['patient']['sex']}")
            print(f"      Condition: {fd['patient']['condition']}")
            print(f"      Service: {fd['service_requested']} ({fd['service_code']})")
            print(f"      Diagnoses: {', '.join(fd['patient']['diagnoses'][:3])}")
            print(f"      Symptoms: {', '.join(fd['patient']['symptoms'][:3])}")
            print(f"      Engine: DENIED — {fd['engine_reasoning'][:150]}...")
            print(f"      Claude: NECESSARY — {fd['claude_rationale']}")
            if fd["engine_risk_score"] is not None:
                print(f"      Risk score: {fd['engine_risk_score']:.2f}")
        print()

    # False approvals (less dangerous but worth reviewing)
    if report["false_approvals"]:
        print("-" * 72)
        print(f"  FALSE APPROVALS ({len(report['false_approvals'])} total)")
        print("  Engine approved these, but clinical judgment says they")
        print("  are NOT medically necessary. Less dangerous (universal")
        print("  policy approves on uncertainty) but worth reviewing.")
        print("-" * 72)
        for i, fa in enumerate(report["false_approvals"][:10], 1):
            print()
            print(f"  [{i}] {fa['scenario_id']}")
            print(f"      Service: {fa['service_requested']} ({fa['service_code']})")
            print(f"      Engine: APPROVED — {fa['engine_reasoning'][:120]}...")
            print(f"      Claude: NOT NECESSARY — {fa['claude_rationale']}")
        if len(report["false_approvals"]) > 10:
            print(f"\n  ... and {len(report['false_approvals']) - 10} more (see report.json)")
        print()

    # Category breakdown
    print("-" * 72)
    print("  BREAKDOWN BY CATEGORY")
    print("-" * 72)
    print(f"  {'Category':<30} {'Total':>5} {'Agree':>5} {'FDen':>5} {'FApp':>5} {'Err':>4}")
    for cat, stats in sorted(report["by_category"].items()):
        print(
            f"  {cat:<30} {stats['total']:>5} "
            f"{stats['agreements']:>5} {stats['false_denials']:>5} "
            f"{stats['false_approvals']:>5} {stats['errors']:>4}"
        )
    print()

    # Benefit type breakdown
    print("-" * 72)
    print("  BREAKDOWN BY BENEFIT TYPE")
    print("-" * 72)
    print(f"  {'Benefit Type':<20} {'Total':>5} {'Agree':>5} {'FDen':>5} {'FApp':>5} {'Err':>4}")
    for bt, stats in sorted(report["by_benefit_type"].items()):
        print(
            f"  {bt:<20} {stats['total']:>5} "
            f"{stats['agreements']:>5} {stats['false_denials']:>5} "
            f"{stats['false_approvals']:>5} {stats['errors']:>4}"
        )
    print()
    print("=" * 72)
    print()


def save_report(report: dict, path: str | Path) -> None:
    """Save the full report to a JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("Saved report to %s", path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Clinical Determination Testing Harness",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Skip scenario generation; load from --scenarios-file instead",
    )
    parser.add_argument(
        "--scenarios-file",
        default=str(DEFAULT_SCENARIOS_FILE),
        help=f"Path to scenarios JSON file (default: {DEFAULT_SCENARIOS_FILE})",
    )
    parser.add_argument(
        "--report-file",
        default=str(DEFAULT_REPORT_FILE),
        help=f"Path to output report JSON file (default: {DEFAULT_REPORT_FILE})",
    )
    parser.add_argument(
        "--total", type=int, default=500,
        help="Total number of scenarios to generate (default: 500)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=25,
        help="Scenarios per API batch (default: 25)",
    )
    args = parser.parse_args()

    # ---- Step 1: Get scenarios ----
    if args.no_generate:
        logger.info("Loading cached scenarios from %s", args.scenarios_file)
        from tests.harness.scenario_generator import load_scenarios
        scenarios = load_scenarios(args.scenarios_file)
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            from app.config import settings
            api_key = settings.anthropic_api_key
        if not api_key:
            print(
                "ERROR: ANTHROPIC_API_KEY environment variable is required "
                "for scenario generation. Set it or use --no-generate with "
                "a cached scenarios file.",
                file=sys.stderr,
            )
            sys.exit(1)

        logger.info("Generating %d scenarios in batches of %d...", args.total, args.batch_size)
        from tests.harness.scenario_generator import generate_scenarios, save_scenarios
        scenarios = generate_scenarios(
            api_key=api_key,
            batch_size=args.batch_size,
            total=args.total,
        )
        save_scenarios(scenarios, args.scenarios_file)
        logger.info("Saved %d scenarios to %s", len(scenarios), args.scenarios_file)

    if not scenarios:
        print("ERROR: No scenarios available. Cannot run harness.", file=sys.stderr)
        sys.exit(1)

    # ---- Step 2: Setup DB and seed guidelines ----
    logger.info("Setting up in-memory database...")
    db = setup_db()
    guideline_count = seed_guidelines(db)

    # ---- Step 3: Run each scenario through the engine ----
    logger.info("Running %d scenarios through the engine...", len(scenarios))
    results: list[dict] = []
    start_time = time.monotonic()

    for i, scenario in enumerate(scenarios):
        result = run_scenario(db, scenario)
        results.append(result)
        if (i + 1) % 50 == 0:
            elapsed = time.monotonic() - start_time
            logger.info(
                "  %d/%d scenarios processed (%.1fs elapsed)",
                i + 1, len(scenarios), elapsed,
            )

    total_elapsed = time.monotonic() - start_time
    logger.info(
        "Engine processing complete: %d scenarios in %.1fs (%.1f ms/scenario)",
        len(results), total_elapsed,
        total_elapsed / len(results) * 1000 if results else 0,
    )

    # ---- Step 4: Compare and report ----
    report = compare_results(results)
    report["metadata"]["guidelines_seeded"] = guideline_count
    report["metadata"]["engine_processing_seconds"] = round(total_elapsed, 2)

    print_summary(report)
    save_report(report, args.report_file)

    print(f"Full report saved to: {args.report_file}")
    print(f"Scenarios cached at:  {args.scenarios_file}")

    # Exit with non-zero status if there are false denials
    if report["false_denials"]:
        print(
            f"\n⚠  {len(report['false_denials'])} HIGH PRIORITY false denials "
            f"found. Review the report."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
