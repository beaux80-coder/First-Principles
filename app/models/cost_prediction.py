"""Cost Prediction models — Function 3.

Stores every prediction and accuracy measurement as structured data
feeding Function 8 (Constitution F3 items 9, 10).
"""

import enum
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Numeric, DateTime, Text, JSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.compat import GUID
from app.database import Base


class PredictionMethod(str, enum.Enum):
    gradient_boosting_ensemble = "gradient_boosting_ensemble"
    actuarial_heuristic = "actuarial_heuristic"
    public_data_baseline = "public_data_baseline"


class CostPredictionRecord(Base):
    """Persisted prediction record feeding F8.

    Every prediction and its eventual actual outcome are stored so
    accuracy can be tracked over time (Constitution F3 items 9-10).
    """

    __tablename__ = "cost_prediction_records"

    prediction_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    employer_id: Mapped[str] = mapped_column(String(100), index=True)
    benefit_type: Mapped[str] = mapped_column(String(50), index=True)
    predicted_pepm: Mapped[float] = mapped_column(Numeric(12, 2))
    ci_lower: Mapped[float] = mapped_column(Numeric(12, 2))
    ci_upper: Mapped[float] = mapped_column(Numeric(12, 2))
    confidence_pct: Mapped[float] = mapped_column(Numeric(5, 1))
    funding_recommendation_pepm: Mapped[float] = mapped_column(Numeric(12, 2))
    prediction_method: Mapped[PredictionMethod] = mapped_column(
        SAEnum(PredictionMethod)
    )
    data_sources_used: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Actual outcome — filled when claims data is available
    actual_pepm: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    absolute_pct_error: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    predicted_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )
    actual_measured_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AccuracySnapshot(Base):
    """Point-in-time accuracy measurement so improvement is trackable.

    Constitution F3 item 9: accuracy tracked over time.
    """

    __tablename__ = "cost_prediction_accuracy_snapshots"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    measured_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), index=True,
    )
    overall_mape_pct: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    mape_by_benefit_type: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    employers_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    data_points: Mapped[int] = mapped_column(Integer, default=0)
    prediction_method: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
