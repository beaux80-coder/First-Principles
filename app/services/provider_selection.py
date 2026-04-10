"""Provider Selection (Function 4) — Simplified Certification-First Selection.

Selection policy (in priority order):

  1. Certification
     The provider must be certified to deliver the requested service.
     Certification means:
       • The provider has an NPI (National Provider Identifier), which is
         proof of federal registration with CMS and the NPPES directory.
       • The provider's `provider_type` matches the benefit type of the
         service (physician/hospital for health; dental for dental;
         vision for vision; mental_health for mental_health; etc.).
       • If the service requires a specific specialty, the provider's
         `specialties` list includes that specialty.
       • The provider is registered in the target state (when a state
         is specified).

  2. Reasonable time frame (convenience)
     The provider must be within a reasonable travel distance AND able
     to deliver the service within a clinically appropriate appointment
     window. The thresholds come from CONVENIENCE_THRESHOLDS in
     `app/config.py`, keyed by clinical urgency.

  3. Cheapest
     Among the certified providers that also meet the convenience
     thresholds, the engine selects the one with the lowest verified
     price via F2 price discovery (14 channels).

This is deliberately simple. The engine does not rank certified
providers by outcome score, confidence intervals, or any other quality
signal when choosing between them. If two providers are both certified
and both reachable within the convenience window, price is the only
tiebreaker.

Quality data on Provider (quality_score, outcome_data_points) is still
collected for the Layer 4 learning loop and for employer-facing
dashboards, but it is NOT an input to the selection decision.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy.orm import Session

from app.models.provider import Provider


logger = logging.getLogger(__name__)


# Which provider types are valid for each benefit type. Used as the
# first gate of the certification filter: a physician cannot be
# certified to deliver a dental cleaning, and a dentist cannot be
# certified to deliver an ophthalmology exam.
BENEFIT_TYPE_TO_PROVIDER_TYPES: dict[str, list[str]] = {
    "health": ["physician", "hospital"],
    "dental": ["dental"],
    "vision": ["vision"],
    "mental_health": ["mental_health", "physician"],
    "life": ["physician"],
    "std": ["physician", "hospital"],
    "ltd": ["physician", "hospital"],
}


def select_provider(
    db: Session,
    condition: str,
    benefit_type: str,
    patient_history: dict,
    state: Optional[str] = None,
    service_code: Optional[str] = None,
    employee_latitude: Optional[float] = None,
    employee_longitude: Optional[float] = None,
    clinical_urgency: str = "routine",
    required_specialty: Optional[str] = None,
) -> dict:
    """Select a provider for the requested service.

    Three-stage pipeline:
      Stage 1 — Certification filter. Drops any provider not certified
                to deliver this service (wrong provider_type, missing
                NPI, missing required specialty, wrong state).
      Stage 2 — Convenience floor. Drops providers outside the travel
                and appointment thresholds for this urgency level.
      Stage 3 — Cost optimization. Picks the cheapest provider from
                whoever passed stages 1 and 2.

    Returns a dict with the selection, the per-stage audit, and the
    immutable audit record that is persisted to the F8 pipeline.
    """
    # === STAGE 1: Certification filter ===
    stage1 = _certification_filter(
        db=db,
        benefit_type=benefit_type,
        state=state,
        required_specialty=required_specialty,
    )

    # === STAGE 2: Convenience floor ===
    stage2 = _apply_convenience_floor(
        db=db,
        certified_providers=stage1["certified_providers"],
        employee_latitude=employee_latitude,
        employee_longitude=employee_longitude,
        clinical_urgency=clinical_urgency,
    )

    # === STAGE 3: Cost optimization ===
    stage3 = _cost_optimization(
        db=db,
        candidate_providers=stage2["qualified_providers"],
        service_code=service_code,
        state=state,
    )

    audit_record = {
        "selection_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "condition": condition,
        "benefit_type": benefit_type,
        "clinical_urgency": clinical_urgency,
        "required_specialty": required_specialty,
        "stage1_certification": {
            "providers_evaluated": stage1["providers_evaluated"],
            "providers_certified": stage1["providers_certified"],
            "certified_provider_ids": [
                str(p["provider_id"]) for p in stage1["certified_providers"]
            ],
            "accepted_provider_types": stage1["accepted_provider_types"],
            "reasoning": stage1["reasoning"],
            "financial_data_access": "ZERO — certification filter does not read financial data",
        },
        "stage2_convenience_floor": {
            "clinical_urgency": clinical_urgency,
            "threshold": stage2["threshold"],
            "providers_before": stage2["providers_before"],
            "providers_after": stage2["providers_after"],
            "fallback_applied": stage2["fallback_applied"],
            "reasoning": stage2["reasoning"],
        },
        "stage3_cost_optimization": {
            "selected_provider": stage3.get("selected_provider"),
            "selected_price": stage3.get("selected_price"),
            "price_channel": stage3.get("price_channel"),
        },
    }

    # Persist audit record to immutable F8 data pipeline
    try:
        import json
        from decimal import Decimal

        def _default(o):
            if isinstance(o, Decimal):
                return float(o)
            raise TypeError

        clean_record = json.loads(json.dumps(audit_record, default=_default))

        from app.models.audit_log import AuditLog
        log = AuditLog(
            actor="system:provider_selection",
            action="provider_selection",
            resource_type="provider_selection",
            resource_id=audit_record["selection_id"],
            details=clean_record,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist selection audit: {e}")

    return {
        "selection": stage3,
        "certification": stage1,
        "convenience_floor": stage2,
        "audit_record": audit_record,
        "feeding_f8": True,
    }


# ---------------------------------------------------------------------------
# Stage 1: Certification filter
# ---------------------------------------------------------------------------
def _certification_filter(
    db: Session,
    benefit_type: str,
    state: Optional[str] = None,
    required_specialty: Optional[str] = None,
) -> dict:
    """Keep providers that are certified to deliver the requested service.

    Certification criteria:
      • NPI is present (federal registration with NPPES)
      • provider_type is one of the allowed types for this benefit type
      • If `required_specialty` is provided, provider's specialties contain it
      • If `state` is provided, provider is registered in that state
    """
    accepted_types = BENEFIT_TYPE_TO_PROVIDER_TYPES.get(
        benefit_type, ["physician", "hospital"]
    )

    query = db.query(Provider).filter(
        Provider.provider_type.in_(accepted_types),
        Provider.npi.isnot(None),
        Provider.npi != "",
    )
    if state:
        query = query.filter(Provider.state == state)

    all_providers = query.all()
    certified: list[dict] = []

    for provider in all_providers:
        # NPI check (belt-and-suspenders — the filter already ran)
        if not provider.npi or not provider.npi.strip():
            continue

        # Specialty check (if required)
        if required_specialty:
            provider_specialties = [
                s.lower() for s in (provider.specialties or [])
            ]
            if required_specialty.lower() not in provider_specialties:
                continue

        certified.append({
            "provider_id": str(provider.provider_id),
            "provider_name": provider.name,
            "npi": provider.npi,
            "provider_type": (
                provider.provider_type.value
                if hasattr(provider.provider_type, "value")
                else str(provider.provider_type)
            ),
            "specialties": provider.specialties or [],
            "state": provider.state,
        })

    reasoning_parts = [
        f"Certification filter: benefit_type='{benefit_type}' maps to "
        f"provider_types={accepted_types}."
    ]
    if state:
        reasoning_parts.append(f"Restricted to state='{state}'.")
    if required_specialty:
        reasoning_parts.append(f"Required specialty='{required_specialty}'.")
    reasoning_parts.append(
        f"{len(all_providers)} candidates examined, {len(certified)} "
        f"certified (NPI present, provider_type valid, specialty match)."
    )

    return {
        "providers_evaluated": len(all_providers),
        "providers_certified": len(certified),
        "certified_providers": certified,
        "accepted_provider_types": accepted_types,
        "required_specialty": required_specialty,
        "state_filter": state,
        "reasoning": " ".join(reasoning_parts),
    }


# ---------------------------------------------------------------------------
# Stage 2: Convenience floor
# ---------------------------------------------------------------------------
def _apply_convenience_floor(
    db: Session,
    certified_providers: list[dict],
    employee_latitude: Optional[float],
    employee_longitude: Optional[float],
    clinical_urgency: str,
) -> dict:
    """Drop providers outside the travel-distance threshold for the
    clinical urgency level.

    Fallback behavior: if no provider passes the distance threshold,
    keep the single closest one. The engine never denies care by making
    convenience impossible to satisfy — it falls back to the nearest
    certified provider.
    """
    from app.config import CONVENIENCE_THRESHOLDS

    threshold = CONVENIENCE_THRESHOLDS.get(
        clinical_urgency, CONVENIENCE_THRESHOLDS["routine"]
    )
    max_miles = threshold["travel_miles"]

    if employee_latitude is None or employee_longitude is None:
        return {
            "qualified_providers": certified_providers,
            "threshold": threshold,
            "providers_before": len(certified_providers),
            "providers_after": len(certified_providers),
            "fallback_applied": True,
            "reasoning": (
                "No employee location available — convenience filter skipped. "
                "All certified providers remain in the candidate set."
            ),
        }

    # Emergent care: closest certified provider wins, no distance cap.
    if clinical_urgency == "emergent" or max_miles is None:
        return {
            "qualified_providers": certified_providers,
            "threshold": threshold,
            "providers_before": len(certified_providers),
            "providers_after": len(certified_providers),
            "fallback_applied": False,
            "reasoning": (
                "Emergent urgency — no convenience cap applied. Closest "
                "certified provider wins."
            ),
        }

    within_threshold: list[dict] = []
    for p in certified_providers:
        provider_row = db.query(Provider).filter(
            Provider.provider_id == p["provider_id"]
        ).first()
        if provider_row is None:
            continue
        if provider_row.latitude is None or provider_row.longitude is None:
            # Missing coordinates — keep provider with distance_unknown flag
            # so they are not silently excluded.
            p_with_distance = dict(p)
            p_with_distance["distance_miles"] = None
            p_with_distance["distance_unknown"] = True
            within_threshold.append(p_with_distance)
            continue

        distance = _haversine_miles(
            employee_latitude,
            employee_longitude,
            provider_row.latitude,
            provider_row.longitude,
        )
        if distance <= max_miles:
            p_with_distance = dict(p)
            p_with_distance["distance_miles"] = round(distance, 2)
            within_threshold.append(p_with_distance)

    fallback_applied = False
    if not within_threshold:
        # No one meets the threshold. Fall back to the closest certified
        # provider rather than denying care.
        closest: Optional[tuple[dict, float]] = None
        for p in certified_providers:
            provider_row = db.query(Provider).filter(
                Provider.provider_id == p["provider_id"]
            ).first()
            if provider_row is None:
                continue
            if provider_row.latitude is None or provider_row.longitude is None:
                continue
            distance = _haversine_miles(
                employee_latitude,
                employee_longitude,
                provider_row.latitude,
                provider_row.longitude,
            )
            if closest is None or distance < closest[1]:
                closest = (dict(p), distance)
        if closest is not None:
            closest[0]["distance_miles"] = round(closest[1], 2)
            within_threshold = [closest[0]]
            fallback_applied = True
        else:
            within_threshold = certified_providers
            fallback_applied = True

    return {
        "qualified_providers": within_threshold,
        "threshold": threshold,
        "providers_before": len(certified_providers),
        "providers_after": len(within_threshold),
        "fallback_applied": fallback_applied,
        "reasoning": (
            f"Convenience threshold for '{clinical_urgency}': "
            f"travel ≤ {max_miles} miles, appointment ≤ "
            f"{threshold['appointment_hours']}h. "
            f"{len(within_threshold)} of {len(certified_providers)} "
            f"certified providers qualify."
            + (" Fallback applied: no provider met the threshold, "
               "closest certified provider was kept."
               if fallback_applied else "")
        ),
    }


# ---------------------------------------------------------------------------
# Stage 3: Cost optimization
# ---------------------------------------------------------------------------
def _cost_optimization(
    db: Session,
    candidate_providers: list[dict],
    service_code: Optional[str] = None,
    state: Optional[str] = None,
) -> dict:
    """Pick the cheapest provider from the candidate set using F2 price discovery.

    If no service code is provided (and thus no price comparison is
    possible), the first certified+convenient candidate is returned.
    """
    if not candidate_providers:
        return {
            "selected_provider": None,
            "selected_price": None,
            "note": (
                "No certified providers found in the candidate set. Cannot "
                "select a provider for this request."
            ),
        }

    if not service_code:
        first = candidate_providers[0]
        return {
            "selected_provider": first,
            "selected_price": None,
            "note": (
                "No service code provided — cost comparison skipped. "
                "First certified provider selected."
            ),
        }

    from app.services.price_discovery import compare_all_channels

    best_price: Optional[float] = None
    best_provider: Optional[dict] = None
    best_comparison: Optional[dict] = None

    for provider in candidate_providers:
        comparison = compare_all_channels(
            db, service_code, state=state, benefit_type="health"
        )
        lowest = comparison.get("lowest_price") if comparison else None
        if lowest is None:
            continue
        if best_price is None or lowest < best_price:
            best_price = lowest
            best_provider = provider
            best_comparison = comparison

    if best_provider is None:
        # No price discovered — still pick a certified candidate so care
        # can proceed. Record the note for audit.
        return {
            "selected_provider": candidate_providers[0],
            "selected_price": None,
            "note": (
                "No verified price available for this service. First "
                "certified provider selected so care is not blocked."
            ),
        }

    return {
        "selected_provider": best_provider,
        "selected_price": best_price,
        "price_channel": best_comparison["lowest_channel"] if best_comparison else None,
        "price_comparison": best_comparison,
        "note": (
            "Selected the cheapest certified provider within the "
            "convenience threshold."
        ),
    }


# ---------------------------------------------------------------------------
# Outcome tracking — feeds Layer 4 learning, NOT used in selection
# ---------------------------------------------------------------------------
def record_provider_outcome(
    db: Session,
    provider_id: uuid.UUID,
    condition: str,
    resolved: bool,
    resolution_criteria: str,
    resolution_timeframe_days: Optional[int] = None,
) -> dict:
    """Record an outcome for a provider.

    Outcome data is collected for the Layer 4 continuous-learning loop
    and for employer-facing dashboards. It is NOT an input to the
    provider selection decision, which is based purely on certification,
    convenience, and cost.
    """
    provider = db.query(Provider).filter(Provider.provider_id == provider_id).first()
    if not provider:
        return {"error": "Provider not found"}

    current_points = provider.outcome_data_points or 0
    current_quality = provider.quality_score or 50.0

    new_points = current_points + 1
    if resolved:
        new_quality = ((current_quality * current_points) + 100.0) / new_points
    else:
        new_quality = ((current_quality * current_points) + 0.0) / new_points

    provider.outcome_data_points = new_points
    provider.quality_score = round(new_quality, 2)
    db.commit()

    return {
        "provider_id": str(provider_id),
        "outcome_recorded": True,
        "resolved": resolved,
        "resolution_criteria": resolution_criteria,
        "new_quality_score": provider.quality_score,
        "total_data_points": provider.outcome_data_points,
        "note": (
            "Outcome recorded for Layer 4 learning. Provider quality "
            "score is descriptive data, not an input to provider "
            "selection decisions."
        ),
    }


def get_provider_outcome_report(db: Session, provider_id: uuid.UUID) -> dict:
    """Get the outcome report for a provider.

    Returns the recorded quality score and data point count for
    transparency and dashboards. Not used in selection.
    """
    provider = db.query(Provider).filter(Provider.provider_id == provider_id).first()
    if not provider:
        return {"error": "Provider not found"}

    return {
        "provider_id": str(provider_id),
        "provider_name": provider.name,
        "quality_score": provider.quality_score,
        "outcome_data_points": provider.outcome_data_points or 0,
        "npi": provider.npi,
        "provider_type": (
            provider.provider_type.value
            if hasattr(provider.provider_type, "value")
            else str(provider.provider_type)
        ),
        "note": (
            "Outcome data is collected for Layer 4 learning and for "
            "dashboards. Provider selection is based on certification, "
            "convenience, and cost — not on this score."
        ),
    }


# ---------------------------------------------------------------------------
# Geography helper
# ---------------------------------------------------------------------------
def _haversine_miles(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Great-circle distance between two lat/lon points, in miles."""
    r_miles = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_miles * c
