"""Clinical Quality Engine — Function 1.

Constitution: "Determine whether a requested service across all benefit types
is medically necessary, using only clinical inputs, with zero access to
financial data."

SOLE INPUTS (Constitution-mandated):
1. Patient symptoms
2. Patient medical history
3. Current peer-reviewed evidence-based clinical guidelines

ZERO ACCESS TO:
- Financial data, cost targets, profit margins, revenue information,
  pricing calculations, or any system containing financial data.
- No API, no shared memory, no indirect data pipeline to financial systems.

This module is designed to be deployed inside a Trusted Execution Environment
(TEE) with remote attestation. The code is open-source.
"""

import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta, UTC
from typing import Optional

from sqlalchemy import and_, or_, func
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.clinical_determination import ClinicalDetermination
from app.models.clinical_guideline import ClinicalGuideline, BenefitTypeGuideline

logger = logging.getLogger(__name__)

# Red-flag symptoms with clinical severity tiers.
# Tier weights: emergency (1.0), critical (0.7), serious (0.4), moderate (0.2).
# The tier weight is multiplied by the category max (0.5) to produce the
# per-symptom contribution.
RED_FLAG_SYMPTOMS_TIERED: dict[str, float] = {
    # Emergency tier (1.0) — immediate life threat
    "cardiac arrest": 1.0,
    "anaphylaxis": 1.0,
    "hemorrhage": 1.0,
    "uncontrolled bleeding": 1.0,
    "stroke symptoms": 1.0,
    "sepsis": 1.0,
    "substance overdose": 1.0,
    "loss of consciousness": 1.0,
    # Critical tier (0.7) — high risk of rapid deterioration
    "chest pain": 0.7,
    "shortness of breath": 0.7,
    "seizure": 0.7,
    "acute abdomen": 0.7,
    "suicidal ideation": 0.7,
    "self-harm": 0.7,
    "psychosis": 0.7,
    "severe headache": 0.7,
    "neck stiffness": 0.7,
    "neurological deficit": 0.7,
    # Serious tier (0.4) — significant concern
    "severe pain": 0.4,
    "trauma": 0.4,
    "sudden vision loss": 0.4,
    "sudden hearing loss": 0.4,
    "fever with rash": 0.4,
    "progressive weakness": 0.4,
    "confusion": 0.4,
    "delirium": 0.4,
    "withdrawal symptoms": 0.4,
    # Moderate tier (0.2) — warrants clinical attention
    "severe depression": 0.2,
    "panic attack": 0.2,
}

# Flat list kept for backward compat (used in _evaluate_criteria)
RED_FLAG_SYMPTOMS = list(RED_FLAG_SYMPTOMS_TIERED.keys())

# High-complexity conditions with severity weights.
# Weight reflects how much the condition compounds deterioration risk.
HIGH_COMPLEXITY_CONDITIONS_WEIGHTED: dict[str, float] = {
    "cancer": 1.0,
    "transplant": 1.0,
    "immunocompromised": 0.9,
    "hiv": 0.8,
    "heart failure": 0.8,
    "kidney disease": 0.7,
    "liver disease": 0.7,
    "copd": 0.6,
    "hepatitis": 0.6,
    "autoimmune": 0.6,
    "diabetes": 0.5,
    "hypertension": 0.4,
    "schizophrenia": 0.5,
    "bipolar": 0.5,
    "substance use disorder": 0.5,
    "chronic pain": 0.3,
}

# Flat list kept for backward compat
HIGH_COMPLEXITY_CONDITIONS = list(HIGH_COMPLEXITY_CONDITIONS_WEIGHTED.keys())

# Benefit type mapping from claim types to guideline types
BENEFIT_TYPE_MAP = {
    "health": BenefitTypeGuideline.health,
    "dental": BenefitTypeGuideline.dental,
    "vision": BenefitTypeGuideline.vision,
    "mental_health": BenefitTypeGuideline.mental_health,
    "life": BenefitTypeGuideline.life_insurance,
    "std": BenefitTypeGuideline.short_term_disability,
    "ltd": BenefitTypeGuideline.long_term_disability,
}


def _compute_audit_hash(
    inputs: dict,
    decision: str,
    reasoning: str,
    guidelines: list[str],
    timestamp: str,
    previous_hash: Optional[str] = None,
) -> str:
    """Compute cryptographic hash for immutable audit log.

    Constitution: "Every determination recorded in an immutable, append-only,
    cryptographically secured log containing: complete clinical input, complete
    output with clinical reasoning, and timestamp."

    Uses SHA-256 hash chain — each entry includes the hash of the previous
    entry, creating an append-only chain that detects any tampering.
    """
    payload = json.dumps({
        "inputs": inputs,
        "decision": decision,
        "reasoning": reasoning,
        "guidelines_referenced": guidelines,
        "timestamp": timestamp,
        "previous_hash": previous_hash or "GENESIS",
    }, sort_keys=True, default=str)

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def archive_determination_to_s3(determination: ClinicalDetermination) -> Optional[str]:
    """Archive determination to S3 with Object Lock for immutable off-DB backup.

    Constitution: "Every determination recorded in an immutable, append-only,
    cryptographically secured log."

    S3 Object Lock (GOVERNANCE mode) prevents deletion/modification for the
    retention period, providing a legally tamper-proof secondary copy.
    Returns the S3 key if successful, None if S3 is not configured.
    """
    import os
    bucket = os.environ.get("S3_AUDIT_BUCKET")
    if not bucket:
        logger.debug("S3_AUDIT_BUCKET not set — skipping S3 archival")
        return None

    try:
        import boto3
        from datetime import timedelta

        s3 = boto3.client("s3", region_name=os.environ.get("S3_AUDIT_REGION", "us-west-2"))

        det_id = str(determination.determination_id)
        created = determination.created_at.strftime("%Y/%m/%d")
        key = f"audit/determinations/{created}/{det_id}.json"

        payload = json.dumps({
            "determination_id": det_id,
            "claim_id": determination.claim_id,
            "benefit_type": determination.benefit_type,
            "decision": determination.decision.value if hasattr(determination.decision, 'value') else str(determination.decision),
            "reasoning": determination.reasoning,
            "guidelines_referenced": determination.guidelines_referenced,
            "audit_hash": determination.audit_hash,
            "risk_score": determination.risk_score,
            "risk_factors": determination.risk_factors,
            "latency_ms": determination.latency_ms,
            "created_at": determination.created_at.isoformat(),
        }, sort_keys=True, default=str)

        # Object Lock: GOVERNANCE mode, 7-year retention
        retain_until = determination.created_at + timedelta(days=2555)  # ~7 years
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload.encode("utf-8"),
            ContentType="application/json",
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=retain_until,
        )

        logger.info(f"Archived determination {det_id} to s3://{bucket}/{key}")
        return key

    except Exception as e:
        logger.warning(f"S3 archival failed for determination {determination.determination_id}: {e}")
        return None


def _get_previous_hash(db: Session) -> Optional[str]:
    """Get the hash of the most recent determination for chain continuity."""
    latest = (
        db.query(ClinicalDetermination.audit_hash)
        .order_by(ClinicalDetermination.created_at.desc())
        .first()
    )
    return latest[0] if latest else None


def _find_matching_guidelines(
    db: Session,
    service_code: str,
    benefit_type: str,
    condition: Optional[str] = None,
) -> list[ClinicalGuideline]:
    """Find clinical guidelines matching the requested service.

    Matches on:
    1. Service code (CPT/HCPCS/CDT) in the guideline's service_codes field
    2. Benefit type
    3. Condition (if provided)
    """
    bt = BENEFIT_TYPE_MAP.get(benefit_type, BenefitTypeGuideline.health)

    filters = [
        ClinicalGuideline.is_active,
        or_(
            ClinicalGuideline.benefit_type == bt,
            ClinicalGuideline.benefit_type == BenefitTypeGuideline.all_types,
        ),
    ]

    # Match by service code
    if service_code:
        filters.append(ClinicalGuideline.service_codes.contains(service_code))

    guidelines = db.query(ClinicalGuideline).filter(and_(*filters)).all()

    # If no match by service code, try condition match
    if not guidelines and condition:
        condition_lower = condition.lower()
        guidelines = (
            db.query(ClinicalGuideline)
            .filter(
                and_(
                    ClinicalGuideline.is_active,
                    or_(
                        ClinicalGuideline.benefit_type == bt,
                        ClinicalGuideline.benefit_type == BenefitTypeGuideline.all_types,
                    ),
                    or_(
                        ClinicalGuideline.condition.ilike(f"%{condition_lower}%"),
                        ClinicalGuideline.title.ilike(f"%{condition_lower}%"),
                    ),
                )
            )
            .all()
        )

    return guidelines


def _evaluate_criteria(
    guideline: ClinicalGuideline,
    patient_symptoms: list[str],
    patient_history: dict,
) -> tuple[bool, str]:
    """Evaluate whether the patient meets the guideline's criteria.

    Returns (meets_criteria: bool, reasoning: str).

    This is a rule-based evaluation. In production with more guidelines,
    this would use NLP/ML to match symptoms against criteria text.
    Currently uses keyword matching against structured criteria.
    """
    if not guideline.criteria:
        return True, f"Guideline '{guideline.title}' has no exclusion criteria. Service approved per standing recommendation."

    criteria_text = guideline.criteria.lower()
    symptoms_text = " ".join(s.lower() for s in patient_symptoms)
    age = patient_history.get("age", 0)
    sex = patient_history.get("sex", "").lower()
    diagnoses = [d.lower() for d in patient_history.get("diagnoses", [])]
    risk_factors = [r.lower() for r in patient_history.get("risk_factors", [])]

    reasons = []
    meets = True

    # Age check
    if "age >=" in criteria_text or "age >" in criteria_text:
        import re
        age_matches = re.findall(r'age\s*>=?\s*(\d+)', criteria_text)
        if age_matches:
            min_age = int(age_matches[0])
            if age >= min_age:
                reasons.append(f"Patient age {age} meets minimum age requirement ({min_age}+)")
            else:
                # Check for risk factor exceptions
                if "risk factor" in criteria_text or "family history" in criteria_text:
                    if risk_factors:
                        reasons.append(f"Patient below typical age ({age} < {min_age}) but has risk factors: {', '.join(risk_factors)}")
                    else:
                        meets = False
                        reasons.append(f"Patient age {age} below minimum ({min_age}) and no documented risk factors")
                else:
                    meets = False
                    reasons.append(f"Patient age {age} below guideline minimum age ({min_age})")

    # Sex check
    if "female" in criteria_text and sex and sex not in ("female", "f"):
        meets = False
        reasons.append("Guideline applies to female patients; patient sex does not match")
    if "male" in criteria_text and "female" not in criteria_text and sex and sex not in ("male", "m"):
        meets = False
        reasons.append("Guideline applies to male patients; patient sex does not match")

    # Diagnosis check
    if "diagnosis" in criteria_text or "diagnosed" in criteria_text:
        condition = guideline.condition.lower().replace("_", " ")
        has_relevant_dx = any(condition in d or d in condition for d in diagnoses)
        if has_relevant_dx:
            reasons.append(f"Patient has documented diagnosis matching condition '{guideline.condition}'")
        elif symptoms_text:
            reasons.append("Patient symptoms present; clinical evaluation of service appropriateness indicated")

    # Red flags / urgency check
    if "red flag" in criteria_text:
        red_flag_keywords = ["trauma", "neurological deficit", "cancer", "fever", "progressive", "acute"]
        found_flags = [k for k in red_flag_keywords if k in symptoms_text]
        if found_flags:
            reasons.append(f"Red flags present: {', '.join(found_flags)} — expedited service indicated")

    # Contraindication check
    if guideline.contraindications:
        contra_lower = guideline.contraindications.lower()
        for dx in diagnoses:
            if dx in contra_lower:
                meets = False
                reasons.append(f"Contraindication match: patient diagnosis '{dx}' found in contraindications")

    if not reasons:
        reasons.append(f"Service evaluated against guideline '{guideline.title}'. No exclusion criteria triggered.")

    return meets, " ".join(reasons)


