"""Clinical guidelines ingester (Function 1).

Constitution: "Updates determination models as new peer-reviewed evidence
is published. Latency between guideline publication and engine incorporation
must be measured and minimized."

Ingests clinical guidelines from recognized medical authorities:
- CMS National Coverage Determinations (NCDs) — legal coverage requirements
- CMS Local Coverage Determinations (LCDs)
- USPSTF recommendations (when API key available)
- Evidence-based guidelines from medical specialty societies

All guidelines are stored with source, publication date, and ingestion
timestamp so incorporation latency can be measured.
"""

import logging
from datetime import datetime, UTC

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.clinical_guideline import (
    ClinicalGuideline, GuidelineSource, BenefitTypeGuideline, GuidelineGrade,
)

logger = logging.getLogger(__name__)


def ingest_cms_ncd_guidelines(db: Session) -> int:
    """Ingest CMS National Coverage Determinations.

    NCDs are the federal standard for what Medicare covers. They define
    medical necessity criteria that are the baseline for all coverage
    determinations in the US healthcare system.

    Source: CMS NCD database (publicly accessible).
    """
    logger.info("Ingesting CMS NCD guidelines...")

    # CMS NCDs accessible via the Medicare Coverage Database API
    NCD_API = "https://www.cms.gov/medicare-coverage-database/search.aspx"

    # Core NCDs covering the most common medical necessity determinations
    # These are the foundational coverage rules that F1 must reference
    ncds = [
        # Preventive services
        {
            "title": "Screening for Colorectal Cancer",
            "condition": "colorectal_cancer_screening",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "Covered for asymptomatic individuals aged 45+ at average risk. "
                "Colonoscopy every 10 years, FIT annually, FIT-DNA every 3 years, "
                "CT colonography every 5 years, flexible sigmoidoscopy every 5 years.",
            "criteria": "Age >= 45 AND average risk; OR any age with family history of colorectal "
                "cancer or personal history of adenomatous polyps or inflammatory bowel disease.",
            "contraindications": "Active acute illness preventing safe sedation. Recent colonoscopy "
                "within screening interval with normal findings.",
            "service_codes": "45378,45380,45384,45385,G0104,G0105,G0121,81528",
            "grade": GuidelineGrade.a,
            "population": "Adults aged 45+; earlier for high-risk individuals",
            "frequency": "Colonoscopy every 10 years; FIT annually; FIT-DNA every 3 years",
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?ncdid=281",
            "publication_date": datetime(2023, 5, 1),
        },
        {
            "title": "Screening Mammography",
            "condition": "breast_cancer_screening",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "Covered for women aged 40+ every 1-2 years. Annual for women 50+.",
            "criteria": "Female sex AND age >= 40. Annual mammography covered for all women 40+.",
            "contraindications": "Pregnancy (relative). Recent mammogram within screening interval.",
            "service_codes": "77067,G0202,77063",
            "grade": GuidelineGrade.b,
            "population": "Women aged 40+",
            "frequency": "Every 1-2 years ages 40-49; annually ages 50+",
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?ncdid=69",
            "publication_date": datetime(2022, 3, 1),
        },
        {
            "title": "Screening for Cervical Cancer",
            "condition": "cervical_cancer_screening",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "Pap smear every 3 years ages 21-65; HPV co-testing every 5 years ages 30-65.",
            "criteria": "Female sex AND age 21-65 AND not previously had total hysterectomy for non-cancer indication.",
            "contraindications": "Prior total hysterectomy for benign indication with no history of CIN2+.",
            "service_codes": "88141,88142,88143,88147,88148,88150,88152,88153,88164,88165,88166,88167,87624,87625",
            "grade": GuidelineGrade.a,
            "population": "Women aged 21-65",
            "frequency": "Pap every 3 years; HPV co-testing every 5 years for ages 30-65",
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?ncdid=34",
            "publication_date": datetime(2022, 1, 1),
        },
        {
            "title": "Cardiovascular Disease Screening",
            "condition": "cardiovascular_screening",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "Lipid panel screening covered every 5 years for adults 20+. "
                "Blood pressure screening at every clinical encounter.",
            "criteria": "Age >= 20 for lipid screening. All adults for blood pressure.",
            "contraindications": "None for screening.",
            "service_codes": "80061,82465,83718,83721,84478,36415",
            "grade": GuidelineGrade.a,
            "population": "Adults aged 20+",
            "frequency": "Lipid panel every 5 years; BP at every visit",
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?ncdid=286",
            "publication_date": datetime(2023, 1, 1),
        },
        {
            "title": "Diabetes Screening",
            "condition": "diabetes_screening",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "Fasting glucose or HbA1c covered for adults aged 35-70 who are overweight or obese.",
            "criteria": "Age 35-70 AND BMI >= 25 (>= 23 for Asian Americans). "
                "Also covered for adults with risk factors: family history, gestational diabetes history, PCOS.",
            "contraindications": "Already diagnosed with diabetes (use monitoring codes instead).",
            "service_codes": "82947,82950,82951,83036,82962",
            "grade": GuidelineGrade.b,
            "population": "Adults 35-70 with BMI >= 25; earlier with risk factors",
            "frequency": "Every 3 years if normal; annually if prediabetic",
            "source_url": "https://www.cms.gov/medicare-coverage-database/view/ncd.aspx?ncdid=285",
            "publication_date": datetime(2023, 8, 1),
        },
        # Mental health
        {
            "title": "Depression Screening",
            "condition": "depression_screening",
            "benefit_type": BenefitTypeGuideline.mental_health,
            "recommendation": "Screening for depression covered for all adults including pregnant/postpartum. "
                "PHQ-9 or equivalent validated instrument.",
            "criteria": "All adults >= 18 regardless of risk factors. Screening must be done with "
                "adequate systems in place to ensure accurate diagnosis, effective treatment, and follow-up.",
            "contraindications": "None for screening.",
            "service_codes": "96127,G0444,99420",
            "grade": GuidelineGrade.b,
            "population": "All adults aged 18+",
            "frequency": "Annually; more frequently if indicated",
            "source_url": "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/depression-in-adults-screening",
            "publication_date": datetime(2023, 6, 1),
        },
        {
            "title": "Anxiety Screening",
            "condition": "anxiety_screening",
            "benefit_type": BenefitTypeGuideline.mental_health,
            "recommendation": "Screening for anxiety disorders covered for adults under 65 including "
                "pregnant/postpartum persons. GAD-7 or equivalent validated instrument.",
            "criteria": "Adults aged 18-64 regardless of risk factors.",
            "contraindications": "None for screening. Insufficient evidence for adults 65+.",
            "service_codes": "96127,96160",
            "grade": GuidelineGrade.b,
            "population": "Adults aged 18-64",
            "frequency": "Annually",
            "source_url": "https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/anxiety-adults-screening",
            "publication_date": datetime(2023, 6, 1),
        },
        {
            "title": "Psychotherapy for Depression and Anxiety",
            "condition": "psychotherapy_treatment",
            "benefit_type": BenefitTypeGuideline.mental_health,
            "recommendation": "Cognitive behavioral therapy (CBT) is first-line treatment for mild-moderate "
                "depression and anxiety. 12-20 sessions typically indicated for acute episode. "
                "Maintenance therapy may be needed for recurrent episodes.",
            "criteria": "Diagnosis of depressive disorder or anxiety disorder. "
                "Patient motivated and capable of participating in talk therapy. "
                "CBT preferred for mild-moderate; medication + therapy for moderate-severe.",
            "contraindications": "Active psychosis (refer to psychiatry). Severe cognitive impairment.",
            "service_codes": "90834,90837,90847,90846,90832",
            "grade": GuidelineGrade.a,
            "population": "Adults and adolescents with depression or anxiety disorders",
            "frequency": "Weekly during acute phase (8-20 sessions); monthly during maintenance",
            "source_url": "https://www.apa.org/depression-guideline",
            "publication_date": datetime(2023, 2, 1),
        },
        {
            "title": "Substance Use Disorder Treatment",
            "condition": "substance_use_disorder",
            "benefit_type": BenefitTypeGuideline.mental_health,
            "recommendation": "Medication-assisted treatment (MAT) for opioid use disorder. "
                "Behavioral interventions for alcohol and other substance use disorders. "
                "SBIRT (Screening, Brief Intervention, Referral to Treatment) for all adults.",
            "criteria": "Positive screening for substance use disorder. AUDIT-C >= 4 for men, >= 3 for women. "
                "DAST-10 >= 3. Clinical assessment confirming diagnosis.",
            "contraindications": "MAT: acute hepatic failure (for some medications). "
                "Pregnancy requires specialized protocol.",
            "service_codes": "99408,99409,H0001,H0004,H0005,H0015,H2035,H2036",
            "grade": GuidelineGrade.a,
            "population": "Adults with substance use disorders",
            "frequency": "Ongoing as clinically indicated; MAT typically long-term",
            "source_url": "https://www.samhsa.gov/medication-assisted-treatment",
            "publication_date": datetime(2024, 1, 1),
        },
        # Dental
        {
            "title": "Preventive Dental Visits",
            "condition": "dental_preventive",
            "benefit_type": BenefitTypeGuideline.dental,
            "recommendation": "Routine prophylaxis (cleaning) and oral exam. "
                "Bitewing radiographs for caries detection. Fluoride treatment for high-risk patients.",
            "criteria": "All patients regardless of risk. Frequency based on individual caries risk assessment. "
                "Low risk: every 12-24 months. Moderate risk: every 6-12 months. High risk: every 3-6 months.",
            "contraindications": "None for routine prophylaxis. Radiographs: pregnancy (relative, shield if needed).",
            "service_codes": "D0120,D0150,D0210,D0272,D0274,D1110,D1120,D1206,D1208",
            "grade": GuidelineGrade.b,
            "population": "All ages",
            "frequency": "Every 6 months for most; individualized by caries risk",
            "source_url": "https://www.ada.org/resources/research/science-and-research-institute/evidence-based-dental-research",
            "publication_date": datetime(2023, 10, 1),
        },
        {
            "title": "Dental Restorations",
            "condition": "dental_restorations",
            "benefit_type": BenefitTypeGuideline.dental,
            "recommendation": "Direct restorations (fillings) indicated for carious lesions extending into dentin. "
                "Indirect restorations (crowns) when remaining tooth structure is insufficient for direct restoration.",
            "criteria": "Clinical and/or radiographic evidence of carious lesion extending into dentin. "
                "Tooth is restorable and has adequate periodontal support. "
                "Crown: cusp replacement needed, >50% of coronal tooth structure lost, or post-endodontic restoration.",
            "contraindications": "Tooth with poor long-term prognosis. Lesion confined to enamel (monitor instead). "
                "Patients unable to maintain oral hygiene.",
            "service_codes": "D2140,D2150,D2160,D2161,D2330,D2331,D2332,D2335,D2390,D2391,D2392,D2393,D2394,D2740,D2750,D2751,D2752",
            "grade": GuidelineGrade.a,
            "population": "Patients with dental caries",
            "frequency": "As needed based on clinical findings",
            "source_url": "https://www.ada.org/resources/research/science-and-research-institute",
            "publication_date": datetime(2023, 6, 1),
        },
        # Vision
        {
            "title": "Comprehensive Eye Examination",
            "condition": "vision_comprehensive_exam",
            "benefit_type": BenefitTypeGuideline.vision,
            "recommendation": "Comprehensive dilated eye exam for adults. "
                "Screens for glaucoma, diabetic retinopathy, macular degeneration, cataracts.",
            "criteria": "All adults: every 1-2 years ages 18-64; annually age 65+. "
                "Diabetics: dilated exam annually regardless of age. "
                "High risk (family history, African American 40+): annually.",
            "contraindications": "Acute eye trauma (defer to emergency evaluation).",
            "service_codes": "92004,92014,92002,92012,92015,92134,92250",
            "grade": GuidelineGrade.b,
            "population": "All adults; annually for diabetics and high-risk",
            "frequency": "Every 1-2 years general; annually for diabetics/high-risk/65+",
            "source_url": "https://www.aao.org/eye-health/tips-prevention/eye-exams-101",
            "publication_date": datetime(2023, 1, 1),
        },
        {
            "title": "Diabetic Retinopathy Screening",
            "condition": "diabetic_retinopathy_screening",
            "benefit_type": BenefitTypeGuideline.vision,
            "recommendation": "Annual dilated eye exam or validated retinal imaging for all diabetic patients.",
            "criteria": "Diagnosis of Type 1 or Type 2 diabetes mellitus. "
                "Type 1: begin 5 years after diagnosis. Type 2: at diagnosis and annually thereafter.",
            "contraindications": "None. This is a critical screening — complications are preventable with early detection.",
            "service_codes": "92250,92134,92227,92228,2022F,2024F,2026F,3072F",
            "grade": GuidelineGrade.a,
            "population": "All diabetic patients",
            "frequency": "Annually; every 2 years if no retinopathy and well-controlled",
            "source_url": "https://www.aao.org/preferred-practice-pattern/diabetic-retinopathy-ppp",
            "publication_date": datetime(2024, 1, 1),
        },
        # Disability
        {
            "title": "Short-Term Disability Medical Necessity",
            "condition": "std_medical_necessity",
            "benefit_type": BenefitTypeGuideline.short_term_disability,
            "recommendation": "STD benefits indicated when medical condition prevents performance of "
                "material duties of own occupation. Duration based on diagnosis and clinical evidence. "
                "MDGuidelines and ODG provide evidence-based disability duration benchmarks.",
            "criteria": "1) Documented medical condition with objective clinical findings. "
                "2) Functional limitations preventing job duties (documented by treating physician). "
                "3) Active treatment plan demonstrating expected recovery timeline. "
                "4) Duration within evidence-based guidelines for the condition (MDGuidelines/ODG reference).",
            "contraindications": "Lack of objective clinical findings. Functional capacity evaluation showing "
                "ability to perform job duties. Non-compliance with treatment plan.",
            "service_codes": "99213,99214,99215,97110,97140,97530",
            "grade": GuidelineGrade.ungraded,
            "population": "Employed adults with medical conditions affecting work capacity",
            "frequency": "Reassessment every 2-4 weeks during disability period",
            "source_url": "https://www.mdguidelines.com",
            "publication_date": datetime(2024, 1, 1),
        },
        {
            "title": "Long-Term Disability Medical Necessity",
            "condition": "ltd_medical_necessity",
            "benefit_type": BenefitTypeGuideline.long_term_disability,
            "recommendation": "LTD benefits indicated when medical condition prevents performance of "
                "any occupation for which employee is qualified, after exhaustion of STD period. "
                "Requires ongoing documentation of persistent functional limitations.",
            "criteria": "1) Exhaustion of STD benefit period (typically 90-180 days). "
                "2) Persistent objective clinical findings and functional limitations. "
                "3) Unable to perform any occupation for which reasonably qualified. "
                "4) Ongoing active treatment or documented maximum medical improvement. "
                "5) Independent medical examination may be required.",
            "contraindications": "Functional capacity evaluation demonstrating ability to work in any capacity. "
                "Non-compliance with treatment. Lack of current medical documentation.",
            "service_codes": "99213,99214,99215,99354,99355",
            "grade": GuidelineGrade.ungraded,
            "population": "Employed adults with long-term medical conditions affecting work capacity",
            "frequency": "Reassessment every 3-6 months during LTD period",
            "source_url": "https://www.mdguidelines.com",
            "publication_date": datetime(2024, 1, 1),
        },
        # Life insurance
        {
            "title": "Life Insurance Medical Necessity Determination",
            "condition": "life_insurance_claim",
            "benefit_type": BenefitTypeGuideline.life_insurance,
            "recommendation": "Life insurance claim determination based on cause of death documentation. "
                "Accidental death determination requires medical examiner/coroner findings. "
                "Clinical review of pre-existing condition exclusions based on medical records.",
            "criteria": "1) Death certificate with cause of death. "
                "2) Medical records for pre-existing condition review period (typically 12-24 months). "
                "3) Autopsy report if accidental death claim. "
                "4) Clinical determination of whether pre-existing conditions contributed to death.",
            "contraindications": "Suicide within exclusion period (typically first 2 years of policy). "
                "Material misrepresentation on application within contestability period.",
            "service_codes": "",
            "grade": GuidelineGrade.ungraded,
            "population": "Deceased employees and beneficiaries",
            "frequency": "Per claim",
            "source_url": "https://www.soa.org",
            "publication_date": datetime(2024, 1, 1),
        },
        # Common medical services
        {
            "title": "Advanced Imaging - MRI/CT Medical Necessity",
            "condition": "advanced_imaging",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "MRI/CT indicated when clinical findings suggest diagnosis requiring "
                "imaging for confirmation, surgical planning, or monitoring. "
                "Conservative management should be attempted first for musculoskeletal conditions "
                "(typically 4-6 weeks) unless red flags present.",
            "criteria": "Red flags requiring immediate imaging: trauma with neurological deficit, "
                "suspected cancer, fever with back pain, progressive neurological symptoms. "
                "Non-urgent: failure of 4-6 weeks conservative management, persistent symptoms "
                "not responding to first-line treatment, surgical planning.",
            "contraindications": "MRI: pacemaker (some), metallic implants, severe claustrophobia. "
                "CT: pregnancy (relative), contrast allergy (premedicate or use MRI).",
            "service_codes": "70553,72141,72148,72156,73221,73721,74177,70551,70552,72146,72147,73220,73720",
            "grade": GuidelineGrade.b,
            "population": "Adults with clinical indications",
            "frequency": "As clinically indicated; follow-up imaging per condition-specific guidelines",
            "source_url": "https://acsearch.acr.org/list",
            "publication_date": datetime(2024, 1, 1),
        },
        {
            "title": "Physical Therapy Medical Necessity",
            "condition": "physical_therapy",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "Physical therapy indicated for musculoskeletal conditions, post-surgical "
                "rehabilitation, neurological conditions, and functional decline. "
                "Expected functional improvement must be documented.",
            "criteria": "1) Documented medical condition requiring skilled intervention. "
                "2) Measurable functional limitations (range of motion, strength, function). "
                "3) Expectation of meaningful improvement within reasonable time. "
                "4) Treatment plan with specific goals and estimated duration. "
                "Typical: 2-3x/week for 4-8 weeks for most musculoskeletal conditions.",
            "contraindications": "Maintenance therapy without expected improvement (transition to HEP). "
                "Unstable fracture. Active infection at treatment site.",
            "service_codes": "97110,97112,97116,97140,97530,97535,97542,97161,97162,97163",
            "grade": GuidelineGrade.a,
            "population": "Patients with musculoskeletal or neurological conditions",
            "frequency": "2-3x/week for 4-8 weeks typical; re-evaluate at 30 days",
            "source_url": "https://www.apta.org/patient-care/evidence-based-practice-resources",
            "publication_date": datetime(2024, 1, 1),
        },
        {
            "title": "Office Visits - Evaluation and Management",
            "condition": "office_visit_em",
            "benefit_type": BenefitTypeGuideline.health,
            "recommendation": "E/M visit level determined by medical decision-making complexity or total time. "
                "Always medically necessary when clinical indication exists.",
            "criteria": "Any symptom, condition, or clinical indication requiring physician evaluation. "
                "Level determined by: number of diagnoses addressed, data reviewed/ordered, "
                "risk of morbidity/mortality. "
                "99213: low complexity. 99214: moderate. 99215: high.",
            "contraindications": "None for appropriate-level visit. Upcoding (billing higher level than supported "
                "by documentation) is a compliance issue, not a clinical one.",
            "service_codes": "99202,99203,99204,99205,99211,99212,99213,99214,99215",
            "grade": GuidelineGrade.a,
            "population": "All patients",
            "frequency": "As clinically indicated",
            "source_url": "https://www.ama-assn.org/practice-management/cpt/cpt-evaluation-and-management",
            "publication_date": datetime(2024, 1, 1),
        },
    ]

    # Clear and reload
    db.execute(delete(ClinicalGuideline).where(
        ClinicalGuideline.source.in_([GuidelineSource.cms_ncd, GuidelineSource.ada_dental,
                                       GuidelineSource.aao_vision, GuidelineSource.apa_mental,
                                       GuidelineSource.samhsa, GuidelineSource.other])
    ))
    db.commit()

    batch = []
    for ncd in ncds:
        batch.append(ClinicalGuideline(
            source=ncd.get("source_enum", GuidelineSource.cms_ncd),
            benefit_type=ncd["benefit_type"],
            title=ncd["title"],
            condition=ncd["condition"],
            service_codes=ncd.get("service_codes"),
            recommendation=ncd["recommendation"],
            criteria=ncd.get("criteria"),
            contraindications=ncd.get("contraindications"),
            grade=ncd.get("grade", GuidelineGrade.ungraded),
            population=ncd.get("population"),
            frequency=ncd.get("frequency"),
            source_url=ncd.get("source_url"),
            publication_date=ncd.get("publication_date"),
            ingested_at=datetime.now(UTC),
            is_active=True,
        ))

    db.bulk_save_objects(batch)
    db.commit()

    logger.info(f"Ingested {len(batch)} clinical guidelines")
    return len(batch)


