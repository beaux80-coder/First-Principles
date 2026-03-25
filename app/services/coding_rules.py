"""Medical coding validation rules (Function 5).

Constitution: "Has every physically possible, legally permitted accuracy
improvement method been implemented?"

ICD-10 format validation, CPT modifier validation, frequency limits,
and benefit-type-specific adjudication rules.
"""

import logging
import re
from datetime import datetime, UTC, timedelta
from typing import Optional

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.models.claim import Claim

logger = logging.getLogger(__name__)

# ---- ICD-10-CM Validation ----
# Format: 1 letter + 2 digits + optional (decimal + up to 4 alphanumeric)
# Examples: A00, A00.0, S72.001A, Z23
ICD10_PATTERN = re.compile(
    r"^[A-TV-Z]"        # First char: letter (U reserved for special use)
    r"\d{2}"             # Two required digits
    r"(?:\.\w{1,4})?$",  # Optional: decimal + 1-4 alphanumeric
    re.IGNORECASE,
)

# ICD-10 chapter ranges for category validation
ICD10_CHAPTER_RANGES = {
    "A": (0, 99), "B": (0, 99),   # Infectious diseases
    "C": (0, 96), "D": (0, 89),   # Neoplasms / blood diseases
    "E": (0, 89),                  # Endocrine / metabolic
    "F": (0, 99),                  # Mental / behavioral
    "G": (0, 99),                  # Nervous system
    "H": (0, 95),                  # Eye and ear (H00-H59 eye, H60-H95 ear)
    "I": (0, 99),                  # Circulatory
    "J": (0, 99),                  # Respiratory
    "K": (0, 95),                  # Digestive
    "L": (0, 99),                  # Skin
    "M": (0, 99),                  # Musculoskeletal
    "N": (0, 99),                  # Genitourinary
    "O": (0, 99),                  # Pregnancy
    "P": (0, 96),                  # Perinatal
    "Q": (0, 99),                  # Congenital
    "R": (0, 99),                  # Symptoms / signs
    "S": (0, 99), "T": (0, 88),   # Injury / poisoning
    "V": (0, 99), "W": (0, 99),   # External causes
    "X": (0, 99), "Y": (0, 99),
    "Z": (0, 99),                  # Factors influencing health
}


def validate_icd10_code(code: str) -> dict:
    """Validate an ICD-10-CM format code.

    Pattern: letter + 2 digits + optional decimal + up to 4 alphanumeric.
    Returns {valid, code, format_errors}.
    """
    code = code.strip().upper()
    format_errors = []

    if not code:
        return {"valid": False, "code": code, "format_errors": ["Empty code"]}

    # Check overall pattern
    if not ICD10_PATTERN.match(code):
        format_errors.append(
            f"Invalid ICD-10-CM format: '{code}'. "
            f"Expected: letter + 2 digits + optional (.alphanumeric, up to 4)."
        )

    # Check first character is valid letter (U is reserved)
    if code and code[0] == "U":
        format_errors.append(
            "Letter 'U' is reserved for provisional assignment by WHO."
        )

    # Check category number is within valid range for the chapter
    if len(code) >= 3 and code[0].isalpha():
        try:
            category_num = int(code[1:3])
            chapter = code[0]
            if chapter in ICD10_CHAPTER_RANGES:
                lo, hi = ICD10_CHAPTER_RANGES[chapter]
                if category_num < lo or category_num > hi:
                    format_errors.append(
                        f"Category {code[:3]} is outside valid range "
                        f"{chapter}{lo:02d}-{chapter}{hi:02d}."
                    )
        except ValueError:
            format_errors.append(f"Characters 2-3 must be digits, got '{code[1:3]}'.")

    # Length check (max is X##.XXXX = 8 chars)
    if len(code) > 8:
        format_errors.append(
            f"Code too long ({len(code)} chars). Maximum is 8 characters "
            f"(e.g., S72.001A)."
        )

    return {
        "valid": len(format_errors) == 0,
        "code": code,
        "format_errors": format_errors,
    }


# ---- CPT Modifier Rules ----
# Key modifiers and their legal combination rules

