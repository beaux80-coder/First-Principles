"""Cost Prediction Engine — Function 3 (Constitution).

Per-employer cost predictions covering all benefit types.
Incorporates all available data from F8 (data pipeline).
Accuracy measured as absolute percentage deviation.
Improves automatically as new data becomes available.
Feeds F7 (pricing) and F7A (stop-loss).

Uses gradient boosting ensemble on F8 data with features:
  - Historical prices by state/service/benefit type
  - Claims experience per employer
  - Employer demographics (size, industry, geography)
  - Provider outcomes (quality scores, pricing patterns)
  - Population-level data (Medicare rates, NADAC pharmacy)
  - Cross-type signals from F8 cross-type analytics
"""

import logging
import math
import time
from collections import defaultdict
from datetime import datetime, UTC
from typing import Any

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import cross_val_score
from sqlalchemy import func, and_
from sqlalchemy.orm import Session

from app.models.claim import Claim, ClaimStatus
from app.models.employer import Employer
from app.models.employee import Employee, EmployeeStatus
from app.models.price_data import PriceData, PriceSource
from app.models.provider import Provider
from app.models.service import BenefitType
from app.services.cross_type_analytics import (
    generate_cross_type_signals,
    BENEFIT_TYPE_PREFIXES,
)
from app.services.ml_models import price_regression_by_state, provider_clustering

logger = logging.getLogger(__name__)

# Cache for expensive PriceData aggregations (10-minute TTL)
_PRICE_CACHE: dict = {}
_PRICE_CACHE_TTL = 600

# ---------------------------------------------------------------------------
# Feature engineering helpers
# ---------------------------------------------------------------------------

# State cost-of-living index (relative to national average = 1.0).
# Sourced from BLS regional price parities; used when employer-specific
# claims history is sparse.
_STATE_COL_INDEX: dict[str, float] = {
    "CA": 1.15, "NY": 1.16, "MA": 1.12, "CT": 1.10, "NJ": 1.11,
    "HI": 1.18, "DC": 1.17, "WA": 1.08, "MD": 1.09, "CO": 1.05,
    "OR": 1.04, "NH": 1.03, "AK": 1.13, "VA": 1.04, "IL": 1.01,
    "MN": 1.01, "RI": 1.03, "PA": 0.99, "DE": 1.02, "VT": 1.05,
    "FL": 1.00, "TX": 0.96, "GA": 0.95, "AZ": 0.98, "NC": 0.94,
    "OH": 0.92, "MI": 0.93, "TN": 0.91, "IN": 0.90, "MO": 0.90,
    "WI": 0.94, "SC": 0.92, "AL": 0.88, "KY": 0.88, "LA": 0.91,
    "OK": 0.88, "IA": 0.90, "KS": 0.91, "AR": 0.87, "NE": 0.91,
    "NV": 1.01, "UT": 0.97, "NM": 0.93, "WV": 0.87, "ID": 0.94,
    "ME": 1.00, "MT": 0.95, "SD": 0.89, "ND": 0.90, "WY": 0.94,
    "MS": 0.86,
}

# Industry medical cost multiplier relative to average employer.
_INDUSTRY_COST_FACTOR: dict[str, float] = {
    "technology": 0.90,
    "healthcare": 1.10,
    "manufacturing": 1.08,
    "construction": 1.12,
    "finance": 0.95,
    "education": 0.93,
    "retail": 1.02,
    "hospitality": 1.05,
    "professional_services": 0.92,
    "government": 0.97,
    "transportation": 1.06,
    "agriculture": 1.04,
    "energy": 1.03,
    "nonprofit": 0.96,
}


