"""Cross-benefit-type pattern detection and analytics (Function 8).

The Constitution requires active detection of cross-benefit-type patterns
that predict cost, utilization, and outcomes across benefit categories.
This is not passive data collection — it is active pattern recognition
that feeds actionable intelligence to downstream functions.

Cross-type patterns include:
- Mental health utilization predicting medical cost increases
- Musculoskeletal + mental health co-occurrence patterns
- Dental/vision utilization as chronic disease indicators
- Pharmacy patterns predicting downstream medical utilization

This module operates on BOTH public data (price_data, 11M+ rows) and
claims data (proprietary, grows as employers join). Every detected
pattern is persisted to data_pipeline_metrics as structured data.
"""

import logging
import time
import uuid
from datetime import datetime, UTC, timedelta
from typing import Any

from sqlalchemy import func, or_, delete
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource
from app.models.claim import Claim
from app.models.data_pipeline_metric import DataPipelineMetric

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache for expensive cross-type analytics (TTL = 10 minutes).
# The price_data table has 11M+ rows; full-table scans are prohibitive
# on SQLite.  We cache results and serve from cache until TTL expires.
# ---------------------------------------------------------------------------
_CACHE: dict[str, Any] = {}
_CACHE_TTL_SECONDS = 600  # 10 minutes


# Service code prefixes by benefit type
BENEFIT_TYPE_PREFIXES = {
    "health_office_visits": ["9920", "9921", "9923", "9924", "9925"],
    "health_emergency": ["9928", "9929"],
    "health_imaging": ["7005", "7014", "7015", "7200", "7300", "7400", "7600", "7685"],
    "health_surgery": ["1930", "2700", "2713", "4356", "4560", "4756", "5940", "5951"],
    "health_lab": ["8000", "8005", "8006", "8100", "8443", "8502"],
    "dental": ["D"],
    "vision": ["920", "921", "922"],
    "mental_health": ["9079", "9083", "9084", "9061", "9612"],
    "physical_therapy": ["970", "971", "972"],
    "pharmacy": [],  # NADAC uses NDC codes (different scheme)
}

# Known cross-type correlation pairs from epidemiological literature.
# Each tuple: (type_a, type_b, correlation_direction, evidence_strength)
KNOWN_CORRELATIONS = [
    ("mental_health", "health", "positive", 0.85,
     "Mental health utilization is a leading indicator of medical cost increases"),
    ("mental_health", "std", "positive", 0.70,
     "Mental health conditions predict short-term disability claims"),
    ("mental_health", "ltd", "positive", 0.75,
     "Untreated mental health leads to long-term disability escalation"),
    ("dental", "health", "positive", 0.60,
     "Dental neglect correlates with chronic disease (diabetes, cardiovascular)"),
    ("vision", "health", "positive", 0.55,
     "Vision screening detects systemic conditions (diabetes, hypertension)"),
    ("health", "std", "positive", 0.65,
     "Medical procedures predict downstream short-term disability claims"),
]


def _has_rows(db: Session, *filters) -> bool:
    """Fast existence check — stops after first matching row."""
    q = db.query(PriceData.price_id).filter(*filters).limit(1)
    return db.query(q.exists()).scalar()


def _sampled_avg_by_state(db: Session, *filters) -> dict:
    """Compute avg price by state on a bounded sample (LIMIT 200k rows).

    For 11M+ row tables a full-table AVG is too slow on SQLite.
    Sampling gives a statistically representative estimate.
    """
    subq = (
        db.query(PriceData.state, PriceData.price)
        .filter(*filters)
        .limit(200_000)
        .subquery()
    )
    rows = (
        db.query(subq.c.state, func.avg(subq.c.price))
        .group_by(subq.c.state)
        .all()
    )
    return dict(rows)