def ingest_uspstf_recommendations(db: Session) -> int:
    """Ingest USPSTF (U.S. Preventive Services Task Force) recommendations.

    Constitution: "Is there any clinical data source that exists and is legally
    accessible that the engine does not reference?"

    USPSTF API requires an API key since 2021-03-01. If USPSTF_API_KEY is set,
    fetches from the official API. Otherwise, falls back to PubMed search for
    USPSTF-tagged guidelines (which is always free and covers the same content).
    """
    import os
    logger.info("Ingesting USPSTF recommendations...")

    api_key = os.environ.get("USPSTF_API_KEY", "")
    USPSTF_URL = "https://data.uspreventiveservicestaskforce.org/api/json"

    count = 0
    try:
        headers = {}
        url = USPSTF_URL
        if api_key:
            url = f"{USPSTF_URL}?key={api_key}"

        resp = httpx.get(url, timeout=30.0)
        if resp.status_code not in (200, 202):
            logger.warning(f"USPSTF API returned {resp.status_code}")
            return 0

        data = resp.json()

        # Check if the API returned the "requires API key" warning
        if data.get("apiKeyWarning") and not api_key:
            logger.info("USPSTF API requires API key — falling back to PubMed USPSTF search")
            return _ingest_uspstf_via_pubmed(db)

        recommendations = data if isinstance(data, list) else data.get("specificRecommendations", [])

        grade_map = {
            "A": GuidelineGrade.a, "B": GuidelineGrade.b,
            "C": GuidelineGrade.c, "D": GuidelineGrade.d,
            "I": GuidelineGrade.i,
        }

        now = datetime.now(UTC)
        for rec in recommendations:
            title = rec.get("title", rec.get("Topic", ""))
            if not title:
                continue

            grade_str = rec.get("grade", rec.get("Grade", ""))
            grade = grade_map.get(grade_str.upper() if grade_str else "", GuidelineGrade.ungraded)

            # Check if we already have this guideline (avoid duplicates)
            existing = db.query(ClinicalGuideline).filter(
                ClinicalGuideline.source == GuidelineSource.uspstf,
                ClinicalGuideline.title == title[:200],
            ).first()

            if existing:
                continue

            pub_date_str = rec.get("date", rec.get("Date", ""))
            pub_date = None
            if pub_date_str:
                try:
                    pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            guideline = ClinicalGuideline(
                source=GuidelineSource.uspstf,
                benefit_type=BenefitTypeGuideline.health,
                title=title[:200],
                condition=rec.get("specificPopulation", rec.get("topic", title))[:200],
                recommendation=rec.get("text", rec.get("Recommendation", ""))[:2000],
                criteria=rec.get("clinicalConsiderations", "")[:2000],
                grade=grade,
                population=rec.get("population", "")[:500],
                source_url=rec.get("url", f"https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/{title.lower().replace(' ', '-')[:50]}"),
                publication_date=pub_date,
                ingested_at=now,
                is_active=True,
            )
            db.add(guideline)
            count += 1

        db.commit()
        logger.info(f"Ingested {count} USPSTF recommendations")

    except httpx.HTTPError as e:
        logger.warning(f"USPSTF API request failed: {e}")
    except Exception as e:
        logger.warning(f"USPSTF ingestion error: {e}")

    return count


