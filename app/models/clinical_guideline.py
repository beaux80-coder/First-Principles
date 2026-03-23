"""Clinical guidelines knowledge base model (Function 1).

Constitution: "Sole inputs: patient symptoms, patient medical history,
current peer-reviewed evidence-based clinical guidelines from recognized
medical authorities."

This model stores clinical guidelines that the determination engine
references when making medical necessity decisions.
"""

import enum

from sqlalchemy import Column, String, Text, DateTime, Enum, Integer, Boolean
from sqlalchemy.sql import func

from app.compat import GUID
from app.database import Base


class GuidelineSource(str, enum.Enum):
    """Recognized medical authority sources."""
    uspstf = "uspstf"                     # US Preventive Services Task Force
    cms_ncd = "cms_ncd"                   # CMS National Coverage Determinations
    cms_lcd = "cms_lcd"                   # CMS Local Coverage Determinations
    aha = "aha"                           # American Heart Association
    ada_dental = "ada_dental"             # American Dental Association
    aao_vision = "aao_vision"             # American Academy of Ophthalmology
    apa_mental = "apa_mental"             # American Psychiatric Association
    acog = "acog"                         # American College of OB/GYN
    aap = "aap"                           # American Academy of Pediatrics
    asco = "asco"                         # American Society of Clinical Oncology
    nice = "nice"                         # UK NICE guidelines (peer-reviewed reference)
    cochrane = "cochrane"                 # Cochrane systematic reviews
    who = "who"                           # World Health Organization
    cdc = "cdc"                           # Centers for Disease Control
    samhsa = "samhsa"                     # SAMHSA (mental health/substance abuse)
    other = "other"


class BenefitTypeGuideline(str, enum.Enum):
    health = "health"
    dental = "dental"
    vision = "vision"
    mental_health = "mental_health"
    life_insurance = "life_insurance"
    short_term_disability = "short_term_disability"
    long_term_disability = "long_term_disability"
    all_types = "all_types"


class GuidelineGrade(str, enum.Enum):
    """Evidence strength grade."""
    a = "A"           # High certainty of substantial net benefit
    b = "B"           # High certainty of moderate benefit or moderate certainty of substantial benefit
    c = "C"           # Moderate certainty of small net benefit
    d = "D"           # Moderate/high certainty of no net benefit or harms outweigh benefits
    i = "I"           # Insufficient evidence
    ungraded = "ungraded"


class ClinicalGuideline(Base):
    __tablename__ = "clinical_guidelines"

    guideline_id = Column(GUID(), primary_key=True, server_default=func.gen_random_uuid() if False else None)
    source = Column(Enum(GuidelineSource), nullable=False, index=True)
    benefit_type = Column(Enum(BenefitTypeGuideline), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    condition = Column(String(255), nullable=False, index=True)
    service_codes = Column(Text, nullable=True)  # Comma-separated CPT/HCPCS/CDT codes
    recommendation = Column(Text, nullable=False)  # The actual clinical recommendation
    criteria = Column(Text, nullable=True)  # Structured criteria for when service is indicated
    contraindications = Column(Text, nullable=True)  # When service should NOT be provided
    grade = Column(Enum(GuidelineGrade), default=GuidelineGrade.ungraded)
    population = Column(Text, nullable=True)  # Target population (age, sex, risk factors)
    frequency = Column(String(255), nullable=True)  # Recommended frequency (e.g., "annually", "every 3 years")
    source_url = Column(Text, nullable=True)
    publication_date = Column(DateTime, nullable=True)
    ingested_at = Column(DateTime, server_default=func.now())
    last_verified = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    version = Column(Integer, default=1)

    def __init__(self, **kwargs):
        from uuid import uuid4
        if 'guideline_id' not in kwargs:
            kwargs['guideline_id'] = str(uuid4())
        super().__init__(**kwargs)