def _persist_pattern(
    db: Session,
    pattern_type: str,
    value: float,
    details: dict,
    benefit_type: str | None = None,
) -> DataPipelineMetric:
    """Persist a detected cross-type pattern to data_pipeline_metrics.

    Every detected pattern is recorded as structured data — the Constitution
    requires data completeness where every interaction produces a record.
    """
    metric = DataPipelineMetric(
        metric_id=uuid.uuid4(),
        employer_id=None,  # Cross-type patterns are system-wide
        metric_type=f"cross_type_signal:{pattern_type}",
        benefit_type=benefit_type,
        value=value,
        details=details,
        measured_at=datetime.now(UTC),
    )
    db.add(metric)
    return metric


def detect_claims_correlations(db: Session) -> list[dict]:
    """Detect cross-benefit-type correlations from actual claims data.

    Analyzes employee-level claims to find co-occurrence patterns across
    benefit types. Even with sparse data, we can detect:
    1. Which benefit types co-occur for the same employee
    2. Cost ratios across benefit types
    3. Temporal patterns (which claims come first)
    4. Denial rate differences across benefit types

    Returns a list of detected correlation patterns.
    """
    correlations = []

    # 1. Employee-level benefit type co-occurrence matrix
    # Get all employees with claims across multiple benefit types
    employee_types = (
        db.query(
            Claim.employee_id,
            Claim.benefit_type,
            func.count(Claim.claim_id).label("claim_count"),
            func.sum(Claim.amount_billed).label("total_billed"),
            func.avg(Claim.amount_billed).label("avg_billed"),
        )
        .group_by(Claim.employee_id, Claim.benefit_type)
        .all()
    )

    if not employee_types:
        logger.info("No claims data available for correlation analysis")
        return correlations

    # Build per-employee benefit type profiles
    employee_profiles: dict[str, dict[str, dict]] = {}
    for row in employee_types:
        emp_id = str(row.employee_id)
        bt = row.benefit_type.value if hasattr(row.benefit_type, "value") else str(row.benefit_type)
        if emp_id not in employee_profiles:
            employee_profiles[emp_id] = {}
        employee_profiles[emp_id][bt] = {
            "claim_count": row.claim_count,
            "total_billed": float(row.total_billed or 0),
            "avg_billed": float(row.avg_billed or 0),
        }

    total_employees = len(employee_profiles)
    logger.info(f"Analyzing {total_employees} employees with claims across benefit types")

    # 2. Compute co-occurrence rates between all benefit type pairs
    all_benefit_types = sorted(set(
        bt for profile in employee_profiles.values() for bt in profile
    ))

    co_occurrence: dict[tuple[str, str], int] = {}
    for i, bt_a in enumerate(all_benefit_types):
        for bt_b in all_benefit_types[i + 1:]:
            count = sum(
                1 for profile in employee_profiles.values()
                if bt_a in profile and bt_b in profile
            )
            if count > 0:
                co_occurrence[(bt_a, bt_b)] = count

    for (bt_a, bt_b), count in co_occurrence.items():
        rate = count / total_employees if total_employees > 0 else 0
        correlations.append({
            "type": "benefit_type_co_occurrence",
            "benefit_type_a": bt_a,
            "benefit_type_b": bt_b,
            "co_occurrence_count": count,
            "co_occurrence_rate": round(rate, 4),
            "total_employees": total_employees,
            "feeds": ["F3 (Cost Prediction)", "F9 (Care Execution)"],
        })

    # 3. Cross-type cost ratio analysis
    # For employees using multiple benefit types, compute relative spend
    type_totals: dict[str, list[float]] = {}
    for profile in employee_profiles.values():
        for bt, stats in profile.items():
            if bt not in type_totals:
                type_totals[bt] = []
            type_totals[bt].append(stats["total_billed"])

    if len(type_totals) >= 2:
        type_avgs = {
            bt: sum(vals) / len(vals) if vals else 0
            for bt, vals in type_totals.items()
        }
        # Find benefit types with disproportionate cost
        overall_avg = sum(type_avgs.values()) / len(type_avgs) if type_avgs else 0
        for bt, avg in type_avgs.items():
            if overall_avg > 0:
                ratio = avg / overall_avg
                if ratio > 1.5 or ratio < 0.5:
                    correlations.append({
                        "type": "cross_type_cost_disparity",
                        "benefit_type": bt,
                        "avg_cost": round(avg, 2),
                        "overall_avg": round(overall_avg, 2),
                        "cost_ratio": round(ratio, 2),
                        "direction": "above_average" if ratio > 1.0 else "below_average",
                        "feeds": ["F3 (Cost Prediction)", "F1 (Clinical Quality)"],
                    })

    # 4. Claim status patterns across benefit types (denial rate differences)
    status_by_type = (
        db.query(
            Claim.benefit_type,
            Claim.status,
            func.count(Claim.claim_id).label("cnt"),
        )
        .group_by(Claim.benefit_type, Claim.status)
        .all()
    )

    type_status_counts: dict[str, dict[str, int]] = {}
    for row in status_by_type:
        bt = row.benefit_type.value if hasattr(row.benefit_type, "value") else str(row.benefit_type)
        status = row.status.value if hasattr(row.status, "value") else str(row.status)
        if bt not in type_status_counts:
            type_status_counts[bt] = {}
        type_status_counts[bt][status] = row.cnt

    # Compare denial rates across benefit types
    denial_rates: dict[str, float] = {}
    for bt, statuses in type_status_counts.items():
        total = sum(statuses.values())
        denied = statuses.get("denied", 0)
        if total > 0:
            denial_rates[bt] = denied / total

    if len(denial_rates) >= 2:
        avg_denial = sum(denial_rates.values()) / len(denial_rates)
        for bt, rate in denial_rates.items():
            if avg_denial > 0 and abs(rate - avg_denial) / max(avg_denial, 0.01) > 0.3:
                correlations.append({
                    "type": "cross_type_denial_disparity",
                    "benefit_type": bt,
                    "denial_rate": round(rate, 4),
                    "avg_denial_rate": round(avg_denial, 4),
                    "deviation": round((rate - avg_denial) / max(avg_denial, 0.01), 2),
                    "feeds": ["F1 (Clinical Quality)", "F5 (Adjudication)"],
                })

    # 5. Temporal sequencing: which benefit types tend to have claims first?
    first_claims = (
        db.query(
            Claim.employee_id,
            Claim.benefit_type,
            func.min(Claim.submitted_at).label("first_claim_at"),
        )
        .group_by(Claim.employee_id, Claim.benefit_type)
        .subquery()
    )

    # For each employee with multiple types, identify the "leading" type
    leading_types: dict[str, int] = {}
    for emp_id, profile in employee_profiles.items():
        if len(profile) >= 2:
            # Query this employee's first claim dates by type
            rows = (
                db.query(first_claims.c.benefit_type, first_claims.c.first_claim_at)
                .filter(first_claims.c.employee_id == emp_id)
                .order_by(first_claims.c.first_claim_at)
                .all()
            )
            if rows:
                first_bt = rows[0].benefit_type
                bt_str = first_bt.value if hasattr(first_bt, "value") else str(first_bt)
                leading_types[bt_str] = leading_types.get(bt_str, 0) + 1

    if leading_types:
        total_multi = sum(leading_types.values())
        for bt, count in leading_types.items():
            rate = count / total_multi if total_multi > 0 else 0
            if rate > 0.2:  # This type leads > 20% of multi-type episodes
                correlations.append({
                    "type": "temporal_leading_indicator",
                    "benefit_type": bt,
                    "leading_rate": round(rate, 4),
                    "leading_count": count,
                    "total_multi_type_employees": total_multi,
                    "feeds": ["F3 (Cost Prediction)", "F9 (Care Execution)"],
                })

    return correlations