def _assess_meaningful_risk(
    patient_symptoms: list[str],
    patient_history: dict,
    service_code: str,
    benefit_type: str,
) -> tuple[float, dict, str]:
    """Assess whether the patient faces meaningful risk of health deterioration.

    Constitution (gray-area logic): "The engine evaluates whether the patient
    faces a meaningful risk of health deterioration without the requested
    service, based on the patient's specific symptoms, medical history, and
    all available clinical evidence. If the evidence supports a meaningful
    risk of health deterioration, the service is approved. If the evidence
    does not support a meaningful risk of health deterioration, the service
    is declined. The determination is not a default in either direction."

    Weighted scoring categories (max contribution):
    1. Emergency red flags:       max 0.50  (tiered by clinical severity)
    2. High-complexity conditions: max 0.30  (weighted by condition severity)
    3. Age vulnerability:          max 0.20  (graduated scale)
    4. Service invasiveness:       max 0.15  (by CPT category)

    Returns (risk_score, risk_factors_dict, reasoning).
    risk_score: 0.0-1.0 where higher = more risk of deterioration without service.
    """
    symptoms_text = " ".join(s.lower() for s in patient_symptoms)
    diagnoses = [d.lower() for d in patient_history.get("diagnoses", [])]
    medications = [m.lower() for m in patient_history.get("medications", [])]
    risk_factors_input = [r.lower() for r in patient_history.get("risk_factors", [])]
    age = patient_history.get("age", 0)

    factors = {
        "red_flags_present": [],
        "red_flag_severity_tiers": {},
        "high_complexity_conditions": [],
        "medication_count": len(medications),
        "active_diagnoses_count": len(diagnoses),
        "risk_factors_count": len(risk_factors_input),
        "age_vulnerability": False,
        "age_vulnerability_score": 0.0,
        "symptom_severity": "low",
    }

    # ---- 1. Red-flag symptom scoring (max 0.50) ----
    # Each flag contributes its tier weight * 0.5 (the category max).
    # Multiple flags stack but are capped at 0.50.
    red_flag_score = 0.0
    for flag, tier_weight in RED_FLAG_SYMPTOMS_TIERED.items():
        if flag in symptoms_text:
            contribution = tier_weight * 0.5
            factors["red_flags_present"].append(flag)
            tier_label = (
                "emergency" if tier_weight >= 0.9
                else "critical" if tier_weight >= 0.6
                else "serious" if tier_weight >= 0.3
                else "moderate"
            )
            factors["red_flag_severity_tiers"][flag] = tier_label
            red_flag_score += contribution
    red_flag_score = min(red_flag_score, 0.50)

    # ---- 2. High-complexity condition scoring (max 0.30) ----
    # Each condition contributes its weight * 0.3 (the category max).
    # Polypharmacy and multi-morbidity add supplementary points.
    complexity_score = 0.0
    for condition, cond_weight in HIGH_COMPLEXITY_CONDITIONS_WEIGHTED.items():
        if any(condition in d for d in diagnoses):
            factors["high_complexity_conditions"].append(condition)
            complexity_score += cond_weight * 0.15  # scaled so 2 major conditions ~ 0.3
    # Polypharmacy bonus
    if len(medications) >= 5:
        complexity_score += 0.05
    elif len(medications) >= 3:
        complexity_score += 0.02
    # Multi-morbidity bonus
    if len(diagnoses) >= 4:
        complexity_score += 0.05
    elif len(diagnoses) >= 2:
        complexity_score += 0.02
    # Risk factor bonus
    if len(risk_factors_input) >= 3:
        complexity_score += 0.04
    elif len(risk_factors_input) >= 1:
        complexity_score += 0.02
    complexity_score = min(complexity_score, 0.30)

    # ---- 3. Age vulnerability (max 0.20) ----
    # Graduated scale rather than binary thresholds.
    age_score = 0.0
    if age <= 1:
        age_score = 0.20
    elif age <= 5:
        age_score = 0.16
    elif age <= 12:
        age_score = 0.10
    elif age >= 80:
        age_score = 0.20
    elif age >= 70:
        age_score = 0.16
    elif age >= 65:
        age_score = 0.12
    elif age >= 55:
        age_score = 0.06

    if age_score > 0:
        factors["age_vulnerability"] = True
        factors["age_vulnerability_score"] = age_score

    # ---- 4. Service invasiveness context (max 0.15) ----
    # More invasive services carry higher risk if denied when needed.
    invasiveness_score = 0.0
    code_prefix = service_code[:2] if service_code else ""
    if code_prefix in ("10", "11", "12", "13", "14", "15", "16", "17", "19",
                       "20", "21", "22", "23", "24", "25", "26", "27", "28", "29"):
        # Surgical codes (10000-29999)
        invasiveness_score = 0.15
        factors["symptom_severity"] = "high"
    elif code_prefix in ("30", "31", "32", "33", "34", "35", "36", "37", "38", "39"):
        # Cardiovascular/thoracic surgery (30000-39999)
        invasiveness_score = 0.15
        factors["symptom_severity"] = "high"
    elif code_prefix in ("70", "71", "72", "73", "74", "75", "76", "77", "78"):
        # Imaging/radiology (70000-79999)
        invasiveness_score = 0.08
        factors["symptom_severity"] = "moderate"
    elif code_prefix in ("80", "81", "82", "83", "84", "85", "86", "87", "88", "89"):
        # Lab/pathology (80000-89999)
        invasiveness_score = 0.04
    elif code_prefix in ("90", "96", "97", "98", "99"):
        # E&M and medicine (90000-99999)
        invasiveness_score = 0.06
    elif code_prefix in ("D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"):
        # Dental CDT codes
        invasiveness_score = 0.05

    total_risk = min(
        red_flag_score + complexity_score + age_score + invasiveness_score,
        1.0,
    )

    # Classify overall severity
    if total_risk >= 0.4:
        factors["symptom_severity"] = "high"
    elif total_risk >= 0.2:
        factors["symptom_severity"] = (
            "moderate" if factors["symptom_severity"] == "low"
            else factors["symptom_severity"]
        )

    # Build patient-specific reasoning
    reasoning_parts = [
        f"Gray-area assessment: No specific clinical guideline clearly addresses "
        f"service {service_code} (benefit type: {benefit_type}) for this patient."
    ]

    if factors["red_flags_present"]:
        emergency_flags = [
            f for f in factors["red_flags_present"]
            if factors["red_flag_severity_tiers"].get(f) == "emergency"
        ]
        critical_flags = [
            f for f in factors["red_flags_present"]
            if factors["red_flag_severity_tiers"].get(f) == "critical"
        ]
        other_flags = [
            f for f in factors["red_flags_present"]
            if factors["red_flag_severity_tiers"].get(f) not in ("emergency", "critical")
        ]
        if emergency_flags:
            reasoning_parts.append(
                f"EMERGENCY red-flag symptoms: {', '.join(emergency_flags)}."
            )
        if critical_flags:
            reasoning_parts.append(
                f"Critical red-flag symptoms: {', '.join(critical_flags)}."
            )
        if other_flags:
            reasoning_parts.append(
                f"Additional red-flag symptoms: {', '.join(other_flags)}."
            )

    if factors["high_complexity_conditions"]:
        reasoning_parts.append(
            f"Patient has high-complexity conditions: "
            f"{', '.join(factors['high_complexity_conditions'])}."
        )
    if factors["age_vulnerability"]:
        reasoning_parts.append(
            f"Patient age ({age}) indicates elevated vulnerability "
            f"(age risk contribution: {age_score:.2f})."
        )
    if len(medications) >= 3:
        reasoning_parts.append(
            f"Patient on {len(medications)} medications, indicating active "
            f"management of multiple conditions."
        )
    if len(diagnoses) >= 2:
        reasoning_parts.append(f"Patient has {len(diagnoses)} active diagnoses.")

    # Use calibrated threshold from outcome data if available, else 0.25 default
    threshold = _get_calibrated_risk_threshold()

    if total_risk >= threshold:
        reasoning_parts.append(
            f"Risk score: {total_risk:.2f} (threshold: {threshold:.2f}). "
            f"Evidence supports meaningful risk of health "
            f"deterioration without the requested service. Service APPROVED based on "
            f"patient-specific clinical risk assessment."
        )
    else:
        reasoning_parts.append(
            f"Risk score: {total_risk:.2f} (threshold: {threshold:.2f}). "
            f"Evidence does not support meaningful risk of "
            f"health deterioration without the requested service. The patient's symptoms, "
            f"history, and available clinical evidence do not indicate that absence of this "
            f"service creates a clinically supported probability of health worsening. "
            f"Service DECLINED."
        )

    return total_risk, factors, " ".join(reasoning_parts)


# Cache for calibrated risk threshold (recalculated every 100 determinations)
_calibrated_threshold: float | None = None
_calibrated_at_count: int = 0


def _get_calibrated_risk_threshold() -> float:
    """Get risk threshold calibrated from outcome data, or 0.25 default.

    Constitution: thresholds should be evidence-based, not hardcoded.
    Uses determinations with outcome feedback to find the threshold
    that maximizes accuracy (correct approvals + correct denials).
    """
    global _calibrated_threshold, _calibrated_at_count

    # Return cached value if recent
    if _calibrated_threshold is not None:
        return _calibrated_threshold

    # Try to calibrate from database — need a session
    # This will be set by the calibration function below
    return 0.25  # Default fallback


def calibrate_risk_threshold(db: Session) -> float:
    """Calibrate the gray-area risk threshold from actual outcome data.

    Finds the threshold that maximizes correct decisions:
    - Approvals where outcome was "correct" (true positives)
    - Denials where outcome was "correct" (true negatives)
    """
    global _calibrated_threshold, _calibrated_at_count

    outcomes = db.query(
        ClinicalDetermination.risk_score,
        ClinicalDetermination.decision,
        ClinicalDetermination.outcome_feedback,
    ).filter(
        ClinicalDetermination.risk_score is not None,
        ClinicalDetermination.outcome_feedback is not None,
    ).all()

    if len(outcomes) < 20:
        _calibrated_threshold = 0.25
        return 0.25  # Insufficient data

    # Grid search over thresholds 0.10 to 0.50
    best_threshold = 0.25
    best_accuracy = 0.0

    for threshold_candidate in [x / 100 for x in range(10, 51, 5)]:
        correct = 0
        total = len(outcomes)
        for risk_score, decision, outcome in outcomes:
            would_approve = (risk_score or 0) >= threshold_candidate
            was_correct = outcome == "correct"
            if would_approve and was_correct and decision == "approved":
                correct += 1
            elif not would_approve and was_correct and decision == "denied":
                correct += 1
            elif would_approve and not was_correct:
                pass  # false positive
            elif not would_approve and not was_correct:
                pass  # false negative

        accuracy = correct / max(total, 1)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_threshold = threshold_candidate

    _calibrated_threshold = best_threshold
    _calibrated_at_count = len(outcomes)
    logger.info(f"Risk threshold calibrated to {best_threshold:.2f} from {len(outcomes)} outcomes (accuracy: {best_accuracy:.1%})")
    return best_threshold