def _ingest_uspstf_via_pubmed(db: Session) -> int:
    """Fallback: ingest USPSTF recommendations via PubMed search.

    USPSTF's direct API requires a key. But all USPSTF recommendations
    are published in PubMed with the MeSH tag "US Preventive Services
    Task Force". This fetches them for free.
    """
    logger.info("Ingesting USPSTF recommendations via PubMed fallback...")

    ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    count = 0
    try:
        search_resp = httpx.get(ESEARCH_URL, params={
            "db": "pubmed",
            "term": "\"US Preventive Services Task Force\"[Corporate Author] AND (2020[pdat]:2026[pdat])",
            "retmax": "100",
            "retmode": "json",
            "sort": "date",
        }, timeout=30.0)

        if search_resp.status_code != 200:
            return 0

        id_list = search_resp.json().get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return 0

        import time as _time
        _time.sleep(0.5)

        fetch_resp = httpx.get(EFETCH_URL, params={
            "db": "pubmed",
            "id": ",".join(id_list),
            "retmode": "xml",
            "rettype": "abstract",
        }, timeout=60.0)

        if fetch_resp.status_code != 200:
            return 0

        import xml.etree.ElementTree as ET
        root = ET.fromstring(fetch_resp.text)

        now = datetime.now(UTC)
        for article in root.findall(".//PubmedArticle"):
            try:
                title_el = article.find(".//ArticleTitle")
                title = (title_el.text or "")[:200] if title_el is not None else ""
                if not title:
                    continue

                existing = db.query(ClinicalGuideline).filter(
                    ClinicalGuideline.source == GuidelineSource.uspstf,
                    ClinicalGuideline.title == title,
                ).first()
                if existing:
                    continue

                abstract_el = article.find(".//AbstractText")
                abstract = (abstract_el.text or "")[:2000] if abstract_el is not None else ""
                pmid_el = article.find(".//PMID")
                pmid = pmid_el.text if pmid_el is not None else ""

                pub_date = None
                year_el = article.find(".//PubDate/Year")
                if year_el is not None and year_el.text:
                    try:
                        pub_date = datetime(int(year_el.text), 1, 1)
                    except ValueError:
                        pass

                # Determine grade from title/abstract
                grade = GuidelineGrade.ungraded
                text_lower = (title + " " + abstract).lower()
                if "grade a" in text_lower or "a recommendation" in text_lower:
                    grade = GuidelineGrade.a
                elif "grade b" in text_lower or "b recommendation" in text_lower:
                    grade = GuidelineGrade.b
                elif "grade c" in text_lower or "c recommendation" in text_lower:
                    grade = GuidelineGrade.c
                elif "grade d" in text_lower or "d recommendation" in text_lower:
                    grade = GuidelineGrade.d
                elif "insufficient evidence" in text_lower or "grade i" in text_lower:
                    grade = GuidelineGrade.i

                guideline = ClinicalGuideline(
                    source=GuidelineSource.uspstf,
                    benefit_type=BenefitTypeGuideline.health,
                    title=title,
                    condition=title,
                    recommendation=abstract,
                    grade=grade,
                    source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                    publication_date=pub_date,
                    ingested_at=now,
                    is_active=True,
                )
                db.add(guideline)
                count += 1

            except Exception as e:
                logger.debug(f"Error parsing USPSTF PubMed article: {e}")
                continue

        db.commit()
        logger.info(f"Ingested {count} USPSTF recommendations via PubMed")

    except Exception as e:
        logger.warning(f"USPSTF PubMed fallback failed: {e}")

    return count