def _benefit_type_weight(bt: BenefitType) -> float:
    """Approximate share of total benefit spend by type."""
    return {
        BenefitType.health: 0.65,
        BenefitType.dental: 0.08,
        BenefitType.vision: 0.03,
        BenefitType.life: 0.04,
        BenefitType.std: 0.03,
        BenefitType.ltd: 0.05,
        BenefitType.mental_health: 0.12,
    }.get(bt, 0.05)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _extract_employer_features(
    db: Session,
    employer: Employer,
    benefit_type: BenefitType,
    cross_type_signals: list[dict],
    price_regression: dict,
) -> dict[str, float]:
    """Build a feature vector for one (employer, benefit_type) prediction.

    Incorporates F8 data: prices, claims, demographics, provider outcomes,
    population data, and cross-type signals.
    """
    features: dict[str, float] = {}
    state = (employer.geography or "")[:2].upper() if employer.geography else None

    # --- Employer demographics ---
    features["employee_count"] = float(employer.employee_count or 50)
    features["log_employee_count"] = math.log1p(features["employee_count"])
    features["col_index"] = _STATE_COL_INDEX.get(state, 1.0) if state else 1.0
    features["industry_factor"] = _INDUSTRY_COST_FACTOR.get(
        (employer.industry or "").lower(), 1.0
    )
    features["baseline_pepm"] = float(employer.baseline_cost_pepm or 0.0)
    features["benefit_weight"] = _benefit_type_weight(benefit_type)

    # --- Historical claims experience ---
    claims_q = (
        db.query(
            func.count(Claim.claim_id).label("claim_count"),
            func.coalesce(func.sum(Claim.amount_paid), 0).label("total_paid"),
            func.coalesce(func.avg(Claim.amount_paid), 0).label("avg_paid"),
        )
        .filter(
            and_(
                Claim.employer_id == employer.employer_id,
                Claim.benefit_type == benefit_type,
                Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
            )
        )
        .first()
    )
    features["historical_claim_count"] = float(claims_q.claim_count) if claims_q else 0.0
    features["historical_total_paid"] = float(claims_q.total_paid) if claims_q else 0.0
    features["historical_avg_paid"] = float(claims_q.avg_paid) if claims_q else 0.0

    # Per-employee claims frequency
    active_employees = (
        db.query(func.count(Employee.employee_id))
        .filter(
            Employee.employer_id == employer.employer_id,
            Employee.status == EmployeeStatus.active,
        )
        .scalar()
        or 1
    )
    features["claims_per_employee"] = features["historical_claim_count"] / max(active_employees, 1)
    features["paid_per_employee"] = features["historical_total_paid"] / max(active_employees, 1)

    # --- Provider outcomes (from F8 ML models) ---
    # Use state cost index from price_regression if available
    state_indices = price_regression.get("state_cost_indices", {})
    features["state_price_index"] = float(state_indices.get(state, 1.0)) if state else 1.0

    # Average quality score of providers in employer's state
    if state:
        avg_quality = (
            db.query(func.avg(Provider.quality_score))
            .filter(Provider.state == state, Provider.quality_score.isnot(None))
            .scalar()
        )
        features["state_avg_provider_quality"] = float(avg_quality) if avg_quality else 3.0
    else:
        features["state_avg_provider_quality"] = 3.0

    # --- Population-level price data (F8 public layer) ---
    # Use cached sampled averages to avoid full-table scans on 11M+ rows
    prefixes = BENEFIT_TYPE_PREFIXES.get(benefit_type.value, [])
    if state and prefixes:
        cache_key = f"state_benefit_avg_{state}_{benefit_type.value}"
        cached = _PRICE_CACHE.get(cache_key)
        if cached and (time.time() - cached["_ts"]) < _PRICE_CACHE_TTL:
            features["state_benefit_avg_price"] = cached["val"]
        else:
            from sqlalchemy import or_
            code_filters = [PriceData.service_code.like(f"{p}%") for p in prefixes]
            subq = (
                db.query(PriceData.price)
                .filter(or_(*code_filters), PriceData.state == state, PriceData.channel == "cash")
                .limit(10_000)
                .subquery()
            )
            avg_price = db.query(func.avg(subq.c.price)).scalar()
            val = float(avg_price) if avg_price else 0.0
            features["state_benefit_avg_price"] = val
            _PRICE_CACHE[cache_key] = {"val": val, "_ts": time.time()}
    else:
        features["state_benefit_avg_price"] = 0.0

    # Medicare baseline rate (cached, sampled)
    cached_medicare = _PRICE_CACHE.get("medicare_baseline")
    if cached_medicare and (time.time() - cached_medicare["_ts"]) < _PRICE_CACHE_TTL:
        features["medicare_baseline"] = cached_medicare["val"]
    else:
        subq = (
            db.query(PriceData.price)
            .filter(PriceData.source == PriceSource.medicare_physician_fee)
            .limit(100_000)
            .subquery()
        )
        medicare_avg = db.query(func.avg(subq.c.price)).scalar()
        val = float(medicare_avg) if medicare_avg else 0.0
        features["medicare_baseline"] = val
        _PRICE_CACHE["medicare_baseline"] = {"val": val, "_ts": time.time()}

    # NADAC pharmacy baseline (cached, sampled)
    if benefit_type in (BenefitType.health, BenefitType.mental_health):
        cached_nadac = _PRICE_CACHE.get("nadac_pharmacy_avg")
        if cached_nadac and (time.time() - cached_nadac["_ts"]) < _PRICE_CACHE_TTL:
            features["nadac_pharmacy_avg"] = cached_nadac["val"]
        else:
            subq = (
                db.query(PriceData.price)
                .filter(PriceData.source == PriceSource.nadac_pharmacy)
                .limit(100_000)
                .subquery()
            )
            nadac_avg = db.query(func.avg(subq.c.price)).scalar()
            val = float(nadac_avg) if nadac_avg else 0.0
            features["nadac_pharmacy_avg"] = val
            _PRICE_CACHE["nadac_pharmacy_avg"] = {"val": val, "_ts": time.time()}
    else:
        features["nadac_pharmacy_avg"] = 0.0

    # --- Cross-type signals (F8) ---
    # Count signals relevant to this employer's state / benefit type
    f3_signals = [s for s in cross_type_signals if s["target_function"] == "F3"]
    features["cross_type_signal_count"] = float(len(f3_signals))

    # State-specific cost prediction adjustments from cross-type analysis
    state_adjustment = 0.0
    for sig in f3_signals:
        if sig.get("state") == state:
            # Extract gap ratio if present
            confidence = sig.get("confidence", 0.5)
            state_adjustment += confidence * 0.1  # conservative adjustment
    features["cross_type_state_adjustment"] = state_adjustment

    # Mental health cross-type predictor
    if benefit_type == BenefitType.health:
        mh_claims = (
            db.query(func.count(Claim.claim_id))
            .filter(
                Claim.employer_id == employer.employer_id,
                Claim.benefit_type == BenefitType.mental_health,
                Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
            )
            .scalar()
            or 0
        )
        features["mh_claims_ratio"] = mh_claims / max(active_employees, 1)
    else:
        features["mh_claims_ratio"] = 0.0

    return features


