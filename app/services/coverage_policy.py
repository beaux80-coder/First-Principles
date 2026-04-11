"""Universal Coverage Policy.

The Beneflex plan covers the same services for every employer and every
employee. There are no employer-level coverage toggles, no optional
benefit packages, no tiered plans, and no employer-specific exclusion
categories. The policy is one document, one set of rules, applied the
same way everywhere.

Policy (verbatim, for audit and legal reference):

  The plan covers all services that are medically necessary per
  evidence-based clinical guidelines from recognized peer-reviewed
  authorities (USPSTF, CMS NCDs/LCDs, Cochrane systematic reviews,
  specialty-society guidelines, and the peer-reviewed literature).

  The ONLY exclusions are:
    1. Cosmetic services with no medical indication.
    2. Experimental or investigational services with no peer-reviewed
       evidence base.

  If the system is uncertain whether either exclusion applies, the
  service is APPROVED.

Every denial emitted by this module cites (a) the specific exclusion
category, (b) the evidence supporting the classification, and (c) the
plain-language path to reversal (typically: documentation of a medical
indication, or citation of a peer-reviewed guideline).

This module is the single source of truth for coverage policy. The
clinical engine calls it after matching a service against evidence-
based guidelines; the result contributes to the immutable audit log
but does NOT alter the denial-notice format, appeal path, or any
ERISA compliance logic — those are handled downstream and are
unchanged by this module.
"""

from __future__ import annotations

import logging
from typing import Optional


logger = logging.getLogger(__name__)


# Version string stamped onto every evaluation for audit traceability.
# Bump when any rule below is added, removed, or materially changed.
POLICY_VERSION = "universal-coverage-policy-v1"


# Human-readable summary safe to expose through API responses, denial
# notices, and audit reports. This is the same statement shown to
# employees and employers.
UNIVERSAL_POLICY_STATEMENT: str = (
    "The plan covers every service that is medically necessary per "
    "evidence-based clinical guidelines. The only exclusions are "
    "(1) cosmetic services with no medical indication and "
    "(2) experimental or investigational services with no peer-reviewed "
    "evidence base. If the evidence is uncertain, the service is approved. "
    "There are no employer-specific coverage toggles — the policy is the "
    "same for every employee in every employer group."
)