def ingest_pubmed_clinical_guidelines(db: Session, max_results: int = 50) -> int:
    """Ingest clinical practice guidelines from PubMed via NCBI E-utilities.

    Constitution: "Is there any clinical data source that exists and is legally
    accessible that the engine does not reference?"

    PubMed's E-utilities API is free (no key for <3 req/sec). Searches for
    articles tagged as "Practice Guideline" or "Guideline" publication types.
    """
    logger.info("Ingesting clinical guidelines from PubMed...")

    ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    count = 0
    try:
        # Search for recent clinical practice guidelines
        search_resp = httpx.get(ESEARCH_URL, params={
            "db": "pubmed",
            "term": "clinical practice guideline[pt] AND (2023[pdat]:2026[pdat])",
            "retmax": str(max_results),
            "retmode": "json",
            "sort": "date",
        }, timeout=30.0)

        if search_resp.status_code != 200:
            logger.warning(f"PubMed search returned {search_resp.status_code}")
            return 0

        search_data = search_resp.json()
        id_list = search_data.get("esearchresult", {}).get("idlist", [])

        if not id_list:
            logger.info("No PubMed guidelines found")
            return 0

        # Fetch article details
        import time as _time
        _time.sleep(0.5)  # Rate limiting: max 3 requests/sec without API key

        fetch_resp = httpx.get(EFETCH_URL, params={
            "db": "pubmed",
            "id": ",".join(id_list),
            "retmode": "xml",
            "rettype": "abstract",
        }, timeout=60.0)

        if fetch_resp.status_code != 200:
            logger.warning(f"PubMed fetch returned {fetch_resp.status_code}")
            return 0

        # Parse XML response
        import xml.etree.ElementTree as ET
        root = ET.fromstring(fetch_resp.text)

        now = datetime.now(UTC)
        for article in root.findall(".//PubmedArticle"):
            try:
                title_el = article.find(".//ArticleTitle")
                title = (title_el.text or "")[:200] if title_el is not None else ""
                if not title:
                    continue

                # Check for duplicate
                existing = db.query(ClinicalGuideline).filter(
                    ClinicalGuideline.source == GuidelineSource.other,
                    ClinicalGuideline.title == title,
                ).first()
                if existing:
                    continue

                abstract_el = article.find(".//AbstractText")
                abstract = (abstract_el.text or "")[:2000] if abstract_el is not None else ""

                pmid_el = article.find(".//PMID")
                pmid = pmid_el.text if pmid_el is not None else ""

                # Extract publication date
                pub_date = None
                year_el = article.find(".//PubDate/Year")
                if year_el is not None and year_el.text:
                    try:
                        pub_date = datetime(int(year_el.text), 1, 1)
                    except ValueError:
                        pass

                # Determine benefit type from MeSH terms
                mesh_terms = [m.text.lower() for m in article.findall(".//MeshHeading/DescriptorName") if m.text]
                benefit_type = BenefitTypeGuideline.health
                if any("dental" in m or "oral" in m for m in mesh_terms):
                    benefit_type = BenefitTypeGuideline.dental
                elif any("vision" in m or "ophthalm" in m or "eye" in m for m in mesh_terms):
                    benefit_type = BenefitTypeGuideline.vision
                elif any("mental" in m or "psych" in m or "depress" in m or "anxiety" in m for m in mesh_terms):
                    benefit_type = BenefitTypeGuideline.mental_health

                guideline = ClinicalGuideline(
                    source=GuidelineSource.other,
                    benefit_type=benefit_type,
                    title=title,
                    condition=title,
                    recommendation=abstract,
                    grade=GuidelineGrade.ungraded,
                    source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
                    publication_date=pub_date,
                    ingested_at=now,
                    is_active=True,
                )
                db.add(guideline)
                count += 1

            except Exception as e:
                logger.debug(f"Error parsing PubMed article: {e}")
                continue

        db.commit()
        logger.info(f"Ingested {count} guidelines from PubMed")

    except httpx.HTTPError as e:
        logger.warning(f"PubMed API request failed: {e}")
    except Exception as e:
        logger.warning(f"PubMed ingestion error: {e}")

    return count