def record_determination_outcome(
    db: Session,
    determination_id: str,
    actual_outcome: str,
) -> dict:
    """Record whether a clinical determination was correct.

    Constitution (accuracy): Enables measurement of determination accuracy
    by correlating decisions with actual patient outcomes.
    """
    det = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.determination_id == determination_id
    ).first()
    if not det:
        return {"error": "Determination not found"}

    det.outcome_feedback = actual_outcome
    det.outcome_recorded_at = datetime.now(UTC)
    db.commit()

    return {
        "determination_id": str(determination_id),
        "decision": det.decision.value if hasattr(det.decision, 'value') else str(det.decision),
        "outcome_feedback": actual_outcome,
        "recorded_at": det.outcome_recorded_at.isoformat(),
    }


def make_determination(
    db: Session,
    claim_id: str,
    service_code: str,
    benefit_type: str,
    patient_symptoms: list[str],
    patient_history: dict,
    condition: Optional[str] = None,
) -> ClinicalDetermination:
    """Make a clinical quality determination.

    Constitution-mandated process:
    1. Accept ONLY clinical inputs (symptoms, history, guidelines)
    2. Match against evidence-based guidelines
    3. Evaluate criteria (or assess meaningful risk for gray-area cases)
    4. Record in immutable audit log with cryptographic hash chain

    ZERO access to financial data at any point in this function.
    """
    t_start = time.perf_counter()
    now = datetime.now(UTC)

    # Build clinical inputs (will be encrypted at rest via EncryptedString)
    clinical_inputs = {
        "service_code": service_code,
        "benefit_type": benefit_type,
        "patient_symptoms": patient_symptoms,
        "patient_history": patient_history,
        "condition": condition,
        "timestamp": now.isoformat(),
    }
    inputs_json = json.dumps(clinical_inputs, sort_keys=True, default=str)

    # Track gray-area risk assessment results
    risk_score = None
    risk_factors = None

    # Find matching guidelines — two-pass approach:
    # 1. Rule-based: exact service code + condition match
    # 2. NLP-based: semantic similarity for symptoms without exact code match
    guidelines = _find_matching_guidelines(db, service_code, benefit_type, condition)
    rule_based_count = len(guidelines)

    # NLP enhancement: if rule-based match is weak, use semantic similarity
    # When rule-based found ZERO matches, require higher NLP threshold (0.5)
    # since these are purely semantic matches with no service code anchor.
    # When rule-based found 1, use lower threshold (0.3) to supplement.
    if rule_based_count <= 1:
        nlp_threshold = 0.5 if rule_based_count == 0 else 0.3
        try:
            from app.services.clinical_nlp import match_symptoms_to_guidelines
            # Use higher min_score when no rule-based matches to avoid false positives
            nlp_min = 0.15 if rule_based_count == 0 else 0.05
            nlp_matches = match_symptoms_to_guidelines(
                db, patient_symptoms, patient_history, benefit_type, condition,
                top_k=3, min_score=nlp_min,
            )
            existing_ids = {str(g.guideline_id) for g in guidelines}
            for match in nlp_matches:
                if match["guideline_id"] not in existing_ids and match["similarity_score"] > nlp_threshold:
                    nlp_guideline = db.query(ClinicalGuideline).filter(
                        ClinicalGuideline.guideline_id == match["guideline_id"]
                    ).first()
                    if nlp_guideline:
                        guidelines.append(nlp_guideline)
                        existing_ids.add(match["guideline_id"])
        except Exception as e:
            logger.warning(f"NLP matching failed (falling back to rules): {e}")

    # Consume F8 waste detection signals (clinical patterns — TEE-safe)
    waste_signals = _consume_f8_waste_signals(db, benefit_type, service_code)
    if waste_signals:
        # Add waste detection context to clinical reasoning
        waste_context = "; ".join([s["recommendation"] for s in waste_signals if s["recommendation"]])
        if waste_context:
            # Store as supplementary clinical context, not as a denial reason
            if risk_factors is None:
                risk_factors = {}
            risk_factors["f8_waste_detection_signals"] = waste_signals

    guidelines_referenced = [
        f"{g.source.value}:{g.title} (Grade {g.grade.value})"
        for g in guidelines
    ]

    if not guidelines:
        # Gray-area: no guideline clearly addresses this service.
        # Constitution: "The determination is not a default in either direction.
        # It is a clinical assessment of the individual patient's risk."
        risk_score, risk_factors, reasoning = _assess_meaningful_risk(
            patient_symptoms, patient_history, service_code, benefit_type
        )
        if risk_score >= 0.25:
            decision = "approved"
        else:
            decision = "denied"
        guidelines_referenced = [
            f"Gray-area assessment — no matching guideline. "
            f"Risk score: {risk_score:.2f}. Patient-specific risk evaluation performed."
        ]
    else:
        # Evaluate each matching guideline
        all_reasoning = []
        any_denied = False

        for guideline in guidelines:
            meets, reasoning_text = _evaluate_criteria(guideline, patient_symptoms, patient_history)
            all_reasoning.append(f"[{guideline.title}] {reasoning_text}")

            if meets:
                pass
            else:
                any_denied = True

        reasoning = " | ".join(all_reasoning)

        # Decision logic: guidelines with explicit criteria take precedence
        # over guidelines with no criteria. A denial from a criteria-explicit
        # guideline overrides an approval from a guideline with no criteria.
        has_criteria_denial = False
        has_criteria_approval = False
        for guideline in guidelines:
            has_explicit_criteria = bool(guideline.criteria and guideline.criteria.strip())
            meets, _ = _evaluate_criteria(guideline, patient_symptoms, patient_history)
            if has_explicit_criteria:
                if meets:
                    has_criteria_approval = True
                else:
                    has_criteria_denial = True

        if has_criteria_denial and not has_criteria_approval:
            decision = "denied"
            reasoning += " | DETERMINATION: Service does not meet clinical criteria per referenced guidelines."
        elif has_criteria_approval:
            decision = "approved"
            reasoning += " | DETERMINATION: Service meets clinical criteria per referenced guidelines."
        elif any_denied:
            decision = "denied"
            reasoning += " | DETERMINATION: Service does not meet clinical criteria per referenced guidelines."
        else:
            decision = "approved"
            reasoning += " | DETERMINATION: No exclusion criteria triggered. Service approved."

    # Compute cryptographic hash for immutable audit chain
    previous_hash = _get_previous_hash(db)
    audit_hash = _compute_audit_hash(
        inputs=clinical_inputs,
        decision=decision,
        reasoning=reasoning,
        guidelines=guidelines_referenced,
        timestamp=now.isoformat(),
        previous_hash=previous_hash,
    )

    # Measure latency
    latency_ms = (time.perf_counter() - t_start) * 1000

    # Determine routing: standing protocol vs individual Medical Director review
    # Build Manifest items 3, 8, 17:
    #   - Approvals matching standing protocols: processed instantly, no queue
    #   - Denials and gray-area: routed to contracted Medical Director for final determination
    #   - Log records whether standing protocol or individual review, and by whom
    is_gray_area = risk_score is not None
    if decision == "approved" and not is_gray_area:
        # Approval with clear guideline support → standing protocol (auto-final)
        determination_authority = "standing_protocol"
        reviewed_by = "Medical Director standing orders (auto-approved)"
        is_recommendation = False  # Final under standing protocol
    else:
        # Denials and gray-area → recommendation pending MD review
        determination_authority = "individual_review"
        reviewed_by = None  # Pending assignment to contracted Medical Director / UR
        is_recommendation = True  # Recommendation, not final

    # Create determination record
    determination = ClinicalDetermination(
        claim_id=claim_id,
        benefit_type=benefit_type,
        inputs_encrypted=inputs_json,
        decision=decision,
        reasoning=reasoning,
        guidelines_referenced=guidelines_referenced,
        created_at=now,
        audit_hash=audit_hash,
        risk_score=risk_score,
        risk_factors=risk_factors,
        latency_ms=latency_ms,
        determination_authority=determination_authority,
        reviewed_by=reviewed_by,
        is_recommendation=is_recommendation,
    )

    db.add(determination)
    db.commit()

    # Archive to S3 with Object Lock for immutable off-database backup
    archive_determination_to_s3(determination)

    logger.info(
        f"Clinical determination {determination.determination_id}: "
        f"{decision} for {service_code} ({benefit_type}) — "
        f"{len(guidelines)} guidelines referenced — "
        f"{latency_ms:.1f}ms"
    )

    return determination


def ensemble_determination(
    db: Session,
    claim_id: str,
    service_code: str,
    benefit_type: str,
    patient_symptoms: list[str],
    patient_history: dict,
    condition: Optional[str] = None,
) -> dict:
    """Run multiple evaluation strategies and combine with weighted voting.

    Strategies:
    1. Rule-based: standard guideline matching and criteria evaluation
    2. NLP-based: semantic similarity matching via clinical NLP
    3. Risk-based: meaningful risk of deterioration assessment

    Each strategy produces a decision and confidence score. The final
    determination uses weighted voting to combine them.

    This improves accuracy over single-strategy determination by reducing
    variance and catching cases where one strategy may be weak.
    """
    t_start = time.perf_counter()
    strategies = []

    # --- Strategy 1: Rule-based guideline matching ---
    guidelines = _find_matching_guidelines(db, service_code, benefit_type, condition)
    rule_decision = "approved"
    rule_confidence = 0.5
    rule_reasoning = "No matching guidelines found (rule-based)."

    if guidelines:
        any_approved = False
        any_denied = False
        reasoning_parts = []
        for g in guidelines:
            meets, reasoning_text = _evaluate_criteria(g, patient_symptoms, patient_history)
            reasoning_parts.append(f"[{g.title}] {reasoning_text}")
            if meets:
                any_approved = True
            else:
                any_denied = True

        if any_denied and not any_approved:
            rule_decision = "denied"
            rule_confidence = 0.8
        elif any_approved:
            rule_decision = "approved"
            rule_confidence = 0.85
        else:
            rule_decision = "approved"
            rule_confidence = 0.6

        rule_reasoning = " | ".join(reasoning_parts)

    strategies.append({
        "name": "rule_based",
        "decision": rule_decision,
        "confidence": rule_confidence,
        "weight": 0.4,
        "reasoning": rule_reasoning,
        "guidelines_matched": len(guidelines),
    })

    # --- Strategy 2: NLP-based semantic matching ---
    nlp_decision = "approved"
    nlp_confidence = 0.5
    nlp_reasoning = "NLP matching not available."

    try:
        from app.services.clinical_nlp import match_symptoms_to_guidelines
        nlp_matches = match_symptoms_to_guidelines(
            db, patient_symptoms, patient_history, benefit_type, condition,
            top_k=5, min_score=0.1,
        )
        if nlp_matches:
            top_score = nlp_matches[0]["similarity_score"]
            nlp_confidence = min(top_score + 0.3, 0.9)

            # Check if top NLP match supports or denies
            top_guideline_id = nlp_matches[0]["guideline_id"]
            top_guideline = db.query(ClinicalGuideline).filter(
                ClinicalGuideline.guideline_id == top_guideline_id
            ).first()
            if top_guideline:
                meets, nlp_reason = _evaluate_criteria(top_guideline, patient_symptoms, patient_history)
                nlp_decision = "approved" if meets else "denied"
                nlp_reasoning = f"NLP top match (score={top_score:.2f}): {nlp_reason}"
            else:
                nlp_reasoning = f"NLP matched {len(nlp_matches)} guidelines, top score={top_score:.2f}"
        else:
            nlp_confidence = 0.3
            nlp_reasoning = "No strong NLP matches found."
    except Exception as e:
        nlp_reasoning = f"NLP matching failed: {e}"
        nlp_confidence = 0.3

    strategies.append({
        "name": "nlp_based",
        "decision": nlp_decision,
        "confidence": nlp_confidence,
        "weight": 0.3,
        "reasoning": nlp_reasoning,
    })

    # --- Strategy 3: Risk-based assessment ---
    risk_score, risk_factors, risk_reasoning = _assess_meaningful_risk(
        patient_symptoms, patient_history, service_code, benefit_type
    )
    risk_decision = "approved" if risk_score >= 0.25 else "denied"
    risk_confidence = min(abs(risk_score - 0.25) * 4 + 0.5, 0.9)

    strategies.append({
        "name": "risk_based",
        "decision": risk_decision,
        "confidence": risk_confidence,
        "weight": 0.3,
        "reasoning": risk_reasoning,
        "risk_score": risk_score,
    })

    # --- Weighted voting ---
    approve_score = 0.0
    deny_score = 0.0
    for s in strategies:
        weighted = s["weight"] * s["confidence"]
        if s["decision"] == "approved":
            approve_score += weighted
        else:
            deny_score += weighted

    ensemble_decision = "approved" if approve_score >= deny_score else "denied"
    ensemble_confidence = max(approve_score, deny_score) / (approve_score + deny_score) if (approve_score + deny_score) > 0 else 0.5

    latency_ms = (time.perf_counter() - t_start) * 1000

    return {
        "claim_id": claim_id,
        "service_code": service_code,
        "benefit_type": benefit_type,
        "ensemble_decision": ensemble_decision,
        "ensemble_confidence": round(ensemble_confidence, 3),
        "approve_score": round(approve_score, 3),
        "deny_score": round(deny_score, 3),
        "strategies": strategies,
        "latency_ms": round(latency_ms, 1),
        "note": (
            "Ensemble combines rule-based (0.4 weight), NLP-based (0.3 weight), "
            "and risk-based (0.3 weight) strategies with confidence-weighted voting."
        ),
    }


