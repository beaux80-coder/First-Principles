"""Denial notice generator — Constitution F1 Q11.

Generates plain-language denial notices with appeal rights, deadlines,
and instructions. Called automatically at time of any denial.

ERISA requires: written notice with specific reasons, reference to plan
provisions, description of appeal procedures, and applicable timelines.
ACA 2719 requires: internal and external review options.
"""

import logging
from datetime import datetime, timedelta, UTC


logger = logging.getLogger(__name__)

# ERISA/ACA appeal deadlines by appeal type
_APPEAL_DEADLINES = {
    "standard": {"days": 180, "description": "180 days from date of denial"},
    "expedited": {"hours": 72, "description": "72 hours for urgent/emergent situations"},
    "external_iro": {"days": 120, "description": "4 months from exhaustion of internal review"},
}

# State-specific appeal window (days) — all 50 states + DC
# ERISA baseline: 180 days. States may extend but not shorten for insured plans.
# Self-funded ERISA plans follow federal 180-day standard; state extensions
# listed for reference and for insured-plan compatibility.
_STATE_DEADLINE_EXTENSIONS = {
    "AL": 180, "AK": 180, "AZ": 180, "AR": 180, "CA": 180,
    "CO": 180, "CT": 180, "DE": 180, "FL": 180, "GA": 180,
    "HI": 180, "ID": 180, "IL": 180, "IN": 180, "IA": 180,
    "KS": 180, "KY": 180, "LA": 180, "ME": 180, "MD": 180,
    "MA": 180, "MI": 180, "MN": 180, "MS": 180, "MO": 180,
    "MT": 180, "NE": 180, "NV": 180, "NH": 180, "NJ": 180,
    "NM": 180, "NY": 180, "NC": 180, "ND": 180, "OH": 180,
    "OK": 180, "OR": 180, "PA": 180, "RI": 180, "SC": 180,
    "SD": 180, "TN": 180, "TX": 180, "UT": 180, "VT": 180,
    "VA": 180, "WA": 180, "WV": 180, "WI": 180, "WY": 180,
    "DC": 180,
}


def generate_denial_notice(
    claim_id: str,
    employee_id: str,
    benefit_type: str,
    denial_reason: str,
    adjudication_reasoning: str | None = None,
    guidelines_referenced: list | None = None,
    clinical_determination_id: str | None = None,
    state: str | None = None,
    criteria_evaluated: list | None = None,
    risk_score: float | None = None,
) -> dict:
    """Generate a plain-language denial notice with full appeal rights.

    Constitution F1 Q11: The appeals process is explained to the employee
    in plain language at the time of any denial.

    Returns a structured denial notice containing all ERISA/ACA-required
    elements formatted for employee comprehension.
    """
    now = datetime.now(UTC)

    # Calculate deadlines
    standard_deadline = now + timedelta(days=180)
    expedited_available = True  # Always available per ACA for urgent situations

    # State-specific extensions
    state_extension_days = _STATE_DEADLINE_EXTENSIONS.get(state or "", 0)
    if state_extension_days > 180:
        standard_deadline = now + timedelta(days=state_extension_days)

    # Build plain-language explanation with patient-specific detail
    plain_language_reason = _simplify_denial_reason(
        denial_reason, benefit_type, guidelines_referenced, criteria_evaluated, risk_score
    )

    notice = {
        "notice_id": f"DN-{claim_id[:8]}-{now.strftime('%Y%m%d')}",
        "generated_at": now.isoformat(),
        "claim_id": claim_id,
        "employee_id": employee_id,
        "benefit_type": benefit_type,

        # ERISA requirement: specific reason for denial
        "denial_reason": {
            "plain_language": plain_language_reason,
            "technical_detail": denial_reason,
            "adjudication_reasoning": adjudication_reasoning,
        },

        # ERISA requirement: reference to guidelines/plan provisions
        "guidelines_applied": guidelines_referenced or [],
        "clinical_determination_id": clinical_determination_id,

        # ERISA/ACA requirement: description of appeal procedures
        "appeal_rights": {
            "summary": (
                "You have the right to appeal this decision. Your appeal will be "
                "reviewed by a qualified clinical professional who was not involved "
                "in the original decision. If the internal appeal upholds the denial, "
                "you have the right to an independent external review by a qualified "
                "Independent Review Organization (IRO)."
            ),
            "internal_appeal": {
                "description": (
                    "Request an internal review of this denial. A different clinical "
                    "professional will review your case, including any new evidence "
                    "you provide."
                ),
                "deadline": standard_deadline.isoformat(),
                "deadline_description": f"You must file within 180 days of this notice ({standard_deadline.strftime('%B %d, %Y')})",
                "how_to_file": "Submit via /api/v1/appeals/file with your claim_id and any supporting information",
            },
            "expedited_review": {
                "available": expedited_available,
                "description": (
                    "If your situation is urgent or involves an ongoing course of "
                    "treatment, you can request an expedited review. Expedited "
                    "reviews are completed within 72 hours."
                ),
                "criteria": "Medical emergency, ongoing treatment, or imminent health risk",
                "how_to_file": "Submit via /api/v1/appeals/{appeal_id}/expedite with urgency reason",
            },
            "external_review": {
                "description": (
                    "If your internal appeal is denied, you have the right to an "
                    "independent external review by a qualified Independent Review "
                    "Organization (IRO) as required by the ACA. The IRO is completely "
                    "independent of this plan."
                ),
                "how_to_file": "Submit via /api/v1/appeals/{appeal_id}/external-review",
            },
        },

        # Additional information
        "additional_info": {
            "you_may_submit": (
                "You may submit additional information, documents, medical records, "
                "or other evidence to support your appeal. All evidence will be "
                "considered during the review."
            ),
            "no_retaliation": (
                "Filing an appeal will not affect your benefits or coverage in any way."
            ),
            "assistance": (
                "If you need help understanding this notice or filing an appeal, "
                "contact your employer's benefits administrator."
            ),
        },

        "compliance": {
            "erisa_section": "ERISA Section 503 — Claim Review Procedures",
            "aca_section": "ACA Section 2719 — Internal/External Review",
            "state_requirements": f"Applicable state law for {state or 'all states'}",
        },

        "feeding_f8": True,
    }

    logger.info(
        "Denial notice generated for claim %s, employee %s, benefit_type %s",
        claim_id, employee_id, benefit_type,
    )
    return notice


