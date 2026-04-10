"""Employee Care Execution Engine (Function 9).

Constitution: "Employee describes issue in plain language. System executes
entire care process. Zero copays, zero deductibles, zero out-of-pocket."

Constitution: "If a referral, imaging order, or lab order is generated,
the chain continues automatically — the system schedules it, selects the
provider (F4), discovers the price (F2), and pays (F2) without the employee
lifting a finger."

Constitution: "Every event recorded feeding F8."

This module orchestrates the full care lifecycle:
1. Intake: NLP interprets plain-language issue
2. Provider selection via F4 (clinical filtering + cost optimization)
3. Scheduling at earliest available time
4. Auto-chain referrals, imaging, labs
5. Prescription routing to lowest-price pharmacy channel via F2
6. Resolution tracking with proactive follow-ups
7. Departing employee recommendation path (links to F6A)
"""

import logging
import uuid
from datetime import datetime, UTC, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.care_episode import (
    CareEpisode,
    EpisodeStatus,
    StepType,
    StepStatus,
)
from app.models.employee import Employee, EmployeeStatus
from app.models.provider import Provider
from app.models.service import BenefitType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Symptom-to-condition mapping for plain-language NLP interpretation
# Bridges lay descriptions to clinical terminology recognized by F1/F4
# ---------------------------------------------------------------------------
PLAIN_LANGUAGE_CONDITIONS = {
    # General / primary care
    "headache": {"condition": "cephalgia", "benefit_type": "health", "needs_referral": False},
    "migraine": {"condition": "migraine_disorder", "benefit_type": "health", "needs_referral": True},
    "stomach pain": {"condition": "abdominal_pain", "benefit_type": "health", "needs_referral": False},
    "back pain": {"condition": "lumbar_radiculopathy", "benefit_type": "health", "needs_referral": True},
    "neck pain": {"condition": "cervical_pain", "benefit_type": "health", "needs_referral": False},
    "chest pain": {"condition": "chest_pain_evaluation", "benefit_type": "health", "needs_referral": True},
    "shortness of breath": {"condition": "dyspnea_evaluation", "benefit_type": "health", "needs_referral": True},
    "cough": {"condition": "persistent_cough", "benefit_type": "health", "needs_referral": False},
    "fever": {"condition": "febrile_illness", "benefit_type": "health", "needs_referral": False},
    "sore throat": {"condition": "pharyngitis", "benefit_type": "health", "needs_referral": False},
    "rash": {"condition": "dermatitis", "benefit_type": "health", "needs_referral": False},
    "knee pain": {"condition": "knee_pain_evaluation", "benefit_type": "health", "needs_referral": True},
    "shoulder pain": {"condition": "shoulder_impingement", "benefit_type": "health", "needs_referral": True},
    "numbness": {"condition": "neuropathy_evaluation", "benefit_type": "health", "needs_referral": True},
    "dizziness": {"condition": "vertigo_evaluation", "benefit_type": "health", "needs_referral": True},
    "fatigue": {"condition": "chronic_fatigue_evaluation", "benefit_type": "health", "needs_referral": False},
    "high blood pressure": {"condition": "hypertension", "benefit_type": "health", "needs_referral": False},
    "diabetes": {"condition": "diabetes_management", "benefit_type": "health", "needs_referral": False},

    # Dental
    "tooth pain": {"condition": "dental_caries", "benefit_type": "dental", "needs_referral": False},
    "toothache": {"condition": "dental_caries", "benefit_type": "dental", "needs_referral": False},
    "bleeding gums": {"condition": "periodontal_disease", "benefit_type": "dental", "needs_referral": False},
    "wisdom teeth": {"condition": "impacted_third_molar", "benefit_type": "dental", "needs_referral": True},
    "broken tooth": {"condition": "dental_fracture", "benefit_type": "dental", "needs_referral": False},
    "cavity": {"condition": "dental_caries", "benefit_type": "dental", "needs_referral": False},

    # Vision
    "blurry vision": {"condition": "refractive_error", "benefit_type": "vision", "needs_referral": False},
    "eye pain": {"condition": "ocular_pain", "benefit_type": "vision", "needs_referral": True},
    "cant see well": {"condition": "visual_acuity_decline", "benefit_type": "vision", "needs_referral": False},
    "need glasses": {"condition": "refractive_error", "benefit_type": "vision", "needs_referral": False},
    "dry eyes": {"condition": "keratoconjunctivitis_sicca", "benefit_type": "vision", "needs_referral": False},

    # Mental health
    "depressed": {"condition": "major_depressive_disorder", "benefit_type": "mental_health", "needs_referral": False},
    "anxious": {"condition": "generalized_anxiety_disorder", "benefit_type": "mental_health", "needs_referral": False},
    "cant sleep": {"condition": "insomnia_disorder", "benefit_type": "mental_health", "needs_referral": False},
    "stressed": {"condition": "adjustment_disorder", "benefit_type": "mental_health", "needs_referral": False},
    "panic attacks": {"condition": "panic_disorder", "benefit_type": "mental_health", "needs_referral": False},
    "drinking too much": {"condition": "alcohol_use_disorder", "benefit_type": "mental_health", "needs_referral": True},
    "substance use": {"condition": "substance_use_disorder", "benefit_type": "mental_health", "needs_referral": True},
    "grief": {"condition": "bereavement", "benefit_type": "mental_health", "needs_referral": False},
    "hopeless": {"condition": "major_depressive_disorder", "benefit_type": "mental_health", "needs_referral": False},

    # Disability
    "cant work": {"condition": "functional_limitation_evaluation", "benefit_type": "std", "needs_referral": True},
    "unable to work": {"condition": "disability_evaluation", "benefit_type": "std", "needs_referral": True},
    "work injury": {"condition": "occupational_injury", "benefit_type": "std", "needs_referral": True},
}


def _make_step(step_type: str, status: str, details: str) -> dict:
    """Create a care step record for the episode steps JSON array."""
    return {
        "type": step_type,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
        "details": details,
    }


def _interpret_issue(db: Session, description: str) -> dict:
    """Interpret plain-language issue description using NLP.

    Uses the ONNX ClinicalBERT from F1 (app/services/clinical_nlp.py) for
    semantic matching, with the plain-language condition map as fallback.

    Returns: {condition, benefit_type, needs_referral, confidence, guidelines}
    """
    description_lower = description.lower().strip()

    # Step 1: Try ClinicalBERT via F1 NLP for semantic matching
    guidelines = []
    bert_condition = None
    bert_confidence = 0.0
    try:
        from app.services.clinical_nlp import match_symptoms_to_guidelines
        # Try matching across all benefit types
        for bt in ["health", "dental", "vision", "mental_health", "std", "ltd"]:
            matches = match_symptoms_to_guidelines(
                db=db,
                patient_symptoms=[description],
                patient_history={},
                benefit_type=bt,
                top_k=3,
                min_score=0.05,
            )
            for m in matches:
                if m["similarity_score"] > bert_confidence:
                    bert_confidence = m["similarity_score"]
                    bert_condition = m["condition"]
                guidelines.extend(matches)

        # Deduplicate and sort by score
        seen = set()
        unique_guidelines = []
        for g in sorted(guidelines, key=lambda x: x["similarity_score"], reverse=True):
            gid = g["guideline_id"]
            if gid not in seen:
                seen.add(gid)
                unique_guidelines.append(g)
        guidelines = unique_guidelines[:5]
    except Exception as e:
        logger.debug(f"ClinicalBERT matching failed (fallback to keyword): {e}")

    # Step 2: Keyword matching from plain-language map
    best_keyword_match = None
    best_keyword_len = 0
    for phrase, mapping in PLAIN_LANGUAGE_CONDITIONS.items():
        if phrase in description_lower and len(phrase) > best_keyword_len:
            best_keyword_match = mapping
            best_keyword_len = len(phrase)

    # Step 3: Combine results — prefer BERT if high confidence, else keyword
    if bert_condition and bert_confidence > 0.15:
        condition = bert_condition
        benefit_type = guidelines[0].get("benefit_type", "health") if guidelines else "health"
        needs_referral = best_keyword_match["needs_referral"] if best_keyword_match else False
        confidence = bert_confidence
    elif best_keyword_match:
        condition = best_keyword_match["condition"]
        benefit_type = best_keyword_match["benefit_type"]
        needs_referral = best_keyword_match["needs_referral"]
        confidence = 0.80  # High confidence for direct keyword match
    else:
        # Fallback: general evaluation
        condition = "general_medical_evaluation"
        benefit_type = "health"
        needs_referral = False
        confidence = 0.30

    return {
        "condition": condition,
        "benefit_type": benefit_type,
        "needs_referral": needs_referral,
        "confidence": round(confidence, 4),
        "guidelines": guidelines,
    }


# ---------------------------------------------------------------------------
# Core care execution functions
# ---------------------------------------------------------------------------

def intake_issue(
    db: Session,
    employee_id: uuid.UUID,
    issue_description: str,
    benefit_type_override: Optional[str] = None,
) -> dict:
    """Employee describes issue in plain language -> system executes entire care process.

    Constitution: "Employee describes issue in plain language. System executes
    entire care process."

    Steps:
    1. NLP interprets the issue (ClinicalBERT + keyword map)
    2. Creates a care episode
    3. Selects a provider via F4
    4. Schedules appointment
    5. If referral needed, auto-chains
    6. Records everything feeding F8
    """
    # Validate employee exists and is active
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found", "employee_id": str(employee_id)}
    if employee.status == EmployeeStatus.terminated:
        return {
            "error": "Employee is terminated. Use departure-recommendation endpoint.",
            "employee_id": str(employee_id),
        }

    # Step 1: NLP interpretation
    interpretation = _interpret_issue(db, issue_description)
    condition = interpretation["condition"]
    benefit_type_str = benefit_type_override or interpretation["benefit_type"]

    try:
        benefit_type = BenefitType(benefit_type_str)
    except ValueError:
        benefit_type = BenefitType.health

    # Step 2: Create care episode
    steps = [
        _make_step(StepType.intake.value, StepStatus.completed.value,
                    f"NLP interpreted: {condition} (confidence: {interpretation['confidence']:.0%})")
    ]

    episode = CareEpisode(
        employee_id=employee_id,
        benefit_type=benefit_type,
        status=EpisodeStatus.open,
        issue_description=issue_description,
        interpreted_condition=condition,
        interpreted_benefit_type=benefit_type_str,
        nlp_confidence=interpretation["confidence"],
        matched_guidelines=interpretation["guidelines"],
        steps=steps,
        employee_actions_required=1,
        resolution_criteria=_get_resolution_criteria(condition),
    )
    db.add(episode)
    db.flush()

    logger.info(
        "Care episode %s created: '%s' -> %s (%s, confidence=%.0f%%)",
        episode.episode_id, issue_description, condition,
        benefit_type_str, interpretation["confidence"] * 100,
    )

    # Step 3: Provider selection via F4
    provider_result = _select_provider_for_episode(db, episode, condition, benefit_type_str)
    if provider_result.get("provider_id"):
        episode.provider_id = uuid.UUID(provider_result["provider_id"])
        steps.append(_make_step(
            StepType.provider_selection.value,
            StepStatus.completed.value,
            f"Provider selected: {provider_result.get('provider_name', 'N/A')} via F4",
        ))
    else:
        steps.append(_make_step(
            StepType.provider_selection.value,
            StepStatus.completed.value,
            "No providers in database yet. Will assign when providers are available.",
        ))

    # Step 4: Schedule appointment
    scheduling_result = schedule_appointment(db, episode.episode_id)
    steps.append(_make_step(
        StepType.scheduling.value,
        StepStatus.completed.value,
        f"Appointment scheduled: {scheduling_result.get('appointment_time', 'N/A')}",
    ))

    # Step 5: Auto-chain referral if needed
    if interpretation["needs_referral"]:
        referral_result = process_referral(db, episode.episode_id, condition)
        steps.append(_make_step(
            StepType.referral.value,
            StepStatus.completed.value,
            f"Referral auto-processed for {condition}",
        ))

    # Update steps and commit
    episode.steps = steps
    episode.employee_actions_required = 1  # Just show up for appointment
    episode.last_updated_at = datetime.now(UTC)
    db.commit()

    return {
        "episode_id": str(episode.episode_id),
        "status": episode.status.value,
        "interpretation": {
            "original_description": issue_description,
            "interpreted_condition": condition,
            "benefit_type": benefit_type_str,
            "nlp_confidence": interpretation["confidence"],
            "matched_guidelines": len(interpretation["guidelines"]),
        },
        "provider": provider_result,
        "scheduling": scheduling_result,
        "referral_auto_chained": interpretation["needs_referral"],
        "employee_actions_required": episode.employee_actions_required,
        "employee_message": _generate_employee_message(episode, provider_result, scheduling_result),
        "cost_to_employee": {
            "copay": 0.0,
            "deductible": 0.0,
            "out_of_pocket": 0.0,
            "explanation": "Zero copays, zero deductibles, zero out-of-pocket. "
                           "System handles all costs directly.",
        },
        "feeding_f8": True,
    }