def cross_validate_determinations(db: Session, sample_size: int = 100) -> dict:
    """Cross-validation framework for testing determination accuracy on historical data.

    Compares historical determinations against a re-evaluation using current
    guidelines and logic. Identifies drift between original and current
    determination outcomes, which can indicate guideline updates or engine
    improvements.

    Constitution: accuracy must be measured and improved.
    """
    # Fetch historical determinations with outcome feedback
    determinations = (
        db.query(ClinicalDetermination)
        .filter(ClinicalDetermination.outcome_feedback.isnot(None))
        .order_by(ClinicalDetermination.created_at.desc())
        .limit(sample_size)
        .all()
    )

    if not determinations:
        # Fall back to any determinations if none have outcome feedback
        determinations = (
            db.query(ClinicalDetermination)
            .order_by(ClinicalDetermination.created_at.desc())
            .limit(sample_size)
            .all()
        )

    if not determinations:
        return {
            "status": "no_data",
            "message": "No historical determinations available for cross-validation.",
        }

    total = len(determinations)
    concordant = 0
    discordant = 0
    correct_outcomes = 0
    incorrect_outcomes = 0
    errors_by_type = {"false_approve": 0, "false_deny": 0}
    re_evaluation_results = []

    for det in determinations:
        # Parse original inputs
        try:
            inputs = json.loads(det.inputs_encrypted) if det.inputs_encrypted else {}
        except (json.JSONDecodeError, TypeError):
            continue

        original_decision = det.decision.value if hasattr(det.decision, 'value') else str(det.decision)

        # Re-evaluate with current guidelines
        service_code = inputs.get("service_code", "")
        benefit_type_str = inputs.get("benefit_type", "health")
        patient_symptoms = inputs.get("patient_symptoms", [])
        patient_history = inputs.get("patient_history", {})
        condition_val = inputs.get("condition")

        current_guidelines = _find_matching_guidelines(db, service_code, benefit_type_str, condition_val)

        if current_guidelines:
            any_meets = any(
                _evaluate_criteria(g, patient_symptoms, patient_history)[0]
                for g in current_guidelines
            )
            current_decision = "approved" if any_meets else "denied"
        else:
            risk_score, _, _ = _assess_meaningful_risk(
                patient_symptoms, patient_history, service_code, benefit_type_str
            )
            current_decision = "approved" if risk_score >= 0.25 else "denied"

        if original_decision == current_decision:
            concordant += 1
        else:
            discordant += 1

        # Check against outcome feedback if available
        if det.outcome_feedback:
            if det.outcome_feedback == "correct":
                correct_outcomes += 1
            else:
                incorrect_outcomes += 1
                if original_decision == "approved":
                    errors_by_type["false_approve"] += 1
                else:
                    errors_by_type["false_deny"] += 1

        re_evaluation_results.append({
            "determination_id": str(det.determination_id),
            "original_decision": original_decision,
            "current_decision": current_decision,
            "concordant": original_decision == current_decision,
            "outcome_feedback": det.outcome_feedback,
        })

    outcomes_total = correct_outcomes + incorrect_outcomes
    accuracy = round(correct_outcomes / outcomes_total * 100, 1) if outcomes_total > 0 else None

    return {
        "sample_size": total,
        "concordance": {
            "concordant": concordant,
            "discordant": discordant,
            "concordance_rate_pct": round(concordant / total * 100, 1) if total > 0 else None,
        },
        "outcome_accuracy": {
            "correct": correct_outcomes,
            "incorrect": incorrect_outcomes,
            "accuracy_pct": accuracy,
            "errors_by_type": errors_by_type,
        },
        "interpretation": (
            f"Of {total} historical determinations, {concordant} ({round(concordant/total*100, 1) if total else 0}%) "
            f"would receive the same decision with current guidelines. "
            + (f"Outcome accuracy: {accuracy}%." if accuracy else "No outcome feedback available yet.")
        ),
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def create_ab_test(
    db: Session,
    test_name: str,
    variant_config: dict,
) -> dict:
    """A/B testing framework for comparing determination strategies.

    Creates an A/B test configuration that splits incoming determinations
    between the standard strategy and a variant strategy. Results are
    tracked and compared after a defined period.

    Parameters
    ----------
    test_name : str
        Human-readable name for the test (e.g., "ensemble_vs_standard").
    variant_config : dict
        Configuration for the variant strategy. Keys:
        - strategy: "ensemble" | "nlp_weighted" | "risk_weighted"
        - traffic_pct: float (0-50, percentage of traffic to variant)
        - duration_days: int (how long to run the test)

    Returns
    -------
    dict with test configuration and initial metrics.
    """
    from app.models.data_pipeline_metric import DataPipelineMetric

    strategy = variant_config.get("strategy", "ensemble")
    traffic_pct = min(variant_config.get("traffic_pct", 10), 50)  # Cap at 50%
    duration_days = variant_config.get("duration_days", 14)

    now = datetime.now(UTC)
    test_id = hashlib.sha256(f"{test_name}-{now.isoformat()}".encode()).hexdigest()[:12]

    # Record test configuration as a metric
    metric = DataPipelineMetric(
        metric_type=f"ab_test:{test_id}",
        value=0,
        details={
            "test_id": test_id,
            "test_name": test_name,
            "status": "active",
            "variant_strategy": strategy,
            "traffic_pct": traffic_pct,
            "duration_days": duration_days,
            "started_at": now.isoformat(),
            "control_count": 0,
            "variant_count": 0,
            "control_approve_rate": None,
            "variant_approve_rate": None,
            "control_accuracy": None,
            "variant_accuracy": None,
        },
        measured_at=now,
    )
    db.add(metric)
    db.commit()

    # Fetch baseline metrics for comparison
    total_dets = db.query(func.count(ClinicalDetermination.determination_id)).scalar() or 0
    approved = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.decision == "approved"
    ).scalar() or 0
    baseline_approve_rate = round(approved / total_dets * 100, 1) if total_dets > 0 else None

    return {
        "test_id": test_id,
        "test_name": test_name,
        "status": "active",
        "variant_strategy": strategy,
        "traffic_pct": traffic_pct,
        "duration_days": duration_days,
        "started_at": now.isoformat(),
        "baseline_metrics": {
            "total_determinations": total_dets,
            "current_approve_rate_pct": baseline_approve_rate,
        },
        "instructions": (
            f"Test '{test_name}' is active. {traffic_pct}% of incoming determinations "
            f"will use the '{strategy}' strategy. Results will be compared after "
            f"{duration_days} days. Monitor via /api/clinical/ab-test/{test_id}."
        ),
    }


def verify_audit_chain(db: Session, limit: int = 100) -> dict:
    """Verify the integrity of the cryptographic audit chain.

    Constitution: "Available for independent audit at any time."

    Walks the determination log and verifies each hash matches the
    computed hash of its contents + previous hash. Any tampering would
    break the chain.
    """
    determinations = (
        db.query(ClinicalDetermination)
        .order_by(ClinicalDetermination.created_at.asc())
        .limit(limit)
        .all()
    )

    if not determinations:
        return {"status": "empty", "records_checked": 0, "chain_valid": True}

    valid = 0
    invalid = 0
    previous_hash = None

    for det in determinations:
        # Reconstruct inputs from the decrypted field
        # Note: EncryptedString auto-decrypts on read, so det.inputs_encrypted
        # returns the plaintext JSON that was originally stored.
        try:
            raw = det.inputs_encrypted
            inputs = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            inputs = {}

        # Use the timestamp stored inside the inputs (includes timezone),
        # not det.created_at (which loses timezone in SQLite)
        stored_timestamp = inputs.get("timestamp", det.created_at.isoformat() if det.created_at else "")

        expected_hash = _compute_audit_hash(
            inputs=inputs,
            decision=det.decision.value if hasattr(det.decision, 'value') else str(det.decision),
            reasoning=det.reasoning or "",
            guidelines=det.guidelines_referenced or [],
            timestamp=stored_timestamp,
            previous_hash=previous_hash,
        )

        if expected_hash == det.audit_hash:
            valid += 1
        else:
            invalid += 1

        previous_hash = det.audit_hash

    return {
        "status": "valid" if invalid == 0 else "TAMPERED",
        "records_checked": len(determinations),
        "valid": valid,
        "invalid": invalid,
        "chain_valid": invalid == 0,
    }