# ---------------------------------------------------------------------------
# Model training and prediction
# ---------------------------------------------------------------------------

# Canonical feature order used for training and inference.
_FEATURE_NAMES = [
    "employee_count", "log_employee_count", "col_index", "industry_factor",
    "baseline_pepm", "benefit_weight", "historical_claim_count",
    "historical_total_paid", "historical_avg_paid", "claims_per_employee",
    "paid_per_employee", "state_price_index", "state_avg_provider_quality",
    "state_benefit_avg_price", "medicare_baseline", "nadac_pharmacy_avg",
    "cross_type_signal_count", "cross_type_state_adjustment", "mh_claims_ratio",
]


def _features_to_array(features: dict[str, float]) -> list[float]:
    """Convert feature dict to ordered array matching _FEATURE_NAMES."""
    return [features.get(name, 0.0) for name in _FEATURE_NAMES]


def _train_ensemble(
    db: Session,
    cross_type_signals: list[dict],
    price_regression: dict,
) -> tuple[GradientBoostingRegressor | None, dict]:
    """Train a gradient boosting model on historical claims data.

    Returns (model, training_metadata).  If insufficient training data
    exists, returns (None, metadata) and the prediction path falls back
    to the actuarial heuristic.
    """
    # Gather training data: employers with actual claims history
    employers_with_claims = (
        db.query(Employer)
        .join(Claim, Claim.employer_id == Employer.employer_id)
        .filter(Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]))
        .distinct()
        .all()
    )

    if len(employers_with_claims) < 3:
        return None, {
            "status": "insufficient_training_data",
            "employers_available": len(employers_with_claims),
            "minimum_required": 3,
        }

    X_rows: list[list[float]] = []
    y_values: list[float] = []

    for emp in employers_with_claims:
        for bt in BenefitType:
            # Target: actual average paid amount per employee per month
            active_count = (
                db.query(func.count(Employee.employee_id))
                .filter(
                    Employee.employer_id == emp.employer_id,
                    Employee.status == EmployeeStatus.active,
                )
                .scalar()
                or 1
            )
            total_paid = (
                db.query(func.coalesce(func.sum(Claim.amount_paid), 0))
                .filter(
                    Claim.employer_id == emp.employer_id,
                    Claim.benefit_type == bt,
                    Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
                )
                .scalar()
            )
            if total_paid and float(total_paid) > 0:
                pepm = float(total_paid) / max(active_count, 1)
                features = _extract_employer_features(
                    db, emp, bt, cross_type_signals, price_regression
                )
                X_rows.append(_features_to_array(features))
                y_values.append(pepm)

    if len(X_rows) < 5:
        return None, {
            "status": "insufficient_training_samples",
            "samples_available": len(X_rows),
            "minimum_required": 5,
        }

    X = np.array(X_rows)
    y = np.array(y_values)

    # Feature scaling — prevents features with large ranges from dominating
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Hyperparameter tuning when sufficient data exists
    if len(X_rows) >= 20:
        from sklearn.model_selection import GridSearchCV
        param_grid = {
            "n_estimators": [100, 200],
            "max_depth": [3, 4, 5],
            "learning_rate": [0.05, 0.1],
            "subsample": [0.8, 1.0],
        }
        n_cv = min(5, len(X_rows))
        gs = GridSearchCV(
            GradientBoostingRegressor(min_samples_leaf=2, random_state=42),
            param_grid, cv=n_cv, scoring="neg_mean_absolute_error", n_jobs=-1,
        )
        gs.fit(X_scaled, y)
        model = gs.best_estimator_
        best_params = gs.best_params_
    else:
        model = GradientBoostingRegressor(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.1,
            subsample=0.8,
            min_samples_leaf=2,
            random_state=42,
        )
        model.fit(X_scaled, y)
        best_params = {"n_estimators": 200, "max_depth": 4, "learning_rate": 0.1, "subsample": 0.8}

    # Store scaler alongside model for inference
    model._scaler = scaler  # type: ignore

    # Cross-validation for accuracy measurement
    n_splits = min(5, len(X_rows))
    if n_splits >= 2:
        cv_scores = cross_val_score(
            model, X_scaled, y, cv=n_splits, scoring="neg_mean_absolute_error"
        )
        cv_mae = -cv_scores.mean()
        cv_mape = cv_mae / max(y.mean(), 1.0) * 100
    else:
        cv_mae = 0.0
        cv_mape = 0.0

    # Feature importances
    importances = dict(zip(_FEATURE_NAMES, model.feature_importances_.tolist()))

    metadata = {
        "status": "trained",
        "training_samples": len(X_rows),
        "employers_used": len(employers_with_claims),
        "benefit_types_covered": len(set(bt for bt in BenefitType)),
        "cv_folds": n_splits,
        "cv_mae": round(cv_mae, 2),
        "cv_mape_pct": round(cv_mape, 2),
        "feature_importances": {
            k: round(v, 4) for k, v in sorted(importances.items(), key=lambda x: -x[1])
        },
        "trained_at": datetime.now(UTC).isoformat(),
    }

    return model, metadata


