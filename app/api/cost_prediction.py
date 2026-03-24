"""Cost Prediction API endpoints — Function 3 (Constitution).

POST /cost-prediction/predict   — Generate cost prediction for an employer
GET  /cost-prediction/accuracy  — Get prediction accuracy metrics (MAPE)
GET  /cost-prediction/methodology — Describe the prediction methodology
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.cost_prediction import (
    predict_employer_costs,
    get_prediction_accuracy,
    get_prediction_methodology,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cost-prediction", tags=["cost-prediction"])


class CostPredictionRequest(BaseModel):
    """Request body for cost prediction."""

    employer_id: str = Field(..., description="UUID of the employer")
    benefit_types: Optional[list[str]] = Field(
        None,
        description=(
            "Benefit types to predict. Defaults to all. "
            "Options: health, dental, vision, life, std, ltd, mental_health"
        ),
    )


@router.post("/predict")
def predict_costs(
    request: CostPredictionRequest,
    db: Session = Depends(get_db),
):
    """Generate per-employer cost predictions across benefit types.

    Constitution F3: per-employer cost predictions covering all benefit types.
    Incorporates all available F8 data, measures accuracy as absolute
    percentage deviation, and feeds F7 (pricing) and F7A (stop-loss).
    """
    result = predict_employer_costs(
        db,
        employer_id=request.employer_id,
        benefit_types=request.benefit_types,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/accuracy")
def prediction_accuracy(db: Session = Depends(get_db)):
    """Get prediction accuracy metrics across all employers.

    Constitution F3 metric: absolute percentage deviation (MAPE)
    measured per employer, per benefit type, and aggregate.
    Auto-improves as new data becomes available.
    """
    return get_prediction_accuracy(db)


@router.get("/methodology")
def prediction_methodology():
    """Describe the cost prediction methodology.

    Transparency requirement: methodology is explainable and auditable.
    Covers model architecture, features, data sources, accuracy
    measurement, auto-improvement policy, and downstream consumers
    (F7 pricing, F7A stop-loss).
    """
    return get_prediction_methodology()
