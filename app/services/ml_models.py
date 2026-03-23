"""ML models running on current data (Function 8).

Constitution: "Is there any ML technique or architecture that's physically
possible and would improve performance but hasn't been evaluated?"

These models run on the 5M+ price records currently in the pipeline and
produce outputs that improve downstream functions immediately.
"""

import logging
from collections import defaultdict

from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session

from app.models.price_data import PriceData, PriceSource

logger = logging.getLogger(__name__)


def price_regression_by_state(db: Session) -> dict:
    """Price regression: predict prices for services/states with sparse data.

    Uses dense-data states to estimate prices for sparse-data states.
    Feeds F2 (Price Discovery) and F6A (Benchmark accuracy).
    """
    # Get average prices by service code and state (hospital transparency)
    state_service_prices = (
        db.query(
            PriceData.state,
            PriceData.service_code,
            func.avg(PriceData.price).label("avg_price"),
            func.count(PriceData.price_id).label("record_count"),
        )
        .filter(
            and_(
                PriceData.source == PriceSource.hospital_transparency,
                PriceData.channel == "cash",
                PriceData.state.isnot(None),
            )
        )
        .group_by(PriceData.state, PriceData.service_code)
        .all()
    )

    if not state_service_prices:
        return {"status": "no_data", "predictions": 0}

    # Build a state-service price matrix
    prices_by_service = defaultdict(dict)
    for row in state_service_prices:
        prices_by_service[row.service_code][row.state] = float(row.avg_price)

    # For each service, compute national average and state cost indices
    national_avgs = {}
    state_indices = defaultdict(list)

    for service, state_prices in prices_by_service.items():
        if len(state_prices) < 3:
            continue
        nat_avg = sum(state_prices.values()) / len(state_prices)
        national_avgs[service] = nat_avg

        for state_code, price in state_prices.items():
            state_indices[state_code].append(price / nat_avg)

    # Compute state cost index (average price relative to national)
    state_cost_index = {}
    for state_code, indices in state_indices.items():
        if len(indices) >= 3:
            state_cost_index[state_code] = sum(indices) / len(indices)

    # Predict missing prices: for services where a state has no data,
    # use national average × state cost index
    predictions = 0
    predicted_prices = {}

    for service, nat_avg in national_avgs.items():
        existing_states = set(prices_by_service[service].keys())
        for state_code, cost_idx in state_cost_index.items():
            if state_code not in existing_states:
                predicted_price = round(nat_avg * cost_idx, 2)
                predicted_prices[(state_code, service)] = predicted_price
                predictions += 1

    return {
        "status": "complete",
        "services_modeled": len(national_avgs),
        "states_with_cost_index": len(state_cost_index),
        "prices_predicted": predictions,
        "state_cost_indices": {k: round(v, 3) for k, v in sorted(state_cost_index.items())},
        "feeds": ["F2 (Price Discovery — sparse data estimation)", "F6A (Benchmark accuracy for all states)"],
    }


def provider_clustering(db: Session) -> dict:
    """Cluster providers by price pattern and quality rating.

    Groups hospitals into cost-quality tiers for F4 (Provider Selection).
    """
    # Get hospitals with both prices and quality ratings
    hospital_prices = dict(
        db.query(
            PriceData.provider_name,
            func.avg(PriceData.price).label("avg_price"),
        )
        .filter(
            and_(
                PriceData.source == PriceSource.hospital_transparency,
                PriceData.channel == "cash",
            )
        )
        .group_by(PriceData.provider_name)
        .all()
    )

    hospital_ratings = dict(
        db.query(PriceData.provider_name, PriceData.price)
        .filter(
            and_(
                PriceData.channel == "cms_quality_rating",
                PriceData.price > 0,
            )
        )
        .all()
    )

    if not hospital_prices or not hospital_ratings:
        return {"status": "insufficient_data", "clusters": []}

    # Fuzzy match hospital names: normalize to uppercase, strip common suffixes
    def _normalize(name: str) -> str:
        n = name.upper().strip()
        for suffix in [" HOSPITAL", " MEDICAL CENTER", " MED CTR", " MED CENTER",
                       " HEALTH", " HEALTHCARE", " HEALTH CARE", " - ROCHESTER",
                       " - MAIN CAMPUS", " - HILLCREST", " (MAIN CAMPUS)"]:
            n = n.replace(suffix, "")
        return n.strip()

    norm_prices = {_normalize(k): k for k in hospital_prices}
    norm_ratings = {_normalize(k): k for k in hospital_ratings}

    # Match on normalized names
    both_norm = set(norm_prices.keys()) & set(norm_ratings.keys())
    both_prices = {norm_prices[n] for n in both_norm}
    both_ratings = {norm_ratings[n] for n in both_norm}

    # Also try substring matching for remaining
    unmatched_prices = set(hospital_prices.keys()) - both_prices
    unmatched_ratings = set(hospital_ratings.keys()) - both_ratings
    for pn in list(unmatched_prices):
        pn_norm = _normalize(pn)
        for rn in list(unmatched_ratings):
            rn_norm = _normalize(rn)
            if pn_norm in rn_norm or rn_norm in pn_norm:
                both_prices.add(pn)
                both_ratings.add(rn)
                # Map price name to rating name
                hospital_ratings[pn] = hospital_ratings[rn]
                unmatched_ratings.discard(rn)
                break

    both = both_prices

    if not both:
        return {"status": "no_overlap", "hospitals_with_prices": len(hospital_prices),
                "hospitals_with_ratings": len(hospital_ratings)}

    # Simple clustering: high/medium/low cost × high/medium/low quality
    price_values = [float(hospital_prices[h]) for h in both]
    price_median = sorted(price_values)[len(price_values) // 2]
    price_p25 = sorted(price_values)[len(price_values) // 4]
    price_p75 = sorted(price_values)[3 * len(price_values) // 4]

    clusters = {
        "high_quality_low_cost": [],
        "high_quality_high_cost": [],
        "low_quality_low_cost": [],
        "low_quality_high_cost": [],
    }

    for hospital in both:
        price = float(hospital_prices[hospital])
        rating_val = hospital_ratings.get(hospital)
        if rating_val is None:
            continue
        rating = float(rating_val)

        quality = "high_quality" if rating >= 4 else "low_quality"
        cost = "low_cost" if price <= price_median else "high_cost"
        cluster = f"{quality}_{cost}"

        clusters[cluster].append({
            "name": hospital,
            "avg_price": round(price, 2),
            "quality_rating": int(rating),
        })

    return {
        "status": "complete",
        "hospitals_analyzed": len(both),
        "clusters": {k: {"count": len(v), "sample": v[:3]} for k, v in clusters.items()},
        "insight": f"Of {len(both)} hospitals with both price and quality data, "
                   f"{len(clusters['high_quality_low_cost'])} are high-quality AND low-cost — "
                   f"these are the providers the system would prioritize.",
        "feeds": ["F4 (Provider Selection — quality-weighted cost optimization)"],
    }


def run_all_models(db: Session) -> dict:
    """Run all implementable ML models on current data."""
    return {
        "price_regression": price_regression_by_state(db),
        "provider_clustering": provider_clustering(db),
    }
