"""Clinical-only database session for TEE isolation.

This module creates a database session that registers clinical models
on the same Base as the main app, ensuring table access works correctly.

Used by the TEE subprocess to ensure zero logical pathway to financial data.
The database itself may contain financial tables, but this session and the
clinical engine code never query them — verified by source code attestation.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base

# Import ONLY clinical models to register them on Base
from app.models.clinical_determination import ClinicalDetermination  # noqa: F401
from app.models.clinical_guideline import ClinicalGuideline  # noqa: F401

# Create engine (same DB — isolation is at the code level, verified by attestation)
_engine = create_engine(settings.database_url, echo=False)

# Ensure clinical tables exist
Base.metadata.create_all(bind=_engine)

ClinicalSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