# ---------------------------------------------------------------------------
# Exclusion 1: Cosmetic services with no medical indication
# ---------------------------------------------------------------------------
# A curated set of CPT codes whose primary indication is aesthetic. Each
# code is paired with the list of clinical indications that would flip
# the service from "cosmetic" to "medically necessary". If any indication
# is present in the patient context, the service is NOT excluded and
# falls through to the normal evidence-based review.
#
# The list is intentionally conservative. We only add a code when:
#   • The AMA CPT description identifies it as primarily cosmetic, OR
#   • It appears in CMS's list of non-covered cosmetic procedures.
#
# Uncertainty rule: if a service code is NOT on this list, we do NOT
# treat it as cosmetic. Unknown = covered.
COSMETIC_PROCEDURES: dict[str, dict] = {
    # Dermatologic / skin aesthetic procedures
    "15775": {
        "description": "Punch graft for hair transplant (1-15 grafts)",
        "medical_indications": [
            "burn", "alopecia areata", "alopecia totalis",
            "cicatricial alopecia", "trauma scarring", "scarring alopecia",
            "chemotherapy alopecia",
        ],
    },
    "15776": {
        "description": "Punch graft for hair transplant (>15 grafts)",
        "medical_indications": [
            "burn", "alopecia areata", "alopecia totalis",
            "cicatricial alopecia", "trauma scarring", "scarring alopecia",
            "chemotherapy alopecia",
        ],
    },
    "15780": {
        "description": "Dermabrasion, total face",
        "medical_indications": [
            "severe acne scarring", "rhinophyma", "traumatic scarring",
            "congenital deformity",
        ],
    },
    "15781": {
        "description": "Dermabrasion, segmental face",
        "medical_indications": [
            "severe acne scarring", "rhinophyma", "traumatic scarring",
            "congenital deformity",
        ],
    },
    "15782": {
        "description": "Dermabrasion, regional (non-facial)",
        "medical_indications": [
            "severe acne scarring", "rhinophyma", "traumatic scarring",
            "congenital deformity",
        ],
    },
    "15783": {
        "description": "Dermabrasion, superficial",
        "medical_indications": [
            "severe acne scarring", "rhinophyma", "traumatic scarring",
            "congenital deformity",
        ],
    },
    "15788": {
        "description": "Chemical peel, epidermal facial",
        "medical_indications": [
            "actinic keratoses", "severe acne scarring", "trauma scarring",
        ],
    },
    "15789": {
        "description": "Chemical peel, dermal facial",
        "medical_indications": [
            "actinic keratoses", "severe acne scarring", "trauma scarring",
        ],
    },
    "15792": {
        "description": "Chemical peel, dermal non-facial",
        "medical_indications": [
            "actinic keratoses", "severe acne scarring", "trauma scarring",
        ],
    },
    "15793": {
        "description": "Chemical peel, regional other",
        "medical_indications": [
            "actinic keratoses", "severe acne scarring", "trauma scarring",
        ],
    },
    # Soft-tissue augmentation / fillers
    "11950": {
        "description": "Subcutaneous filler injection (≤1 cc)",
        "medical_indications": [
            "hiv lipoatrophy", "facial lipoatrophy", "post-traumatic deformity",
            "reconstructive",
        ],
    },
    "11951": {
        "description": "Subcutaneous filler injection (1.1-5.0 cc)",
        "medical_indications": [
            "hiv lipoatrophy", "facial lipoatrophy", "post-traumatic deformity",
            "reconstructive",
        ],
    },
    "11952": {
        "description": "Subcutaneous filler injection (5.1-10.0 cc)",
        "medical_indications": [
            "hiv lipoatrophy", "facial lipoatrophy", "post-traumatic deformity",
            "reconstructive",
        ],
    },
    "11954": {
        "description": "Subcutaneous filler injection (>10.0 cc)",
        "medical_indications": [
            "hiv lipoatrophy", "facial lipoatrophy", "post-traumatic deformity",
            "reconstructive",
        ],
    },
    # Face lift / rhytidectomy
    "15824": {
        "description": "Rhytidectomy (forehead)",
        "medical_indications": [
            "trauma", "burn reconstruction", "congenital deformity",
            "tumor resection reconstruction",
        ],
    },
    "15825": {
        "description": "Rhytidectomy (neck with platysmal tightening)",
        "medical_indications": [
            "trauma", "burn reconstruction", "congenital deformity",
            "tumor resection reconstruction",
        ],
    },
    "15826": {
        "description": "Rhytidectomy (glabellar frown lines)",
        "medical_indications": [
            "trauma", "burn reconstruction", "congenital deformity",
            "tumor resection reconstruction",
        ],
    },
    "15828": {
        "description": "Rhytidectomy (cheek, chin, neck)",
        "medical_indications": [
            "trauma", "burn reconstruction", "congenital deformity",
            "tumor resection reconstruction",
        ],
    },
    "15829": {
        "description": "Rhytidectomy (SMAS flap)",
        "medical_indications": [
            "trauma", "burn reconstruction", "congenital deformity",
            "tumor resection reconstruction",
        ],
    },
    # Liposuction (suction-assisted lipectomy)
    "15876": {
        "description": "Suction-assisted lipectomy (head, neck)",
        "medical_indications": [
            "lymphedema", "lipedema", "gender affirmation", "lipoma",
            "post-bariatric reconstruction",
        ],
    },
    "15877": {
        "description": "Suction-assisted lipectomy (trunk)",
        "medical_indications": [
            "lymphedema", "lipedema", "gender affirmation",
            "post-bariatric reconstruction",
        ],
    },
    "15878": {
        "description": "Suction-assisted lipectomy (upper extremity)",
        "medical_indications": [
            "lymphedema", "lipedema", "gender affirmation",
            "post-bariatric reconstruction",
        ],
    },
    "15879": {
        "description": "Suction-assisted lipectomy (lower extremity)",
        "medical_indications": [
            "lymphedema", "lipedema", "gender affirmation",
            "post-bariatric reconstruction",
        ],
    },
    # Breast augmentation (cosmetic indications excluded; reconstructive covered)
    "19325": {
        "description": "Breast augmentation with implant",
        "medical_indications": [
            "mastectomy", "breast cancer", "reconstruction", "congenital absence",
            "tuberous breast", "poland syndrome", "gender affirmation",
        ],
    },
    "19340": {
        "description": "Immediate insertion of breast prosthesis",
        "medical_indications": [
            "mastectomy", "breast cancer", "reconstruction", "congenital absence",
            "gender affirmation",
        ],
    },
    "19342": {
        "description": "Delayed insertion of breast prosthesis",
        "medical_indications": [
            "mastectomy", "breast cancer", "reconstruction", "congenital absence",
            "gender affirmation",
        ],
    },
    # Rhinoplasty (cosmetic vs. functional)
    "30400": {
        "description": "Rhinoplasty, primary; lateral and alar cartilages",
        "medical_indications": [
            "nasal obstruction", "septal deviation", "trauma", "fracture",
            "cleft lip", "cleft palate", "congenital deformity", "breathing",
            "obstructive sleep apnea",
        ],
    },
    "30410": {
        "description": "Rhinoplasty, primary; complete",
        "medical_indications": [
            "nasal obstruction", "septal deviation", "trauma", "fracture",
            "cleft lip", "cleft palate", "congenital deformity", "breathing",
            "obstructive sleep apnea",
        ],
    },
    "30420": {
        "description": "Rhinoplasty, primary; including major septal repair",
        "medical_indications": [
            "nasal obstruction", "septal deviation", "trauma", "fracture",
            "cleft lip", "cleft palate", "congenital deformity", "breathing",
            "obstructive sleep apnea",
        ],
    },
    "30430": {
        "description": "Rhinoplasty, secondary; minor revision",
        "medical_indications": [
            "nasal obstruction", "septal deviation", "trauma", "fracture",
            "congenital deformity", "breathing", "obstructive sleep apnea",
        ],
    },
    "30435": {
        "description": "Rhinoplasty, secondary; intermediate revision",
        "medical_indications": [
            "nasal obstruction", "septal deviation", "trauma", "fracture",
            "congenital deformity", "breathing", "obstructive sleep apnea",
        ],
    },
    "30450": {
        "description": "Rhinoplasty, secondary; major revision",
        "medical_indications": [
            "nasal obstruction", "septal deviation", "trauma", "fracture",
            "congenital deformity", "breathing", "obstructive sleep apnea",
        ],
    },
    # Blepharoplasty / brow ptosis (cosmetic unless visual field affected)
    "15820": {
        "description": "Blepharoplasty, lower eyelid",
        "medical_indications": [
            "visual field obstruction", "ectropion", "entropion", "ptosis",
            "trauma", "tumor", "dermatochalasis obstructing vision",
        ],
    },
    "15821": {
        "description": "Blepharoplasty, lower eyelid with extensive skin removal",
        "medical_indications": [
            "visual field obstruction", "ectropion", "entropion", "ptosis",
            "trauma", "tumor", "dermatochalasis obstructing vision",
        ],
    },
    "15822": {
        "description": "Blepharoplasty, upper eyelid",
        "medical_indications": [
            "visual field obstruction", "ectropion", "entropion", "ptosis",
            "trauma", "tumor", "dermatochalasis obstructing vision",
        ],
    },
    "15823": {
        "description": "Blepharoplasty, upper eyelid with excessive skin",
        "medical_indications": [
            "visual field obstruction", "ectropion", "entropion", "ptosis",
            "trauma", "tumor", "dermatochalasis obstructing vision",
        ],
    },
    "67900": {
        "description": "Repair of brow ptosis",
        "medical_indications": [
            "visual field obstruction", "brow ptosis affecting vision",
            "facial nerve paralysis", "trauma",
        ],
    },
    "67901": {
        "description": "Repair of blepharoptosis (frontalis muscle technique)",
        "medical_indications": [
            "visual field obstruction", "congenital ptosis",
            "acquired ptosis affecting vision", "myasthenia gravis",
            "third nerve palsy",
        ],
    },
    # Cosmetic tattooing (vs. reconstructive tattooing after mastectomy)
    "11920": {
        "description": "Intradermal tattooing",
        "medical_indications": [
            "post-mastectomy nipple-areola reconstruction",
            "vitiligo", "scar camouflage post-trauma", "cleft lip repair",
        ],
    },
    "11921": {
        "description": "Intradermal tattooing (additional 6.0 sq cm)",
        "medical_indications": [
            "post-mastectomy nipple-areola reconstruction",
            "vitiligo", "scar camouflage post-trauma", "cleft lip repair",
        ],
    },
    "11922": {
        "description": "Intradermal tattooing (each additional 20 sq cm)",
        "medical_indications": [
            "post-mastectomy nipple-areola reconstruction",
            "vitiligo", "scar camouflage post-trauma", "cleft lip repair",
        ],
    },
}