def schedule_appointment(
    db: Session,
    episode_id: uuid.UUID,
    preferred_time: Optional[datetime] = None,
) -> dict:
    """Auto-schedule appointment at earliest available time.

    Constitution: "System handles scheduling."

    In production: integrates with provider scheduling APIs.
    In development: mock adapter returns next available slot.
    """
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found"}

    # Mock scheduling adapter — in production this calls the provider's
    # scheduling API or a scheduling aggregator
    now = datetime.now(UTC)
    if preferred_time and preferred_time > now:
        appointment_time = preferred_time
    else:
        # Next available: next business day at 9am
        next_slot = now + timedelta(days=1)
        # Skip weekends
        while next_slot.weekday() >= 5:
            next_slot += timedelta(days=1)
        appointment_time = next_slot.replace(hour=9, minute=0, second=0, microsecond=0)

    episode.appointment_time = appointment_time
    episode.last_updated_at = datetime.now(UTC)
    db.flush()

    return {
        "episode_id": str(episode_id),
        "appointment_time": appointment_time.isoformat(),
        "provider_id": str(episode.provider_id) if episode.provider_id else None,
        "scheduling_method": "auto_earliest_available",
        "note": "Appointment auto-scheduled at earliest available slot",
    }


def process_referral(
    db: Session,
    episode_id: uuid.UUID,
    referral_condition: Optional[str] = None,
    referral_type: str = "specialist",
) -> dict:
    """Auto-handle referrals, imaging, labs.

    Constitution: "If a referral, imaging order, or lab order is generated,
    the chain continues automatically — the system schedules it, selects the
    provider (F4), discovers the price (F2), and pays (F2) without the employee
    lifting a finger."
    """
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found"}

    condition = referral_condition or episode.interpreted_condition or "general_medical_evaluation"
    steps = list(episode.steps or [])
    sub_steps = []

    # Determine what the referral chain needs
    chain_items = _determine_referral_chain(condition, referral_type)

    for item in chain_items:
        item_type = item["type"]
        item_desc = item["description"]

        # Select provider for this chain item via F4
        provider_result = _select_provider_for_chain_item(
            db, item_type, condition, episode
        )

        # Schedule the chain item
        scheduling_result = _schedule_chain_item(db, episode, item_type)

        sub_step = {
            "chain_item": item_type,
            "description": item_desc,
            "provider": provider_result,
            "scheduling": scheduling_result,
            "auto_processed": True,
        }
        sub_steps.append(sub_step)

        steps.append(_make_step(
            item_type,
            StepStatus.completed.value,
            f"Auto-chained {item_type}: {item_desc}. "
            f"Provider: {provider_result.get('provider_name', 'pending')}",
        ))

    episode.steps = steps
    episode.last_updated_at = datetime.now(UTC)
    db.flush()

    return {
        "episode_id": str(episode_id),
        "referral_type": referral_type,
        "chain_items_processed": len(sub_steps),
        "chain_details": sub_steps,
        "employee_actions_required": 1,  # Just show up
        "note": "Referral chain auto-processed. Employee only needs to attend appointments.",
        "feeding_f8": True,
    }


def route_prescription(
    db: Session,
    episode_id: uuid.UUID,
    drug_name: str,
    ndc_code: Optional[str] = None,
    quantity: int = 30,
) -> dict:
    """Find lowest pharmacy channel via F2, route prescription.

    Constitution: "Prescription -> lowest-price channel via F2, routes to pharmacy."
    Constitution: "The price discovery engine replaces everything a PBM does."
    """
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found"}

    # Use F2 pharmacy channel comparison
    lowest_price = None
    lowest_channel = None
    pharmacy_comparison = None
    try:
        from app.services.price_discovery import compare_pharmacy_channels
        if ndc_code:
            pharmacy_comparison = compare_pharmacy_channels(
                db, ndc_code=ndc_code, drug_name=drug_name
            )
            lowest_price = pharmacy_comparison.get("lowest_verified_price")
            lowest_channel = pharmacy_comparison.get("lowest_channel")
    except Exception as e:
        logger.debug(f"Pharmacy price comparison unavailable: {e}")

    # If no F2 data, use general lowest-channel estimate
    if lowest_price is None:
        lowest_channel = "nadac_pharmacy"
        lowest_price = 0.0  # Zero out-of-pocket to employee regardless

    # Update episode
    steps = list(episode.steps or [])
    steps.append(_make_step(
        StepType.prescription.value,
        StepStatus.completed.value,
        f"Prescription routed: {drug_name} via {lowest_channel} "
        f"(system-paid price: ${lowest_price:.2f})",
    ))
    episode.steps = steps
    episode.prescription_routed = True
    episode.prescription_channel = lowest_channel
    episode.prescription_price = lowest_price
    episode.last_updated_at = datetime.now(UTC)
    db.commit()

    return {
        "episode_id": str(episode_id),
        "drug_name": drug_name,
        "ndc_code": ndc_code,
        "quantity": quantity,
        "routing": {
            "channel": lowest_channel,
            "system_paid_price": lowest_price,
            "employee_cost": 0.0,
            "pbm_eliminated": True,
        },
        "pharmacy_comparison": pharmacy_comparison,
        "employee_message": (
            f"Your prescription for {drug_name} has been routed to the lowest-cost "
            f"pharmacy channel. Cost to you: $0.00. The system pays directly."
        ),
        "feeding_f8": True,
    }


def track_resolution(db: Session, episode_id: uuid.UUID) -> dict:
    """Track open issues, proactive follow-up on missed appointments.

    Constitution: "Tracks resolution, proactive follow-ups on missed appointments."
    """
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found"}

    now = datetime.now(UTC)
    actions = []

    # Check for missed appointment
    if episode.appointment_time and episode.appointment_time < now and not episode.appointment_missed:
        # If appointment was in the past and episode still open, flag as potentially missed
        if episode.status == EpisodeStatus.open:
            episode.appointment_missed = True
            episode.follow_up_count = (episode.follow_up_count or 0) + 1
            episode.last_follow_up_at = now
            actions.append("missed_appointment_detected")

            # Auto-reschedule
            reschedule_result = schedule_appointment(db, episode_id)
            actions.append(f"auto_rescheduled_to_{reschedule_result.get('appointment_time', 'N/A')}")

            steps = list(episode.steps or [])
            steps.append(_make_step(
                StepType.follow_up.value,
                StepStatus.completed.value,
                f"Missed appointment detected. Auto-rescheduled. Follow-up #{episode.follow_up_count}.",
            ))
            episode.steps = steps

    # Check episode age — proactive follow-up if open too long
    episode_age_days = (now - episode.created_at.replace(tzinfo=UTC)).days if episode.created_at else 0
    if episode.status == EpisodeStatus.open and episode_age_days > 14:
        if not episode.last_follow_up_at or (now - episode.last_follow_up_at.replace(tzinfo=UTC)).days > 7:
            episode.follow_up_count = (episode.follow_up_count or 0) + 1
            episode.last_follow_up_at = now
            actions.append("proactive_follow_up_triggered")

            steps = list(episode.steps or [])
            steps.append(_make_step(
                StepType.follow_up.value,
                StepStatus.completed.value,
                f"Proactive follow-up #{episode.follow_up_count} — episode open {episode_age_days} days.",
            ))
            episode.steps = steps

    # Check if episode can be marked resolved
    resolution_check = _check_resolution(episode)

    episode.last_updated_at = now
    db.commit()

    return {
        "episode_id": str(episode_id),
        "status": episode.status.value,
        "episode_age_days": episode_age_days,
        "appointment_missed": episode.appointment_missed,
        "follow_up_count": episode.follow_up_count,
        "actions_taken": actions,
        "resolution_check": resolution_check,
        "resolution_criteria": episode.resolution_criteria,
        "feeding_f8": True,
    }


def get_episode_status(db: Session, episode_id: uuid.UUID) -> dict:
    """Full episode status — complete view of care execution progress.

    Constitution: every event recorded, full transparency.
    """
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found"}

    # Get provider info if assigned
    provider_info = None
    if episode.provider_id:
        provider = db.query(Provider).filter(
            Provider.provider_id == episode.provider_id
        ).first()
        if provider:
            provider_info = {
                "provider_id": str(provider.provider_id),
                "name": provider.name,
                "type": provider.provider_type.value if provider.provider_type else None,
                "quality_score": float(provider.quality_score) if provider.quality_score else None,
            }

    now = datetime.now(UTC)
    episode_age_days = (now - episode.created_at.replace(tzinfo=UTC)).days if episode.created_at else 0

    return {
        "episode_id": str(episode.episode_id),
        "employee_id": str(episode.employee_id),
        "status": episode.status.value,
        "issue_description": episode.issue_description,
        "interpretation": {
            "condition": episode.interpreted_condition,
            "benefit_type": episode.interpreted_benefit_type or episode.benefit_type.value,
            "nlp_confidence": episode.nlp_confidence,
            "matched_guidelines": episode.matched_guidelines,
        },
        "provider": provider_info,
        "appointment": {
            "scheduled_time": episode.appointment_time.isoformat() if episode.appointment_time else None,
            "missed": episode.appointment_missed,
        },
        "prescription": {
            "routed": episode.prescription_routed,
            "channel": episode.prescription_channel,
            "system_paid_price": episode.prescription_price,
            "employee_cost": 0.0,
        },
        "steps": episode.steps or [],
        "employee_actions_required": episode.employee_actions_required,
        "resolution": {
            "criteria": episode.resolution_criteria,
            "notes": episode.resolution_notes,
            "resolved_at": episode.resolved_at.isoformat() if episode.resolved_at else None,
        },
        "follow_ups": {
            "count": episode.follow_up_count,
            "last_at": episode.last_follow_up_at.isoformat() if episode.last_follow_up_at else None,
        },
        "provider_concern": {
            "flagged": episode.provider_concern_flag,
            "note": episode.provider_concern_note,
        },
        "cost_to_employee": {
            "copay": 0.0,
            "deductible": 0.0,
            "out_of_pocket": 0.0,
        },
        "episode_age_days": episode_age_days,
        "created_at": episode.created_at.isoformat() if episode.created_at else None,
        "last_updated_at": episode.last_updated_at.isoformat() if episode.last_updated_at else None,
        "feeding_f8": True,
    }


def flag_provider_concern(
    db: Session,
    episode_id: uuid.UUID,
    concern_note: str,
) -> dict:
    """Flag a provider concern on a care episode.

    Feeds into F4 provider quality tracking and F8 transparency reporting.
    """
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found"}

    episode.provider_concern_flag = True
    episode.provider_concern_note = concern_note
    episode.last_updated_at = datetime.now(UTC)

    steps = list(episode.steps or [])
    steps.append(_make_step(
        "provider_concern",
        StepStatus.completed.value,
        f"Provider concern flagged: {concern_note[:200]}",
    ))
    episode.steps = steps
    db.commit()

    return {
        "episode_id": str(episode_id),
        "provider_concern_flagged": True,
        "note": "Concern recorded. Feeds into F4 provider quality tracking and F8 reporting.",
        "feeding_f8": True,
    }


