import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Numeric, Enum as SAEnum, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base
from app.encryption import EncryptedString

import enum


class EmployerStatus(str, enum.Enum):
    prospect = "prospect"
    benchmark = "benchmark"
    shadow = "shadow"
    active = "active"
    churned = "churned"


class BaselineSource(str, enum.Enum):
    actual_prior_spend = "actual_prior_spend"
    market_rate = "market_rate"


class Employer(Base):
    __tablename__ = "employers"

    employer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255))
    ein_encrypted: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geography: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[EmployerStatus] = mapped_column(
        SAEnum(EmployerStatus), default=EmployerStatus.prospect
    )
    baseline_cost_pepm: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    baseline_source: Mapped[BaselineSource | None] = mapped_column(
        SAEnum(BaselineSource), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    employees = relationship("Employee", back_populates="employer")
    claims = relationship("Claim", back_populates="employer")
    benchmark_queries = relationship("BenchmarkQuery", back_populates="employer")
