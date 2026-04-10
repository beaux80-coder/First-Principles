"""Emergent care detection — prudent layperson standard.

Legal basis: 42 USC §300gg-19a and related ACA emergency services rules.
"Emergency medical condition" is defined as a medical condition manifesting
itself by acute symptoms of sufficient severity (including severe pain) such
that a prudent layperson, who possesses an average knowledge of health and
medicine, could reasonably expect the absence of immediate medical attention
to result in placing the health of the individual in serious jeopardy.

This module classifies a claim (or a care request) as emergent so that
Layer 3 (claims verification) can bypass the "unrouted = deny" rule and
pay the claim regardless of whether the employee used the platform first.

The classifier is deliberately permissive: false positives here mean we
pay a claim we didn't need to pay emergently (small cost), while false
negatives mean we deny legitimate emergency care (huge legal liability
and, more importantly, real harm to the employee).

Signals (any one triggers emergent classification):
  1. Place-of-service code indicating ER, ambulance, etc.
  2. Procedure code range indicating ER E&M or emergent surgery
  3. Diagnosis code indicating a red-flag condition (MI, stroke, sepsis,
     anaphylaxis, severe trauma, etc.)
  4. Employee self-attestation ("I thought it was an emergency")
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional


logger = logging.getLogger(__name__)


# CMS Place of Service codes that indicate emergent care.
# Reference: https://www.cms.gov/medicare/coding-billing/place-of-service-codes
EMERGENT_PLACE_OF_SERVICE_CODES: frozenset[str] = frozenset({
    "23",  # Emergency Room — Hospital
    "41",  # Ambulance — Land
    "42",  # Ambulance — Air or Water
})


# CPT Evaluation & Management codes for emergency department visits.
# 99281-99285 cover the five levels of ED E&M encounters.
EMERGENT_EM_CODES: frozenset[str] = frozenset({
    "99281", "99282", "99283", "99284", "99285",
    "99288",  # Physician direction of emergency medical systems
    "99291", "99292",  # Critical care services
})


# ICD-10 prefixes associated with conditions that, if present, justify
# emergent treatment under the prudent layperson standard. A prefix match
# is sufficient — e.g. "I21" catches all acute myocardial infarction
# subtypes (I21.0, I21.01, I21.4, I21.9, etc.).
RED_FLAG_ICD10_PREFIXES: tuple[str, ...] = (
    # Cardiovascular emergencies
    "I21",  # Acute myocardial infarction
    "I22",  # Subsequent MI
    "I46",  # Cardiac arrest
    "I50.2", "I50.3", "I50.4",  # Acute heart failure
    "I71",  # Aortic aneurysm/dissection
    # Cerebrovascular emergencies
    "I60", "I61", "I62", "I63",  # Hemorrhagic + ischemic stroke
    "G45",  # TIA
    # Pulmonary emergencies
    "I26",  # Pulmonary embolism
    "J81",  # Pulmonary edema
    "J96.0",  # Acute respiratory failure
    # Shock and sepsis
    "A40", "A41",  # Streptococcal/other sepsis
    "R57",  # Shock
    "R65.2",  # Severe sepsis
    # Anaphylaxis and severe allergic reactions
    "T78.0", "T78.2", "T80.5", "T88.6",
    # Trauma — major categories
    "S02",  # Skull fracture
    "S06",  # Traumatic brain injury
    "S12",  # Cervical fracture
    "S14",  # Spinal cord injury (cervical)
    "S24",  # Spinal cord injury (thoracic)
    "S26",  # Cardiac contusion/injury
    "S27",  # Thoracic organ injury
    "S35",  # Abdominal great vessel injury
    "S36",  # Intra-abdominal organ injury
    "T07",  # Unspecified multiple injuries
    # Acute surgical abdomen
    "K35",  # Acute appendicitis
    "K56",  # Intestinal obstruction
    "K65",  # Peritonitis
    # Obstetric emergencies
    "O15",  # Eclampsia
    "O44.1",  # Placenta previa with hemorrhage
    "O45",  # Placental abruption
    "O72",  # Postpartum hemorrhage
    # Pediatric and infectious emergencies
    "G03",  # Meningitis
    "G04",  # Encephalitis
    "A39",  # Meningococcal infection
    # Mental health emergencies
    "R45.81",  # Suicidal ideation
    "R45.82",  # Homicidal ideation
    "T14.91",  # Suicide attempt
    "F10.23",  # Alcohol withdrawal with seizure
    "F11.23",  # Opioid withdrawal
    "F13.23",  # Sedative withdrawal with seizure
    # Other
    "R40.2",  # Coma
    "R56",  # Convulsions / seizures
    "D65",  # DIC / uncontrolled bleeding
)


def is_emergent(
    *,
    place_of_service: Optional[str] = None,
    procedure_codes: Optional[Iterable[str]] = None,
    diagnosis_codes: Optional[Iterable[str]] = None,
    patient_attested_emergency: bool = False,
) -> tuple[bool, dict]:
    """Classify a claim as emergent under the prudent layperson standard.

    Returns a tuple: (is_emergent, signals). `signals` is a structured
    dict recording which rule(s) fired and with which values, for audit.
    Any single positive signal is sufficient — the classifier is
    deliberately permissive because false negatives are far more costly
    than false positives.
    """
    signals: dict = {
        "place_of_service": None,
        "procedure_codes": [],
        "diagnosis_codes": [],
        "patient_attested": bool(patient_attested_emergency),
        "rules_fired": [],
    }

    fired = False

    # ---- Signal 1: Place of service ----
    if place_of_service and str(place_of_service).strip() in EMERGENT_PLACE_OF_SERVICE_CODES:
        signals["place_of_service"] = str(place_of_service).strip()
        signals["rules_fired"].append("place_of_service")
        fired = True

    # ---- Signal 2: Procedure codes ----
    if procedure_codes:
        matching_procs = [
            str(code).strip() for code in procedure_codes
            if str(code).strip() in EMERGENT_EM_CODES
        ]
        if matching_procs:
            signals["procedure_codes"] = matching_procs
            signals["rules_fired"].append("procedure_codes")
            fired = True

    # ---- Signal 3: Diagnosis codes ----
    if diagnosis_codes:
        normalized = [str(c).strip().upper() for c in diagnosis_codes if c]
        matching_dx: list[str] = []
        for code in normalized:
            for prefix in RED_FLAG_ICD10_PREFIXES:
                if code.startswith(prefix.upper()):
                    matching_dx.append(code)
                    break
        if matching_dx:
            signals["diagnosis_codes"] = matching_dx
            signals["rules_fired"].append("diagnosis_codes")
            fired = True

    # ---- Signal 4: Patient self-attestation ----
    if patient_attested_emergency:
        signals["rules_fired"].append("patient_attestation")
        fired = True

    return fired, signals


def classify_claim(claim) -> tuple[bool, dict]:
    """Classify a Claim ORM instance.

    Pulls POS, procedure codes, and diagnosis codes from the claim's
    associated Service record (when available) and any JSON fields the
    claim may carry. Self-attestation is read from coding_validation
    metadata if the provider or employee flagged the visit as emergent.
    """
    place_of_service: Optional[str] = None
    procedure_codes: list[str] = []
    diagnosis_codes: list[str] = []

    # Pull from the Service record, if linked.
    service = getattr(claim, "service", None)
    if service is not None:
        cpt = getattr(service, "cpt_code", None) or getattr(service, "code", None)
        if cpt:
            procedure_codes.append(str(cpt))
        pos = getattr(service, "place_of_service", None)
        if pos:
            place_of_service = str(pos)

    # Pull from claim-level metadata (coding_validation is a JSON blob
    # populated by the adjudication pipeline or at claim submission).
    coding = getattr(claim, "coding_validation", None) or {}
    if isinstance(coding, dict):
        if not place_of_service and coding.get("place_of_service"):
            place_of_service = str(coding["place_of_service"])
        for code in coding.get("procedure_codes", []) or []:
            procedure_codes.append(str(code))
        for dx in coding.get("diagnosis_codes", []) or []:
            diagnosis_codes.append(str(dx))

    attested = bool(
        coding.get("patient_attested_emergency")
        if isinstance(coding, dict)
        else False
    )

    return is_emergent(
        place_of_service=place_of_service,
        procedure_codes=procedure_codes,
        diagnosis_codes=diagnosis_codes,
        patient_attested_emergency=attested,
    )