MODIFIER_RULES = {
    # modifier -> description, conflicts (cannot be used together), requires
    "25": {
        "description": "Significant, separately identifiable E/M service",
        "conflicts": [],
        "requires_em": True,  # Only valid on E/M codes (99xxx)
        "notes": "Used when E/M is performed on same day as procedure",
    },
    "26": {
        "description": "Professional component",
        "conflicts": ["TC"],  # Cannot use both 26 and TC on same line
        "requires_em": False,
        "notes": "Splits service into professional vs technical component",
    },
    "50": {
        "description": "Bilateral procedure",
        "conflicts": ["LT", "RT"],  # If bilateral, don't use LT/RT
        "requires_em": False,
        "notes": "Procedure performed on both sides of the body",
    },
    "51": {
        "description": "Multiple procedures",
        "conflicts": ["59", "XE", "XS", "XP", "XU"],
        "requires_em": False,
        "notes": "Multiple procedures during same session; subject to reduction",
    },
    "59": {
        "description": "Distinct procedural service",
        "conflicts": ["51", "XE", "XS", "XP", "XU"],
        "requires_em": False,
        "notes": "Bypasses bundling edits; use only when truly distinct",
    },
    "TC": {
        "description": "Technical component",
        "conflicts": ["26"],  # Cannot use both TC and 26 on same line
        "requires_em": False,
        "notes": "Technical portion of a diagnostic service",
    },
    "XE": {
        "description": "Separate encounter",
        "conflicts": ["51", "59", "XS", "XP", "XU"],
        "requires_em": False,
        "notes": "Distinct service provided during separate encounter on same day",
    },
    "XS": {
        "description": "Separate structure",
        "conflicts": ["51", "59", "XE", "XP", "XU"],
        "requires_em": False,
        "notes": "Distinct service on separate organ/structure",
    },
    "XP": {
        "description": "Separate practitioner",
        "conflicts": ["51", "59", "XE", "XS", "XU"],
        "requires_em": False,
        "notes": "Distinct service performed by different practitioner",
    },
    "XU": {
        "description": "Unusual non-overlapping service",
        "conflicts": ["51", "59", "XE", "XS", "XP"],
        "requires_em": False,
        "notes": "Service that is distinct because it does not overlap usual components",
    },
    "76": {
        "description": "Repeat procedure by same physician",
        "conflicts": ["77"],
        "requires_em": False,
        "notes": "Same procedure repeated by same physician on same day",
    },
    "77": {
        "description": "Repeat procedure by different physician",
        "conflicts": ["76"],
        "requires_em": False,
        "notes": "Same procedure repeated by another physician on same day",
    },
    "LT": {
        "description": "Left side",
        "conflicts": ["50", "RT"],
        "requires_em": False,
        "notes": "Procedure performed on left side of the body",
    },
    "RT": {
        "description": "Right side",
        "conflicts": ["50", "LT"],
        "requires_em": False,
        "notes": "Procedure performed on right side of the body",
    },
}


def validate_modifiers(cpt_code: str, modifiers: list[str]) -> dict:
    """Validate modifier combination for a given CPT code.

    Checks:
    - Modifiers exist in known rules
    - No conflicting modifiers used together
    - E/M-only modifiers used only with E/M codes
    - No duplicate modifiers

    Returns {valid, errors, warnings}.
    """
    errors = []
    warnings = []
    normalized = [m.strip().upper() for m in modifiers]

    # Check for duplicates
    seen = set()
    for mod in normalized:
        if mod in seen:
            errors.append(f"Duplicate modifier: {mod}")
        seen.add(mod)

    # Check each modifier
    for mod in normalized:
        if mod not in MODIFIER_RULES:
            warnings.append(f"Modifier {mod} not in standard validation rules.")
            continue

        rule = MODIFIER_RULES[mod]

        # E/M requirement check (E/M codes start with 992xx-995xx)
        if rule.get("requires_em"):
            if not (cpt_code.startswith("992") or cpt_code.startswith("993")
                    or cpt_code.startswith("994") or cpt_code.startswith("995")):
                errors.append(
                    f"Modifier {mod} ({rule['description']}) requires an E/M code "
                    f"(99xxx). Code {cpt_code} does not qualify."
                )

        # Conflict check
        for conflict_mod in rule.get("conflicts", []):
            if conflict_mod in normalized:
                errors.append(
                    f"Modifier conflict: {mod} cannot be used with {conflict_mod}. "
                    f"{mod}={rule['description']}, "
                    f"{conflict_mod}={MODIFIER_RULES.get(conflict_mod, {}).get('description', 'unknown')}."
                )

    # Deduplicate symmetric conflict errors (A conflicts with B and B conflicts with A)
    unique_errors = list(dict.fromkeys(errors))

    return {
        "valid": len(unique_errors) == 0,
        "errors": unique_errors,
        "warnings": warnings,
        "modifiers_checked": normalized,
    }