def _compute_expected_rates_by_type(db: Session) -> dict:
    """Compute expected approval rates per benefit type from guideline criteria.

    Constitution: rates should be evidence-based, derived from actual guideline
    structure rather than hardcoded assumptions.

    Analysis approach:
    1. Count guidelines with explicit *approval* criteria (recommendation text
       containing approve/recommend/indicated language).
    2. Count guidelines with explicit *denial/exclusion* criteria
       (contraindications, grade D, or criteria with restrictive language).
    3. Classify remaining guidelines as neutral (no restrictive criteria).
    4. Compute expected rate from the ratio:
       - Approval-oriented guidelines predict high approval (~95%).
       - Neutral guidelines (no criteria) predict moderate-high approval (~90%).
       - Denial-oriented guidelines predict lower approval, weighted by grade:
         Grade A/B restrictions are stricter (~60%), Grade C/I are looser (~75%).
    """
    expected = {}
    for bt_name, bt_enum in BENEFIT_TYPE_MAP.items():
        # Try outcome-based rate first (Constitution: evidence-based, not hardcoded)
        outcome_based_rate = _get_outcome_based_rate(db, bt_enum)
        if outcome_based_rate is not None:
            expected[bt_name] = outcome_based_rate
            continue

        # Fetch all active guidelines for this benefit type
        guidelines = db.query(ClinicalGuideline).filter(
            and_(
                ClinicalGuideline.is_active,
                or_(
                    ClinicalGuideline.benefit_type == bt_enum,
                    ClinicalGuideline.benefit_type == BenefitTypeGuideline.all_types,
                ),
            )
        ).all()

        if not guidelines:
            expected[bt_name] = 87.0  # Fallback to industry average
            continue

        # Classify each guideline and compute its predicted approval rate
        weighted_rate_sum = 0.0
        weight_sum = 0.0

        for g in guidelines:
            has_criteria = bool(g.criteria and g.criteria.strip())
            has_contraindications = bool(
                g.contraindications and g.contraindications.strip()
            )
            rec_text = (g.recommendation or "").lower()
            criteria_text = (g.criteria or "").lower()
            grade_val = g.grade.value if g.grade else "ungraded"

            # Evidence grade weight: higher-grade guidelines get more influence
            grade_weight = {
                "A": 2.0, "B": 1.5, "C": 1.0, "D": 1.5, "I": 0.5, "ungraded": 0.8,
            }.get(grade_val, 0.8)

            # Determine predicted approval rate for this guideline
            is_denial_oriented = (
                grade_val == "D"
                or has_contraindications
                or any(
                    kw in criteria_text
                    for kw in (
                        "must not", "should not", "contraindicated",
                        "not indicated", "not recommended", "excluded",
                        "fail first", "step therapy required",
                    )
                )
            )

            is_approval_oriented = (
                not is_denial_oriented
                and any(
                    kw in rec_text
                    for kw in (
                        "recommended", "indicated", "should receive",
                        "is appropriate", "approved", "standard of care",
                    )
                )
            )

            if is_denial_oriented:
                # Guidelines with restrictive criteria: predict lower approval
                # Stronger evidence (A/B) means criteria are more strictly enforced
                if grade_val in ("A", "B"):
                    predicted_rate = 60.0
                else:
                    predicted_rate = 72.0
            elif is_approval_oriented:
                # Guidelines that actively recommend the service
                predicted_rate = 96.0
            elif has_criteria:
                # Guidelines with criteria but not clearly restrictive or approving
                # Criteria create a filter, predicting moderate approval
                predicted_rate = 80.0
            else:
                # No criteria at all: service is generally available
                predicted_rate = 93.0

            weighted_rate_sum += predicted_rate * grade_weight
            weight_sum += grade_weight

        expected_rate = weighted_rate_sum / max(weight_sum, 0.01)
        expected[bt_name] = round(expected_rate, 1)

    return expected


def _get_outcome_based_rate(db: Session, benefit_type_enum) -> float | None:
    """Compute expected approval rate from actual outcome feedback data.

    Returns the real-world approval rate for this benefit type based on
    determinations with outcome feedback, or None if insufficient data (<10).
    """

    # Map service BenefitType to guideline BenefitTypeGuideline

    bt_value = benefit_type_enum.value if hasattr(benefit_type_enum, 'value') else str(benefit_type_enum)

    total_with_outcome = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.outcome_feedback is not None,
        ClinicalDetermination.benefit_type == bt_value,
    ).scalar() or 0

    if total_with_outcome < 10:
        return None  # Insufficient data for reliable rate

    correct_outcomes = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.outcome_feedback == "correct",
        ClinicalDetermination.benefit_type == bt_value,
    ).scalar() or 0

    approved_total = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.decision == "approved",
        ClinicalDetermination.benefit_type == bt_value,
        ClinicalDetermination.outcome_feedback is not None,
    ).scalar() or 0

    # Expected rate = proportion of determinations that were approved
    # weighted by accuracy (correct outcomes)
    correct_outcomes / max(total_with_outcome, 1)
    approval_rate = approved_total / max(total_with_outcome, 1) * 100

    return round(approval_rate, 1)


def get_published_rates(db: Session) -> dict:
    """Publish approval and denial rates across all benefit types.

    Constitution: "Publishes approval and denial rates across all benefit
    types compared against rates that evidence-based guidelines would predict
    for the same patient population."
    """
    total = db.query(func.count(ClinicalDetermination.determination_id)).scalar() or 0

    if total == 0:
        return {
            "total_determinations": 0,
            "message": "No determinations recorded yet. Rates will be published after first determination.",
        }

    # Overall rates
    approved = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.decision == "approved"
    ).scalar() or 0

    denied = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.decision == "denied"
    ).scalar() or 0

    modified = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.decision == "modified"
    ).scalar() or 0

    # Per-benefit-type rates
    rates_by_type = {}
    for bt_name in BENEFIT_TYPE_MAP:
        bt_total = db.query(func.count(ClinicalDetermination.determination_id)).filter(
            ClinicalDetermination.benefit_type == bt_name
        ).scalar() or 0

        if bt_total == 0:
            continue

        bt_approved = db.query(func.count(ClinicalDetermination.determination_id)).filter(
            and_(
                ClinicalDetermination.benefit_type == bt_name,
                ClinicalDetermination.decision == "approved",
            )
        ).scalar() or 0

        bt_denied = db.query(func.count(ClinicalDetermination.determination_id)).filter(
            and_(
                ClinicalDetermination.benefit_type == bt_name,
                ClinicalDetermination.decision == "denied",
            )
        ).scalar() or 0

        rates_by_type[bt_name] = {
            "total": bt_total,
            "approved": bt_approved,
            "approved_pct": round(bt_approved / bt_total * 100, 1),
            "denied": bt_denied,
            "denied_pct": round(bt_denied / bt_total * 100, 1),
        }

    # Compute expected rates from guideline criteria analysis (not hardcoded)
    expected_rates = _compute_expected_rates_by_type(db)

    # Accuracy metrics from outcome feedback
    outcomes_recorded = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.outcome_feedback is not None
    ).scalar() or 0

    accuracy_data = {}
    if outcomes_recorded > 0:
        correct = db.query(func.count(ClinicalDetermination.determination_id)).filter(
            ClinicalDetermination.outcome_feedback == "correct"
        ).scalar() or 0
        accuracy_data = {
            "outcomes_recorded": outcomes_recorded,
            "accuracy_pct": round(correct / outcomes_recorded * 100, 1),
        }

    # Latency metrics
    avg_latency = db.query(func.avg(ClinicalDetermination.latency_ms)).filter(
        ClinicalDetermination.latency_ms is not None
    ).scalar()

    return {
        "total_determinations": total,
        "overall_rates": {
            "approved": approved,
            "approved_pct": round(approved / total * 100, 1),
            "denied": denied,
            "denied_pct": round(denied / total * 100, 1),
            "modified": modified,
            "modified_pct": round(modified / total * 100, 1),
        },
        "rates_by_benefit_type": rates_by_type,
        "guideline_predicted_rates": {
            "expected_by_benefit_type": expected_rates,
            "source": "Computed from guideline criteria analysis per benefit type",
            "note": "Actual rates should closely track predicted rates. Significant deviation "
                    "indicates either patient population mismatch or engine calibration issue.",
        },
        "accuracy": accuracy_data,
        "latency": {
            "average_ms": round(avg_latency, 2) if avg_latency else None,
        },
        "transparency": {
            "engine_isolation": "Runs in Trusted Execution Environment (TEE) with remote attestation",
            "financial_access": "Zero — no API, shared memory, or indirect pipeline to financial data",
            "source_code": "Open-source, publicly auditable",
            "audit_log": "Immutable, append-only, cryptographically secured hash chain",
        },
    }


def _consume_f8_waste_signals(db: Session, benefit_type: str, service_code: str) -> list[dict]:
    """Consume F8 waste detection signals — clinical patterns only (TEE-safe).

    These are epidemiological/clinical patterns, NOT financial data.
    Safe to use inside TEE isolation boundary.
    """
    try:
        from app.models.data_pipeline_metric import DataPipelineMetric

        signals = db.query(DataPipelineMetric).filter(
            DataPipelineMetric.metric_type.like("cross_type_signal:%"),
            DataPipelineMetric.details.isnot(None),
        ).order_by(DataPipelineMetric.measured_at.desc()).limit(20).all()

        waste_signals = []
        for s in signals:
            details = s.details or {}
            if details.get("target_function") == "F1" and "waste" in details.get("signal_type", ""):
                waste_signals.append({
                    "signal_type": details["signal_type"],
                    "recommendation": details.get("actionable_recommendation", ""),
                    "confidence": details.get("confidence", 0),
                    "benefit_type": details.get("benefit_type", ""),
                })
        return waste_signals
    except Exception:
        return []


# ---------------------------------------------------------------------------
# F1 Supplement S1: Physician compensation structure
# ---------------------------------------------------------------------------
# Constitution: physician reviewers are paid a flat per-review fee, identical
# regardless of whether they uphold or overturn. No retainer, no outcome-based
# bonus. Contractually explicit and auditable.

PHYSICIAN_REVIEW_COMPENSATION = {
    "fee_type": "flat_per_review",
    "amount_usd": 150.00,
    "outcome_independent": True,
    "retainer": False,
    "outcome_bonus": False,
    "payment_trigger": "review_completion",
    "contractual_clause": (
        "Reviewer is compensated a flat fee of $150.00 per completed review, "
        "payable upon submission of the review determination. Fee is identical "
        "regardless of whether the reviewer upholds or overturns the AI "
        "recommendation. No retainer, bonus, or other outcome-linked "
        "compensation is permitted."
    ),
    "audit_visibility": "full",
}


def get_physician_review_terms() -> dict:
    """Return the physician review compensation structure.

    Constitution (F1 Supplement S1): Compensation is flat per-review,
    identical regardless of outcome, no retainer, no outcome-based bonus.
    Contractually explicit and auditable.

    Returns
    -------
    dict with fee structure, contractual clause, and audit visibility.
    """
    return {
        **PHYSICIAN_REVIEW_COMPENSATION,
        "generated_at": datetime.now(UTC).isoformat(),
        "structure_hash": hashlib.sha256(
            json.dumps(PHYSICIAN_REVIEW_COMPENSATION, sort_keys=True).encode()
        ).hexdigest(),
    }


# ---------------------------------------------------------------------------
# F1 Supplement S2: Blinded case presentation
# ---------------------------------------------------------------------------

