"""Tracks data pipeline contribution and downstream improvement per employer.

Constitution F8: "Each additional employer must produce measurable improvement
in at least one downstream function."
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Float, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class DataPipelineMetric(Base):
    __tablename__ = "data_pipeline_metrics"

    metric_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    employer_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    metric_type: Mapped[str] = mapped_column(String(100), index=True)
    # Types: data_contribution, downstream_improvement, cross_type_signal,
    #        data_completeness, public_coverage
    benefit_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    value: Mapped[float] = mapped_column(Float)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
