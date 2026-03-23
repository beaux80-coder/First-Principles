import uuid

import enum

from sqlalchemy import String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class BenefitType(str, enum.Enum):
    health = "health"
    dental = "dental"
    vision = "vision"
    life = "life"
    std = "std"
    ltd = "ltd"
    mental_health = "mental_health"


class CodeType(str, enum.Enum):
    cpt = "cpt"
    hcpcs = "hcpcs"
    ndc = "ndc"
    cdt = "cdt"
    other = "other"


class Service(Base):
    __tablename__ = "services"

    service_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String(20), index=True)
    code_type: Mapped[CodeType] = mapped_column(SAEnum(CodeType))
    benefit_type: Mapped[BenefitType] = mapped_column(SAEnum(BenefitType))
    description: Mapped[str] = mapped_column(String(500))