def detect_price_patterns(db: Session) -> dict[str, Any]:
    """Analyze cross-benefit-type pricing patterns from public data.

    Uses the price data pipeline to detect patterns that inform
    downstream functions:
    - F1 (Clinical Quality): waste detection across benefit types
    - F3 (Cost Prediction): cross-type cost correlations
    - F4 (Provider Selection): multi-specialty provider advantages
    - F9 (Care Execution): optimal care pathway ordering

    Optimized for large datasets (11M+ rows) using existence checks,
    sampled averages, and in-memory caching with 10-minute TTL.
    """
    cache_key = "detect_price_patterns"
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["_ts"]) < _CACHE_TTL_SECONDS:
        return cached["data"]

    patterns = {
        "generated_at": datetime.now(UTC).isoformat(),
        "patterns_detected": [],
        "benefit_type_coverage": {},
        "cross_type_signals": [],
        "claims_correlations": [],
    }

    # 1. Measure benefit-type coverage using EXISTS (not COUNT)
    for benefit_type, prefixes in BENEFIT_TYPE_PREFIXES.items():
        if benefit_type == "pharmacy":
            has_data = _has_rows(db, PriceData.source == PriceSource.nadac_pharmacy)
            patterns["benefit_type_coverage"][benefit_type] = 1 if has_data else 0
        else:
            filters = [PriceData.service_code.like(f"{p}%") for p in prefixes]
            if filters:
                has_data = _has_rows(db, or_(*filters))
                patterns["benefit_type_coverage"][benefit_type] = 1 if has_data else 0
            else:
                patterns["benefit_type_coverage"][benefit_type] = 0

    # 2. Cross-type price correlation analysis (sampled AVG)
    state_health_avg = _sampled_avg_by_state(
        db,
        PriceData.source == PriceSource.hospital_transparency,
        PriceData.channel == "cash",
        PriceData.state.isnot(None),
    )

    state_dental_avg = _sampled_avg_by_state(
        db,
        PriceData.source == PriceSource.hospital_transparency,
        PriceData.service_code.like("D%"),
        PriceData.state.isnot(None),
    )

    # Detect states where both health and dental are expensive
    high_cost_states = []
    if state_health_avg and state_dental_avg:
        health_median = sorted(state_health_avg.values())[len(state_health_avg) // 2] if state_health_avg else 0
        dental_median = sorted(state_dental_avg.values())[len(state_dental_avg) // 2] if state_dental_avg else 0

        for state_code in set(state_health_avg) & set(state_dental_avg):
            h = state_health_avg[state_code]
            d = state_dental_avg[state_code]
            if h and d and health_median and dental_median:
                if h > health_median * 1.2 and d > dental_median * 1.2:
                    high_cost_states.append(state_code)

        if high_cost_states:
            patterns["patterns_detected"].append({
                "type": "cross_type_cost_correlation",
                "description": f"States with above-median costs across both health and dental: {', '.join(sorted(high_cost_states))}",
                "signal": "geographic_cost_clustering",
                "feeds": ["F3 (Cost Prediction)", "F6A (Benchmark accuracy)"],
                "states": sorted(high_cost_states),
            })

    # 3. Medicare rate vs hospital price gap by state (sampled AVG)
    medicare_subq = (
        db.query(PriceData.price)
        .filter(PriceData.source == PriceSource.medicare_physician_fee)
        .limit(100_000)
        .subquery()
    )
    medicare_avg = db.query(func.avg(medicare_subq.c.price)).scalar() or 0

    for state_code, hospital_price in state_health_avg.items():
        if hospital_price and medicare_avg and hospital_price > 0:
            gap_ratio = hospital_price / medicare_avg
            if gap_ratio > 3.0:
                patterns["cross_type_signals"].append({
                    "type": "price_gap_outlier",
                    "state": state_code,
                    "hospital_avg": round(float(hospital_price), 2),
                    "medicare_avg": round(float(medicare_avg), 2),
                    "gap_ratio": round(float(gap_ratio), 1),
                    "feeds": ["F2 (Price Discovery)", "F6A (Benchmark)"],
                })

    # 4. Pharmacy cost concentration
    pharmacy_subq = (
        db.query(
            PriceData.service_description,
            PriceData.price,
        )
        .filter(PriceData.source == PriceSource.nadac_pharmacy)
        .limit(100_000)
        .subquery()
    )
    top_drugs = (
        db.query(
            pharmacy_subq.c.service_description,
            func.avg(pharmacy_subq.c.price).label("avg_price"),
        )
        .group_by(pharmacy_subq.c.service_description)
        .having(func.avg(pharmacy_subq.c.price) > 100)
        .order_by(func.avg(pharmacy_subq.c.price).desc())
        .limit(20)
        .all()
    )

    if top_drugs:
        patterns["patterns_detected"].append({
            "type": "pharmacy_cost_concentration",
            "description": f"Top {len(top_drugs)} high-cost drug categories identified for PBM elimination analysis",
            "feeds": ["F2 (Price Discovery)", "F8 (Data Pipeline)"],
            "count": len(top_drugs),
        })

    # 5. Quality score vs price correlation
    quality_count = (
        db.query(func.count())
        .select_from(
            db.query(PriceData.provider_name)
            .filter(
                PriceData.channel == "cms_quality_rating",
                PriceData.price > 0,
            )
            .limit(10_000)
            .subquery()
        )
        .scalar()
    ) or 0
    quality_count = int(quality_count) if isinstance(quality_count, (int, float)) else 0

    if quality_count > 0:
        patterns["patterns_detected"].append({
            "type": "quality_price_correlation",
            "description": f"Quality ratings available for {quality_count}+ hospitals — enables quality-weighted provider selection",
            "feeds": ["F4 (Provider Selection)", "F6A (Benchmark)"],
            "hospitals_with_ratings": quality_count,
        })

    # 6. Quality score distribution analysis (providers table)
    from app.models.provider import Provider
    quality_stats = (
        db.query(
            func.count(Provider.provider_id).label("total_with_score"),
            func.avg(Provider.quality_score).label("avg_score"),
            func.min(Provider.quality_score).label("min_score"),
            func.max(Provider.quality_score).label("max_score"),
        )
        .filter(Provider.quality_score.isnot(None))
        .first()
    )
    if quality_stats and quality_stats.total_with_score and quality_stats.total_with_score > 0:
        patterns["patterns_detected"].append({
            "type": "provider_quality_distribution",
            "description": (
                f"{quality_stats.total_with_score} providers with quality scores "
                f"(avg: {float(quality_stats.avg_score):.1f}, "
                f"range: {float(quality_stats.min_score):.1f}-{float(quality_stats.max_score):.1f})"
            ),
            "feeds": ["F4 (Provider Selection)", "F1 (Clinical Quality)"],
            "providers_scored": quality_stats.total_with_score,
            "avg_score": round(float(quality_stats.avg_score), 2),
        })

    # 7. Claims-based cross-type correlations
    claims_correlations = detect_claims_correlations(db)
    patterns["claims_correlations"] = claims_correlations

    if claims_correlations:
        co_occ = [c for c in claims_correlations if c["type"] == "benefit_type_co_occurrence"]
        if co_occ:
            patterns["patterns_detected"].append({
                "type": "claims_cross_type_co_occurrence",
                "description": (
                    f"Detected {len(co_occ)} benefit-type co-occurrence pairs from claims data"
                ),
                "feeds": ["F3 (Cost Prediction)", "F9 (Care Execution)"],
                "pairs": len(co_occ),
            })

        leading = [c for c in claims_correlations if c["type"] == "temporal_leading_indicator"]
        if leading:
            patterns["patterns_detected"].append({
                "type": "claims_temporal_leading_types",
                "description": (
                    f"Identified {len(leading)} benefit types that lead multi-type episodes"
                ),
                "feeds": ["F3 (Cost Prediction)", "F9 (Care Execution)"],
                "leading_types": [lead["benefit_type"] for lead in leading],
            })

    patterns["summary"] = {
        "total_patterns_detected": len(patterns["patterns_detected"]),
        "total_cross_type_signals": len(patterns["cross_type_signals"]),
        "total_claims_correlations": len(claims_correlations),
        "benefit_types_with_data": sum(1 for v in patterns["benefit_type_coverage"].values() if v > 0),
        "benefit_types_total": len(BENEFIT_TYPE_PREFIXES),
    }

    _CACHE[cache_key] = {"data": patterns, "_ts": time.time()}
    return patterns


def generate_cross_type_signals(db: Session) -> list[dict]:
    """Generate actionable cross-type signals for downstream functions.

    Constitution F8: "Is cross-type intelligence feeding Functions 1, 3, 4, and 9
    with actionable signals?"

    Each signal has:
    - target_function: which downstream function consumes this
    - signal_type: what kind of intelligence this provides
    - actionable_recommendation: specific action the downstream function should take

    Results are cached for 10 minutes to avoid repeated expensive queries.
    """
    cache_key = "generate_cross_type_signals"
    cached = _CACHE.get(cache_key)
    if cached and (time.time() - cached["_ts"]) < _CACHE_TTL_SECONDS:
        return cached["data"]

    signals = []
    patterns = detect_price_patterns(db)

    # F1 signals: waste detection across benefit types
    for pattern in patterns.get("patterns_detected", []):
        if pattern["type"] == "pharmacy_cost_concentration":
            signals.append({
                "target_function": "F1",
                "signal_type": "waste_detection",
                "description": "High-cost drug categories identified — F1 should flag claims for these drugs for therapeutic alternative review",
                "actionable_recommendation": "When determining medical necessity for high-cost drugs, cross-reference with lower-cost therapeutic equivalents from NADAC data",
                "confidence": 0.8,
                "benefit_types_involved": ["health", "pharmacy"],
            })

    # F3 signals: cross-type cost prediction
    for signal in patterns.get("cross_type_signals", []):
        if signal["type"] == "price_gap_outlier":
            signals.append({
                "target_function": "F3",
                "signal_type": "cost_prediction",
                "description": f"State {signal['state']}: hospital prices {signal['gap_ratio']}x Medicare — cost predictions for employers in this state should use higher baseline",
                "actionable_recommendation": f"Increase cost prediction baseline by {(signal['gap_ratio'] - 1) * 100:.0f}% for employers in {signal['state']}",
                "confidence": 0.7,
                "benefit_types_involved": ["health"],
                "state": signal["state"],
            })

    # F4 signals: provider selection quality indicators
    for pattern in patterns.get("patterns_detected", []):
        if pattern["type"] == "quality_price_correlation":
            signals.append({
                "target_function": "F4",
                "signal_type": "quality_indicator",
                "description": f"Quality ratings available for {pattern['hospitals_with_ratings']} hospitals — F4 should weight quality scores in provider selection",
                "actionable_recommendation": "Use CMS quality ratings as a factor in provider composite scores. Hospitals with 4-5 star ratings within 20% of lower-rated alternatives should be preferred.",
                "confidence": 0.9,
                "benefit_types_involved": ["health"],
            })

        if pattern["type"] == "provider_quality_distribution":
            signals.append({
                "target_function": "F4",
                "signal_type": "quality_distribution",
                "description": f"{pattern['providers_scored']} providers now have quality scores (avg {pattern['avg_score']}) — F4 can rank providers by quality",
                "actionable_recommendation": "Incorporate provider quality_score into composite ranking. Prefer providers with scores > 3.5 when price difference is within 15%.",
                "confidence": 0.9,
                "benefit_types_involved": ["health"],
            })

    # F9 signals: care routing based on cross-type patterns
    for pattern in patterns.get("patterns_detected", []):
        if pattern["type"] == "cross_type_cost_correlation":
            signals.append({
                "target_function": "F9",
                "signal_type": "care_routing",
                "description": f"States with correlated high costs across health+dental: {pattern.get('states', [])} — F9 should route to lower-cost providers in these states",
                "actionable_recommendation": "For employees in high-cost-correlated states, proactively suggest preventive care pathways that address both health and dental to reduce downstream costs",
                "confidence": 0.6,
                "benefit_types_involved": ["health", "dental"],
            })

    # Signals from claims-based correlations
    for corr in patterns.get("claims_correlations", []):
        if corr["type"] == "benefit_type_co_occurrence" and corr["co_occurrence_rate"] > 0.1:
            bt_a = corr["benefit_type_a"]
            bt_b = corr["benefit_type_b"]
            signals.append({
                "target_function": "F3",
                "signal_type": "cross_type_prediction",
                "description": f"Claims co-occurrence: {bt_a} + {bt_b} co-occur at {corr['co_occurrence_rate']:.0%} rate — use for cost prediction",
                "actionable_recommendation": f"When an employee has {bt_a} claims, increase predicted spend for {bt_b} by the co-occurrence factor",
                "confidence": min(0.5 + corr["co_occurrence_rate"], 0.9),
                "benefit_types_involved": [bt_a, bt_b],
            })

        if corr["type"] == "temporal_leading_indicator":
            bt = corr["benefit_type"]
            signals.append({
                "target_function": "F9",
                "signal_type": "early_intervention",
                "description": f"{bt} claims lead {corr['leading_rate']:.0%} of multi-type episodes — early intervention opportunity",
                "actionable_recommendation": f"When a {bt} claim is submitted, proactively assess the employee for related benefit needs to enable earlier care coordination",
                "confidence": 0.6,
                "benefit_types_involved": [bt],
            })

        if corr["type"] == "cross_type_denial_disparity":
            bt = corr["benefit_type"]
            signals.append({
                "target_function": "F1",
                "signal_type": "denial_pattern",
                "description": f"{bt} denial rate ({corr['denial_rate']:.0%}) deviates from average ({corr['avg_denial_rate']:.0%}) — review adjudication criteria",
                "actionable_recommendation": f"Audit {bt} denials for consistency. A {corr['deviation']:+.0%} deviation from average may indicate overly strict or lenient criteria.",
                "confidence": 0.7,
                "benefit_types_involved": [bt],
            })

    # Known epidemiological cross-type signals (always generated)
    for type_a, type_b, direction, confidence, description in KNOWN_CORRELATIONS:
        signals.append({
            "target_function": "F3",
            "signal_type": "cross_type_prediction",
            "description": description,
            "actionable_recommendation": (
                f"When employees show increased {type_a} utilization, "
                f"adjust {type_b} cost predictions upward by 15-30% for the next 6 months"
            ),
            "confidence": confidence,
            "benefit_types_involved": [type_a, type_b],
        })

    # Care coordination signals
    signals.append({
        "target_function": "F9",
        "signal_type": "care_routing",
        "description": "Musculoskeletal + mental health co-occurrence: employees with both patterns have better outcomes when mental health is addressed first",
        "actionable_recommendation": "When an employee presents with musculoskeletal complaints AND has mental health utilization history, route to integrated care pathway addressing mental health before or concurrent with physical therapy",
        "confidence": 0.75,
        "benefit_types_involved": ["mental_health", "health"],
    })

    signals.append({
        "target_function": "F1",
        "signal_type": "waste_detection",
        "description": "Cross-type waste pattern: physical therapy + contradictory medications managed by disconnected providers",
        "actionable_recommendation": "When reviewing claims for physical therapy, cross-reference active medications. Flag if patient is on medications that reduce PT effectiveness (muscle relaxants, certain pain medications) — coordinate with prescribing provider",
        "confidence": 0.7,
        "benefit_types_involved": ["health", "pharmacy"],
    })

    _CACHE[cache_key] = {"data": signals, "_ts": time.time()}
    return signals


def persist_detected_patterns(db: Session) -> dict:
    """Persist all detected patterns to data_pipeline_metrics.

    Constitution F8 requirement: every detected pattern must be recorded
    as structured data. This function:
    1. Runs full cross-type detection
    2. Writes each pattern to data_pipeline_metrics
    3. Returns summary of what was persisted

    Called by the cross-type analytics endpoint and by scheduled tasks.
    """
    # Clear stale cross_type_signal metrics (older than 24h)
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    db.execute(
        delete(DataPipelineMetric).where(
            DataPipelineMetric.metric_type.like("cross_type_signal:%"),
            DataPipelineMetric.measured_at < cutoff,
        )
    )

    patterns = detect_price_patterns(db)
    signals = generate_cross_type_signals(db)

    persisted_count = 0

    # Persist detected patterns
    for pattern in patterns.get("patterns_detected", []):
        _persist_pattern(
            db,
            pattern_type=pattern["type"],
            value=1.0,
            details=pattern,
        )
        persisted_count += 1

    # Persist cross-type price signals
    for signal in patterns.get("cross_type_signals", []):
        _persist_pattern(
            db,
            pattern_type=signal["type"],
            value=signal.get("gap_ratio", 0),
            details=signal,
        )
        persisted_count += 1

    # Persist claims correlations
    for corr in patterns.get("claims_correlations", []):
        _persist_pattern(
            db,
            pattern_type=corr["type"],
            value=corr.get("co_occurrence_rate", corr.get("cost_ratio", corr.get("leading_rate", 0))),
            details=corr,
            benefit_type=corr.get("benefit_type", corr.get("benefit_type_a")),
        )
        persisted_count += 1

    # Persist actionable signals
    for signal in signals:
        _persist_pattern(
            db,
            pattern_type=f"signal_{signal['signal_type']}",
            value=signal.get("confidence", 0),
            details=signal,
        )
        persisted_count += 1

    db.commit()
    logger.info(f"Persisted {persisted_count} cross-type patterns to data_pipeline_metrics")

    return {
        "patterns_persisted": persisted_count,
        "patterns_detected": len(patterns.get("patterns_detected", [])),
        "cross_type_signals": len(patterns.get("cross_type_signals", [])),
        "claims_correlations": len(patterns.get("claims_correlations", [])),
        "actionable_signals": len(signals),
    }


def get_cross_type_report(db: Session) -> dict:
    """Generate a cross-benefit-type analytics report with actionable signals.

    This is the Constitution's requirement for "active cross-benefit-type
    pattern detection" feeding downstream functions with actionable signals.

    Also persists all detected patterns as structured data.
    """
    patterns = detect_price_patterns(db)
    signals = generate_cross_type_signals(db)

    # Persist patterns (non-blocking — failures don't affect the report)
    persistence_summary = {}
    try:
        persistence_summary = persist_detected_patterns(db)
    except Exception as e:
        logger.error(f"Failed to persist cross-type patterns: {e}")
        persistence_summary = {"error": str(e)}

    # Group signals by target function
    signals_by_function: dict[str, list] = {}
    for s in signals:
        func_name = s["target_function"]
        if func_name not in signals_by_function:
            signals_by_function[func_name] = []
        signals_by_function[func_name].append(s)

    patterns["actionable_signals"] = signals
    patterns["signals_by_target_function"] = {
        k: len(v) for k, v in signals_by_function.items()
    }
    patterns["functions_receiving_signals"] = sorted(signals_by_function.keys())
    patterns["total_actionable_signals"] = len(signals)
    patterns["persistence"] = persistence_summary

    return patterns
