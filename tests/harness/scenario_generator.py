"""Scenario generator — uses the Anthropic API to create realistic patient scenarios.

Each scenario is a structured dict containing a patient profile, a
requested service, and Claude's own clinical judgment of whether that
service is medically necessary. The judgments form the "answer key"
that the harness compares against the engine's decisions.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import anthropic


logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

# ---------------------------------------------------------------------------
# Batch categories — each batch of 25 scenarios focuses on one clinical area.
# 20 batches × 25 = 500 scenarios. Categories rotate if total > 500.
# ---------------------------------------------------------------------------
BATCH_CATEGORIES: list[dict] = [
    {
        "name": "routine_primary_care_1",
        "description": (
            "Routine primary care office visits — annual physicals, follow-up visits "
            "for stable conditions, minor acute complaints (URI, UTI, sprains). "
            "Use E/M codes: 99202-99215."
        ),
        "approve_pct": 95,
    },
    {
        "name": "routine_primary_care_2",
        "description": (
            "More routine primary care — medication refills, lab reviews, "
            "well-woman exams, health maintenance visits. E/M codes: 99381-99397, 99202-99215."
        ),
        "approve_pct": 95,
    },
    {
        "name": "emergency_1",
        "description": (
            "Emergency department presentations — chest pain, acute abdomen, "
            "fractures, lacerations, severe allergic reactions, syncope. "
            "ED E/M codes: 99281-99285. Include red-flag ICD-10 diagnoses."
        ),
        "approve_pct": 98,
    },
    {
        "name": "emergency_2",
        "description": (
            "More emergency scenarios — stroke symptoms, overdose, anaphylaxis, "
            "pediatric emergencies, psychiatric emergencies (suicidal ideation, "
            "psychosis). ED codes: 99281-99285, 99291-99292."
        ),
        "approve_pct": 98,
    },
    {
        "name": "mental_health_1",
        "description": (
            "Mental health services — individual psychotherapy (90834, 90837), "
            "psychiatric evaluation (90791), medication management (90863), "
            "group therapy (90853). Conditions: depression, anxiety, PTSD, "
            "bipolar disorder, OCD, ADHD."
        ),
        "approve_pct": 90,
    },
    {
        "name": "mental_health_2",
        "description": (
            "Substance use disorder treatment and severe mental illness — "
            "inpatient detox, intensive outpatient (IOP), MAT (medication-assisted "
            "treatment with buprenorphine/naltrexone), schizophrenia management, "
            "eating disorder treatment. Codes: 90834, 90837, H0015, H0020."
        ),
        "approve_pct": 92,
    },
    {
        "name": "chronic_disease_1",
        "description": (
            "Chronic disease management — Type 2 diabetes (A1c monitoring, "
            "insulin pump, CGM), hypertension management, COPD with "
            "exacerbation, heart failure follow-up. Codes: 99213-99215, "
            "83036 (A1c), 94060 (spirometry)."
        ),
        "approve_pct": 95,
    },
    {
        "name": "chronic_disease_2",
        "description": (
            "More chronic disease — rheumatoid arthritis (biologic therapy), "
            "chronic kidney disease, HIV management (antiretroviral therapy), "
            "multiple sclerosis (disease-modifying therapy). Include "
            "medication codes and office visit codes."
        ),
        "approve_pct": 93,
    },
    {
        "name": "pediatric",
        "description": (
            "Pediatric care — well-child visits (99381-99394), childhood "
            "vaccinations (90460, 90471), developmental screening, "
            "ear infections (otitis media), asthma in children, ADHD evaluation, "
            "scoliosis screening, sports physicals. Ages 0-17."
        ),
        "approve_pct": 96,
    },
    {
        "name": "prenatal_obgyn",
        "description": (
            "Prenatal and OB/GYN care — routine prenatal visits (59400-59430), "
            "ultrasounds (76801, 76805, 76817), gestational diabetes screening, "
            "contraceptive management (IUD insertion 58300), pap smear (88175), "
            "high-risk pregnancy monitoring."
        ),
        "approve_pct": 97,
    },
    {
        "name": "surgical",
        "description": (
            "Surgical procedures — appendectomy (44970), cholecystectomy "
            "(47562), hernia repair (49650), knee arthroscopy (29881), "
            "carpal tunnel release (64721), tonsillectomy (42826). "
            "Include both clearly indicated and borderline cases."
        ),
        "approve_pct": 85,
    },
    {
        "name": "imaging",
        "description": (
            "Advanced imaging — MRI brain (70553), MRI knee (73721), "
            "CT abdomen/pelvis (74177), PET scan (78816), X-ray (71046), "
            "ultrasound (76700). Include both appropriate (post-trauma) "
            "and potentially low-value (back pain <6 weeks) imaging requests."
        ),
        "approve_pct": 80,
    },
    {
        "name": "prescriptions",
        "description": (
            "Prescription medication management — GLP-1 agonists for diabetes "
            "and for weight loss, statin therapy, opioid prescriptions for "
            "chronic pain, brand-name vs generic choices, specialty pharmacy "
            "(biologics for Crohn's, TNF inhibitors). Include off-label use cases."
        ),
        "approve_pct": 82,
    },
    {
        "name": "physical_therapy",
        "description": (
            "Physical therapy and rehabilitation — post-surgical PT (97110, "
            "97140), PT for chronic low back pain, PT for rotator cuff "
            "injury, occupational therapy post-stroke (97530), speech "
            "therapy post-TBI. Include cases at various session counts "
            "(initial vs. 40th session)."
        ),
        "approve_pct": 88,
    },
    {
        "name": "dental",
        "description": (
            "Dental services — prophylaxis (D1110), fluoride treatment "
            "(D1206), dental X-rays (D0210), composite filling (D2391), "
            "crown (D2750), root canal (D3310), extraction (D7140), "
            "orthodontics (D8090), dental implant (D6010). Mix of "
            "preventive, restorative, and elective."
        ),
        "approve_pct": 85,
    },
    {
        "name": "vision",
        "description": (
            "Vision services — comprehensive eye exam (92014), refraction "
            "(92015), fundus photography (92250), OCT retinal scan (92134), "
            "cataract surgery (66984), LASIK (not typically covered), "
            "contact lens fitting (92310), diabetic retinopathy screening."
        ),
        "approve_pct": 82,
    },
    {
        "name": "preventive",
        "description": (
            "Preventive care and screenings — colonoscopy (45378), "
            "mammography (77067), cervical cancer screening (88175), "
            "PSA screening (84153), bone density DEXA (77080), "
            "flu vaccine (90658), COVID vaccine (91318), lipid panel "
            "(80061), depression screening (96127). All USPSTF A/B."
        ),
        "approve_pct": 99,
    },
    {
        "name": "edge_cosmetic",
        "description": (
            "Edge cases: COSMETIC procedures — some with clear medical "
            "indication, some purely aesthetic. Include: rhinoplasty for "
            "nasal obstruction (30400) vs purely cosmetic rhinoplasty, "
            "blepharoplasty for visual field obstruction (15822) vs "
            "cosmetic, breast reconstruction post-mastectomy (19340) vs "
            "purely cosmetic augmentation (19325), dermabrasion for severe "
            "acne scarring (15780) vs cosmetic. Half should have medical "
            "indication, half should not."
        ),
        "approve_pct": 50,
    },
    {
        "name": "edge_experimental",
        "description": (
            "Edge cases: EXPERIMENTAL and investigational services — "
            "Category III CPT codes (0019T, 0101T, 0347T, 0648T), "
            "off-label drug use (e.g., low-dose naltrexone for fibromyalgia, "
            "ketamine for depression), proton beam therapy for standard "
            "indications vs experimental. Include cases where experimental "
            "treatment has emerging evidence and cases with no evidence."
        ),
        "approve_pct": 35,
    },
    {
        "name": "edge_mixed",
        "description": (
            "Mixed edge cases — services where medical necessity is "
            "genuinely ambiguous: weight loss surgery for BMI 35 with "
            "comorbidities vs BMI 32 without, gender-affirming care "
            "(hormone therapy, surgical), varicose vein treatment "
            "(symptomatic vs cosmetic), allergy testing panels "
            "(targeted vs shotgun), genetic testing (BRCA with family "
            "history vs without). Deliberately borderline cases."
        ),
        "approve_pct": 65,
    },
]


SYSTEM_PROMPT = """\
You are a clinical scenario generator for a health benefits coverage testing \
system. Generate realistic patient scenarios that would be submitted to a \
clinical determination engine.