# ---------------------------------------------------------------------------
# Exclusion 2: Experimental / investigational services with no evidence base
# ---------------------------------------------------------------------------
# Signals that a service is experimental:
#   • CPT Category III code (0xxxT format) — AMA tracks these as emerging
#     technology whose clinical utility is not yet established.
#   • Service description contains explicit experimental-status keywords.
#
# The exclusion fires ONLY when the experimental signal is present AND
# the clinical engine found zero matching peer-reviewed guidelines. If
# any guideline backs the service, it is NOT experimental-without-evidence
# and we fall through to approval.

EXPERIMENTAL_KEYWORDS: tuple[str, ...] = (
    "experimental",
    "investigational",
    "unproven",
    "not fda approved",
    "not fda-approved",
    "pre-fda",
    "clinical trial only",
    "category iii",
    "category 3",
    "emerging technology",
    "no established efficacy",
    "efficacy not established",
)


def _is_category_iii_cpt(code: str) -> bool:
    """Return True if `code` is a CPT Category III code (format: NNNNT)."""
    code = (code or "").strip().upper()
    if len(code) != 5 or not code.endswith("T"):
        return False
    return code[:4].isdigit()


# Keywords in the patient's condition, symptoms, or diagnoses that
# indicate the visit purpose is cosmetic or non-medical. These are
# checked ONLY when the CPT code is not already on the cosmetic list
# (Path B). A match means the engine found cosmetic intent in the
# visit purpose; the exclusion then fires unless a medical indication
# is also present.
#
# Deliberately narrow: we only flag clearly cosmetic language so that
# ambiguous visits default to covered (uncertainty → approve).
COSMETIC_PURPOSE_KEYWORDS: tuple[str, ...] = (
    "cosmetic",
    "aesthetic",
    "beautification",
    "appearance only",
    "appearance enhancement",
    "elective cosmetic",
    "cosmetic consultation",
    "cosmetic evaluation",
    "cosmetic procedure",
    "cosmetic surgery",
    "cosmetic treatment",
    "wrinkle",
    "anti-aging",
    "antiaging",
    "botox cosmetic",
    "filler cosmetic",
    "liposuction cosmetic",
    "nose job",
    "face lift",
    "facelift",
    "tummy tuck",
    "breast augmentation cosmetic",
    "body contouring cosmetic",
    "hair removal cosmetic",
    "teeth whitening",
    "tooth whitening",
    "smile makeover",
    "vanity",
)

