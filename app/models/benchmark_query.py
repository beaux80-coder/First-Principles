import uuid
from datetime import datetime

import enum

from sqlalchemy import ForeignKey, DateTime, JSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.compat import GUID
from app.database import Base


class BenchmarkStage(str, enum.Enum):
    static = "static"
    shadow = "shadow"
    activated = "activated"


class BenchmarkQuery(Base):
    __tablename__ = "benchmark_queries"

    query_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employers.employer_id"), nullable=True
    )
    inputs: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stage: Mapped[BenchmarkStage] = mapped_column(
        SAEnum(BenchmarkStage), default=BenchmarkStage.static
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    employer = relationship("Employer", back_populates="benchmark_queries")