The health plan uses a universal coverage policy:
- Covers ALL services that are medically necessary per evidence-based clinical \
guidelines (USPSTF, CMS NCDs, specialty societies, peer-reviewed literature)
- Only two exclusions: (1) cosmetic services with no medical indication, \
(2) experimental/investigational services with no peer-reviewed evidence base
- If the evidence is uncertain, the service IS covered (approve)

For each scenario, provide your expert clinical judgment on whether the \
service is medically necessary under this policy.

Return ONLY a JSON array with exactly {batch_size} objects. No markdown \
fencing, no commentary — just the JSON array. Each object must have these \
exact fields:

{{
  "scenario_id": "<category>_<number>",
  "category": "<category_name>",
  "age": <integer 0-100>,
  "sex": "<male|female>",
  "diagnoses": ["<condition or ICD-10 description>"],
  "symptoms": ["<symptom>"],
  "risk_factors": ["<risk factor>"],
  "medications": ["<current medication>"],
  "service_requested": "<plain English description of the requested service>",
  "service_code": "<CPT, HCPCS, or CDT code>",
  "benefit_type": "<health|dental|vision|mental_health>",
  "condition": "<clinical condition being addressed>",
  "claude_determination": "<medically_necessary|not_medically_necessary>",
  "claude_rationale": "<1-2 sentence clinical reasoning for your determination>"
}}