# Keywords that indicate the visit has a medical purpose even if
# cosmetic language is also present. If any of these appear alongside
# a cosmetic keyword, the visit is NOT excluded (the medical indication
# overrides the cosmetic signal).
MEDICAL_OVERRIDE_KEYWORDS: tuple[str, ...] = (
    "pain",
    "obstruction",
    "breathing",
    "infection",
    "trauma",
    "cancer",
    "tumor",
    "burn",
    "reconstruction",
    "congenital",
    "deformity",
    "functional",
    "impairment",
    "disability",
    "nerve",
    "neuropathy",
    "sleep apnea",
    "vision loss",
    "visual field",
    "ptosis",
    "ectropion",
    "entropion",
    "lymphedema",
    "lipedema",
    "gender affirmation",
    "gender affirming",
    "gender dysphoria",
    "mastectomy",
    "post-surgical",
    "postsurgical",
    "cleft",
    "scar",
    "scarring",
    "disfigurement",
    "medically necessary",
)


def _visit_purpose_is_cosmetic(
    *,
    condition: Optional[str],
    patient_symptoms: Optional[list[str]],
    patient_history: Optional[dict],
) -> tuple[bool, list[str]]:
    """Determine whether the visit's stated purpose is cosmetic/non-medical.

    Returns (is_cosmetic, signals) where `signals` is the list of cosmetic
    keywords that matched. Returns (False, []) if no cosmetic language is
    found, or if medical-override keywords are also present (meaning the
    visit has a medical component that protects it from exclusion).
    """
    condition_text = (condition or "").lower()
    symptoms_text = " ".join(str(s).lower() for s in (patient_symptoms or []))
    diagnoses_text = " ".join(
        str(d).lower() for d in ((patient_history or {}).get("diagnoses") or [])
    )

    haystack = " ".join([condition_text, symptoms_text, diagnoses_text])
    if not haystack.strip():
        return False, []

    # Check for cosmetic keywords
    matched_cosmetic: list[str] = []
    for kw in COSMETIC_PURPOSE_KEYWORDS:
        if kw in haystack:
            matched_cosmetic.append(kw)

    if not matched_cosmetic:
        return False, []

    # Check for medical overrides — any medical keyword present means the
    # visit has a medical component and should NOT be excluded.
    for kw in MEDICAL_OVERRIDE_KEYWORDS:
        if kw in haystack:
            return False, []

    return True, matched_cosmetic



