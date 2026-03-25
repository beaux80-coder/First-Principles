import uuid
from datetime import datetime

import enum

from sqlalchemy import ForeignKey, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base
from app.encryption import EncryptedString


class EmployeeStatus(str, enum.Enum):
    active = "active"
    terminated = "terminated"
    cobra = "cobra"


class Employee(Base):
    __tablename__ = "employees"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    employer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employers.employer_id"), index=True
    )
    status: Mapped[EmployeeStatus] = mapped_column(
        SAEnum(EmployeeStatus), default=EmployeeStatus.active
    )
    enrolled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Demographics stored as encrypted JSON (age, zip, dependents)
    demographics_encrypted: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)

    employer = relationship("Employer", back_populates="employees")
    claims = relationship("Claim", back_populates="employee")
    care_episodes = relationship("CareEpisode", back_populates="employee")