CRITICAL code guidance:
- Use real, valid CPT codes (99213 for office visit, 99284 for ED, etc.)
- Dental: CDT codes starting with D (D1110 for cleaning, D2750 for crown)
- Vision: 92014 for eye exam, 66984 for cataract surgery
- Cosmetic: 15775, 15780, 15822, 15824, 19325, 30400
- Experimental: Category III format NNNNT (0019T, 0101T, 0347T, 0648T)
- Mental health: 90791, 90834, 90837, 90853, 90863
- Preventive: 45378, 77067, 88175, 90658, 80061, 96127
"""


def _parse_json_array(text: str) -> list[dict]:
    """Extract a JSON array from Claude's response, handling markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` fencing if present
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    # Try direct parse
    if text.startswith("["):
        return json.loads(text)
    # Last resort: find the first [ and last ]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"Could not extract JSON array from response (length {len(text)})")


REQUIRED_FIELDS = {
    "scenario_id", "age", "sex", "diagnoses", "symptoms",
    "service_requested", "service_code", "benefit_type",
    "condition", "claude_determination", "claude_rationale",
}


def _validate_scenario(scenario: dict, batch_idx: int, item_idx: int) -> Optional[dict]:
    """Validate and normalize a single scenario. Returns None if invalid."""
    missing = REQUIRED_FIELDS - set(scenario.keys())
    if missing:
        logger.warning(
            "Batch %d item %d missing fields %s — skipping", batch_idx, item_idx, missing
        )
        return None

    # Normalize determination value
    det = str(scenario.get("claude_determination", "")).strip().lower()
    if det not in ("medically_necessary", "not_medically_necessary"):
        logger.warning(
            "Batch %d item %d has invalid determination '%s' — skipping",
            batch_idx, item_idx, det,
        )
        return None
    scenario["claude_determination"] = det

    # Ensure list fields are lists
    for key in ("diagnoses", "symptoms", "risk_factors", "medications"):
        val = scenario.get(key)
        if val is None:
            scenario[key] = []
        elif isinstance(val, str):
            scenario[key] = [val]

    # Ensure benefit_type is valid
    valid_bt = {"health", "dental", "vision", "mental_health", "life", "std", "ltd"}
    if scenario.get("benefit_type", "").lower() not in valid_bt:
        scenario["benefit_type"] = "health"
    else:
        scenario["benefit_type"] = scenario["benefit_type"].lower()

    # Ensure category is present
    if "category" not in scenario:
        scenario["category"] = scenario.get("scenario_id", "unknown").rsplit("_", 1)[0]

    return scenario