def _medical_indication_present(
    indications: list[str],
    patient_symptoms: list[str],
    patient_history: dict,
    condition: Optional[str],
) -> tuple[bool, list[str]]:
    """Check whether any of the listed medical indications appears in the
    patient's context (symptoms, diagnoses, risk factors, condition field)."""
    symptoms_text = " ".join(s.lower() for s in patient_symptoms or [])
    diagnoses_text = " ".join(
        str(d).lower() for d in (patient_history or {}).get("diagnoses", []) or []
    )
    risk_factors_text = " ".join(
        str(r).lower() for r in (patient_history or {}).get("risk_factors", []) or []
    )
    condition_text = (condition or "").lower()

    haystack = " ".join(
        [symptoms_text, diagnoses_text, risk_factors_text, condition_text]
    )

    matched: list[str] = []
    for indication in indications:
        if indication.lower() in haystack:
            matched.append(indication)
    return (len(matched) > 0), matched


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def evaluate_universal_exclusions(
    *,
    service_code: Optional[str],
    service_description: Optional[str] = None,
    condition: Optional[str] = None,
    patient_symptoms: Optional[list[str]] = None,
    patient_history: Optional[dict] = None,
    matched_guidelines_count: int = 0,
) -> dict:
    """Evaluate a requested service against the universal coverage policy.

    Returns a dict with:
        is_excluded: bool           — True only if a specific exclusion fires
        category: str | None        — 'cosmetic_no_indication'
                                      | 'experimental_no_evidence'
                                      | None
        reasoning: str              — plain-language explanation suitable
                                      for the audit trail and denial notice
        policy_version: str         — stamped for audit traceability
        signals: dict               — raw signals for audit drill-down

    Default behavior: NOT excluded. If the system is uncertain (unknown
    code, ambiguous classification, incomplete patient data), it returns
    `is_excluded=False` so the clinical engine can fall back to its
    normal evidence-based approval path.
    """
    service_code = (service_code or "").strip()
    service_description_lc = (service_description or "").lower()
    patient_symptoms = list(patient_symptoms or [])
    patient_history = dict(patient_history or {})

    signals: dict = {
        "service_code": service_code or None,
        "matched_guidelines_count": matched_guidelines_count,
        "cosmetic_classification": None,
        "experimental_classification": None,
    }

    # ---- Exclusion 1: Cosmetic with no medical indication ----
    #
    # Two detection paths:
    #   Path A: The CPT code itself is on the COSMETIC_PROCEDURES list.
    #   Path B: The CPT code is a generic service code (e.g., an E/M
    #           office visit like 99213) but the patient's stated reason
    #           for the visit is purely cosmetic. A standard office visit
    #           used for a cosmetic consultation, an employment physical
    #           unrelated to a health concern, or any non-medical purpose
    #           is evaluated against the cosmetic exclusion based on the
    #           visit's stated purpose — not just the CPT code.
    #
    # In both paths, the exclusion is overridden if any medical
    # indication is present in the patient context.

    # Path A: CPT code is explicitly cosmetic
    cosmetic_info = COSMETIC_PROCEDURES.get(service_code)
    if cosmetic_info is not None:
        indications = cosmetic_info["medical_indications"]
        has_indication, matched = _medical_indication_present(
            indications, patient_symptoms, patient_history, condition,
        )
        signals["cosmetic_classification"] = {
            "listed_as_cosmetic": True,
            "detection_path": "cpt_code",
            "description": cosmetic_info["description"],
            "matched_indications": matched,
        }
        if not has_indication:
            return {
                "is_excluded": True,
                "category": "cosmetic_no_indication",
                "reasoning": (
                    f"Service {service_code} ({cosmetic_info['description']}) is "
                    f"classified as cosmetic by the AMA CPT description. No "
                    f"medical indication was found in the patient's symptoms, "
                    f"history, or diagnoses. Under the universal coverage "
                    f"policy, cosmetic services without a medical indication "
                    f"are the first of two narrow exclusions. Coverage is "
                    f"available if documentation of a medical indication is "
                    f"provided — for example: "
                    f"{', '.join(indications[:4])}."
                ),
                "policy_version": POLICY_VERSION,
                "signals": signals,
            }
        # Medical indication present → NOT excluded. Fall through.

    # Path B: Generic code but the visit purpose is cosmetic
    # This catches the case where a standard office visit (99213, etc.)
    # or other generic procedure code is used for a non-medical purpose.
    if cosmetic_info is None:
        purpose_is_cosmetic, cosmetic_purpose_signals = _visit_purpose_is_cosmetic(
            condition=condition,
            patient_symptoms=patient_symptoms,
            patient_history=patient_history,
        )
        if purpose_is_cosmetic:
            signals["cosmetic_classification"] = {
                "listed_as_cosmetic": False,
                "detection_path": "visit_purpose",
                "cosmetic_purpose_signals": cosmetic_purpose_signals,
            }
            return {
                "is_excluded": True,
                "category": "cosmetic_no_indication",
                "reasoning": (
                    f"Service {service_code} is a standard procedure code, "
                    f"but the stated purpose of the visit is cosmetic or "
                    f"non-medical ({', '.join(cosmetic_purpose_signals[:3])}). "
                    f"No medical indication was found in the patient's "
                    f"symptoms, diagnoses, or risk factors. Under the "
                    f"universal coverage policy, services performed for a "
                    f"purely cosmetic or non-medical purpose are excluded "
                    f"regardless of the billing code used. Coverage is "
                    f"available if a medical indication is documented."
                ),
                "policy_version": POLICY_VERSION,
                "signals": signals,
            }

    # ---- Exclusion 2: Experimental with no peer-reviewed evidence base ----
    is_category_iii = _is_category_iii_cpt(service_code)
    has_experimental_keyword = any(
        kw in service_description_lc for kw in EXPERIMENTAL_KEYWORDS
    )
    appears_experimental = is_category_iii or has_experimental_keyword
    signals["experimental_classification"] = {
        "category_iii_cpt": is_category_iii,
        "experimental_keyword_in_description": has_experimental_keyword,
    }

    if appears_experimental and matched_guidelines_count == 0:
        signal_source = (
            "CPT Category III code (emerging technology tracked by the AMA "
            "before Category I status)"
            if is_category_iii
            else "experimental-status keyword in the service description"
        )
        return {
            "is_excluded": True,
            "category": "experimental_no_evidence",
            "reasoning": (
                f"Service {service_code} was flagged as experimental or "
                f"investigational ({signal_source}), and the clinical engine "
                f"found zero matching peer-reviewed guidelines. Under the "
                f"universal coverage policy, experimental services without a "
                f"peer-reviewed evidence base are the second of two narrow "
                f"exclusions. Coverage is available if the service is "
                f"provided as part of an approved clinical trial, or once "
                f"peer-reviewed evidence is published and ingested into the "
                f"clinical guideline library."
            ),
            "policy_version": POLICY_VERSION,
            "signals": signals,
        }

    # ---- Default: NOT excluded ----
    # The engine is uncertain or the service is clearly neither cosmetic
    # nor experimental. Per the universal policy, uncertainty resolves in
    # favor of the patient: approve.
    return {
        "is_excluded": False,
        "category": None,
        "reasoning": (
            "Service is not on the universal exclusion list. Coverage is "
            "determined by evidence-based clinical necessity per the normal "
            "clinical engine path. Per the universal coverage policy, "
            "uncertainty resolves in favor of approval."
        ),
        "policy_version": POLICY_VERSION,
        "signals": signals,
    }


def get_universal_policy_statement() -> dict:
    """Return the canonical universal coverage policy statement.

    Surfaced through the benefits admin API so employers and employees
    can read the same policy document the engine enforces."""
    return {
        "policy_version": POLICY_VERSION,
        "statement": UNIVERSAL_POLICY_STATEMENT,
        "exclusions": [
            {
                "category": "cosmetic_no_indication",
                "description": (
                    "Cosmetic services with no medical indication. Coverage "
                    "is restored automatically when a medical indication is "
                    "documented (e.g., post-trauma reconstruction, visual-"
                    "field obstruction, congenital deformity)."
                ),
            },
            {
                "category": "experimental_no_evidence",
                "description": (
                    "Experimental or investigational services with no peer-"
                    "reviewed evidence base. Coverage is restored when the "
                    "service is provided through an approved clinical trial "
                    "or when peer-reviewed evidence is established."
                ),
            },
        ],
        "uncertainty_rule": (
            "If the system is uncertain whether an exclusion applies, the "
            "service is approved."
        ),
        "employer_specific_toggles": None,
        "employer_specific_toggles_note": (
            "There are no employer-specific coverage toggles. Every employer "
            "on the platform is bound by the same universal coverage policy."
        ),
    }