def _simplify_denial_reason(
    technical_reason: str,
    benefit_type: str,
    guidelines_referenced: list | None = None,
    criteria_evaluated: list | None = None,
    risk_score: float | None = None,
) -> str:
    """Convert technical denial reason to patient-specific plain language.

    Constitution F1 Q11: Must cite specific guidelines, specific criteria
    the patient did/didn't meet, and what evidence would change the outcome.
    """
    reason_lower = technical_reason.lower()

    # Build guideline citation if available
    guideline_citation = ""
    if guidelines_referenced:
        guideline_names = [g.split(":")[-1].strip() if ":" in g else g for g in guidelines_referenced[:3]]
        guideline_citation = f" The following clinical guidelines were consulted: {'; '.join(guideline_names)}."

    if "duplicate" in reason_lower:
        return (
            "This claim appears to be a duplicate of a claim already processed. "
            "If you believe this is a separate service, please provide additional "
            "documentation (such as a different date of service or provider note) "
            "when filing your appeal."
        )
    if "eligibility" in reason_lower:
        return (
            "Our records indicate that the member may not be eligible for this "
            f"{benefit_type} benefit at the time of service. If you believe this "
            "is incorrect, please provide proof of eligibility (such as enrollment "
            "confirmation or employer letter) with your appeal."
        )
    if "coding" in reason_lower or "bundling" in reason_lower:
        return (
            "The service codes submitted do not meet the requirements for "
            "separate reimbursement under current coding guidelines. "
            f"{guideline_citation} "
            "If you believe the coding is correct, please provide clinical "
            "documentation explaining why separate billing is appropriate."
        )
    if "denied" in reason_lower or "does not meet" in reason_lower or "clinical" in reason_lower:
        explanation = (
            f"Based on current peer-reviewed clinical guidelines, the requested "
            f"{benefit_type} service was not determined to be medically necessary "
            f"for your specific condition.{guideline_citation}"
        )
        if risk_score is not None:
            explanation += (
                f" Your clinical risk assessment score was {risk_score:.2f} "
                f"(threshold for approval: 0.25)."
            )
        explanation += (
            " To appeal, you may submit: (1) additional medical records showing "
            "your condition meets the guideline criteria, (2) a letter from your "
            "treating provider explaining medical necessity, or (3) documentation "
            "of failed conservative treatment if applicable."
        )
        return explanation

    if "risk" in reason_lower and "deterioration" in reason_lower:
        explanation = (
            "After evaluating your specific symptoms, medical history, and available "
            "clinical evidence, the system did not find sufficient evidence of a "
            "meaningful risk of health deterioration without this service."
            f"{guideline_citation}"
        )
        if risk_score is not None:
            explanation += (
                f" Your clinical risk score was {risk_score:.2f} "
                f"(threshold for approval: 0.25)."
            )
        explanation += (
            " To change this assessment, provide: new diagnostic results, "
            "updated clinical notes, or specialist referral documentation "
            "demonstrating elevated risk."
        )
        return explanation

    return (
        f"The requested {benefit_type} service was denied for the following reason: "
        f"{technical_reason}.{guideline_citation} "
        "You have the right to appeal this decision."
    )