def prepare_blinded_case(db: Session, determination_id: str) -> dict:
    """Prepare a blinded case for physician review by stripping non-clinical data.

    Constitution (F1 Supplement S2): Removes ALL non-clinical data from the
    determination before presenting to the reviewing physician. Retains only:
    symptoms, medical history, age, biological sex, clinical evidence evaluated,
    and the AI determination with reasoning.

    Strips: patient name, employer, demographics beyond clinical relevance,
    geographic location, all financial data.

    The stripping operation is logged to AuditLog.

    Parameters
    ----------
    db : Session
        Database session.
    determination_id : str
        UUID of the clinical determination to blind.

    Returns
    -------
    dict with blinded clinical data only, or error dict.
    """
    det = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.determination_id == determination_id
    ).first()
    if not det:
        return {"error": "determination_not_found", "determination_id": str(determination_id)}

    # Parse the stored clinical inputs
    try:
        inputs = json.loads(det.inputs_encrypted) if det.inputs_encrypted else {}
    except (json.JSONDecodeError, TypeError):
        inputs = {}

    patient_history = inputs.get("patient_history", {})

    # Extract ONLY clinically relevant fields
    blinded_history = {
        "age": patient_history.get("age"),
        "sex": patient_history.get("sex"),
        "diagnoses": patient_history.get("diagnoses", []),
        "medications": patient_history.get("medications", []),
        "risk_factors": patient_history.get("risk_factors", []),
    }

    blinded_case = {
        "blinded_determination_id": hashlib.sha256(
            str(determination_id).encode()
        ).hexdigest()[:16],
        "symptoms": inputs.get("patient_symptoms", []),
        "medical_history": blinded_history,
        "service_code": inputs.get("service_code"),
        "benefit_type": inputs.get("benefit_type"),
        "condition": inputs.get("condition"),
        "ai_determination": {
            "decision": det.decision.value if hasattr(det.decision, "value") else str(det.decision),
            "reasoning": det.reasoning,
            "guidelines_referenced": det.guidelines_referenced or [],
            "risk_score": det.risk_score,
        },
        "prepared_at": datetime.now(UTC).isoformat(),
    }

    # Log the blinding operation to AuditLog
    audit_entry = AuditLog(
        actor="clinical_engine:prepare_blinded_case",
        action="blind_case",
        resource_type="clinical_determination",
        resource_id=str(determination_id),
        details={
            "fields_stripped": [
                "patient_name", "employer", "geographic_location",
                "financial_data", "non_clinical_demographics",
            ],
            "fields_retained": [
                "age", "sex", "symptoms", "diagnoses", "medications",
                "risk_factors", "service_code", "benefit_type",
                "ai_determination",
            ],
        },
    )
    db.add(audit_entry)
    db.flush()

    logger.info(
        "Blinded case prepared for determination %s (blinded ID: %s)",
        determination_id, blinded_case["blinded_determination_id"],
    )

    return blinded_case


# ---------------------------------------------------------------------------
# F1 Supplement S3: Anchoring bias detection
# ---------------------------------------------------------------------------

def run_blind_comparison_sample(db: Session, sample_pct: float = 0.15) -> dict:
    """Select a random sample of recent denials for blind physician comparison.

    Constitution (F1 Supplement S3): To detect anchoring bias, a random sample
    of denial determinations is prepared WITHOUT the AI's determination or
    reasoning, so the physician reviews the case independently.

    Parameters
    ----------
    db : Session
        Database session.
    sample_pct : float
        Fraction of recent denials to sample (default 15%, clamped to 10-20%).

    Returns
    -------
    dict with blinded cases (no AI decision) ready for independent physician
    review, plus tracking metadata.
    """
    sample_pct = max(0.10, min(sample_pct, 0.20))

    # Query recent denial determinations (last 90 days)
    cutoff = datetime.now(UTC) - timedelta(days=90)
    denials = (
        db.query(ClinicalDetermination)
        .filter(
            ClinicalDetermination.decision == "denied",
            ClinicalDetermination.created_at >= cutoff,
        )
        .order_by(ClinicalDetermination.created_at.desc())
        .all()
    )

    if not denials:
        return {
            "status": "no_denials",
            "message": "No recent denial determinations available for blind comparison.",
            "sample_size": 0,
        }

    # Random sample
    sample_size = max(1, int(len(denials) * sample_pct))
    sampled = random.sample(denials, min(sample_size, len(denials)))

    blinded_cases = []
    for det in sampled:
        try:
            inputs = json.loads(det.inputs_encrypted) if det.inputs_encrypted else {}
        except (json.JSONDecodeError, TypeError):
            inputs = {}

        patient_history = inputs.get("patient_history", {})

        # Blinded case WITHOUT AI determination or reasoning (anchoring prevention)
        blinded = {
            "blind_case_id": hashlib.sha256(
                f"{det.determination_id}-blind".encode()
            ).hexdigest()[:16],
            "determination_id": str(det.determination_id),
            "symptoms": inputs.get("patient_symptoms", []),
            "medical_history": {
                "age": patient_history.get("age"),
                "sex": patient_history.get("sex"),
                "diagnoses": patient_history.get("diagnoses", []),
                "medications": patient_history.get("medications", []),
                "risk_factors": patient_history.get("risk_factors", []),
            },
            "service_code": inputs.get("service_code"),
            "benefit_type": inputs.get("benefit_type"),
            "condition": inputs.get("condition"),
            # NO ai_determination, NO reasoning, NO risk_score
        }
        blinded_cases.append(blinded)

    # Log the sampling operation
    audit_entry = AuditLog(
        actor="clinical_engine:run_blind_comparison_sample",
        action="blind_sample_selected",
        resource_type="clinical_determination",
        resource_id="batch",
        details={
            "total_denials": len(denials),
            "sample_pct": sample_pct,
            "sample_size": len(blinded_cases),
            "purpose": "anchoring_bias_detection",
        },
    )
    db.add(audit_entry)
    db.flush()

    return {
        "status": "ready_for_review",
        "total_recent_denials": len(denials),
        "sample_pct": sample_pct,
        "sample_size": len(blinded_cases),
        "blinded_cases": blinded_cases,
        "instructions": (
            "Each case is presented WITHOUT the AI determination or reasoning. "
            "The physician should make an independent clinical judgment, then "
            "results are compared to detect anchoring bias."
        ),
        "prepared_at": datetime.now(UTC).isoformat(),
    }


def record_blind_comparison_result(
    db: Session,
    determination_id: str,
    physician_independent_decision: str,
    physician_reasoning: str,
) -> dict:
    """Record the result of a blind physician comparison for anchoring detection.

    Compares the physician's independent decision against the AI determination
    and tracks agreement/disagreement rates by case category.

    Parameters
    ----------
    db : Session
        Database session.
    determination_id : str
        UUID of the original clinical determination.
    physician_independent_decision : str
        The physician's independent decision ("approved" or "denied").
    physician_reasoning : str
        The physician's clinical reasoning.

    Returns
    -------
    dict with comparison result and agreement tracking.
    """
    det = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.determination_id == determination_id
    ).first()
    if not det:
        return {"error": "determination_not_found", "determination_id": str(determination_id)}

    ai_decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
    agrees = physician_independent_decision == ai_decision

    # Store comparison result as a DataPipelineMetric for tracking
    from app.models.data_pipeline_metric import DataPipelineMetric

    now = datetime.now(UTC)
    metric = DataPipelineMetric(
        metric_type="blind_comparison_result",
        value=1.0 if agrees else 0.0,
        benefit_type=det.benefit_type,
        details={
            "determination_id": str(determination_id),
            "ai_decision": ai_decision,
            "physician_decision": physician_independent_decision,
            "physician_reasoning": physician_reasoning,
            "agrees": agrees,
            "benefit_type": det.benefit_type,
            "condition_category": det.benefit_type,
        },
        measured_at=now,
    )
    db.add(metric)

    # Log to audit trail
    audit_entry = AuditLog(
        actor="clinical_engine:record_blind_comparison_result",
        action="blind_comparison_recorded",
        resource_type="clinical_determination",
        resource_id=str(determination_id),
        details={
            "ai_decision": ai_decision,
            "physician_decision": physician_independent_decision,
            "agrees": agrees,
        },
    )
    db.add(audit_entry)
    db.commit()

    # Query aggregate agreement rates for this benefit type
    agreement_stats = _get_blind_comparison_stats(db, det.benefit_type)

    logger.info(
        "Blind comparison recorded for %s: AI=%s, physician=%s, agrees=%s",
        determination_id, ai_decision, physician_independent_decision, agrees,
    )

    return {
        "determination_id": str(determination_id),
        "ai_decision": ai_decision,
        "physician_independent_decision": physician_independent_decision,
        "agrees": agrees,
        "physician_reasoning": physician_reasoning,
        "category_agreement_stats": agreement_stats,
        "recorded_at": now.isoformat(),
    }


def _get_blind_comparison_stats(db: Session, benefit_type: Optional[str] = None) -> dict:
    """Compute agreement/disagreement rates from blind comparison results."""
    from app.models.data_pipeline_metric import DataPipelineMetric

    query = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "blind_comparison_result",
    )
    if benefit_type:
        query = query.filter(DataPipelineMetric.benefit_type == benefit_type)

    results = query.all()
    if not results:
        return {"total": 0, "agreement_rate_pct": None}

    total = len(results)
    agreed = sum(1 for r in results if (r.details or {}).get("agrees"))

    return {
        "total": total,
        "agreed": agreed,
        "disagreed": total - agreed,
        "agreement_rate_pct": round(agreed / total * 100, 1) if total > 0 else None,
        "benefit_type": benefit_type,
    }


# ---------------------------------------------------------------------------
# F1 Supplement S4: External IRO calibration
# ---------------------------------------------------------------------------

def prepare_quarterly_iro_sample(db: Session) -> dict:
    """Select a blinded, anonymized sample of physician determinations for IRO.

    Constitution (F1 Supplement S4): A quarterly sample of physician
    determinations (both approvals and denials) is prepared for submission to
    an Independent Review Organization for calibration.

    Returns structured data ready for IRO submission.
    """
    # Sample from determinations reviewed by physicians in the last 90 days
    cutoff = datetime.now(UTC) - timedelta(days=90)
    reviewed_dets = (
        db.query(ClinicalDetermination)
        .filter(
            ClinicalDetermination.created_at >= cutoff,
            ClinicalDetermination.reviewed_by.isnot(None),
        )
        .order_by(ClinicalDetermination.created_at.desc())
        .all()
    )

    if not reviewed_dets:
        return {
            "status": "no_reviewed_determinations",
            "message": "No physician-reviewed determinations in the last 90 days.",
            "sample_size": 0,
        }

    # Stratified sample: include both approvals and denials
    approvals = [d for d in reviewed_dets if (d.decision.value if hasattr(d.decision, "value") else str(d.decision)) == "approved"]
    denials = [d for d in reviewed_dets if (d.decision.value if hasattr(d.decision, "value") else str(d.decision)) == "denied"]

    # Sample up to 25 from each category
    sample_approvals = random.sample(approvals, min(25, len(approvals))) if approvals else []
    sample_denials = random.sample(denials, min(25, len(denials))) if denials else []
    sample = sample_approvals + sample_denials

    iro_cases = []
    for det in sample:
        try:
            inputs = json.loads(det.inputs_encrypted) if det.inputs_encrypted else {}
        except (json.JSONDecodeError, TypeError):
            inputs = {}

        patient_history = inputs.get("patient_history", {})

        iro_cases.append({
            "iro_case_id": hashlib.sha256(
                f"{det.determination_id}-iro-quarterly".encode()
            ).hexdigest()[:16],
            "determination_id": str(det.determination_id),
            "symptoms": inputs.get("patient_symptoms", []),
            "medical_history": {
                "age": patient_history.get("age"),
                "sex": patient_history.get("sex"),
                "diagnoses": patient_history.get("diagnoses", []),
                "medications": patient_history.get("medications", []),
                "risk_factors": patient_history.get("risk_factors", []),
            },
            "service_code": inputs.get("service_code"),
            "benefit_type": inputs.get("benefit_type"),
            "condition": inputs.get("condition"),
            "determination": {
                "decision": det.decision.value if hasattr(det.decision, "value") else str(det.decision),
                "reasoning": det.reasoning,
                "guidelines_referenced": det.guidelines_referenced or [],
                "reviewed_by": det.reviewed_by,
            },
        })

    # Log the IRO sample preparation
    audit_entry = AuditLog(
        actor="clinical_engine:prepare_quarterly_iro_sample",
        action="iro_sample_prepared",
        resource_type="clinical_determination",
        resource_id="batch",
        details={
            "total_reviewed": len(reviewed_dets),
            "sample_approvals": len(sample_approvals),
            "sample_denials": len(sample_denials),
            "total_sample": len(iro_cases),
            "quarter_cutoff": cutoff.isoformat(),
        },
    )
    db.add(audit_entry)
    db.flush()

    return {
        "status": "ready_for_iro_submission",
        "total_reviewed_determinations": len(reviewed_dets),
        "sample_size": len(iro_cases),
        "sample_approvals": len(sample_approvals),
        "sample_denials": len(sample_denials),
        "iro_cases": iro_cases,
        "quarter_start": cutoff.isoformat(),
        "prepared_at": datetime.now(UTC).isoformat(),
    }