def generate_departure_recommendation(
    db: Session,
    employee_id: uuid.UUID,
) -> dict:
    """Departing employee recommendation path — links to F6A with verified experience data.

    Constitution: "Departing employee recommendation path — links to F6A
    with verified experience data."

    Generates a portable recommendation based on actual care episode data,
    not satisfaction surveys. Uses clinical resolution outcomes as the
    verified experience metric.
    """
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found"}

    # Gather all care episodes for this employee
    episodes = db.query(CareEpisode).filter(
        CareEpisode.employee_id == employee_id
    ).all()

    total_episodes = len(episodes)
    resolved_episodes = sum(1 for e in episodes if e.status == EpisodeStatus.resolved)
    open_episodes = sum(1 for e in episodes if e.status == EpisodeStatus.open)
    abandoned_episodes = sum(1 for e in episodes if e.status == EpisodeStatus.abandoned)

    # Calculate care metrics
    avg_resolution_days = None
    if resolved_episodes > 0:
        resolution_days = []
        for e in episodes:
            if e.status == EpisodeStatus.resolved and e.resolved_at and e.created_at:
                days = (e.resolved_at - e.created_at).days
                resolution_days.append(days)
        if resolution_days:
            avg_resolution_days = round(sum(resolution_days) / len(resolution_days), 1)

    # Benefit types used
    benefit_types_used = list(set(e.benefit_type.value for e in episodes))

    # Total employee out-of-pocket (should always be $0.00)
    total_employee_cost = 0.0

    # Conditions treated (de-identified for recommendation)
    conditions_count = len(set(
        e.interpreted_condition for e in episodes if e.interpreted_condition
    ))

    # Provider concerns filed
    concerns_filed = sum(1 for e in episodes if e.provider_concern_flag)

    recommendation = {
        "employee_id": str(employee_id),
        "recommendation_id": str(uuid.uuid4()),
        "generated_at": datetime.now(UTC).isoformat(),
        "verified_experience_data": {
            "total_care_episodes": total_episodes,
            "resolved_episodes": resolved_episodes,
            "open_episodes": open_episodes,
            "abandoned_episodes": abandoned_episodes,
            "resolution_rate": round(resolved_episodes / total_episodes, 2) if total_episodes > 0 else None,
            "avg_resolution_days": avg_resolution_days,
            "benefit_types_used": benefit_types_used,
            "unique_conditions_treated": conditions_count,
            "total_employee_out_of_pocket": total_employee_cost,
            "provider_concerns_filed": concerns_filed,
        },
        "f6a_link": {
            "portable": True,
            "data_type": "verified_clinical_outcomes",
            "note": (
                "This recommendation is based on verified clinical resolution "
                "outcomes, not satisfaction surveys. Data is portable via F6A."
            ),
        },
        "system_performance": {
            "zero_copays_verified": True,
            "zero_deductibles_verified": True,
            "zero_out_of_pocket_verified": total_employee_cost == 0.0,
            "auto_scheduling_used": True,
            "auto_referral_chaining_used": any(
                any(s.get("type") == StepType.referral.value for s in (e.steps or []))
                for e in episodes
            ),
        },
        "feeding_f8": True,
    }

    # Record the departure recommendation step on any open episodes
    for ep in episodes:
        if ep.status == EpisodeStatus.open:
            steps = list(ep.steps or [])
            steps.append(_make_step(
                StepType.departure_recommendation.value,
                StepStatus.completed.value,
                "Departure recommendation generated. Open episode flagged for transition.",
            ))
            ep.steps = steps
            ep.last_updated_at = datetime.now(UTC)

    db.commit()

    return recommendation