# ---- Frequency Limits ----
# Max procedures per period by CPT/CDT code
# Format: code -> (max_count, period_years, description)

FREQUENCY_LIMITS = {
    # Preventive care
    "99395": (1, 1, "Annual wellness visit, 18-39"),
    "99396": (1, 1, "Annual wellness visit, 40-64"),
    "99397": (1, 1, "Annual wellness visit, 65+"),
    "99385": (1, 1, "New patient preventive visit, 18-39"),
    "99386": (1, 1, "New patient preventive visit, 40-64"),
    "99387": (1, 1, "New patient preventive visit, 65+"),

    # Screening procedures
    "45378": (1, 10, "Colonoscopy, diagnostic"),
    "45380": (1, 10, "Colonoscopy with biopsy"),
    "45384": (1, 10, "Colonoscopy with polyp removal"),
    "77067": (1, 1, "Screening mammogram, bilateral"),
    "77063": (1, 1, "Screening digital breast tomosynthesis"),
    "88175": (1, 3, "Pap smear, liquid-based"),
    "84153": (1, 1, "PSA screening"),
    "82270": (1, 1, "Fecal occult blood test"),
    "36415": (4, 1, "Routine venipuncture"),
    "80053": (2, 1, "Comprehensive metabolic panel"),
    "80061": (1, 1, "Lipid panel"),
    "85025": (2, 1, "Complete blood count (CBC)"),

    # Immunizations
    "90688": (1, 1, "Influenza vaccine, quadrivalent"),
    "90715": (1, 10, "Tdap vaccine"),
    "90732": (1, 5, "Pneumococcal vaccine"),
    "90746": (1, 99, "Hepatitis B vaccine (lifetime series)"),

    # Dental (CDT codes)
    "D1110": (2, 1, "Adult dental prophylaxis (cleaning)"),
    "D0120": (2, 1, "Periodic oral evaluation"),
    "D0150": (1, 3, "Comprehensive oral evaluation"),
    "D0210": (1, 3, "Full mouth radiographic survey"),
    "D0274": (1, 1, "Bitewing radiographs, four films"),
    "D0330": (1, 5, "Panoramic radiographic image"),

    # Vision
    "92004": (1, 1, "Comprehensive eye exam, new patient"),
    "92014": (1, 1, "Comprehensive eye exam, established"),
    "92015": (1, 1, "Refraction"),
}


def check_frequency_limit(
    db: Session,
    employee_id,
    cpt_code: str,
    benefit_type: str,
) -> dict:
    """Check if a procedure exceeds its frequency limit for an employee.

    Queries claims table for the same employee + code within the limit period.
    Returns {within_limit, max_allowed, current_count, period}.
    """
    if cpt_code not in FREQUENCY_LIMITS:
        return {
            "within_limit": True,
            "max_allowed": None,
            "current_count": 0,
            "period": None,
            "message": f"No frequency limit defined for code {cpt_code}.",
        }

    max_count, period_years, description = FREQUENCY_LIMITS[cpt_code]

    # Calculate the lookback window
    now = datetime.now(UTC)
    window_start = now - timedelta(days=period_years * 365)

    # Query existing approved/paid claims for this employee + code
    from app.models.service import Service
    from app.models.claim import ClaimStatus

    existing_count = (
        db.query(func.count(Claim.claim_id))
        .join(Service, Claim.service_id == Service.service_id)
        .filter(
            and_(
                Claim.employee_id == employee_id,
                Service.code == cpt_code,
                Claim.submitted_at >= window_start,
                Claim.status.in_([
                    ClaimStatus.approved,
                    ClaimStatus.paid,
                    ClaimStatus.adjudicating,
                ]),
            )
        )
        .scalar()
    ) or 0

    within_limit = existing_count < max_count
    period_desc = (
        f"{period_years} year{'s' if period_years != 1 else ''}"
        if period_years < 99
        else "lifetime"
    )

    result = {
        "within_limit": within_limit,
        "max_allowed": max_count,
        "current_count": existing_count,
        "period": period_desc,
        "description": description,
        "code": cpt_code,
    }

    if not within_limit:
        result["message"] = (
            f"Frequency limit exceeded for {cpt_code} ({description}): "
            f"{existing_count} of {max_count} allowed per {period_desc}."
        )

    return result