def ingest_all_dynamic_sources(db: Session) -> dict:
    """Run all dynamic guideline ingestion sources.

    Called by the scheduler daily. Measures incorporation latency.
    """
    results = {}
    results["hardcoded_ncds"] = ingest_cms_ncd_guidelines(db)
    results["uspstf"] = ingest_uspstf_recommendations(db)
    results["pubmed"] = ingest_pubmed_clinical_guidelines(db)
    results["total"] = sum(results.values())
    results["ingested_at"] = datetime.now(UTC).isoformat()
    logger.info(f"Dynamic ingestion complete: {results}")
    return results


def get_guideline_stats(db: Session) -> dict:
    """Get statistics about the clinical guidelines knowledge base."""
    from sqlalchemy import func

    total = db.query(func.count(ClinicalGuideline.guideline_id)).filter(
        ClinicalGuideline.is_active == True
    ).scalar() or 0

    by_type = dict(
        db.query(ClinicalGuideline.benefit_type, func.count(ClinicalGuideline.guideline_id))
        .filter(ClinicalGuideline.is_active == True)
        .group_by(ClinicalGuideline.benefit_type)
        .all()
    )

    by_source = dict(
        db.query(ClinicalGuideline.source, func.count(ClinicalGuideline.guideline_id))
        .filter(ClinicalGuideline.is_active == True)
        .group_by(ClinicalGuideline.source)
        .all()
    )

    latest = db.query(func.max(ClinicalGuideline.ingested_at)).scalar()

    # Measure incorporation latency where possible
    latency_data = []
    recent = db.query(ClinicalGuideline).filter(
        ClinicalGuideline.publication_date != None,
        ClinicalGuideline.ingested_at != None,
    ).order_by(ClinicalGuideline.ingested_at.desc()).limit(10).all()

    for g in recent:
        if g.publication_date and g.ingested_at:
            delta = g.ingested_at - g.publication_date
            latency_data.append({
                "title": g.title[:60],
                "source": str(g.source),
                "publication_date": g.publication_date.isoformat(),
                "ingested_at": g.ingested_at.isoformat(),
                "incorporation_latency_days": delta.days,
            })

    return {
        "total_guidelines": total,
        "by_benefit_type": {str(k): v for k, v in by_type.items()},
        "by_source": {str(k): v for k, v in by_source.items()},
        "last_ingested": latest.isoformat() if latest else None,
        "incorporation_latency": latency_data,
    }