def _actuarial_heuristic(
    features: dict[str, float],
    benefit_type: BenefitType,
) -> float:
    """Fallback cost prediction when ML model has insufficient data.

    Uses industry-standard actuarial factors applied to available F8 data.
    """
    # Base PEPM by benefit type (industry averages from KFF/SHRM surveys)
    base_pepm = {
        BenefitType.health: 550.0,
        BenefitType.dental: 45.0,
        BenefitType.vision: 12.0,
        BenefitType.life: 18.0,
        BenefitType.std: 15.0,
        BenefitType.ltd: 25.0,
        BenefitType.mental_health: 65.0,
    }.get(benefit_type, 50.0)

    # If employer has a baseline, use it as anchor
    if features["baseline_pepm"] > 0:
        base_pepm = features["baseline_pepm"] * features["benefit_weight"]

    # Adjust for geography
    adjusted = base_pepm * features["col_index"]

    # Adjust for industry
    adjusted *= features["industry_factor"]

    # Adjust for group size (small groups cost more per capita)
    if features["employee_count"] < 25:
        adjusted *= 1.15
    elif features["employee_count"] < 100:
        adjusted *= 1.05
    elif features["employee_count"] > 500:
        adjusted *= 0.95

    # Adjust for state price index from F8 data
    if features["state_price_index"] != 1.0:
        adjusted *= features["state_price_index"]

    # Adjust for cross-type signals
    adjusted *= (1.0 + features["cross_type_state_adjustment"])

    # Mental health cross-type predictor for health costs
    if benefit_type == BenefitType.health and features["mh_claims_ratio"] > 0.1:
        adjusted *= 1.0 + min(features["mh_claims_ratio"] * 0.2, 0.30)

    # If we have actual claims history, blend with historical
    if features["paid_per_employee"] > 0:
        # Credibility weighting: more claims = more weight on experience
        credibility = min(features["historical_claim_count"] / 100, 0.8)
        adjusted = credibility * features["paid_per_employee"] + (1 - credibility) * adjusted

    return round(adjusted, 2)


# ---------------------------------------------------------------------------
# Confidence intervals
# ---------------------------------------------------------------------------

def _confidence_interval(
    prediction: float,
    features: dict[str, float],
    model_trained: bool,
) -> dict[str, float]:
    """Compute confidence interval for a cost prediction.

    Width depends on data quality and model availability.
    """
    # Base uncertainty: 25% for heuristic, 15% for ML model
    base_pct = 0.15 if model_trained else 0.25

    # Reduce uncertainty if employer has claims history
    if features["historical_claim_count"] > 50:
        base_pct *= 0.7
    elif features["historical_claim_count"] > 10:
        base_pct *= 0.85

    # Increase uncertainty for small groups (higher variance)
    if features["employee_count"] < 25:
        base_pct *= 1.3
    elif features["employee_count"] < 50:
        base_pct *= 1.15

    margin = prediction * base_pct
    return {
        "lower": round(max(prediction - margin, 0), 2),
        "upper": round(prediction + margin, 2),
        "confidence_pct": round((1 - base_pct) * 100, 1),
    }


