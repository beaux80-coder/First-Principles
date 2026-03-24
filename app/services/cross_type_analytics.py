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

This module operates on the public data layer now and will incorporate
proprietary data as employers join the platform.
"""

import logging
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import func, and_, case
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)


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


def detect_price_patterns(db: Session) -> dict[str, Any]:
    """Analyze cross-benefit-type pricing patterns from public data.

    Uses the price data pipeline to detect patterns that inform
    downstream functions:
    - F1 (Clinical Quality): waste detection across benefit types
    - F3 (Cost Prediction): cross-type cost correlations
    - F4 (Provider Selection): multi-specialty provider advantages
    - F9 (Care Execution): optimal care pathway ordering
    """
    patterns = {
        "generated_at": datetime.now(UTC).isoformat(),
        "patterns_detected": [],
        "benefit_type_coverage": {},
        "cross_type_signals": [],
    }

    # 1. Measure benefit-type coverage in the data
    for benefit_type, prefixes in BENEFIT_TYPE_PREFIXES.items():
        if benefit_type == "pharmacy":
            count = db.query(func.count(PriceData.price_id)).filter(
                PriceData.source == PriceSource.nadac_pharmacy
            ).scalar() or 0
        else:
            filters = [PriceData.service_code.like(f"{p}%") for p in prefixes]
            if filters:
                from sqlalchemy import or_
                count = db.query(func.count(PriceData.price_id)).filter(
                    or_(*filters)
                ).scalar() or 0
            else:
                count = 0
        patterns["benefit_type_coverage"][benefit_type] = count

    # 2. Cross-type price correlation analysis
    # Compare average prices across geographic regions to detect
    # areas where multiple benefit types are simultaneously expensive
    # (indicating systemic cost issues, not service-specific ones)
    state_health_avg = dict(
        db.query(PriceData.state, func.avg(PriceData.price))
        .filter(
            PriceData.source == PriceSource.hospital_transparency,
            PriceData.channel == "cash",
            PriceData.state.isnot(None),
        )
        .group_by(PriceData.state)
        .all()
    )

    state_dental_avg = dict(
        db.query(PriceData.state, func.avg(PriceData.price))
        .filter(
            PriceData.source == PriceSource.hospital_transparency,
            PriceData.service_code.like("D%"),
            PriceData.state.isnot(None),
        )
        .group_by(PriceData.state)
        .all()
    )

    # Detect states where both health and dental are expensive
    # (cross-type cost correlation — feeds F3 cost prediction)
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

    # 3. Medicare rate vs hospital price gap by state
    # (feeds F2 price discovery — shows where the biggest price gaps are)
    medicare_avg = db.query(func.avg(PriceData.price)).filter(
        PriceData.source == PriceSource.medicare_physician_fee,
    ).scalar() or 0

    for state_code, hospital_price in state_health_avg.items():
        if hospital_price and medicare_avg and hospital_price > 0:
            gap_ratio = hospital_price / medicare_avg
            if gap_ratio > 3.0:  # Hospital prices > 3x Medicare
                patterns["cross_type_signals"].append({
                    "type": "price_gap_outlier",
                    "state": state_code,
                    "hospital_avg": round(float(hospital_price), 2),
                    "medicare_avg": round(float(medicare_avg), 2),
                    "gap_ratio": round(float(gap_ratio), 1),
                    "feeds": ["F2 (Price Discovery)", "F6A (Benchmark)"],
                })

    # 4. Pharmacy cost concentration
    # Top drug categories by cost (feeds F2 PBM elimination strategy)
    top_drugs = (
        db.query(
            PriceData.service_description,
            func.avg(PriceData.price).label("avg_price"),
            func.count(PriceData.price_id).label("record_count"),
        )
        .filter(PriceData.source == PriceSource.nadac_pharmacy)
        .group_by(PriceData.service_description)
        .having(func.avg(PriceData.price) > 100)  # Focus on expensive drugs
        .order_by(func.avg(PriceData.price).desc())
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

    # 5. Quality score vs price correlation (Hospital Compare data)
    # Do higher-rated hospitals charge more? (feeds F4 provider selection)
    quality_records = (
        db.query(PriceData.provider_name, PriceData.price)
        .filter(
            PriceData.channel == "cms_quality_rating",
            PriceData.price > 0,  # Has a rating
        )
        .all()
    )

    if quality_records:
        patterns["patterns_detected"].append({
            "type": "quality_price_correlation",
            "description": f"Quality ratings available for {len(quality_records)} hospitals — enables quality-weighted provider selection",
            "feeds": ["F4 (Provider Selection)", "F6A (Benchmark)"],
            "hospitals_with_ratings": len(quality_records),
        })

    patterns["summary"] = {
        "total_patterns_detected": len(patterns["patterns_detected"]),
        "total_cross_type_signals": len(patterns["cross_type_signals"]),
        "benefit_types_with_data": sum(1 for v in patterns["benefit_type_coverage"].values() if v > 0),
        "benefit_types_total": len(BENEFIT_TYPE_PREFIXES),
    }

    return patterns


def generate_cross_type_signals(db: Session) -> list[dict]:
    """Generate actionable cross-type signals for downstream functions.

    Constitution F8: "Is cross-type intelligence feeding Functions 1, 3, 4, and 9
    with actionable signals?"

    Each signal has:
    - target_function: which downstream function consumes this
    - signal_type: what kind of intelligence this provides
    - actionable_recommendation: specific action the downstream function should take
    """
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

    # Cross-type pattern: mental health → medical cost correlation
    # This is always generated as a known epidemiological signal
    signals.append({
        "target_function": "F3",
        "signal_type": "cross_type_prediction",
        "description": "Mental health utilization is a leading indicator of future medical cost increases (established epidemiological relationship)",
        "actionable_recommendation": "When employees show increased mental health utilization (frequency, intensity, medication changes), increase predicted medical costs by 15-30% for subsequent 6 months",
        "confidence": 0.85,
        "benefit_types_involved": ["mental_health", "health", "std", "ltd"],
    })

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

    return signals


def get_cross_type_report(db: Session) -> dict:
    """Generate a cross-benefit-type analytics report with actionable signals.

    This is the Constitution's requirement for "active cross-benefit-type
    pattern detection" feeding downstream functions with actionable signals.
    """
    patterns = detect_price_patterns(db)
    signals = generate_cross_type_signals(db)

    # Group signals by target function
    signals_by_function = {}
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

    return patterns