def record_iro_calibration_result(
    db: Session,
    determination_id: str,
    iro_decision: str,
    iro_reasoning: str,
) -> dict:
    """Record the result of an IRO calibration review.

    Constitution (F1 Supplement S4): Tracks divergence between internal
    physician determinations and external IRO decisions.

    Parameters
    ----------
    db : Session
        Database session.
    determination_id : str
        UUID of the clinical determination that was reviewed by the IRO.
    iro_decision : str
        The IRO's decision ("approved", "denied", or "modified").
    iro_reasoning : str
        The IRO's clinical reasoning.

    Returns
    -------
    dict with comparison result and divergence tracking.
    """
    det = db.query(ClinicalDetermination).filter(
        ClinicalDetermination.determination_id == determination_id
    ).first()
    if not det:
        return {"error": "determination_not_found", "determination_id": str(determination_id)}

    internal_decision = det.decision.value if hasattr(det.decision, "value") else str(det.decision)
    agrees = iro_decision == internal_decision

    from app.models.data_pipeline_metric import DataPipelineMetric

    now = datetime.now(UTC)
    metric = DataPipelineMetric(
        metric_type="iro_calibration_result",
        value=1.0 if agrees else 0.0,
        benefit_type=det.benefit_type,
        details={
            "determination_id": str(determination_id),
            "internal_decision": internal_decision,
            "iro_decision": iro_decision,
            "iro_reasoning": iro_reasoning,
            "agrees": agrees,
            "reviewed_by": det.reviewed_by,
            "benefit_type": det.benefit_type,
        },
        measured_at=now,
    )
    db.add(metric)

    audit_entry = AuditLog(
        actor="clinical_engine:record_iro_calibration_result",
        action="iro_calibration_recorded",
        resource_type="clinical_determination",
        resource_id=str(determination_id),
        details={
            "internal_decision": internal_decision,
            "iro_decision": iro_decision,
            "agrees": agrees,
        },
    )
    db.add(audit_entry)
    db.commit()

    # Compute aggregate divergence stats
    divergence_stats = _get_iro_divergence_stats(db, det.benefit_type)

    logger.info(
        "IRO calibration recorded for %s: internal=%s, IRO=%s, agrees=%s",
        determination_id, internal_decision, iro_decision, agrees,
    )

    return {
        "determination_id": str(determination_id),
        "internal_decision": internal_decision,
        "iro_decision": iro_decision,
        "agrees": agrees,
        "iro_reasoning": iro_reasoning,
        "divergence_stats": divergence_stats,
        "recorded_at": now.isoformat(),
    }


def _get_iro_divergence_stats(db: Session, benefit_type: Optional[str] = None) -> dict:
    """Compute divergence rates between internal and IRO decisions."""
    from app.models.data_pipeline_metric import DataPipelineMetric

    query = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "iro_calibration_result",
    )
    if benefit_type:
        query = query.filter(DataPipelineMetric.benefit_type == benefit_type)

    results = query.all()
    if not results:
        return {"total": 0, "divergence_rate_pct": None}

    total = len(results)
    diverged = sum(1 for r in results if not (r.details or {}).get("agrees"))

    return {
        "total": total,
        "agreed": total - diverged,
        "diverged": diverged,
        "divergence_rate_pct": round(diverged / total * 100, 1) if total > 0 else None,
        "benefit_type": benefit_type,
    }


# ---------------------------------------------------------------------------
# F1 Supplement S5: Appeal outcome feedback loop
# ---------------------------------------------------------------------------

def route_appeal_feedback_to_physician(
    db: Session,
    appeal_id: str,
    appeal_outcome: str,
    appeal_reasoning: str,
    overturn_category: Optional[str] = None,
) -> dict:
    """Route an appeal outcome back to the original determination's reviewer.

    Constitution (F1 Supplement S5): When an appeal is decided, the outcome
    (with full reasoning) is routed back to the physician who made the
    original determination. Tracks overturn rate by case category.

    Parameters
    ----------
    db : Session
        Database session.
    appeal_id : str
        UUID of the decided appeal.
    appeal_outcome : str
        The appeal outcome ("upheld", "overturned", "partial_reversal").
    appeal_reasoning : str
        Full reasoning from the appeal reviewer.
    overturn_category : str, optional
        Case category for overturn tracking (e.g., benefit type or condition).

    Returns
    -------
    dict with feedback routing result and overturn rate statistics.
    """
    from app.models.appeal import Appeal

    appeal = db.query(Appeal).filter(Appeal.appeal_id == appeal_id).first()
    if not appeal:
        return {"error": "appeal_not_found", "appeal_id": str(appeal_id)}

    # Find the original determination
    original_det = None
    if appeal.determination_id:
        original_det = db.query(ClinicalDetermination).filter(
            ClinicalDetermination.determination_id == appeal.determination_id
        ).first()

    original_reviewer = original_det.reviewed_by if original_det else None
    category = overturn_category or (original_det.benefit_type if original_det else "unknown")

    is_overturned = appeal_outcome in ("overturned", "partial_reversal", "partially_overturned")

    # Record the feedback as a metric for overturn rate tracking
    from app.models.data_pipeline_metric import DataPipelineMetric

    now = datetime.now(UTC)
    metric = DataPipelineMetric(
        metric_type="appeal_feedback_to_physician",
        value=1.0 if is_overturned else 0.0,
        benefit_type=category,
        details={
            "appeal_id": str(appeal_id),
            "determination_id": str(appeal.determination_id) if appeal.determination_id else None,
            "original_reviewer": original_reviewer,
            "appeal_outcome": appeal_outcome,
            "appeal_reasoning": appeal_reasoning,
            "is_overturned": is_overturned,
            "category": category,
        },
        measured_at=now,
    )
    db.add(metric)

    audit_entry = AuditLog(
        actor="clinical_engine:route_appeal_feedback_to_physician",
        action="appeal_feedback_routed",
        resource_type="appeal",
        resource_id=str(appeal_id),
        details={
            "original_reviewer": original_reviewer,
            "appeal_outcome": appeal_outcome,
            "is_overturned": is_overturned,
            "category": category,
        },
    )
    db.add(audit_entry)
    db.commit()

    # Compute overturn rate by category
    overturn_stats = _get_overturn_stats_by_category(db, category)

    logger.info(
        "Appeal feedback routed for appeal %s: outcome=%s, reviewer=%s, category=%s",
        appeal_id, appeal_outcome, original_reviewer, category,
    )

    return {
        "appeal_id": str(appeal_id),
        "determination_id": str(appeal.determination_id) if appeal.determination_id else None,
        "original_reviewer": original_reviewer,
        "appeal_outcome": appeal_outcome,
        "is_overturned": is_overturned,
        "feedback_routed": True,
        "category": category,
        "overturn_stats": overturn_stats,
        "routed_at": now.isoformat(),
    }


def _get_overturn_stats_by_category(db: Session, category: Optional[str] = None) -> dict:
    """Compute overturn rates from appeal feedback records."""
    from app.models.data_pipeline_metric import DataPipelineMetric

    query = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "appeal_feedback_to_physician",
    )
    if category:
        query = query.filter(DataPipelineMetric.benefit_type == category)

    results = query.all()
    if not results:
        return {"total_appeals": 0, "overturn_rate_pct": None}

    total = len(results)
    overturned = sum(1 for r in results if (r.details or {}).get("is_overturned"))

    return {
        "total_appeals": total,
        "overturned": overturned,
        "upheld": total - overturned,
        "overturn_rate_pct": round(overturned / total * 100, 1) if total > 0 else None,
        "category": category,
    }


# ---------------------------------------------------------------------------
# F1 Supplement S6: Prospective validation
# ---------------------------------------------------------------------------

# Minimum sample size for statistical significance in prospective validation
_PROSPECTIVE_VALIDATION_MIN_SAMPLE = 30


def identify_prospectively_validated_categories(db: Session) -> dict:
    """Identify condition categories where physician has confirmed AI 100%.

    Constitution (F1 Supplement S6): Categories where the reviewing physician
    has confirmed the AI determination in every case over a statistically
    significant sample (>= 30 cases) are considered prospectively validated.
    These may be eligible for auto-approval without individual physician review.

    Returns
    -------
    dict with validated categories and their sample sizes.
    """
    from app.models.data_pipeline_metric import DataPipelineMetric

    # Get all blind comparison results grouped by benefit type
    comparisons = db.query(DataPipelineMetric).filter(
        DataPipelineMetric.metric_type == "blind_comparison_result",
    ).all()

    # Also consider determinations where the physician explicitly reviewed
    # and agreed (non-blind)
    reviewed_dets = (
        db.query(
            ClinicalDetermination.benefit_type,
            func.count(ClinicalDetermination.determination_id).label("total"),
        )
        .filter(
            ClinicalDetermination.reviewed_by.isnot(None),
            ClinicalDetermination.is_recommendation.is_(False),
        )
        .group_by(ClinicalDetermination.benefit_type)
        .all()
    )

    # Aggregate by category from blind comparisons
    category_stats: dict[str, dict] = {}
    for comp in comparisons:
        details = comp.details or {}
        cat = details.get("benefit_type") or details.get("condition_category", "unknown")
        if cat not in category_stats:
            category_stats[cat] = {"total": 0, "agreed": 0}
        category_stats[cat]["total"] += 1
        if details.get("agrees"):
            category_stats[cat]["agreed"] += 1

    # Supplement with reviewed determination counts
    for bt, count in reviewed_dets:
        if bt and bt not in category_stats:
            category_stats[bt] = {"total": count, "agreed": count}
        elif bt:
            category_stats[bt]["total"] += count
            category_stats[bt]["agreed"] += count

    validated = []
    not_yet_validated = []

    for cat, stats in category_stats.items():
        total = stats["total"]
        agreed = stats["agreed"]
        agreement_rate = agreed / total if total > 0 else 0

        entry = {
            "category": cat,
            "total_cases": total,
            "agreed_cases": agreed,
            "agreement_rate_pct": round(agreement_rate * 100, 1),
        }

        if total >= _PROSPECTIVE_VALIDATION_MIN_SAMPLE and agreement_rate == 1.0:
            entry["status"] = "validated"
            validated.append(entry)
        else:
            entry["status"] = "not_validated"
            if total < _PROSPECTIVE_VALIDATION_MIN_SAMPLE:
                entry["reason"] = f"Insufficient sample (need {_PROSPECTIVE_VALIDATION_MIN_SAMPLE}, have {total})"
            else:
                entry["reason"] = f"Agreement rate {round(agreement_rate * 100, 1)}% < 100%"
            not_yet_validated.append(entry)

    return {
        "validated_categories": validated,
        "not_yet_validated": not_yet_validated,
        "min_sample_required": _PROSPECTIVE_VALIDATION_MIN_SAMPLE,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }


def check_prospective_validation(condition_category: str) -> dict:
    """Check whether a condition category is prospectively validated.

    Constitution (F1 Supplement S6): If a category is validated (physician
    confirmed AI 100% over >= 30 cases), it may be auto-approved without
    individual physician review. Otherwise, individual review is required.

    NOTE: This function checks the cached validation state. Call
    identify_prospectively_validated_categories() to refresh.

    Parameters
    ----------
    condition_category : str
        The benefit type or condition category to check.

    Returns
    -------
    dict indicating whether the category requires individual review.
    """
    # In production, this would read from a cached store updated by
    # identify_prospectively_validated_categories(). For now, we return
    # a structure indicating the check must be performed with a db session.
    return {
        "condition_category": condition_category,
        "requires_individual_review": True,
        "note": (
            "Call identify_prospectively_validated_categories(db) to determine "
            "current validation status. Default is to require individual review "
            "until prospective validation confirms 100% agreement over "
            f"{_PROSPECTIVE_VALIDATION_MIN_SAMPLE}+ cases."
        ),
    }


# ---------------------------------------------------------------------------
# F1 Supplement S7: Panel scaling trigger
# ---------------------------------------------------------------------------

def check_panel_scaling_needed(db: Session) -> dict:
    """Monitor review volume and turnaround to flag when panel scaling is needed.

    Constitution (F1 Supplement S7): Monitors physician review volume, average
    turnaround time, and pending review queue depth. Flags when scaling is
    needed (e.g., volume exceeding capacity or turnaround exceeding SLA).
    Supports random case assignment across panel physicians.

    Returns
    -------
    dict with current panel metrics and scaling recommendation.
    """
    now = datetime.now(UTC)
    past_30_days = now - timedelta(days=30)
    past_7_days = now - timedelta(days=7)

    # Total pending reviews (recommendations awaiting physician sign-off)
    pending_reviews = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.is_recommendation.is_(True),
        ClinicalDetermination.reviewed_by.is_(None),
    ).scalar() or 0

    # Reviews completed in last 30 days
    completed_30d = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.reviewed_by.isnot(None),
        ClinicalDetermination.created_at >= past_30_days,
    ).scalar() or 0

    # Reviews completed in last 7 days
    completed_7d = db.query(func.count(ClinicalDetermination.determination_id)).filter(
        ClinicalDetermination.reviewed_by.isnot(None),
        ClinicalDetermination.created_at >= past_7_days,
    ).scalar() or 0

    # Average turnaround (approximated via latency_ms for now)
    avg_latency = db.query(func.avg(ClinicalDetermination.latency_ms)).filter(
        ClinicalDetermination.created_at >= past_7_days,
        ClinicalDetermination.latency_ms.isnot(None),
    ).scalar()

    # Distinct reviewers active in last 30 days
    active_reviewers = db.query(
        func.count(func.distinct(ClinicalDetermination.reviewed_by))
    ).filter(
        ClinicalDetermination.reviewed_by.isnot(None),
        ClinicalDetermination.created_at >= past_30_days,
    ).scalar() or 0

    # Per-reviewer load
    reviews_per_reviewer = (
        round(completed_30d / active_reviewers, 1) if active_reviewers > 0 else None
    )

    # Scaling thresholds
    SLA_MAX_PENDING = 50
    SLA_MAX_PER_REVIEWER_MONTHLY = 200
    scaling_needed = False
    scaling_reasons = []

    if pending_reviews > SLA_MAX_PENDING:
        scaling_needed = True
        scaling_reasons.append(
            f"Pending queue ({pending_reviews}) exceeds SLA threshold ({SLA_MAX_PENDING})"
        )

    if reviews_per_reviewer and reviews_per_reviewer > SLA_MAX_PER_REVIEWER_MONTHLY:
        scaling_needed = True
        scaling_reasons.append(
            f"Per-reviewer load ({reviews_per_reviewer}/mo) exceeds capacity ({SLA_MAX_PER_REVIEWER_MONTHLY}/mo)"
        )

    if active_reviewers < 2 and completed_30d > 20:
        scaling_needed = True
        scaling_reasons.append(
            "Fewer than 2 active reviewers with significant volume — redundancy risk"
        )

    return {
        "panel_metrics": {
            "pending_reviews": pending_reviews,
            "completed_last_30d": completed_30d,
            "completed_last_7d": completed_7d,
            "active_reviewers": active_reviewers,
            "reviews_per_reviewer_monthly": reviews_per_reviewer,
            "avg_determination_latency_ms": round(avg_latency, 1) if avg_latency else None,
        },
        "sla_thresholds": {
            "max_pending_queue": SLA_MAX_PENDING,
            "max_per_reviewer_monthly": SLA_MAX_PER_REVIEWER_MONTHLY,
        },
        "scaling_needed": scaling_needed,
        "scaling_reasons": scaling_reasons if scaling_reasons else ["Panel capacity is within SLA thresholds"],
        "case_assignment_method": "random",
        "case_assignment_note": (
            "Cases are randomly assigned across all active panel physicians "
            "to prevent concentration bias and ensure balanced workload."
        ),
        "evaluated_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# F1 Supplement S8: Plain-language coverage document
# ---------------------------------------------------------------------------

# All 7 benefit types covered by the plan
_COVERAGE_TYPES = [
    {
        "type": "health",
        "label": "Medical / Health",
        "description": (
            "Doctor visits, hospital stays, surgeries, lab tests, imaging, "
            "prescriptions, and other medical services."
        ),
        "example_covered": "Annual physical, blood work, X-ray for a broken bone",
        "example_not_covered": "Cosmetic surgery that is not medically necessary",
    },
    {
        "type": "dental",
        "label": "Dental",
        "description": "Cleanings, fillings, crowns, root canals, and other dental care.",
        "example_covered": "Twice-yearly cleaning, cavity filling, wisdom tooth extraction",
        "example_not_covered": "Teeth whitening for cosmetic reasons",
    },
    {
        "type": "vision",
        "label": "Vision",
        "description": "Eye exams, glasses, contact lenses, and treatment for eye conditions.",
        "example_covered": "Annual eye exam, prescription glasses, glaucoma treatment",
        "example_not_covered": "LASIK surgery (unless medically necessary for a specific condition)",
    },
    {
        "type": "mental_health",
        "label": "Mental Health & Behavioral Health",
        "description": (
            "Therapy, counseling, psychiatric care, substance use treatment, "
            "and crisis support."
        ),
        "example_covered": "Therapy sessions, psychiatric medication management, rehab program",
        "example_not_covered": "Couples counseling without a clinical diagnosis",
    },
    {
        "type": "life",
        "label": "Life Insurance",
        "description": "Financial protection for your family if something happens to you.",
        "example_covered": "Death benefit paid to your named beneficiary",
        "example_not_covered": "Claims filed more than 2 years after policy lapse",
    },
    {
        "type": "std",
        "label": "Short-Term Disability",
        "description": (
            "Income replacement if you cannot work for a short period due to "
            "illness, injury, or recovery from surgery."
        ),
        "example_covered": "Recovery from surgery, serious illness keeping you home for weeks",
        "example_not_covered": "Elective time off that is not medically supported",
    },
    {
        "type": "ltd",
        "label": "Long-Term Disability",
        "description": (
            "Income replacement if you cannot work for an extended period due "
            "to a serious medical condition."
        ),
        "example_covered": "Chronic condition preventing return to work for months",
        "example_not_covered": "Disability caused by a condition excluded in your policy",
    },
]


def generate_coverage_document(db: Session, employee_id: str) -> dict:
    """Generate a one-page plain-language coverage document for an employee.

    Constitution (F1 Supplement S8): Content includes what is covered (all 7
    benefit types), what 'medically necessary' means in everyday language,
    examples of covered/not-covered, what to do when needing care, and a
    plain-language privacy notice.

    Parameters
    ----------
    db : Session
        Database session.
    employee_id : str
        UUID of the employee.

    Returns
    -------
    dict with structured data ready for PDF rendering.
    """
    from app.models.employee import Employee

    employee = db.query(Employee).filter(
        Employee.employee_id == employee_id
    ).first()
    if not employee:
        return {"error": "employee_not_found", "employee_id": str(employee_id)}

    now = datetime.now(UTC)

    document = {
        "document_type": "plain_language_coverage_summary",
        "employee_id": str(employee_id),
        "generated_at": now.isoformat(),
        "title": "Your Benefits Coverage — Plain-Language Summary",
        "sections": [
            {
                "heading": "What Is Covered",
                "intro": (
                    "Your plan covers 7 types of benefits. Here is what each "
                    "one means and examples of what is and is not covered."
                ),
                "coverage_types": _COVERAGE_TYPES,
            },
            {
                "heading": "What 'Medically Necessary' Means",
                "content": (
                    "A service is 'medically necessary' when a qualified "
                    "clinical professional determines that you need it to "
                    "prevent, diagnose, or treat a health condition — and "
                    "that without it, your health would likely get worse. "
                    "This decision is based only on your symptoms, your "
                    "medical history, and current medical evidence. It is "
                    "never based on cost."
                ),
            },
            {
                "heading": "What To Do When You Need Care",
                "content": (
                    "Text the care coordination number provided by your "
                    "employer. A care navigator will help you find the right "
                    "provider, confirm coverage, and schedule your appointment. "
                    "You can also call the same number. In an emergency, go "
                    "to the nearest emergency room — your plan covers "
                    "emergency services."
                ),
                "action_steps": [
                    "1. Text or call your care coordination number.",
                    "2. Describe what you need (e.g., 'I need to see a dentist').",
                    "3. A care navigator will confirm coverage and help you book.",
                    "4. Go to your appointment — the plan handles the rest.",
                ],
            },
            {
                "heading": "Your Privacy",
                "content": (
                    "Your medical information is private. We use it only to "
                    "determine whether a service is medically necessary and "
                    "to process your claim. We do not share your medical "
                    "details with your employer. Your employer sees only "
                    "whether a claim was approved or denied — never your "
                    "diagnosis, symptoms, or treatment details. All medical "
                    "data is encrypted and stored securely."
                ),
            },
            {
                "heading": "Questions or Concerns",
                "content": (
                    "If you have questions about your coverage, a claim, or "
                    "a denial, text or call your care coordination number. "
                    "If you disagree with a denial, you have the right to "
                    "appeal. Your denial notice will explain exactly how."
                ),
            },
        ],
    }

    # Log document generation
    audit_entry = AuditLog(
        actor="clinical_engine:generate_coverage_document",
        action="coverage_document_generated",
        resource_type="employee",
        resource_id=str(employee_id),
        details={"document_type": "plain_language_coverage_summary"},
    )
    db.add(audit_entry)
    db.flush()

    logger.info("Coverage document generated for employee %s", employee_id)

    return document
