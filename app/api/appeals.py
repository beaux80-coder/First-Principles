"""Appeals Process API (Function 1, Questions 9-11).

Constitution:
Q9: "Clear, plain-language explanation of denial and complete appeal rights."
Q10: "Internal review, external IRO review, expedited review for urgent cases."
Q11: "All appeal proceedings recorded in immutable, cryptographically secured log."

Endpoints:
- POST /appeals/           -- File an appeal against a denied claim
- GET  /appeals/overdue    -- Check for overdue appeal deadlines
- GET  /appeals/{appeal_id} -- Get appeal status with full timeline
- POST /appeals/{appeal_id}/review     -- Process an appeal review decision
- POST /appeals/{appeal_id}/assign-iro -- Assign IRO for external review
- GET  /appeals/claim/{claim_id}       -- Get all appeals for a claim
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/appeals", tags=["appeals-process"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class AppealFileRequest(BaseModel):
    claim_id: str
    appeal_reason: str
    appeal_type: str = "internal_level_1"
    is_expedited: bool = False
    expedited_reason: Optional[str] = None


class AppealReviewRequest(BaseModel):
    reviewer_id: str
    reviewer_notes: str
    outcome: str  # upheld, overturned, partial_reversal
    reviewer_type: str = "clinical_professional"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/")
def file_appeal(req: AppealFileRequest, db: Session = Depends(get_db)):
    """File an appeal against a denied claim.

    Constitution Q9: generates a plain-language denial notice explaining what
    was denied, why, and the employee's complete appeal rights at each level.

    The appeal is recorded in the immutable, cryptographically secured audit log
    and a review deadline is set based on the appeal type:
    - internal_level_1: 30 days
    - internal_level_2: 30 days
    - external_iro: 45 days
    - expedited: 72 hours
    """
    from app.services.appeals_process import file_appeal as svc_file_appeal

    try:
        claim_uuid = uuid.UUID(req.claim_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid claim_id format. Must be a valid UUID.",
        )

    result = svc_file_appeal(
        db=db,
        claim_id=claim_uuid,
        appeal_reason=req.appeal_reason,
        appeal_type=req.appeal_type,
        is_expedited=req.is_expedited,
        expedited_reason=req.expedited_reason,
    )

    if "error" in result:
        status_map = {
            "claim_not_found": 404,
            "claim_not_denied": 400,
            "invalid_appeal_type": 400,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result.get("detail", result["error"]),
        )

    return result


@router.get("/overdue")
def check_overdue_appeals(db: Session = Depends(get_db)):
    """Check all open appeals for overdue or approaching deadlines.

    ERISA requires timely processing of all appeals. This endpoint
    returns a compliance summary with:
    - Overdue appeals (past their review deadline)
    - Approaching-deadline appeals (within 5 days of deadline)
    - On-track appeals
    - Overall compliance status (compliant / at_risk / non_compliant)
    """
    from app.services.appeals_process import check_appeal_deadlines

    return check_appeal_deadlines(db)


@router.get("/{appeal_id}")
def get_appeal(appeal_id: str, db: Session = Depends(get_db)):
    """Get full appeal status with complete timeline.

    Constitution Q11: all appeal proceedings, decisions, and outcomes are
    recorded in the immutable audit log. This endpoint returns the complete
    record including every timeline event.
    """
    from app.services.appeals_process import get_appeal_status

    try:
        aid = uuid.UUID(appeal_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid appeal_id format. Must be a valid UUID.",
        )

    result = get_appeal_status(db, aid)
    if not result:
        raise HTTPException(status_code=404, detail="Appeal not found.")

    return result


@router.post("/{appeal_id}/review")
def process_appeal_review(
    appeal_id: str,
    req: AppealReviewRequest,
    db: Session = Depends(get_db),
):
    """Process an appeal review decision.

    Constitution Q10: the reviewer must be a qualified clinical professional
    who was not involved in the original determination.

    Outcomes:
    - overturned: claim is approved, payment proceeds
    - upheld (Level 1): auto-escalated to Level 2
    - upheld (Level 2): external IRO information provided
    - partial_reversal: some services approved, employee may appeal remainder
    """
    from app.services.appeals_process import process_appeal

    try:
        aid = uuid.UUID(appeal_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid appeal_id format. Must be a valid UUID.",
        )

    result = process_appeal(
        db=db,
        appeal_id=aid,
        reviewer_id=req.reviewer_id,
        reviewer_notes=req.reviewer_notes,
        outcome=req.outcome,
        reviewer_type=req.reviewer_type,
    )

    if "error" in result:
        status_map = {
            "appeal_not_found": 404,
            "appeal_already_decided": 409,
            "invalid_outcome": 400,
            "invalid_reviewer_type": 400,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result.get("detail", result["error"]),
        )

    return result


@router.post("/{appeal_id}/assign-iro")
def assign_iro(appeal_id: str, db: Session = Depends(get_db)):
    """Assign an Independent Review Organization for external review.

    Constitution Q10: external independent review by a qualified IRO.
    The IRO is URAC-accredited with no conflicts of interest.

    Auto-selects from the IRO registry using round-robin assignment
    and sets a 45-day review deadline.
    """
    from app.services.appeals_process import assign_iro as svc_assign_iro

    try:
        aid = uuid.UUID(appeal_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid appeal_id format. Must be a valid UUID.",
        )

    result = svc_assign_iro(db, aid)

    if "error" in result:
        status_map = {
            "appeal_not_found": 404,
            "not_external_appeal": 400,
            "iro_already_assigned": 409,
            "no_active_iros": 503,
        }
        raise HTTPException(
            status_code=status_map.get(result["error"], 400),
            detail=result.get("detail", result["error"]),
        )

    return result


@router.get("/claim/{claim_id}")
def get_appeals_for_claim(claim_id: str, db: Session = Depends(get_db)):
    """Get all appeals filed for a specific claim.

    Returns the full appeal chain showing how the appeal has progressed
    through internal Level 1, Level 2, and external IRO stages.
    """
    from app.services.appeals_process import get_appeals_for_claim as svc_get

    try:
        cid = uuid.UUID(claim_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid claim_id format. Must be a valid UUID.",
        )

    return svc_get(db, cid)