def generate_scenarios(
    api_key: str,
    *,
    batch_size: int = 25,
    total: int = 500,
) -> list[dict]:
    """Generate patient scenarios using the Anthropic API.

    Calls claude-sonnet-4-20250514 in batches, each focused on a specific
    clinical category. Returns a list of validated scenario dicts.
    """
    client = anthropic.Anthropic(api_key=api_key)
    all_scenarios: list[dict] = []
    num_batches = total // batch_size

    for batch_idx in range(num_batches):
        category = BATCH_CATEGORIES[batch_idx % len(BATCH_CATEGORIES)]
        user_prompt = (
            f"Generate exactly {batch_size} realistic patient scenarios focused on: "
            f"{category['description']}\n\n"
            f"Ensure variety in demographics, severity, and complexity.\n"
            f"For this category, approximately {category['approve_pct']}% should be "
            f"medically necessary and {100 - category['approve_pct']}% should not be.\n"
            f"Use scenario_id format: {category['name']}_1, {category['name']}_2, etc.\n"
            f"Set category to: \"{category['name']}\""
        )

        for attempt in range(3):
            try:
                logger.info(
                    "Generating batch %d/%d (%s) — attempt %d",
                    batch_idx + 1, num_batches, category["name"], attempt + 1,
                )
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=SYSTEM_PROMPT.format(batch_size=batch_size),
                    messages=[{"role": "user", "content": user_prompt}],
                )

                raw_text = response.content[0].text
                scenarios = _parse_json_array(raw_text)

                validated = []
                for i, s in enumerate(scenarios):
                    v = _validate_scenario(s, batch_idx, i)
                    if v is not None:
                        validated.append(v)

                all_scenarios.extend(validated)
                logger.info(
                    "Batch %d: %d/%d scenarios validated (total so far: %d)",
                    batch_idx + 1, len(validated), len(scenarios), len(all_scenarios),
                )
                break  # success

            except anthropic.RateLimitError:
                wait = (attempt + 1) * 10
                logger.warning("Rate limited — waiting %ds before retry", wait)
                time.sleep(wait)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(
                    "Batch %d attempt %d JSON parse failed: %s", batch_idx, attempt + 1, e
                )
                if attempt == 2:
                    logger.error("Batch %d failed after 3 attempts — skipping", batch_idx)
            except Exception as e:
                logger.error("Batch %d attempt %d unexpected error: %s", batch_idx, attempt + 1, e)
                if attempt == 2:
                    logger.error("Batch %d failed after 3 attempts — skipping", batch_idx)

        # Rate-limit pause between batches
        if batch_idx < num_batches - 1:
            time.sleep(1.5)

    logger.info("Generation complete: %d total scenarios", len(all_scenarios))
    return all_scenarios


def save_scenarios(scenarios: list[dict], path: str | Path) -> None:
    """Save scenarios to a JSON file for reuse."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(scenarios, f, indent=2, default=str)
    logger.info("Saved %d scenarios to %s", len(scenarios), path)


def load_scenarios(path: str | Path) -> list[dict]:
    """Load previously generated scenarios from a JSON file."""
    with open(path) as f:
        scenarios = json.load(f)
    logger.info("Loaded %d scenarios from %s", len(scenarios), path)
    return scenarios