# ---- Benefit-Type-Specific Adjudication Rules ----

BENEFIT_TYPE_ADJUDICATION_RULES = {
    "dental": {
        "bundling_exclusions": {
            # CDT codes that cannot be billed together on the same date
            "D0120": {"D0150"},  # Periodic eval excludes comprehensive eval
            "D0150": {"D0120"},  # Comprehensive eval excludes periodic eval
            "D2391": {"D2392", "D2393", "D2394"},  # Resin restorations by surface count
            "D2392": {"D2391"},
        },
        "frequency_overrides": {
            "D1110": {"max": 2, "period_years": 1},  # Cleanings limited 2/yr
            "D0120": {"max": 2, "period_years": 1},  # Periodic eval 2/yr
            "D0274": {"max": 1, "period_years": 1},  # Bitewings 1/yr
            "D0210": {"max": 1, "period_years": 3},  # Full mouth x-ray 1/3yr
        },
        "code_prefix": "D",
        "notes": "CDT codes required. ADA coding standards apply.",
    },
    "vision": {
        "bundling_exclusions": {
            # Cannot bundle exam with materials on same claim line
            "92004": set(),  # Eye exam, new patient
            "92014": set(),  # Eye exam, established
        },
        "material_code_prefix": "V2",  # V2xxx = vision materials (frames, lenses)
        "exam_material_separation": True,  # Exam and materials must be separate claims
        "frequency_overrides": {
            "92004": {"max": 1, "period_years": 1},
            "92014": {"max": 1, "period_years": 1},
            "92015": {"max": 1, "period_years": 1},  # Refraction
        },
        "notes": "Vision materials (V2xxx) and exams (920xx) cannot be bundled.",
    },
    "mental_health": {
        "session_limits": {
            "90837": {"max": 52, "period_years": 1, "description": "Individual psychotherapy, 60 min"},
            "90834": {"max": 52, "period_years": 1, "description": "Individual psychotherapy, 45 min"},
            "90832": {"max": 52, "period_years": 1, "description": "Individual psychotherapy, 30 min"},
            "90847": {"max": 26, "period_years": 1, "description": "Family psychotherapy with patient"},
            "90846": {"max": 26, "period_years": 1, "description": "Family psychotherapy without patient"},
            "90853": {"max": 52, "period_years": 1, "description": "Group psychotherapy"},
        },
        "telehealth_parity": True,  # Telehealth sessions count same as in-person
        "telehealth_modifiers": ["95", "GT"],  # Modifiers indicating telehealth
        "bundling_exclusions": {
            "90837": {"90834", "90832"},  # Cannot bill multiple therapy durations
            "90834": {"90837", "90832"},
            "90832": {"90837", "90834"},
        },
        "notes": "Mental Health Parity Act: telehealth parity enforced. Session limits per year.",
    },
    "health": {
        "bundling_exclusions": {
            # Standard CPT/HCPCS bundling via CCI edits
            "99213": {"99211", "99212"},
            "99214": {"99211", "99212", "99213"},
            "99215": {"99211", "99212", "99213", "99214"},
        },
        "standard_rules": True,  # Standard CPT/HCPCS rules apply
        "notes": "Standard CCI edits and CPT bundling rules enforced.",
    },
    "life": {
        "documentation_requirements": [
            "death_certificate",
            "beneficiary_designation",
            "employer_verification",
        ],
        "no_procedure_codes": True,
        "notes": "Life insurance claims require documentation, not procedure codes.",
    },
    "std": {
        "documentation_requirements": [
            "physician_disability_statement",
            "employer_earnings_statement",
            "treatment_plan",
        ],
        "waiting_period_days": 14,
        "max_duration_weeks": 26,
        "no_procedure_codes": True,
        "notes": "Short-term disability: 14-day waiting period, 26-week max duration.",
    },
    "ltd": {
        "documentation_requirements": [
            "physician_disability_statement",
            "employer_earnings_statement",
            "treatment_plan",
            "functional_capacity_evaluation",
        ],
        "waiting_period_days": 90,
        "no_procedure_codes": True,
        "notes": "Long-term disability: 90-day waiting period. Ongoing medical documentation required.",
    },
}