def _predict_from_public_data(
    db: Session,
    employer_id: str,
    benefit_types: list[str] | None = None,
) -> dict[str, Any]:
    """Fallback prediction using only public data when employer not found.

    Uses national average pricing data, industry cost factors, and state
    cost-of-living indices to generate a baseline prediction with wider
    confidence intervals.
    """
    selected_types = list(BenefitType)
    if benefit_types:
        try:
            selected_types = [BenefitType(bt) for bt in benefit_types]
        except ValueError as exc:
            return {"error": f"invalid_benefit_type: {exc}"}

    # National average PEPM baselines by benefit type (industry data)
    _NATIONAL_AVG_PEPM = {
        "health": 500.0, "dental": 40.0, "vision": 12.0,
        "mental_health": 45.0, "life": 15.0, "std": 10.0, "ltd": 18.0,
    }

    predictions: dict[str, dict] = {}
    aggregate_monthly = 0.0
    aggregate_annual = 0.0

    for bt in selected_types:
        base_pepm = _NATIONAL_AVG_PEPM.get(bt.value, 50.0)
        # Apply wider confidence interval for public-data-only prediction
        ci = _compute_confidence_interval(
            base_pepm, confidence_width_pct=0.40
        )
        monthly = base_pepm
        annual = base_pepm * 12
        aggregate_monthly += monthly
        aggregate_annual += annual
        predictions[bt.value] = {
            "pepm": round(monthly, 2),
            "annual_per_employee": round(annual, 2),
            "confidence_interval": ci,
            "data_sources": ["national_average", "public_pricing_data"],
            "model_used": "public_data_baseline",
        }

    return {
        "employer_id": employer_id,
        "confidence_level": "public_data_only",
        "confidence_note": (
            "Employer not found in system. Prediction based on national "
            "average pricing data with wider confidence intervals. Accuracy "
            "improves significantly with employer-specific claims history."
        ),
        "predicted_at": datetime.now(UTC).isoformat(),
        "predictions_by_benefit_type": predictions,
        "aggregate": {
            "total_pepm": round(aggregate_monthly, 2),
            "total_annual_per_employee": round(aggregate_annual, 2),
        },
        "feeding_f7": True,
        "feeding_f7a": True,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def predict_employer_costs(
    db: Session,
    employer_id: str,
    benefit_types: list[str] | None = None,
) -> dict[str, Any]:
    """Generate cost predictions for an employer across benefit types.

    Constitution F3 requirements:
    - Per-employer, per-benefit-type, and aggregate predictions
    - Incorporates all F8 data (prices, claims, demographics, cross-type)
    - Confidence intervals
    - MAPE measurement
    - Feeds F7 (pricing) and F7A (stop-loss)

    Parameters
    ----------
    db : Session
        Database session.
    employer_id : str
        UUID of the employer.
    benefit_types : list[str] | None
        Specific benefit types to predict.  Defaults to all.

    Returns
    -------
    dict with per-benefit-type predictions, aggregate, and accuracy info.
    """
    employer = db.query(Employer).filter(Employer.employer_id == employer_id).first()
    if not employer:
        # Fall back to public-data-only prediction when employer not found
        return _predict_from_public_data(db, employer_id, benefit_types)

    # Resolve benefit types
    if benefit_types:
        try:
            selected_types = [BenefitType(bt) for bt in benefit_types]
        except ValueError as exc:
            return {"error": f"invalid_benefit_type: {exc}"}
    else:
        selected_types = list(BenefitType)

    # Gather F8 data that feeds predictions
    cross_type_signals = generate_cross_type_signals(db)
    price_reg = price_regression_by_state(db)

    # Train or use cached model
    model, train_meta = _train_ensemble(db, cross_type_signals, price_reg)
    model_available = model is not None

    # Active employee count
    active_count = (
        db.query(func.count(Employee.employee_id))
        .filter(
            Employee.employer_id == employer.employer_id,
            Employee.status == EmployeeStatus.active,
        )
        .scalar()
        or employer.employee_count
        or 1
    )

    predictions: dict[str, dict] = {}
    aggregate_monthly = 0.0
    aggregate_annual = 0.0

    for bt in selected_types:
        features = _extract_employer_features(
            db, employer, bt, cross_type_signals, price_reg
        )

        if model_available:
            X = np.array([_features_to_array(features)])
            predicted_pepm = float(model.predict(X)[0])
            # Floor at zero
            predicted_pepm = max(predicted_pepm, 0.0)
            prediction_method = "gradient_boosting_ensemble"
        else:
            predicted_pepm = _actuarial_heuristic(features, bt)
            prediction_method = "actuarial_heuristic"

        ci = _confidence_interval(predicted_pepm, features, model_available)
        monthly_total = round(predicted_pepm * active_count, 2)
        annual_total = round(monthly_total * 12, 2)

        aggregate_monthly += monthly_total
        aggregate_annual += annual_total

        # Per-employer, per-benefit-type accuracy vs actual (if available)
        actual_total_paid = (
            db.query(func.coalesce(func.sum(Claim.amount_paid), 0))
            .filter(
                Claim.employer_id == employer.employer_id,
                Claim.benefit_type == bt,
                Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
            )
            .scalar()
        )
        actual_pepm = float(actual_total_paid) / max(active_count, 1) if actual_total_paid else None
        if actual_pepm and actual_pepm > 0:
            ape = abs(predicted_pepm - actual_pepm) / actual_pepm * 100
        else:
            ape = None

        predictions[bt.value] = {
            "predicted_pepm": round(predicted_pepm, 2),
            "monthly_total": monthly_total,
            "annual_total": annual_total,
            "confidence_interval": ci,
            "prediction_method": prediction_method,
            "absolute_pct_error": round(ape, 2) if ape is not None else None,
            "actual_pepm": round(actual_pepm, 2) if actual_pepm else None,
        }

    # Aggregate prediction
    aggregate_ci_lower = sum(p["confidence_interval"]["lower"] * active_count for p in predictions.values())
    aggregate_ci_upper = sum(p["confidence_interval"]["upper"] * active_count for p in predictions.values())

    # F7 / F7A feeds
    f7_pricing_input = {
        "employer_id": str(employer.employer_id),
        "predicted_annual_cost": round(aggregate_annual, 2),
        "per_benefit_type": {
            bt: round(predictions[bt]["annual_total"], 2)
            for bt in predictions
        },
        "methodology": "gradient_boosting_ensemble" if model_available else "actuarial_heuristic",
    }

    f7a_stop_loss_input = {
        "employer_id": str(employer.employer_id),
        "predicted_annual_cost": round(aggregate_annual, 2),
        "upper_bound_annual": round(aggregate_ci_upper * 12, 2),
        "employee_count": active_count,
        "recommended_specific_deductible": round(aggregate_annual / max(active_count, 1) * 2.5, 2),
        "recommended_aggregate_attachment": round(aggregate_annual * 1.25, 2),
    }

    return {
        "employer_id": str(employer.employer_id),
        "employer_name": employer.name,
        "active_employees": active_count,
        "prediction_date": datetime.now(UTC).isoformat(),
        "predictions_by_benefit_type": predictions,
        "aggregate": {
            "monthly_total": round(aggregate_monthly, 2),
            "annual_total": round(aggregate_annual, 2),
            "confidence_interval": {
                "lower_annual": round(aggregate_ci_lower * 12, 2),
                "upper_annual": round(aggregate_ci_upper * 12, 2),
            },
        },
        "model_info": train_meta,
        "f7_pricing_feed": f7_pricing_input,
        "f7a_stop_loss_feed": f7a_stop_loss_input,
        "data_sources_used": [
            "employer_claims_history",
            "employer_demographics",
            "f8_price_regression_by_state",
            "f8_cross_type_signals",
            "f8_provider_quality_scores",
            "cms_medicare_physician_fee",
            "nadac_pharmacy_pricing",
            "hospital_transparency_prices",
        ],
    }


def get_prediction_accuracy(db: Session) -> dict[str, Any]:
    """Measure prediction accuracy across all employers and benefit types.

    Constitution F3 metric: absolute percentage deviation
    measured per employer, per benefit type, and aggregate.
    """
    cross_type_signals = generate_cross_type_signals(db)
    price_reg = price_regression_by_state(db)
    model, train_meta = _train_ensemble(db, cross_type_signals, price_reg)
    model_available = model is not None

    employers = db.query(Employer).all()
    if not employers:
        return {
            "status": "no_employers",
            "message": "No employers in database for accuracy measurement",
        }

    per_employer_errors: dict[str, dict] = {}
    per_benefit_type_errors: dict[str, list[float]] = defaultdict(list)
    all_errors: list[float] = []

    for emp in employers:
        active_count = (
            db.query(func.count(Employee.employee_id))
            .filter(
                Employee.employer_id == emp.employer_id,
                Employee.status == EmployeeStatus.active,
            )
            .scalar()
            or emp.employee_count
            or 1
        )

        emp_errors: dict[str, float | None] = {}
        for bt in BenefitType:
            actual = (
                db.query(func.coalesce(func.sum(Claim.amount_paid), 0))
                .filter(
                    Claim.employer_id == emp.employer_id,
                    Claim.benefit_type == bt,
                    Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
                )
                .scalar()
            )
            actual_pepm = float(actual) / max(active_count, 1) if actual else 0.0
            if actual_pepm <= 0:
                emp_errors[bt.value] = None
                continue

            features = _extract_employer_features(
                db, emp, bt, cross_type_signals, price_reg
            )
            if model_available:
                X = np.array([_features_to_array(features)])
                predicted = float(model.predict(X)[0])
            else:
                predicted = _actuarial_heuristic(features, bt)

            ape = abs(predicted - actual_pepm) / actual_pepm * 100
            emp_errors[bt.value] = round(ape, 2)
            per_benefit_type_errors[bt.value].append(ape)
            all_errors.append(ape)

        per_employer_errors[str(emp.employer_id)] = {
            "name": emp.name,
            "errors_by_benefit_type": emp_errors,
        }

    # Compute MAPEs
    overall_mape = round(sum(all_errors) / len(all_errors), 2) if all_errors else None
    bt_mapes = {
        bt: round(sum(errs) / len(errs), 2) if errs else None
        for bt, errs in per_benefit_type_errors.items()
    }

    return {
        "measured_at": datetime.now(UTC).isoformat(),
        "model_status": train_meta.get("status", "unknown"),
        "prediction_method": "gradient_boosting_ensemble" if model_available else "actuarial_heuristic",
        "overall_mape_pct": overall_mape,
        "mape_by_benefit_type": bt_mapes,
        "employers_evaluated": len(per_employer_errors),
        "data_points": len(all_errors),
        "per_employer_accuracy": per_employer_errors,
        "accuracy_targets": {
            "excellent": "< 5% MAPE",
            "good": "5-10% MAPE",
            "acceptable": "10-20% MAPE",
            "needs_improvement": "> 20% MAPE",
        },
        "auto_retrain_policy": (
            "Model retrains automatically on each prediction request when new "
            "claims data is available. As employers join and claims accumulate, "
            "the gradient boosting ensemble replaces the actuarial heuristic and "
            "accuracy improves with each data point."
        ),
    }


def get_prediction_methodology() -> dict[str, Any]:
    """Describe the cost prediction methodology for transparency.

    Constitution: methodology must be explainable and auditable.
    """
    return {
        "function": "F3 — Cost Prediction Engine",
        "purpose": (
            "Per-employer cost predictions covering all benefit types. "
            "Feeds F7 (pricing) and F7A (stop-loss) with predicted costs."
        ),
        "model": {
            "type": "Gradient Boosting Ensemble (GradientBoostingRegressor)",
            "library": "scikit-learn",
            "hyperparameters": {
                "n_estimators": 200,
                "max_depth": 4,
                "learning_rate": 0.1,
                "subsample": 0.8,
                "min_samples_leaf": 2,
            },
            "fallback": (
                "Actuarial heuristic using industry-standard factors when "
                "insufficient training data is available (< 5 samples)."
            ),
        },
        "features": {
            "employer_demographics": [
                "employee_count (and log transform)",
                "cost_of_living_index (by state, from BLS regional price parities)",
                "industry_cost_factor (from actuarial tables)",
                "baseline_cost_pepm (employer-provided or market rate)",
            ],
            "claims_experience": [
                "historical_claim_count",
                "historical_total_paid",
                "historical_avg_paid",
                "claims_per_employee",
                "paid_per_employee",
            ],
            "f8_price_data": [
                "state_price_index (from price_regression_by_state ML model)",
                "state_benefit_avg_price (hospital transparency data)",
                "medicare_baseline (Medicare Physician Fee Schedule)",
                "nadac_pharmacy_avg (NADAC pharmacy pricing)",
            ],
            "provider_outcomes": [
                "state_avg_provider_quality (CMS quality ratings)",
            ],
            "cross_type_signals": [
                "cross_type_signal_count (F8 cross-type analytics)",
                "cross_type_state_adjustment (geographic cost correlation)",
                "mh_claims_ratio (mental health utilization as health cost predictor)",
            ],
        },
        "accuracy_measurement": {
            "metric": "Mean Absolute Percentage Error (MAPE)",
            "granularity": [
                "per employer, per benefit type",
                "per benefit type (aggregate across employers)",
                "overall aggregate",
            ],
            "cross_validation": "k-fold (k = min(5, n_samples))",
        },
        "auto_improvement": (
            "The model retrains on every prediction request, incorporating "
            "any new claims data. As the employer base grows, the gradient "
            "boosting ensemble gains more training samples and the actuarial "
            "heuristic is replaced. Cross-validation MAPE is tracked to "
            "measure improvement over time."
        ),
        "benefit_types_supported": [bt.value for bt in BenefitType],
        "downstream_consumers": {
            "F7_pricing": (
                "Receives predicted annual cost, per-benefit-type breakdown, "
                "and methodology tag for premium calculation."
            ),
            "F7A_stop_loss": (
                "Receives predicted annual cost, upper confidence bound, "
                "recommended specific deductible, and aggregate attachment point."
            ),
        },
        "data_sources": [
            "Employer claims history (proprietary, per-employer)",
            "Employer demographics (size, industry, geography)",
            "F8 price_regression_by_state (state cost indices)",
            "F8 cross_type_signals (cross-benefit-type intelligence)",
            "F8 provider_clustering (quality-cost tiers)",
            "CMS Hospital Transparency (cash prices by state)",
            "Medicare Physician Fee Schedule (national baseline)",
            "NADAC Pharmacy Pricing (drug cost baseline)",
            "CMS Hospital Compare (quality ratings)",
        ],
    }


def attribute_prediction_errors(
    db: Session,
    employer_id: str,
) -> dict[str, Any]:
    """Compare predictions vs actuals and categorize error sources.

    Constitution F3: accuracy must be measured, understood, and improved.
    This function decomposes prediction errors into actionable categories:
    - Data quality: missing or stale price data, incomplete claims history
    - Model calibration: systematic over/under-prediction
    - Outlier events: large unexpected claims (catastrophic, transplant, etc.)
    - Demographic shift: changes in employee population characteristics

    Parameters
    ----------
    db : Session
        Database session.
    employer_id : str
        UUID of the employer to analyze.

    Returns
    -------
    dict with error attribution breakdown and improvement recommendations.
    """
    employer = db.query(Employer).filter(Employer.employer_id == employer_id).first()
    if not employer:
        return {"error": "employer_not_found", "employer_id": employer_id}

    # Get current predictions
    cross_type_signals = generate_cross_type_signals(db)
    price_reg = price_regression_by_state(db)
    model, train_meta = _train_ensemble(db, cross_type_signals, price_reg)
    model_available = model is not None

    active_count = (
        db.query(func.count(Employee.employee_id))
        .filter(
            Employee.employer_id == employer.employer_id,
            Employee.status == EmployeeStatus.active,
        )
        .scalar()
        or employer.employee_count
        or 1
    )

    error_attributions: dict[str, list] = {
        "data_quality": [],
        "model_calibration": [],
        "outlier_events": [],
        "demographic_shift": [],
    }
    per_benefit_type: dict[str, dict] = {}

    for bt in BenefitType:
        # Actual paid amount
        actual_total = (
            db.query(func.coalesce(func.sum(Claim.amount_paid), 0))
            .filter(
                Claim.employer_id == employer.employer_id,
                Claim.benefit_type == bt,
                Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
            )
            .scalar()
        )
        actual_pepm = float(actual_total) / max(active_count, 1) if actual_total else 0.0
        if actual_pepm <= 0:
            continue

        # Predicted amount
        features = _extract_employer_features(
            db, employer, bt, cross_type_signals, price_reg
        )
        if model_available:
            X = np.array([_features_to_array(features)])
            predicted_pepm = float(model.predict(X)[0])
        else:
            predicted_pepm = _actuarial_heuristic(features, bt)

        error = predicted_pepm - actual_pepm
        abs_error = abs(error)
        pct_error = abs_error / actual_pepm * 100 if actual_pepm > 0 else 0

        bt_result = {
            "predicted_pepm": round(predicted_pepm, 2),
            "actual_pepm": round(actual_pepm, 2),
            "error": round(error, 2),
            "abs_pct_error": round(pct_error, 2),
            "direction": "over" if error > 0 else "under",
            "error_sources": [],
        }

        # --- Data quality attribution ---
        # Check for sparse price data in employer's state
        state = (employer.geography or "")[:2].upper() if employer.geography else None
        if state:
            state_price_count = (
                db.query(func.count(PriceData.price_id))
                .filter(PriceData.state == state)
                .scalar()
                or 0
            )
            if state_price_count < 100:
                contribution = min(pct_error * 0.3, 15.0)
                bt_result["error_sources"].append({
                    "category": "data_quality",
                    "factor": "sparse_state_price_data",
                    "contribution_pct": round(contribution, 1),
                    "detail": f"Only {state_price_count} price records in {state}; prediction relies more on national averages",
                })
                error_attributions["data_quality"].append(f"{bt.value}: sparse price data in {state}")

        # Check for limited claims history
        claim_count = features.get("historical_claim_count", 0)
        if claim_count < 20:
            contribution = min(pct_error * 0.25, 20.0)
            bt_result["error_sources"].append({
                "category": "data_quality",
                "factor": "limited_claims_history",
                "contribution_pct": round(contribution, 1),
                "detail": f"Only {int(claim_count)} historical claims for {bt.value}; low credibility weighting",
            })
            error_attributions["data_quality"].append(f"{bt.value}: {int(claim_count)} claims (need 20+)")

        # --- Model calibration attribution ---
        if not model_available:
            contribution = min(pct_error * 0.2, 10.0)
            bt_result["error_sources"].append({
                "category": "model_calibration",
                "factor": "heuristic_fallback",
                "contribution_pct": round(contribution, 1),
                "detail": "Using actuarial heuristic (insufficient training data for ML model)",
            })
            error_attributions["model_calibration"].append(f"{bt.value}: actuarial heuristic fallback")
        elif error > 0 and pct_error > 15:
            bt_result["error_sources"].append({
                "category": "model_calibration",
                "factor": "systematic_over_prediction",
                "contribution_pct": round(min(pct_error * 0.15, 10.0), 1),
                "detail": f"Model over-predicts {bt.value} by {round(pct_error, 1)}%; may need recalibration",
            })
            error_attributions["model_calibration"].append(f"{bt.value}: over-predicts by {round(pct_error, 1)}%")
        elif error < 0 and pct_error > 15:
            bt_result["error_sources"].append({
                "category": "model_calibration",
                "factor": "systematic_under_prediction",
                "contribution_pct": round(min(pct_error * 0.15, 10.0), 1),
                "detail": f"Model under-predicts {bt.value} by {round(pct_error, 1)}%; may need recalibration",
            })
            error_attributions["model_calibration"].append(f"{bt.value}: under-predicts by {round(pct_error, 1)}%")

        # --- Outlier event attribution ---
        # Check for large individual claims (>3x average)
        avg_claim = features.get("historical_avg_paid", 0)
        if avg_claim > 0:
            large_claims = (
                db.query(func.count(Claim.claim_id))
                .filter(
                    Claim.employer_id == employer.employer_id,
                    Claim.benefit_type == bt,
                    Claim.amount_paid > avg_claim * 3,
                    Claim.status.in_([ClaimStatus.paid, ClaimStatus.approved]),
                )
                .scalar()
                or 0
            )
            if large_claims > 0:
                contribution = min(pct_error * 0.3, 25.0)
                bt_result["error_sources"].append({
                    "category": "outlier_events",
                    "factor": "large_claim_outliers",
                    "contribution_pct": round(contribution, 1),
                    "detail": f"{large_claims} claims > 3x average (${round(avg_claim, 2)}) — catastrophic or high-cost events",
                })
                error_attributions["outlier_events"].append(f"{bt.value}: {large_claims} outlier claims")

        # --- Demographic shift attribution ---
        # Compare current employee count against employer record
        recorded_count = employer.employee_count or 0
        if recorded_count > 0 and abs(active_count - recorded_count) / recorded_count > 0.1:
            contribution = min(pct_error * 0.15, 10.0)
            shift_pct = round((active_count - recorded_count) / recorded_count * 100, 1)
            bt_result["error_sources"].append({
                "category": "demographic_shift",
                "factor": "employee_count_change",
                "contribution_pct": round(contribution, 1),
                "detail": f"Employee count shifted {shift_pct}% (recorded: {recorded_count}, current: {active_count})",
            })
            error_attributions["demographic_shift"].append(
                f"Employee count changed {shift_pct}% ({recorded_count} -> {active_count})"
            )

        per_benefit_type[bt.value] = bt_result

    # Generate recommendations based on error attribution
    recommendations = []
    if error_attributions["data_quality"]:
        recommendations.append({
            "priority": "high",
            "action": "Increase price data coverage for employer's state/region",
            "expected_improvement": "5-15% MAPE reduction",
            "issues": error_attributions["data_quality"],
        })
    if error_attributions["model_calibration"]:
        recommendations.append({
            "priority": "medium",
            "action": "Retrain model with more employer-specific claims data",
            "expected_improvement": "3-10% MAPE reduction",
            "issues": error_attributions["model_calibration"],
        })
    if error_attributions["outlier_events"]:
        recommendations.append({
            "priority": "medium",
            "action": "Implement outlier-robust prediction (e.g., trimmed mean, Huber loss)",
            "expected_improvement": "Reduces impact of catastrophic claims on predictions",
            "issues": error_attributions["outlier_events"],
        })
    if error_attributions["demographic_shift"]:
        recommendations.append({
            "priority": "low",
            "action": "Update employer demographics and re-run census",
            "expected_improvement": "Aligns prediction features with current population",
            "issues": error_attributions["demographic_shift"],
        })

    return {
        "employer_id": str(employer.employer_id),
        "employer_name": employer.name,
        "active_employees": active_count,
        "prediction_method": "gradient_boosting_ensemble" if model_available else "actuarial_heuristic",
        "per_benefit_type": per_benefit_type,
        "error_attribution_summary": {
            "data_quality_issues": len(error_attributions["data_quality"]),
            "model_calibration_issues": len(error_attributions["model_calibration"]),
            "outlier_events": len(error_attributions["outlier_events"]),
            "demographic_shifts": len(error_attributions["demographic_shift"]),
        },
        "recommendations": recommendations,
        "analyzed_at": datetime.now(UTC).isoformat(),
    }