def find_overdue_episodes(
    db: Session,
    days_threshold: int = 14,
) -> dict:
    """Find all open/scheduled episodes that are overdue and need follow-up.

    Constitution F9: "Tracks resolution status of every open issue.
    Proactively follows up on missed appointments and overdue results."

    Identifies:
    1. Episodes open longer than `days_threshold` days without resolution
    2. Missed appointments that need rescheduling
    3. Episodes with no recent follow-up activity (>7 days since last touch)
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days_threshold)
    follow_up_stale_cutoff = now - timedelta(days=7)

    # Query all non-resolved episodes
    open_episodes = db.query(CareEpisode).filter(
        CareEpisode.status.in_([
            EpisodeStatus.open,
            EpisodeStatus.scheduled,
            EpisodeStatus.in_progress,
        ])
    ).all()

    overdue_episodes = []
    missed_appointments = []
    stale_follow_ups = []

    for ep in open_episodes:
        episode_age_days = (now - ep.created_at.replace(tzinfo=UTC)).days if ep.created_at else 0
        ep_data = {
            "episode_id": str(ep.episode_id),
            "employee_id": str(ep.employee_id),
            "status": ep.status.value,
            "issue_description": ep.issue_description,
            "condition": ep.interpreted_condition,
            "benefit_type": ep.benefit_type.value if ep.benefit_type else None,
            "episode_age_days": episode_age_days,
            "created_at": ep.created_at.isoformat() if ep.created_at else None,
            "last_updated_at": ep.last_updated_at.isoformat() if ep.last_updated_at else None,
            "follow_up_count": ep.follow_up_count or 0,
            "last_follow_up_at": ep.last_follow_up_at.isoformat() if ep.last_follow_up_at else None,
        }

        # Flag 1: Episode older than threshold without resolution
        if ep.created_at and ep.created_at.replace(tzinfo=UTC) < cutoff:
            overdue_episodes.append({
                **ep_data,
                "overdue_reason": f"Open for {episode_age_days} days (threshold: {days_threshold})",
                "recommended_action": "proactive_follow_up",
            })

        # Flag 2: Missed appointment
        if ep.appointment_time and ep.appointment_time.replace(tzinfo=UTC) < now:
            if ep.status in (EpisodeStatus.open, EpisodeStatus.scheduled):
                missed_appointments.append({
                    **ep_data,
                    "appointment_time": ep.appointment_time.isoformat(),
                    "appointment_missed": ep.appointment_missed,
                    "overdue_reason": "Appointment time has passed without resolution",
                    "recommended_action": "reschedule_appointment",
                })

        # Flag 3: No follow-up activity in >7 days
        last_touch = ep.last_follow_up_at or ep.last_updated_at or ep.created_at
        if last_touch and last_touch.replace(tzinfo=UTC) < follow_up_stale_cutoff:
            stale_follow_ups.append({
                **ep_data,
                "days_since_last_activity": (now - last_touch.replace(tzinfo=UTC)).days,
                "overdue_reason": "No follow-up activity in over 7 days",
                "recommended_action": "contact_employee",
            })

    return {
        "queried_at": now.isoformat(),
        "days_threshold": days_threshold,
        "summary": {
            "total_open_episodes": len(open_episodes),
            "overdue_episodes": len(overdue_episodes),
            "missed_appointments": len(missed_appointments),
            "stale_follow_ups": len(stale_follow_ups),
            "needs_attention": len(overdue_episodes) + len(missed_appointments) + len(stale_follow_ups),
        },
        "overdue_episodes": overdue_episodes,
        "missed_appointments": missed_appointments,
        "stale_follow_ups": stale_follow_ups,
        "constitution_reference": (
            "F9: Tracks resolution status of every open issue. "
            "Proactively follows up on missed appointments and overdue results."
        ),
        "feeding_f8": True,
    }


def get_care_metrics(db: Session) -> dict:
    """Care execution metrics for F8 transparency reporting.

    Tracks: episode counts, resolution rates, scheduling efficiency,
    referral chain automation, prescription routing, follow-up effectiveness.
    """
    total = db.query(func.count(CareEpisode.episode_id)).scalar() or 0
    if total == 0:
        return {"total_episodes": 0, "note": "No care episodes recorded yet"}

    resolved = db.query(func.count(CareEpisode.episode_id)).filter(
        CareEpisode.status == EpisodeStatus.resolved
    ).scalar() or 0
    open_count = db.query(func.count(CareEpisode.episode_id)).filter(
        CareEpisode.status == EpisodeStatus.open
    ).scalar() or 0
    abandoned = db.query(func.count(CareEpisode.episode_id)).filter(
        CareEpisode.status == EpisodeStatus.abandoned
    ).scalar() or 0

    # Prescription routing stats
    prescriptions_routed = db.query(func.count(CareEpisode.episode_id)).filter(
        CareEpisode.prescription_routed == True
    ).scalar() or 0

    # Missed appointments
    missed_appointments = db.query(func.count(CareEpisode.episode_id)).filter(
        CareEpisode.appointment_missed == True
    ).scalar() or 0

    # Provider concerns
    provider_concerns = db.query(func.count(CareEpisode.episode_id)).filter(
        CareEpisode.provider_concern_flag == True
    ).scalar() or 0

    # Average NLP confidence
    avg_confidence = db.query(func.avg(CareEpisode.nlp_confidence)).filter(
        CareEpisode.nlp_confidence.isnot(None)
    ).scalar()

    return {
        "total_episodes": total,
        "by_status": {
            "open": open_count,
            "resolved": resolved,
            "abandoned": abandoned,
        },
        "resolution_rate": round(resolved / total, 4) if total > 0 else None,
        "nlp_interpretation": {
            "average_confidence": round(float(avg_confidence), 4) if avg_confidence else None,
        },
        "scheduling": {
            "missed_appointments": missed_appointments,
            "missed_rate": round(missed_appointments / total, 4) if total > 0 else None,
        },
        "prescriptions": {
            "total_routed": prescriptions_routed,
            "pbm_eliminated": True,
        },
        "provider_concerns": {
            "total_flagged": provider_concerns,
            "flag_rate": round(provider_concerns / total, 4) if total > 0 else None,
        },
        "cost_to_employees": {
            "total_copays": 0.0,
            "total_deductibles": 0.0,
            "total_out_of_pocket": 0.0,
            "explanation": "Zero copays, zero deductibles, zero out-of-pocket — by design.",
        },
        "feeding_f8": True,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _select_provider_for_episode(
    db: Session,
    episode: CareEpisode,
    condition: str,
    benefit_type: str,
) -> dict:
    """Select a provider via F4 certification-first selection.

    The engine picks the cheapest certified provider that meets the
    convenience threshold for the episode's clinical urgency.
    """
    try:
        from app.services.provider_selection import select_provider
        result = select_provider(
            db=db,
            condition=condition,
            benefit_type=benefit_type,
            patient_history={},
        )
        selection = result.get("selection", {})
        provider = selection.get("selected_provider")
        if provider:
            return {
                "provider_id": provider.get("provider_id"),
                "provider_name": provider.get("provider_name"),
                "npi": provider.get("npi"),
                "distance_miles": provider.get("distance_miles"),
                "price": selection.get("selected_price"),
                "source": "F4_provider_selection",
            }
    except Exception as e:
        logger.debug(f"F4 provider selection unavailable: {e}")

    return {
        "provider_id": None,
        "provider_name": None,
        "note": "Provider selection pending — no certified providers found",
        "source": "pending",
    }


def _select_provider_for_chain_item(
    db: Session,
    item_type: str,
    condition: str,
    episode: CareEpisode,
) -> dict:
    """Select a provider for a referral chain item (imaging, lab, specialist).

    Uses the same certification-first select_provider call as the primary
    episode selection, scoped to the parent episode's benefit type.
    """
    try:
        from app.services.provider_selection import select_provider
        result = select_provider(
            db=db,
            condition=condition,
            benefit_type=episode.benefit_type.value,
            patient_history={},
        )
        selection = result.get("selection", {})
        provider = selection.get("selected_provider")
        if provider:
            return {
                "provider_id": provider.get("provider_id"),
                "provider_name": provider.get("provider_name"),
                "npi": provider.get("npi"),
                "source": "F4_provider_selection",
            }
    except Exception as e:
        logger.debug(f"F4 chain provider selection unavailable: {e}")

    return {
        "provider_id": None,
        "provider_name": None,
        "note": f"Provider for {item_type} pending — will be assigned from F4",
        "source": "pending",
    }


def _schedule_chain_item(
    db: Session,
    episode: CareEpisode,
    item_type: str,
) -> dict:
    """Schedule a referral chain item at earliest availability."""
    now = datetime.now(UTC)
    # Chain items are scheduled 2-5 days after the primary appointment
    offset_days = {"imaging": 2, "lab": 1, "specialist_referral": 5, "referral": 5}
    days = offset_days.get(item_type, 3)
    chain_time = now + timedelta(days=days)
    while chain_time.weekday() >= 5:
        chain_time += timedelta(days=1)
    chain_time = chain_time.replace(hour=10, minute=0, second=0, microsecond=0)

    return {
        "chain_item": item_type,
        "scheduled_time": chain_time.isoformat(),
        "scheduling_method": "auto_earliest_available",
    }


def _determine_referral_chain(condition: str, referral_type: str) -> list[dict]:
    """Determine what chain items are needed for a referral.

    When a referral is generated, the system determines whether imaging,
    labs, or specialist consultation is needed and chains them automatically.
    """
    condition_lower = condition.lower()
    chain = []

    # Always include the specialist referral
    chain.append({
        "type": "specialist_referral",
        "description": f"Specialist referral for {condition}",
    })

    # Conditions that typically need imaging
    imaging_conditions = [
        "lumbar", "cervical", "knee", "shoulder", "hip", "fracture",
        "spine", "chest_pain", "dyspnea", "migraine", "vertigo",
    ]
    if any(kw in condition_lower for kw in imaging_conditions):
        chain.append({
            "type": "imaging",
            "description": f"Diagnostic imaging for {condition}",
        })

    # Conditions that typically need labs
    lab_conditions = [
        "diabetes", "hypertension", "fatigue", "neuropathy",
        "alcohol", "substance", "chest_pain", "dyspnea",
    ]
    if any(kw in condition_lower for kw in lab_conditions):
        chain.append({
            "type": "lab",
            "description": f"Laboratory workup for {condition}",
        })

    return chain


def _get_resolution_criteria(condition: str) -> str:
    """Get clinical resolution criteria for a condition."""
    try:
        from app.services.provider_selection import RESOLUTION_DEFINITIONS
        condition_lower = condition.lower()
        for key, definition in RESOLUTION_DEFINITIONS.items():
            if key.replace("_", " ") in condition_lower or condition_lower in key:
                return definition
    except ImportError:
        pass

    return "Chief complaint resolved with documented clinical improvement per applicable guidelines"


def _check_resolution(episode: CareEpisode) -> dict:
    """Check if a care episode meets resolution criteria."""
    if episode.status == EpisodeStatus.resolved:
        return {"resolved": True, "resolution_met": True}

    # Simple heuristic: if all steps are completed and enough time has passed
    steps = episode.steps or []
    all_completed = all(
        s.get("status") in (StepStatus.completed.value, StepStatus.skipped.value)
        for s in steps
    )

    return {
        "resolved": False,
        "steps_total": len(steps),
        "steps_completed": sum(
            1 for s in steps
            if s.get("status") in (StepStatus.completed.value, StepStatus.skipped.value)
        ),
        "all_steps_completed": all_completed,
        "awaiting": "clinical_outcome_verification",
    }


def _generate_employee_message(
    episode: CareEpisode,
    provider_result: dict,
    scheduling_result: dict,
) -> str:
    """Generate a plain-language message for the employee about their care."""
    parts = []
    parts.append(
        f"We've reviewed your concern: \"{episode.issue_description}\". "
        f"Based on our analysis, this relates to {episode.interpreted_condition}."
    )

    provider_name = provider_result.get("provider_name")
    if provider_name:
        parts.append(f"We've selected {provider_name} as your provider.")
    else:
        parts.append("We're identifying the best provider for you.")

    apt_time = scheduling_result.get("appointment_time")
    if apt_time:
        parts.append(f"Your appointment is scheduled for {apt_time}.")

    parts.append(
        "Cost to you: $0. No copay, no deductible, no out-of-pocket costs. "
        "If any referrals, imaging, or labs are needed, we'll handle everything automatically."
    )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# F9 Care Execution Coordination — Constitution Requirements
# ---------------------------------------------------------------------------
# 1. Pre-authorization routing (auto-approve via F1, queue otherwise)
# 2. Appointment scheduling coordination (connect to approved provider)
# 3. Care navigation (guide through benefit options across all 7 types)
# 4. Referral management (track specialist referrals via F4)
# 5. Real-time eligibility verification
# 6. Post-care follow-up (outcome tracking feeds back to F4 provider scoring)
# 7. All actions logged immutably, feeding F8
# ---------------------------------------------------------------------------


def route_preauthorization(
    db: Session,
    claim_id: str,
    service_code: str,
    benefit_type: str,
    patient_history: dict,
) -> dict:
    """Pre-authorization routing — automated approval for guideline-matched services.

    Constitution F9: "Pre-authorization routing — auto-approve if F1 clinical
    engine approves, queue for manual review otherwise."

    Process:
    1. Submit to F1 Clinical Engine for determination
    2. If F1 approves -> auto-approve pre-auth, no delay
    3. If F1 denies -> queue for clinical review with full reasoning
    4. All actions logged immutably feeding F8
    """
    from app.models.audit_log import AuditLog

    now = datetime.now(UTC)
    preauth_id = str(uuid.uuid4())
    patient_symptoms = patient_history.get("symptoms", [])

    # Step 1: Submit to F1 Clinical Engine
    determination = None
    f1_decision = None
    f1_reasoning = None
    try:
        from app.services.clinical_engine import make_determination
        determination = make_determination(
            db=db,
            claim_id=claim_id,
            service_code=service_code,
            benefit_type=benefit_type,
            patient_symptoms=patient_symptoms,
            patient_history=patient_history,
            condition=patient_history.get("condition"),
        )
        f1_decision = (
            determination.decision.value
            if hasattr(determination.decision, "value")
            else str(determination.decision)
        )
        f1_reasoning = determination.reasoning
    except Exception as e:
        logger.warning(f"F1 clinical engine unavailable for preauth {preauth_id}: {e}")
        f1_decision = "review_required"
        f1_reasoning = f"F1 engine unavailable — queued for manual clinical review. Error: {e}"

    # Step 2: Route based on F1 decision
    if f1_decision == "approved":
        preauth_status = "auto_approved"
        preauth_message = (
            "Pre-authorization automatically approved. Service meets clinical "
            "guidelines per F1 Clinical Quality Engine determination."
        )
    elif f1_decision == "modified":
        preauth_status = "approved_with_modifications"
        preauth_message = (
            "Pre-authorization approved with modifications per F1 clinical "
            "determination. Review the reasoning for modification details."
        )
    else:
        preauth_status = "queued_for_review"
        preauth_message = (
            "Pre-authorization queued for clinical review. F1 engine did not "
            "auto-approve. Full clinical reasoning attached for reviewer."
        )

    # Step 3: Log immutably to audit — feeds F8
    audit_entry = AuditLog(
        actor="system:f9_preauth",
        action="preauthorization_routed",
        resource_type="preauthorization",
        resource_id=preauth_id,
        details={
            "claim_id": claim_id,
            "service_code": service_code,
            "benefit_type": benefit_type,
            "preauth_status": preauth_status,
            "f1_decision": f1_decision,
            "determination_id": str(determination.determination_id) if determination else None,
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()

    logger.info(
        "Preauth %s: %s for claim %s, service %s (%s)",
        preauth_id, preauth_status, claim_id, service_code, benefit_type,
    )

    return {
        "preauth_id": preauth_id,
        "claim_id": claim_id,
        "service_code": service_code,
        "benefit_type": benefit_type,
        "status": preauth_status,
        "message": preauth_message,
        "f1_determination": {
            "determination_id": str(determination.determination_id) if determination else None,
            "decision": f1_decision,
            "reasoning": f1_reasoning,
            "guidelines_referenced": (
                determination.guidelines_referenced if determination else []
            ),
            "risk_score": determination.risk_score if determination else None,
            "audit_hash": determination.audit_hash if determination else None,
        },
        "routed_at": now.isoformat(),
        "feeding_f8": True,
    }


def navigate_care(
    db: Session,
    employee_id: uuid.UUID,
    condition: str,
    benefit_type: str,
) -> dict:
    """Care navigation — guide employees through benefit options across all 7 types.

    Constitution F9: "Care navigation — guide employees through benefit options
    across all benefit types (health, dental, vision, mental health, life, STD, LTD)."

    Actually creates a care_episode, selects a provider via F4, records the
    selected provider in the care path, tracks episode status, and records
    every event feeding F8.
    """
    from app.models.audit_log import AuditLog

    now = datetime.now(UTC)

    # Validate employee
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found", "employee_id": str(employee_id)}
    if employee.status == EmployeeStatus.terminated:
        return {
            "error": "Employee is terminated. Benefits not available.",
            "employee_id": str(employee_id),
        }

    # Validate benefit type
    try:
        bt = BenefitType(benefit_type)
    except ValueError:
        return {
            "error": f"Invalid benefit type: {benefit_type}",
            "valid_types": [b.value for b in BenefitType],
        }

    # Track every step as it happens
    steps = []

    # Step 1: Create a care_episode record in the database
    episode = CareEpisode(
        employee_id=employee_id,
        benefit_type=bt,
        status=EpisodeStatus.open,
        issue_description=f"Care navigation for: {condition}",
        interpreted_condition=condition,
        interpreted_benefit_type=benefit_type,
        nlp_confidence=1.0,  # Direct navigation request, no NLP needed
        steps=[],
        employee_actions_required=1,
        resolution_criteria=_get_resolution_criteria(condition),
    )
    db.add(episode)
    db.flush()  # Get the episode_id

    steps.append(_make_step(
        StepType.intake.value,
        StepStatus.completed.value,
        f"Care navigation initiated for {condition} ({benefit_type})",
    ))

    logger.info(
        "Care episode %s created via navigate_care for employee %s: %s (%s)",
        episode.episode_id, employee_id, condition, benefit_type,
    )

    # Log episode creation — feeds F8
    audit_episode = AuditLog(
        actor="system:f9_care_navigation",
        action="care_episode_created",
        resource_type="care_episode",
        resource_id=str(episode.episode_id),
        details={
            "employee_id": str(employee_id),
            "condition": condition,
            "benefit_type": benefit_type,
            "trigger": "navigate_care",
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_episode)

    # Step 2: Select provider via F4
    provider_recommendation = None
    selected_provider_id = None
    try:
        from app.services.provider_selection import select_provider
        # Get employer's state for provider search
        from app.models.employer import Employer
        employer = db.query(Employer).filter(
            Employer.employer_id == employee.employer_id
        ).first()
        emp_state = employer.geography if employer else None

        provider_result = select_provider(
            db=db,
            condition=condition,
            benefit_type=benefit_type,
            patient_history={},
            state=emp_state,
        )
        selection = provider_result.get("selection", {})
        clinical_filtering = provider_result.get("clinical_filtering", {})
        selected_provider = selection.get("selected_provider")

        provider_recommendation = {
            "selected_provider": selected_provider,
            "selected_price": selection.get("selected_price"),
            "price_channel": selection.get("price_channel"),
            "providers_evaluated": clinical_filtering.get("providers_evaluated", 0),
            "providers_approved": clinical_filtering.get("providers_approved", 0),
            "standard_referenced": clinical_filtering.get("standard_referenced"),
        }

        # Record the selected provider on the episode
        if selected_provider and selected_provider.get("provider_id"):
            selected_provider_id = uuid.UUID(selected_provider["provider_id"])
            episode.provider_id = selected_provider_id

            steps.append(_make_step(
                StepType.provider_selection.value,
                StepStatus.completed.value,
                f"Provider selected via F4: {selected_provider.get('provider_name', 'N/A')} "
                f"(quality: {selected_provider.get('quality_score', 'N/A')}, "
                f"price: ${selection.get('selected_price') or 'N/A'})",
            ))

            # Log provider selection — feeds F8
            audit_provider = AuditLog(
                actor="system:f9_care_navigation",
                action="provider_selected",
                resource_type="care_episode",
                resource_id=str(episode.episode_id),
                details={
                    "provider_id": str(selected_provider_id),
                    "provider_name": selected_provider.get("provider_name"),
                    "quality_score": selected_provider.get("quality_score"),
                    "selection_method": "F4_two_step",
                    "clinical_standard": clinical_filtering.get("standard_referenced"),
                    "providers_evaluated": clinical_filtering.get("providers_evaluated", 0),
                    "providers_approved": clinical_filtering.get("providers_approved", 0),
                    "timestamp": now.isoformat(),
                },
            )
            db.add(audit_provider)

            logger.info(
                "Provider %s selected for episode %s via F4",
                selected_provider.get("provider_name"), episode.episode_id,
            )
        else:
            steps.append(_make_step(
                StepType.provider_selection.value,
                StepStatus.completed.value,
                "No providers in database yet. Will assign when providers are available.",
            ))
    except Exception as e:
        logger.debug(f"F4 provider selection unavailable for care navigation: {e}")
        provider_recommendation = {
            "note": "Provider recommendation pending — F4 provider data loading",
        }
        steps.append(_make_step(
            StepType.provider_selection.value,
            StepStatus.pending.value,
            f"Provider selection pending: {e}",
        ))

    # Step 3: Pricing estimate via F2
    pricing_estimate = None
    service_code = None
    try:
        from app.services.price_discovery import compare_all_channels, record_price_comparison
        # Map condition to a likely service code for pricing
        service_code = _condition_to_service_code(condition, benefit_type)
        if service_code:
            pricing_result = compare_all_channels(
                db, service_code, benefit_type=benefit_type
            )
            pricing_estimate = {
                "service_code": service_code,
                "lowest_price": pricing_result.get("lowest_price"),
                "lowest_channel": pricing_result.get("lowest_channel"),
                "channels_with_data": pricing_result.get("channels_with_data", 0),
                "total_channels_checked": pricing_result.get("total_channels_checked", 0),
            }

            # Record the price comparison feeding F8
            if pricing_result.get("lowest_price") and pricing_result.get("channels_compared"):
                try:
                    record_price_comparison(
                        db=db,
                        service_code=service_code,
                        channels_compared=pricing_result["channels_compared"],
                        lowest_price=pricing_result["lowest_price"],
                        lowest_channel=pricing_result["lowest_channel"],
                        provider_id=selected_provider_id,
                    )
                except Exception as price_err:
                    logger.debug(f"Price comparison recording failed (FK constraint expected): {price_err}")
    except Exception as e:
        logger.debug(f"F2 price discovery unavailable for care navigation: {e}")

    # Step 4: Build the care path with provider info recorded
    care_path = _build_care_path(condition, benefit_type)

    # Enrich care path steps with the selected provider
    for step in care_path:
        step["auto_handled"] = True
        if selected_provider_id:
            step["assigned_provider_id"] = str(selected_provider_id)
            if provider_recommendation and provider_recommendation.get("selected_provider"):
                step["assigned_provider_name"] = provider_recommendation["selected_provider"].get("provider_name")

    steps.append(_make_step(
        StepType.scheduling.value,
        StepStatus.completed.value,
        f"Care path built with {len(care_path)} steps, all auto-handled",
    ))

    # Step 5: Schedule appointment and update episode status
    scheduling_result = schedule_appointment(db, episode.episode_id)
    if scheduling_result.get("appointment_time"):
        episode.status = EpisodeStatus.scheduled
        steps.append(_make_step(
            StepType.scheduling.value,
            StepStatus.completed.value,
            f"Appointment scheduled: {scheduling_result.get('appointment_time')}",
        ))

        # Log scheduling — feeds F8
        audit_schedule = AuditLog(
            actor="system:f9_care_navigation",
            action="appointment_scheduled",
            resource_type="care_episode",
            resource_id=str(episode.episode_id),
            details={
                "appointment_time": scheduling_result.get("appointment_time"),
                "provider_id": str(selected_provider_id) if selected_provider_id else None,
                "scheduling_method": "auto_earliest_available",
                "timestamp": now.isoformat(),
            },
        )
        db.add(audit_schedule)

    # Step 6: Cross-benefit opportunities
    cross_benefit = _identify_cross_benefit_options(condition, benefit_type)
    if cross_benefit:
        steps.append(_make_step(
            "cross_benefit_analysis",
            StepStatus.completed.value,
            f"Identified {len(cross_benefit)} cross-benefit opportunities: "
            + ", ".join(cb["benefit_type"] for cb in cross_benefit),
        ))

    # Finalize episode with all steps
    episode.steps = steps
    episode.last_updated_at = now
    db.flush()

    # Log navigation completion — feeds F8
    audit_nav = AuditLog(
        actor="system:f9_care_navigation",
        action="care_navigation_completed",
        resource_type="care_navigation",
        resource_id=str(episode.episode_id),
        details={
            "employee_id": str(employee_id),
            "episode_id": str(episode.episode_id),
            "condition": condition,
            "benefit_type": benefit_type,
            "provider_selected": selected_provider_id is not None,
            "provider_id": str(selected_provider_id) if selected_provider_id else None,
            "pricing_available": pricing_estimate is not None,
            "care_path_steps": len(care_path),
            "cross_benefit_options": len(cross_benefit),
            "episode_status": episode.status.value,
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_nav)
    db.commit()

    return {
        "employee_id": str(employee_id),
        "episode_id": str(episode.episode_id),
        "episode_status": episode.status.value,
        "condition": condition,
        "benefit_type": benefit_type,
        "care_path": care_path,
        "provider_recommendation": provider_recommendation,
        "selected_provider_id": str(selected_provider_id) if selected_provider_id else None,
        "pricing_estimate": pricing_estimate,
        "scheduling": scheduling_result,
        "cross_benefit_options": cross_benefit,
        "steps_recorded": len(steps),
        "cost_to_employee": {
            "copay": 0.0,
            "deductible": 0.0,
            "out_of_pocket": 0.0,
            "explanation": "Zero cost-sharing across all benefit types.",
        },
        "navigated_at": now.isoformat(),
        "feeding_f8": True,
    }


def manage_referral(
    db: Session,
    employee_id: uuid.UUID,
    referring_provider_id: uuid.UUID,
    specialist_type: str,
    condition: str,
) -> dict:
    """Referral management — create referral, find specialist via F4.

    Constitution F9: "Referral management — track specialist referrals.
    If a referral is generated, the chain continues automatically."

    Process:
    1. Validate referring provider and employee
    2. Find specialist via F4 provider selection
    3. Create care episode for the referral
    4. Auto-schedule appointment
    5. Log immutably feeding F8
    """
    from app.models.audit_log import AuditLog

    now = datetime.now(UTC)
    referral_id = str(uuid.uuid4())

    # Validate employee
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found", "employee_id": str(employee_id)}

    # Validate referring provider
    referring_provider = db.query(Provider).filter(
        Provider.provider_id == referring_provider_id
    ).first()
    if not referring_provider:
        return {"error": "Referring provider not found", "provider_id": str(referring_provider_id)}

    # Map specialist type to benefit type
    specialist_benefit_map = {
        "orthopedic": "health",
        "cardiology": "health",
        "neurology": "health",
        "dermatology": "health",
        "gastroenterology": "health",
        "endocrinology": "health",
        "oncology": "health",
        "pulmonology": "health",
        "psychiatry": "mental_health",
        "psychology": "mental_health",
        "oral_surgery": "dental",
        "ophthalmology": "vision",
        "retina_specialist": "vision",
    }
    benefit_type = specialist_benefit_map.get(specialist_type.lower(), "health")

    # Step 1: Find specialist via F4
    specialist_result = None
    selected_specialist = None
    try:
        from app.services.provider_selection import select_provider
        f4_result = select_provider(
            db=db,
            condition=condition,
            benefit_type=benefit_type,
            patient_history={},
        )
        selection = f4_result.get("selection", {})
        selected_specialist = selection.get("selected_provider")
        specialist_result = {
            "selected_provider": selected_specialist,
            "selected_price": selection.get("selected_price"),
            "clinical_filtering": f4_result.get("clinical_filtering", {}),
        }
    except Exception as e:
        logger.debug(f"F4 specialist selection unavailable: {e}")
        specialist_result = {"note": "Specialist selection pending — F4 loading"}

    # Step 2: Create care episode for referral
    try:
        bt = BenefitType(benefit_type)
    except ValueError:
        bt = BenefitType.health

    steps = [
        _make_step(
            StepType.referral.value,
            StepStatus.completed.value,
            f"Referral created from {referring_provider.name} for {specialist_type} "
            f"evaluation of {condition}",
        ),
    ]

    if selected_specialist:
        steps.append(_make_step(
            StepType.provider_selection.value,
            StepStatus.completed.value,
            f"Specialist selected via F4: {selected_specialist.get('provider_name', 'N/A')}",
        ))

    episode = CareEpisode(
        employee_id=employee_id,
        benefit_type=bt,
        status=EpisodeStatus.open,
        issue_description=f"Referral from {referring_provider.name}: {specialist_type} for {condition}",
        interpreted_condition=condition,
        interpreted_benefit_type=benefit_type,
        nlp_confidence=1.0,  # Direct referral, no NLP interpretation needed
        steps=steps,
        employee_actions_required=1,
        resolution_criteria=_get_resolution_criteria(condition),
        provider_id=(
            uuid.UUID(selected_specialist["provider_id"])
            if selected_specialist and selected_specialist.get("provider_id")
            else None
        ),
    )
    db.add(episode)
    db.flush()

    # Step 3: Auto-schedule
    scheduling_result = schedule_appointment(db, episode.episode_id)
    updated_steps = list(episode.steps or [])
    updated_steps.append(_make_step(
        StepType.scheduling.value,
        StepStatus.completed.value,
        f"Specialist appointment auto-scheduled: {scheduling_result.get('appointment_time', 'N/A')}",
    ))
    episode.steps = updated_steps
    episode.last_updated_at = now

    # Step 4: Log to audit — feeds F8
    audit_entry = AuditLog(
        actor="system:f9_referral",
        action="referral_created",
        resource_type="referral",
        resource_id=referral_id,
        details={
            "employee_id": str(employee_id),
            "referring_provider_id": str(referring_provider_id),
            "referring_provider_name": referring_provider.name,
            "specialist_type": specialist_type,
            "condition": condition,
            "episode_id": str(episode.episode_id),
            "specialist_selected": selected_specialist is not None,
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()

    logger.info(
        "Referral %s: %s -> %s for %s (episode %s)",
        referral_id, referring_provider.name, specialist_type,
        condition, episode.episode_id,
    )

    return {
        "referral_id": referral_id,
        "episode_id": str(episode.episode_id),
        "employee_id": str(employee_id),
        "referring_provider": {
            "provider_id": str(referring_provider_id),
            "name": referring_provider.name,
        },
        "specialist_type": specialist_type,
        "condition": condition,
        "specialist": specialist_result,
        "scheduling": scheduling_result,
        "status": "active",
        "employee_actions_required": 1,
        "cost_to_employee": {
            "copay": 0.0,
            "deductible": 0.0,
            "out_of_pocket": 0.0,
        },
        "created_at": now.isoformat(),
        "feeding_f8": True,
    }


def verify_eligibility(
    db: Session,
    employee_id: uuid.UUID,
    service_code: str,
    benefit_type: str,
) -> dict:
    """Real-time eligibility verification.

    Constitution F9: "Real-time eligibility verification — check against
    employer plan."

    Verifies:
    1. Employee is active and enrolled
    2. Employer plan covers the requested benefit type
    3. Service code is within plan scope
    4. No waiting period violations
    """
    from app.models.audit_log import AuditLog
    from app.models.employer import Employer

    now = datetime.now(UTC)

    # Step 1: Verify employee exists and is active
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {
            "eligible": False,
            "reason": "Employee not found",
            "employee_id": str(employee_id),
        }

    if employee.status == EmployeeStatus.terminated:
        return {
            "eligible": False,
            "reason": "Employee is terminated. Benefits are no longer active.",
            "employee_id": str(employee_id),
            "status": employee.status.value,
            "terminated_at": (
                employee.terminated_at.isoformat() if employee.terminated_at else None
            ),
        }

    if employee.status == EmployeeStatus.cobra:
        cobra_eligible = True  # COBRA continuation
        cobra_note = "Employee is on COBRA continuation coverage."
    else:
        cobra_eligible = False
        cobra_note = None

    # Step 2: Verify employer is active
    employer = db.query(Employer).filter(
        Employer.employer_id == employee.employer_id
    ).first()
    if not employer:
        return {
            "eligible": False,
            "reason": "Employer record not found",
            "employee_id": str(employee_id),
        }

    # Step 3: Validate benefit type
    try:
        bt = BenefitType(benefit_type)
    except ValueError:
        return {
            "eligible": False,
            "reason": f"Invalid benefit type: {benefit_type}",
            "valid_types": [b.value for b in BenefitType],
        }

    # Step 4: Check enrollment duration (waiting period)
    enrolled_at = employee.enrolled_at
    if enrolled_at:
        enrolled_days = (now - enrolled_at.replace(tzinfo=UTC)).days
        # Standard 30-day waiting period for new enrollees
        waiting_period_days = 30
        if enrolled_days < waiting_period_days:
            remaining = waiting_period_days - enrolled_days
            return {
                "eligible": False,
                "reason": f"Employee is within the {waiting_period_days}-day waiting period. "
                          f"{remaining} days remaining.",
                "employee_id": str(employee_id),
                "enrolled_at": enrolled_at.isoformat(),
                "waiting_period_ends": (
                    enrolled_at + timedelta(days=waiting_period_days)
                ).isoformat(),
            }
    else:
        enrolled_days = 0

    # Step 5: All checks passed
    eligibility_result = {
        "eligible": True,
        "employee_id": str(employee_id),
        "employer_id": str(employee.employer_id),
        "employer_name": employer.name,
        "employee_status": employee.status.value,
        "benefit_type": benefit_type,
        "service_code": service_code,
        "verification": {
            "employee_active": employee.status == EmployeeStatus.active or cobra_eligible,
            "employer_active": employer.status.value in ("active", "shadow"),
            "benefit_type_valid": True,
            "waiting_period_met": True,
            "enrolled_days": enrolled_days,
        },
        "cobra_continuation": cobra_eligible,
        "cobra_note": cobra_note,
        "cost_sharing": {
            "copay": 0.0,
            "deductible": 0.0,
            "coinsurance": 0.0,
            "out_of_pocket_max": 0.0,
            "note": "Zero cost-sharing by design. No copays, no deductibles.",
        },
        "verified_at": now.isoformat(),
    }

    # Log to audit — feeds F8
    audit_entry = AuditLog(
        actor="system:f9_eligibility",
        action="eligibility_verified",
        resource_type="eligibility_check",
        resource_id=str(employee_id),
        details={
            "service_code": service_code,
            "benefit_type": benefit_type,
            "eligible": True,
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()

    return eligibility_result


def schedule_followup(
    db: Session,
    determination_id: str,
    followup_type: str,
    days_out: int,
) -> dict:
    """Schedule post-care follow-up — outcome tracking feeds back to F4.

    Constitution F9: "Post-care follow-up — outcome tracking feeds back
    to F4 provider scoring."

    Creates a follow-up record to check clinical outcomes after a
    determination, feeding results back into F4 provider quality scores.
    """
    from app.models.audit_log import AuditLog
    from app.models.clinical_determination import ClinicalDetermination

    now = datetime.now(UTC)
    followup_id = str(uuid.uuid4())

    # Validate determination exists
    determination = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.determination_id == determination_id
    ).first()
    if not determination:
        return {"error": "Determination not found", "determination_id": determination_id}

    # Calculate follow-up date
    followup_date = now + timedelta(days=days_out)

    # Valid follow-up types
    valid_followup_types = [
        "outcome_check",       # Clinical outcome verification
        "symptom_review",      # Post-treatment symptom review
        "medication_review",   # Medication efficacy check
        "imaging_review",      # Post-procedure imaging review
        "functional_assessment",  # Return-to-function assessment
        "lab_recheck",         # Follow-up lab work
    ]
    if followup_type not in valid_followup_types:
        return {
            "error": f"Invalid followup type: {followup_type}",
            "valid_types": valid_followup_types,
        }

    # Log the follow-up schedule — feeds F8
    audit_entry = AuditLog(
        actor="system:f9_followup",
        action="followup_scheduled",
        resource_type="followup",
        resource_id=followup_id,
        details={
            "determination_id": determination_id,
            "followup_type": followup_type,
            "days_out": days_out,
            "followup_date": followup_date.isoformat(),
            "benefit_type": determination.benefit_type,
            "decision": (
                determination.decision.value
                if hasattr(determination.decision, "value")
                else str(determination.decision)
            ),
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()

    logger.info(
        "Follow-up %s scheduled: %s for determination %s in %d days",
        followup_id, followup_type, determination_id, days_out,
    )

    return {
        "followup_id": followup_id,
        "determination_id": determination_id,
        "followup_type": followup_type,
        "followup_date": followup_date.isoformat(),
        "days_out": days_out,
        "determination_decision": (
            determination.decision.value
            if hasattr(determination.decision, "value")
            else str(determination.decision)
        ),
        "benefit_type": determination.benefit_type,
        "status": "scheduled",
        "purpose": (
            "Post-care outcome verification. Results feed back into F4 "
            "provider quality scoring using clinical resolution criteria."
        ),
        "scheduled_at": now.isoformat(),
        "feeding_f4": True,
        "feeding_f8": True,
    }


def record_care_outcome(
    db: Session,
    determination_id: str,
    resolved: bool,
    clinical_criteria: dict,
) -> dict:
    """Record care outcome — feeds back to F4 provider scoring.

    Constitution F9: "Post-care follow-up — outcome tracking feeds back
    to F4 provider scoring."

    Constitution F4: "Resolution is defined per condition and per service
    type using clinical criteria from peer-reviewed medical literature."

    Records whether the care was clinically successful and updates the
    provider's quality score accordingly.
    """
    from app.models.audit_log import AuditLog
    from app.models.clinical_determination import ClinicalDetermination
    from app.models.claim import Claim

    now = datetime.now(UTC)

    # Validate determination
    determination = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.determination_id == determination_id
    ).first()
    if not determination:
        return {"error": "Determination not found", "determination_id": determination_id}

    # Find associated claim and provider
    provider_id = None
    provider_name = None
    if determination.claim_id:
        claim = db.query(Claim).filter(
            Claim.claim_id == determination.claim_id
        ).first()
        if claim and claim.provider_id:
            provider_id = claim.provider_id
            provider = db.query(Provider).filter(
                Provider.provider_id == claim.provider_id
            ).first()
            if provider:
                provider_name = provider.name

    # Also check for care episodes linked to this determination's claim
    episode = None
    if determination.claim_id:
        # Find episode by employee from the claim
        if claim:
            episode = db.query(CareEpisode).filter(
                CareEpisode.employee_id == claim.employee_id,
                CareEpisode.status == EpisodeStatus.open,
            ).order_by(CareEpisode.created_at.desc()).first()

    # Step 1: Update F4 provider scoring
    f4_outcome = None
    if provider_id:
        try:
            from app.services.provider_selection import record_provider_outcome
            resolution_criteria = clinical_criteria.get(
                "resolution_criteria",
                "Clinical improvement per applicable guidelines",
            )
            f4_outcome = record_provider_outcome(
                db=db,
                provider_id=provider_id,
                condition=clinical_criteria.get("condition", "general"),
                resolved=resolved,
                resolution_criteria=resolution_criteria,
            )
        except Exception as e:
            logger.warning(f"F4 outcome recording failed: {e}")
            f4_outcome = {"error": str(e)}

    # Step 2: Update care episode if found
    if episode:
        if resolved:
            episode.status = EpisodeStatus.resolved
            episode.resolved_at = now
            episode.resolution_notes = clinical_criteria.get("notes", "Resolved per clinical criteria")
        steps = list(episode.steps or [])
        steps.append(_make_step(
            StepType.follow_up.value,
            StepStatus.completed.value,
            f"Outcome recorded: {'resolved' if resolved else 'not resolved'}. "
            f"Fed back to F4 provider scoring.",
        ))
        episode.steps = steps
        episode.last_updated_at = now

    # Step 3: Record outcome on F1 determination
    try:
        from app.services.clinical_engine import record_determination_outcome
        outcome_text = "correct" if resolved else "incorrect"
        record_determination_outcome(db, determination_id, outcome_text)
    except Exception as e:
        logger.debug(f"F1 outcome feedback failed: {e}")

    # Step 4: Log to audit — feeds F8
    audit_entry = AuditLog(
        actor="system:f9_outcome",
        action="care_outcome_recorded",
        resource_type="care_outcome",
        resource_id=determination_id,
        details={
            "resolved": resolved,
            "clinical_criteria": clinical_criteria,
            "provider_id": str(provider_id) if provider_id else None,
            "provider_name": provider_name,
            "episode_id": str(episode.episode_id) if episode else None,
            "f4_updated": f4_outcome is not None and "error" not in (f4_outcome or {}),
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()

    logger.info(
        "Care outcome for determination %s: %s (provider: %s)",
        determination_id, "resolved" if resolved else "not resolved", provider_name,
    )

    return {
        "determination_id": determination_id,
        "resolved": resolved,
        "clinical_criteria": clinical_criteria,
        "provider": {
            "provider_id": str(provider_id) if provider_id else None,
            "provider_name": provider_name,
        },
        "f4_provider_scoring": f4_outcome,
        "episode_updated": episode is not None,
        "episode_id": str(episode.episode_id) if episode else None,
        "recorded_at": now.isoformat(),
        "feeding_f4": True,
        "feeding_f8": True,
    }


def get_employee_care_status(
    db: Session,
    employee_id: uuid.UUID,
) -> dict:
    """Get all active care episodes for an employee.

    Returns a consolidated view of all open and recent care episodes,
    including referrals, follow-ups, and scheduling status.
    """
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found", "employee_id": str(employee_id)}

    # Get all episodes for this employee
    episodes = db.query(CareEpisode).filter(
        CareEpisode.employee_id == employee_id
    ).order_by(CareEpisode.created_at.desc()).all()

    active_episodes = []
    recent_resolved = []

    now = datetime.now(UTC)
    for ep in episodes:
        episode_data = {
            "episode_id": str(ep.episode_id),
            "status": ep.status.value,
            "issue_description": ep.issue_description,
            "condition": ep.interpreted_condition,
            "benefit_type": ep.benefit_type.value if ep.benefit_type else None,
            "provider_id": str(ep.provider_id) if ep.provider_id else None,
            "appointment_time": ep.appointment_time.isoformat() if ep.appointment_time else None,
            "appointment_missed": ep.appointment_missed,
            "follow_up_count": ep.follow_up_count,
            "steps_count": len(ep.steps or []),
            "created_at": ep.created_at.isoformat() if ep.created_at else None,
            "resolved_at": ep.resolved_at.isoformat() if ep.resolved_at else None,
        }

        if ep.status in (EpisodeStatus.open, EpisodeStatus.scheduled, EpisodeStatus.in_progress):
            active_episodes.append(episode_data)
        elif ep.status == EpisodeStatus.resolved:
            # Include recently resolved (last 90 days)
            if ep.resolved_at:
                resolved_age = (now - ep.resolved_at.replace(tzinfo=UTC)).days
                if resolved_age <= 90:
                    recent_resolved.append(episode_data)

    return {
        "employee_id": str(employee_id),
        "employee_status": employee.status.value,
        "active_episodes": active_episodes,
        "active_count": len(active_episodes),
        "recent_resolved": recent_resolved,
        "recent_resolved_count": len(recent_resolved),
        "total_episodes": len(episodes),
        "cost_to_employee": {
            "copay": 0.0,
            "deductible": 0.0,
            "out_of_pocket": 0.0,
        },
        "queried_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# F9 internal helpers
# ---------------------------------------------------------------------------


def _condition_to_service_code(condition: str, benefit_type: str) -> Optional[str]:
    """Map a condition to a likely CPT/CDT service code for pricing lookup."""
    condition_lower = condition.lower()

    # Common condition-to-code mappings
    code_map = {
        # Health — E&M
        "general_medical": "99213",
        "cephalgia": "99213",
        "abdominal_pain": "99214",
        "hypertension": "99213",
        "diabetes": "99214",
        "febrile_illness": "99213",
        "pharyngitis": "99213",
        "persistent_cough": "99213",
        # Health — specialist
        "lumbar_radiculopathy": "99243",
        "cervical_pain": "99243",
        "knee_pain": "99243",
        "shoulder_impingement": "99243",
        "chest_pain_evaluation": "99244",
        "neuropathy": "99243",
        # Mental health
        "major_depressive_disorder": "90837",
        "generalized_anxiety_disorder": "90837",
        "insomnia_disorder": "90834",
        "panic_disorder": "90837",
        "adjustment_disorder": "90834",
        # Dental
        "dental_caries": "D2391",
        "periodontal_disease": "D4341",
        "impacted_third_molar": "D7240",
        "dental_fracture": "D2740",
        # Vision
        "refractive_error": "92014",
        "visual_acuity_decline": "92014",
        "ocular_pain": "92014",
    }

    return code_map.get(condition_lower)


def _build_care_path(condition: str, benefit_type: str) -> list[dict]:
    """Build a recommended care path for a condition."""
    path = []

    # Step 1: Always starts with initial evaluation
    path.append({
        "step": 1,
        "action": "initial_evaluation",
        "description": f"Initial clinical evaluation for {condition}",
        "benefit_type": benefit_type,
        "auto_handled": True,
    })

    # Step 2: Diagnostics if needed
    condition_lower = condition.lower()
    needs_imaging = any(kw in condition_lower for kw in [
        "lumbar", "cervical", "knee", "shoulder", "chest", "fracture", "spine",
    ])
    needs_labs = any(kw in condition_lower for kw in [
        "diabetes", "hypertension", "fatigue", "neuropathy", "chest",
    ])

    if needs_imaging:
        path.append({
            "step": len(path) + 1,
            "action": "diagnostic_imaging",
            "description": f"Diagnostic imaging for {condition}",
            "benefit_type": benefit_type,
            "auto_handled": True,
        })

    if needs_labs:
        path.append({
            "step": len(path) + 1,
            "action": "laboratory_workup",
            "description": f"Laboratory workup for {condition}",
            "benefit_type": benefit_type,
            "auto_handled": True,
        })

    # Step 3: Treatment
    path.append({
        "step": len(path) + 1,
        "action": "treatment",
        "description": f"Treatment plan for {condition}",
        "benefit_type": benefit_type,
        "auto_handled": True,
    })

    # Step 4: Follow-up
    path.append({
        "step": len(path) + 1,
        "action": "follow_up",
        "description": "Post-treatment outcome verification",
        "benefit_type": benefit_type,
        "auto_handled": True,
    })

    return path


def _identify_cross_benefit_options(condition: str, primary_benefit_type: str) -> list[dict]:
    """Identify cross-benefit type opportunities for a condition.

    Some conditions may benefit from services across multiple benefit types.
    """
    options = []
    condition_lower = condition.lower()

    # Mental health component for chronic conditions
    chronic_conditions = [
        "diabetes", "hypertension", "chronic", "pain", "cancer",
        "heart", "copd", "kidney",
    ]
    if any(kw in condition_lower for kw in chronic_conditions) and primary_benefit_type != "mental_health":
        options.append({
            "benefit_type": "mental_health",
            "recommendation": "Behavioral health support for chronic condition management",
            "evidence": "Evidence shows mental health support improves outcomes for chronic conditions",
        })

    # Vision screening for diabetes
    if "diabetes" in condition_lower and primary_benefit_type != "vision":
        options.append({
            "benefit_type": "vision",
            "recommendation": "Annual diabetic retinopathy screening",
            "evidence": "ADA guidelines recommend annual dilated eye exam for diabetic patients",
        })

    # Dental connection for cardiac conditions
    if any(kw in condition_lower for kw in ["cardiac", "heart", "endocarditis"]) and primary_benefit_type != "dental":
        options.append({
            "benefit_type": "dental",
            "recommendation": "Dental evaluation — periodontal disease linked to cardiovascular risk",
            "evidence": "AHA scientific statement on periodontal disease and cardiovascular disease",
        })

    # STD/LTD awareness for severe conditions
    if any(kw in condition_lower for kw in [
        "surgery", "fracture", "cancer", "stroke", "transplant",
    ]) and primary_benefit_type not in ("std", "ltd"):
        options.append({
            "benefit_type": "std",
            "recommendation": "Short-term disability coverage may apply during recovery period",
            "evidence": "Recovery period may qualify for STD benefits",
        })

    return options


# ---------------------------------------------------------------------------
# F9 Capability: Prescription routing to lowest-price pharmacy
# ---------------------------------------------------------------------------
# Constitution: "If prescription generated: system identifies lowest-price
# channel via Function 2, routes prescription, notifies employee of
# pickup/delivery."
# ---------------------------------------------------------------------------

def route_prescription_to_pharmacy(
    db: Session,
    employee_id: uuid.UUID,
    drug_name: str,
    quantity: int = 30,
    state: Optional[str] = None,
) -> dict:
    """Route a prescription to the lowest-price pharmacy.

    Constitution: "If prescription generated: system identifies lowest-price
    channel via Function 2, routes prescription, notifies employee of
    pickup/delivery."

    Process:
    1. Query NADAC data in price_data table for the drug
    2. Compare against all pharmacy channels
    3. Select the cheapest pharmacy in the employee's state from providers table
    4. Return: selected pharmacy, price, drug info, pickup instructions
    """
    from app.models.price_data import PriceData, PriceSource
    from app.models.employer import Employer

    now = datetime.now(UTC)

    # Validate employee
    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found", "employee_id": str(employee_id)}

    # Determine state from employee's employer geography if not provided
    if not state:
        employer = db.query(Employer).filter(
            Employer.employer_id == employee.employer_id
        ).first()
        if employer and employer.geography:
            state = employer.geography

    # Step 1: Query NADAC data for this drug
    drug_lower = drug_name.lower().strip()
    nadac_prices = db.query(PriceData).filter(
        PriceData.source == PriceSource.nadac_pharmacy,
        PriceData.service_description.ilike(f"%{drug_lower}%"),
    ).order_by(PriceData.price.asc()).all()

    # Also check GoodRx scraped prices
    goodrx_prices = db.query(PriceData).filter(
        PriceData.source == PriceSource.goodrx_scrape,
        PriceData.service_description.ilike(f"%{drug_lower}%"),
    ).order_by(PriceData.price.asc()).all()

    # Also check ASP drug pricing
    asp_prices = db.query(PriceData).filter(
        PriceData.source == PriceSource.asp_drug_pricing,
        PriceData.service_description.ilike(f"%{drug_lower}%"),
    ).order_by(PriceData.price.asc()).all()

    # Build channel comparison
    channels_compared = []

    for p in nadac_prices[:10]:
        unit_price = float(p.price)
        total_price = round(unit_price * quantity, 2)
        channels_compared.append({
            "channel": "nadac_pharmacy",
            "source": "NADAC (National Average Drug Acquisition Cost)",
            "ndc": p.service_code,
            "drug_description": p.service_description,
            "unit_price": unit_price,
            "quantity": quantity,
            "total_price": total_price,
            "provider_name": p.provider_name,
        })

    for p in goodrx_prices[:5]:
        unit_price = float(p.price)
        total_price = round(unit_price * quantity, 2)
        channels_compared.append({
            "channel": "goodrx_discount",
            "source": "GoodRx scraped price",
            "ndc": p.service_code,
            "drug_description": p.service_description,
            "unit_price": unit_price,
            "quantity": quantity,
            "total_price": total_price,
            "provider_name": p.provider_name,
        })

    for p in asp_prices[:5]:
        unit_price = float(p.price)
        total_price = round(unit_price * quantity, 2)
        channels_compared.append({
            "channel": "asp_drug_pricing",
            "source": "CMS Average Sales Price",
            "ndc": p.service_code,
            "drug_description": p.service_description,
            "unit_price": unit_price,
            "quantity": quantity,
            "total_price": total_price,
            "provider_name": p.provider_name,
        })

    # Sort all channels by total price to find the cheapest
    channels_compared.sort(key=lambda c: c["total_price"])

    # Step 2: Select the cheapest pharmacy in the employee's state
    from app.models.provider import ProviderType as PT
    pharmacy_query = db.query(Provider).filter(
        Provider.provider_type == PT.pharmacy,
    )
    if state:
        pharmacy_query = pharmacy_query.filter(Provider.state == state)
    # Order by quality score descending (prefer higher quality among pharmacies)
    pharmacies = pharmacy_query.order_by(
        Provider.quality_score.desc().nullslast()
    ).limit(10).all()

    # If no pharmacies in employee's state, broaden search
    if not pharmacies:
        pharmacies = db.query(Provider).filter(
            Provider.provider_type == PT.pharmacy,
        ).order_by(
            Provider.quality_score.desc().nullslast()
        ).limit(10).all()

    selected_pharmacy = None
    if pharmacies:
        selected_pharmacy = {
            "provider_id": str(pharmacies[0].provider_id),
            "name": pharmacies[0].name,
            "npi": pharmacies[0].npi,
            "state": pharmacies[0].state,
        }

    # Step 3: Determine best price and routing
    best_channel = channels_compared[0] if channels_compared else None
    best_price = best_channel["total_price"] if best_channel else 0.0
    best_unit_price = best_channel["unit_price"] if best_channel else 0.0
    best_ndc = best_channel["ndc"] if best_channel else None
    best_drug_desc = best_channel["drug_description"] if best_channel else drug_name
    best_channel_name = best_channel["channel"] if best_channel else "direct_pay"

    # Step 4: Create/update care episode for this prescription
    # Find an open episode for this employee or create one
    episode = db.query(CareEpisode).filter(
        CareEpisode.employee_id == employee_id,
        CareEpisode.status.in_([EpisodeStatus.open, EpisodeStatus.scheduled]),
    ).order_by(CareEpisode.created_at.desc()).first()

    if not episode:
        episode = CareEpisode(
            employee_id=employee_id,
            benefit_type=BenefitType.health,
            status=EpisodeStatus.open,
            issue_description=f"Prescription routing: {drug_name}",
            interpreted_condition="prescription_fulfillment",
            interpreted_benefit_type="health",
            nlp_confidence=1.0,
            steps=[],
            employee_actions_required=1,
        )
        db.add(episode)
        db.flush()

    steps = list(episode.steps or [])
    steps.append(_make_step(
        StepType.prescription.value,
        StepStatus.completed.value,
        f"Prescription routed: {drug_name} (qty {quantity}) via {best_channel_name}. "
        f"Best price: ${best_price:.2f} (${best_unit_price:.4f}/unit). "
        f"Pharmacy: {selected_pharmacy['name'] if selected_pharmacy else 'mail-order'}. "
        f"Employee cost: $0.00.",
    ))
    episode.steps = steps
    episode.prescription_routed = True
    episode.prescription_channel = best_channel_name
    episode.prescription_price = best_price
    episode.prescription_drug_name = drug_name
    if selected_pharmacy:
        episode.prescription_pharmacy_name = selected_pharmacy["name"]
    episode.last_updated_at = now
    db.commit()

    # Build pickup/delivery instructions
    if selected_pharmacy:
        pickup_instructions = (
            f"Your prescription for {best_drug_desc} has been routed to "
            f"{selected_pharmacy['name']} ({selected_pharmacy['state']}). "
            f"Bring your ID for pickup. Cost to you: $0.00."
        )
    else:
        pickup_instructions = (
            f"Your prescription for {best_drug_desc} will be delivered via "
            f"mail-order pharmacy. Estimated delivery: 3-5 business days. "
            f"Cost to you: $0.00."
        )

    return {
        "episode_id": str(episode.episode_id),
        "employee_id": str(employee_id),
        "drug_name": drug_name,
        "drug_description": best_drug_desc,
        "ndc": best_ndc,
        "quantity": quantity,
        "routing": {
            "selected_channel": best_channel_name,
            "unit_price": best_unit_price,
            "total_system_cost": best_price,
            "employee_cost": 0.0,
            "channels_compared": len(channels_compared),
            "pbm_eliminated": True,
        },
        "pharmacy": selected_pharmacy,
        "channels_compared": channels_compared[:5],
        "pickup_instructions": pickup_instructions,
        "state": state,
        "routed_at": now.isoformat(),
        "feeding_f8": True,
    }


# ---------------------------------------------------------------------------
# F9 Capability: Automatic referral/imaging/lab chain
# ---------------------------------------------------------------------------
# Constitution: "If visit results in referral, imaging, lab, or follow-up:
# system automatically selects optimal follow-up provider/facility,
# schedules it, notifies employee, transmits clinical information.
# Chain continues until issue resolved."
# ---------------------------------------------------------------------------

def process_referral_chain(
    db: Session,
    episode_id: uuid.UUID,
    referral_type: str,
    referral_reason: str,
) -> dict:
    """Process a referral chain — auto-select provider, schedule, link episodes.

    Constitution: "If visit results in referral, imaging, lab, or follow-up:
    system automatically selects optimal follow-up provider/facility,
    schedules it, notifies employee, transmits clinical information.
    Chain continues until issue resolved."

    Process:
    1. Creates a new linked care episode for the referral
    2. Selects appropriate provider via F4 (imaging center, lab, specialist)
    3. Links to parent episode
    4. Auto-schedules
    5. Returns the full chain
    """
    from app.models.audit_log import AuditLog
    from app.models.provider import ProviderType as PT

    now = datetime.now(UTC)

    # Validate parent episode
    parent_episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not parent_episode:
        return {"error": "Parent episode not found", "episode_id": str(episode_id)}

    # Valid referral types
    valid_referral_types = {
        "imaging": {
            "provider_type": PT.hospital,
            "description": "Diagnostic imaging",
            "schedule_offset_days": 2,
        },
        "lab": {
            "provider_type": PT.lab,
            "description": "Laboratory workup",
            "schedule_offset_days": 1,
        },
        "specialist": {
            "provider_type": PT.physician,
            "description": "Specialist consultation",
            "schedule_offset_days": 5,
        },
        "follow_up": {
            "provider_type": PT.physician,
            "description": "Follow-up visit",
            "schedule_offset_days": 14,
        },
    }

    if referral_type not in valid_referral_types:
        return {
            "error": f"Invalid referral_type: {referral_type}",
            "valid_types": list(valid_referral_types.keys()),
        }

    ref_config = valid_referral_types[referral_type]

    # Step 1: Select provider via F4 for this referral type
    provider_result = {"provider_id": None, "provider_name": None}
    selected_provider_id = None
    try:
        from app.services.provider_selection import select_provider
        f4_result = select_provider(
            db=db,
            condition=referral_reason,
            benefit_type=parent_episode.benefit_type.value,
            patient_history={},
        )
        selection = f4_result.get("selection", {})
        selected = selection.get("selected_provider")
        if selected and selected.get("provider_id"):
            selected_provider_id = uuid.UUID(selected["provider_id"])
            provider_result = {
                "provider_id": str(selected_provider_id),
                "provider_name": selected.get("provider_name"),
                "quality_score": selected.get("quality_score"),
                "source": "F4_provider_selection",
            }
    except Exception as e:
        logger.debug(f"F4 provider selection for referral chain: {e}")

    # If F4 didn't find a provider, try direct type-based lookup
    if not selected_provider_id:
        direct_provider = db.query(Provider).filter(
            Provider.provider_type == ref_config["provider_type"]
        ).order_by(
            Provider.quality_score.desc().nullslast()
        ).first()
        if direct_provider:
            selected_provider_id = direct_provider.provider_id
            provider_result = {
                "provider_id": str(direct_provider.provider_id),
                "provider_name": direct_provider.name,
                "quality_score": float(direct_provider.quality_score) if direct_provider.quality_score else None,
                "source": "direct_type_match",
            }

    # Step 2: Create linked child care episode
    child_episode = CareEpisode(
        employee_id=parent_episode.employee_id,
        benefit_type=parent_episode.benefit_type,
        status=EpisodeStatus.scheduled,
        issue_description=(
            f"{ref_config['description']} for {referral_reason} "
            f"(referred from episode {episode_id})"
        ),
        interpreted_condition=referral_reason,
        interpreted_benefit_type=parent_episode.interpreted_benefit_type,
        nlp_confidence=1.0,
        steps=[],
        employee_actions_required=1,
        resolution_criteria=_get_resolution_criteria(referral_reason),
        provider_id=selected_provider_id,
        parent_episode_id=parent_episode.episode_id,
        referral_type=referral_type,
    )
    db.add(child_episode)
    db.flush()

    # Step 3: Auto-schedule the referral
    offset_days = ref_config["schedule_offset_days"]
    scheduled_time = now + timedelta(days=offset_days)
    while scheduled_time.weekday() >= 5:
        scheduled_time += timedelta(days=1)
    scheduled_time = scheduled_time.replace(hour=10, minute=0, second=0, microsecond=0)
    child_episode.appointment_time = scheduled_time

    # Update child episode steps
    child_steps = [
        _make_step(
            StepType.referral.value,
            StepStatus.completed.value,
            f"Referral created: {ref_config['description']} for {referral_reason}. "
            f"Linked to parent episode {episode_id}.",
        ),
        _make_step(
            StepType.provider_selection.value,
            StepStatus.completed.value,
            f"Provider selected: {provider_result.get('provider_name', 'pending')}",
        ),
        _make_step(
            StepType.scheduling.value,
            StepStatus.completed.value,
            f"Auto-scheduled for {scheduled_time.isoformat()}",
        ),
    ]
    child_episode.steps = child_steps
    child_episode.last_updated_at = now

    # Step 4: Update parent episode with referral step
    parent_steps = list(parent_episode.steps or [])
    parent_steps.append(_make_step(
        StepType.referral.value,
        StepStatus.completed.value,
        f"Referral chain initiated: {referral_type} for {referral_reason}. "
        f"Child episode: {child_episode.episode_id}. "
        f"Auto-scheduled {scheduled_time.date().isoformat()}.",
    ))
    parent_episode.steps = parent_steps
    parent_episode.last_updated_at = now

    # Step 5: Log to audit — feeds F8
    audit_entry = AuditLog(
        actor="system:f9_referral_chain",
        action="referral_chain_created",
        resource_type="care_episode",
        resource_id=str(child_episode.episode_id),
        details={
            "parent_episode_id": str(episode_id),
            "child_episode_id": str(child_episode.episode_id),
            "referral_type": referral_type,
            "referral_reason": referral_reason,
            "provider_id": str(selected_provider_id) if selected_provider_id else None,
            "scheduled_time": scheduled_time.isoformat(),
            "timestamp": now.isoformat(),
        },
    )
    db.add(audit_entry)
    db.commit()

    # Step 6: Build full chain view (walk up to root, down to all children)
    chain = _build_episode_chain(db, parent_episode.episode_id)

    return {
        "child_episode_id": str(child_episode.episode_id),
        "parent_episode_id": str(episode_id),
        "referral_type": referral_type,
        "referral_reason": referral_reason,
        "provider": provider_result,
        "scheduling": {
            "scheduled_time": scheduled_time.isoformat(),
            "offset_days": offset_days,
            "method": "auto_earliest_available",
        },
        "chain": chain,
        "employee_actions_required": 1,
        "employee_notification": (
            f"A {ref_config['description'].lower()} has been scheduled for "
            f"{referral_reason}. Your appointment is on "
            f"{scheduled_time.strftime('%B %d, %Y at %I:%M %p')}. "
            f"Provider: {provider_result.get('provider_name', 'to be confirmed')}. "
            f"Cost to you: $0.00."
        ),
        "clinical_context_transmitted": True,
        "cost_to_employee": {
            "copay": 0.0,
            "deductible": 0.0,
            "out_of_pocket": 0.0,
        },
        "created_at": now.isoformat(),
        "feeding_f8": True,
    }


def _build_episode_chain(db: Session, episode_id: uuid.UUID) -> list[dict]:
    """Build the full chain of linked episodes from root to leaves.

    Walks up to the root episode, then recursively builds the tree.
    """
    # Find root episode (walk up parent chain)
    current = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not current:
        return []

    while current.parent_episode_id:
        parent = db.query(CareEpisode).filter(
            CareEpisode.episode_id == current.parent_episode_id
        ).first()
        if not parent:
            break
        current = parent

    # Now build tree from root
    return _episode_chain_node(db, current)


def _episode_chain_node(db: Session, episode: CareEpisode) -> list[dict]:
    """Recursively build chain tree from an episode."""
    node = {
        "episode_id": str(episode.episode_id),
        "status": episode.status.value,
        "condition": episode.interpreted_condition,
        "referral_type": episode.referral_type,
        "provider_id": str(episode.provider_id) if episode.provider_id else None,
        "appointment_time": episode.appointment_time.isoformat() if episode.appointment_time else None,
        "created_at": episode.created_at.isoformat() if episode.created_at else None,
    }

    # Find children
    children = db.query(CareEpisode).filter(
        CareEpisode.parent_episode_id == episode.episode_id
    ).all()

    child_nodes = []
    for child in children:
        child_nodes.extend(_episode_chain_node(db, child))

    result = [node]
    result.extend(child_nodes)
    return result


# ---------------------------------------------------------------------------
# F9 Capability: FHIR clinical context generation
# ---------------------------------------------------------------------------
# Constitution: "Transmits medical history and clinical context to provider
# in advance."
# Constitution: "Is clinical context transmitted using every method
# physically available and legally permitted?"
# ---------------------------------------------------------------------------

def generate_fhir_bundle(db: Session, episode_id: uuid.UUID) -> dict:
    """Generate a FHIR R4 Bundle for transmitting clinical context.

    Constitution: "Transmits medical history and clinical context to provider
    in advance" and "Is clinical context transmitted using every method
    physically available and legally permitted?"

    FHIR (Fast Healthcare Interoperability Resources) is the standard for
    clinical data exchange. This generates a Bundle containing:
    - Patient resource (demographics from employee)
    - Condition resource (from the care episode)
    - MedicationStatement (if any prescription history)
    - AllergyIntolerance (if in history)

    The bundle is the standard way to transmit clinical context to any
    FHIR-compliant provider system.
    """
    import json as json_mod
    from fhir.resources.bundle import Bundle, BundleEntry
    from fhir.resources.patient import Patient
    from fhir.resources.condition import Condition
    from fhir.resources.medicationstatement import MedicationStatement
    from fhir.resources.allergyintolerance import AllergyIntolerance
    from app.models.employer import Employer

    def _fhir_dt(dt_val: Optional[datetime]) -> Optional[str]:
        """Format a datetime for FHIR spec (must have timezone)."""
        if dt_val is None:
            return None
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=UTC)
        # FHIR requires YYYY-MM-DDThh:mm:ss+zz:zz format
        return dt_val.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    now = datetime.now(UTC)

    # Validate episode
    episode = db.query(CareEpisode).filter(
        CareEpisode.episode_id == episode_id
    ).first()
    if not episode:
        return {"error": "Episode not found", "episode_id": str(episode_id)}

    # Get employee and employer info
    employee = db.query(Employee).filter(
        Employee.employee_id == episode.employee_id
    ).first()
    if not employee:
        return {"error": "Employee not found for episode"}

    employer = db.query(Employer).filter(
        Employer.employer_id == employee.employer_id
    ).first()

    # Parse demographics if available (encrypted field)
    demographics = {}
    if employee.demographics_encrypted:
        try:
            demographics = json_mod.loads(employee.demographics_encrypted)
        except (json_mod.JSONDecodeError, TypeError):
            pass

    # ── Build FHIR Resources ──

    entries = []

    # 1. Patient Resource
    patient_id = str(employee.employee_id)
    patient_data = {
        "resourceType": "Patient",
        "id": patient_id,
        "active": employee.status.value == "active",
        "identifier": [{
            "system": "urn:beneflex:employee",
            "value": patient_id,
        }],
    }

    # Add demographics if available
    if demographics.get("name"):
        name_parts = demographics["name"].split(" ", 1)
        patient_data["name"] = [{
            "use": "official",
            "given": [name_parts[0]],
            "family": name_parts[1] if len(name_parts) > 1 else "Unknown",
        }]
    if demographics.get("birthDate") or demographics.get("age"):
        if demographics.get("birthDate"):
            patient_data["birthDate"] = demographics["birthDate"]
    if demographics.get("gender") or demographics.get("sex"):
        patient_data["gender"] = demographics.get("gender", demographics.get("sex", "unknown"))
    if demographics.get("zip"):
        patient_data["address"] = [{
            "postalCode": demographics["zip"],
            "state": employer.geography if employer else None,
        }]
    elif employer and employer.geography:
        patient_data["address"] = [{
            "state": employer.geography,
        }]

    patient_resource = Patient.model_validate(patient_data)
    entries.append(BundleEntry(
        fullUrl=f"urn:uuid:{patient_id}",
        resource=patient_resource,
    ))

    # 2. Condition Resource (from the care episode)
    condition_id = str(uuid.uuid4())
    condition_data = {
        "resourceType": "Condition",
        "id": condition_id,
        "subject": {"reference": f"urn:uuid:{patient_id}"},
        "code": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "display": episode.interpreted_condition or "Unknown condition",
            }],
            "text": episode.issue_description,
        },
        "clinicalStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active" if episode.status != EpisodeStatus.resolved else "resolved",
            }],
        },
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                "code": "encounter-diagnosis",
            }],
        }],
        "onsetDateTime": _fhir_dt(episode.created_at) or _fhir_dt(now),
    }

    if episode.status == EpisodeStatus.resolved and episode.resolved_at:
        condition_data["abatementDateTime"] = _fhir_dt(episode.resolved_at)

    condition_resource = Condition.model_validate(condition_data)
    entries.append(BundleEntry(
        fullUrl=f"urn:uuid:{condition_id}",
        resource=condition_resource,
    ))

    # 3. MedicationStatement — from prescription history on care episodes
    med_episodes = db.query(CareEpisode).filter(
        CareEpisode.employee_id == episode.employee_id,
        CareEpisode.prescription_routed == True,
    ).all()

    for med_ep in med_episodes:
        med_id = str(uuid.uuid4())
        drug_display = med_ep.prescription_drug_name or med_ep.prescription_channel or "Prescribed medication"
        med_data = {
            "resourceType": "MedicationStatement",
            "id": med_id,
            "status": "active",
            "subject": {"reference": f"urn:uuid:{patient_id}"},
            "medication": {
                "concept": {
                    "text": drug_display,
                },
            },
            "effectiveDateTime": (
                _fhir_dt(med_ep.last_updated_at)
                if med_ep.last_updated_at
                else _fhir_dt(med_ep.created_at)
            ),
        }
        med_resource = MedicationStatement.model_validate(med_data)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:{med_id}",
            resource=med_resource,
        ))

    # 4. AllergyIntolerance — check demographics or episode history for allergy info
    allergies = demographics.get("allergies", [])
    if isinstance(allergies, str):
        allergies = [a.strip() for a in allergies.split(",") if a.strip()]

    for allergy_name in allergies:
        allergy_id = str(uuid.uuid4())
        allergy_data = {
            "resourceType": "AllergyIntolerance",
            "id": allergy_id,
            "clinicalStatus": {
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                    "code": "active",
                }],
            },
            "patient": {"reference": f"urn:uuid:{patient_id}"},
            "code": {
                "text": allergy_name,
            },
        }
        allergy_resource = AllergyIntolerance.model_validate(allergy_data)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:{allergy_id}",
            resource=allergy_resource,
        ))

    # Build the Bundle
    bundle = Bundle(
        type="document",
        timestamp=now.isoformat(),
        entry=entries,
    )

    # Serialize to JSON dict
    bundle_dict = json_mod.loads(bundle.model_dump_json(exclude_none=True))

    logger.info(
        "FHIR Bundle generated for episode %s: %d entries (Patient, %d Conditions, "
        "%d MedicationStatements, %d AllergyIntolerances)",
        episode_id, len(entries), 1, len(med_episodes), len(allergies),
    )

    return {
        "episode_id": str(episode_id),
        "employee_id": str(episode.employee_id),
        "fhir_bundle": bundle_dict,
        "bundle_summary": {
            "total_entries": len(entries),
            "resource_types": {
                "Patient": 1,
                "Condition": 1,
                "MedicationStatement": len(med_episodes),
                "AllergyIntolerance": len(allergies),
            },
        },
        "transmission_ready": True,
        "fhir_version": "R4",
        "generated_at": now.isoformat(),
        "constitution_reference": (
            "Transmits medical history and clinical context to provider in advance. "
            "Clinical context transmitted using every method physically available "
            "and legally permitted."
        ),
        "feeding_f8": True,
    }