def apply_benefit_type_rules(
    benefit_type: str,
    service_code: str,
    modifiers: list[str],
) -> dict:
    """Apply benefit-type-specific adjudication rules to a service code.

    Validates coding against type-specific bundling exclusions,
    frequency overrides, documentation requirements, and parity rules.

    Returns {valid, adjustments, warnings}.
    """
    adjustments = []
    warnings = []
    errors = []

    rules = BENEFIT_TYPE_ADJUDICATION_RULES.get(benefit_type)
    if not rules:
        return {
            "valid": True,
            "adjustments": [],
            "warnings": [f"No specific adjudication rules for benefit type: {benefit_type}"],
        }

    # ---- Check if benefit type expects procedure codes ----
    if rules.get("no_procedure_codes"):
        if service_code:
            warnings.append(
                f"Benefit type '{benefit_type}' does not use procedure codes. "
                f"Code '{service_code}' may not be applicable."
            )
        # Check documentation requirements
        doc_reqs = rules.get("documentation_requirements", [])
        if doc_reqs:
            adjustments.append({
                "type": "documentation_required",
                "documents": doc_reqs,
                "message": f"Required documentation for {benefit_type}: {', '.join(doc_reqs)}",
            })
        return {
            "valid": True,
            "adjustments": adjustments,
            "warnings": warnings,
        }

    # ---- Dental: verify CDT code prefix ----
    if benefit_type == "dental":
        expected_prefix = rules.get("code_prefix", "D")
        if service_code and not service_code.startswith(expected_prefix):
            warnings.append(
                f"Dental claims typically use CDT codes (prefix '{expected_prefix}'). "
                f"Code '{service_code}' may not be a valid dental code."
            )

    # ---- Vision: exam/material separation ----
    if benefit_type == "vision" and rules.get("exam_material_separation"):
        material_prefix = rules.get("material_code_prefix", "V2")
        if service_code and service_code.startswith(material_prefix):
            adjustments.append({
                "type": "material_claim",
                "message": (
                    f"Vision material code ({service_code}) must be billed "
                    f"separately from exam codes."
                ),
            })

    # ---- Mental health: telehealth parity ----
    if benefit_type == "mental_health" and rules.get("telehealth_parity"):
        telehealth_mods = rules.get("telehealth_modifiers", [])
        is_telehealth = any(m in modifiers for m in telehealth_mods)
        if is_telehealth:
            adjustments.append({
                "type": "telehealth_parity",
                "message": (
                    "Telehealth session treated at parity with in-person "
                    "per Mental Health Parity Act."
                ),
            })

    # ---- Mental health: session limits ----
    if benefit_type == "mental_health":
        session_limits = rules.get("session_limits", {})
        if service_code in session_limits:
            limit_info = session_limits[service_code]
            adjustments.append({
                "type": "session_limit_applicable",
                "max": limit_info["max"],
                "period_years": limit_info["period_years"],
                "description": limit_info.get("description", ""),
                "message": (
                    f"Session limit: {limit_info['max']} per "
                    f"{limit_info['period_years']} year(s) for {service_code}."
                ),
            })

    # ---- Check bundling exclusions ----
    bundling = rules.get("bundling_exclusions", {})
    if service_code in bundling:
        excluded = bundling[service_code]
        if excluded:
            adjustments.append({
                "type": "bundling_exclusion_applicable",
                "code": service_code,
                "excluded_codes": list(excluded),
                "message": (
                    f"Code {service_code} cannot be billed with: "
                    f"{', '.join(sorted(excluded))} on the same date of service."
                ),
            })

    # ---- Check frequency overrides ----
    freq_overrides = rules.get("frequency_overrides", {})
    if service_code in freq_overrides:
        override = freq_overrides[service_code]
        adjustments.append({
            "type": "frequency_override",
            "code": service_code,
            "max": override["max"],
            "period_years": override["period_years"],
            "message": (
                f"Benefit-type frequency override: {service_code} limited to "
                f"{override['max']} per {override['period_years']} year(s)."
            ),
        })

    return {
        "valid": len(errors) == 0,
        "adjustments": adjustments,
        "warnings": warnings,
    }
