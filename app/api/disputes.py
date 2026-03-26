"""Dispute resolution API — Constitution F2 Q8-Q9.

Provider disputes payment amounts. System resolves programmatically
where possible, escalates to human review when needed.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.dispute_resolution import (
    file_dispute,
    auto_resolve,
    escalate_to_human,
    resolve_dispute,
    get_dispute,
)

router = APIRouter(prefix="/disputes", tags=["disputes"])


class DisputeFileRequest(BaseModel):
    claim_id: str = Field(description="Claim ID being disputed")
    provider_npi: str = Field(description="Provider NPI filing the dispute")
    dispute_type: str = Field(description="factual_error or price_disagreement")
    provider_stated_amount: float = Field(ge=0, description="Amount provider believes is correct")
    system_verified_amount: float = Field(ge=0, description="Amount system verified and paid")


class ResolveDisputeRequest(BaseModel):
    resolution: str = Field(description="Resolution description")
    resolution_method: str = Field(default="human", description="programmatic or human")


@router.post("/file")
def api_file_dispute(request: DisputeFileRequest, db: Session = Depends(get_db)):
    """Provider files a dispute on a payment amount."""
    return file_dispute(
        db=db,
        claim_id=request.claim_id,
        provider_npi=request.provider_npi,
        dispute_type=request.dispute_type,
        provider_stated_amount=request.provider_stated_amount,
        system_verified_amount=request.system_verified_amount,
    )


@router.post("/{dispute_id}/resolve")
def api_auto_resolve(dispute_id: str, db: Session = Depends(get_db)):
    """Attempt automatic resolution using published price references."""
    return auto_resolve(db, dispute_id)


@router.post("/{dispute_id}/escalate")
def api_escalate(dispute_id: str, db: Session = Depends(get_db)):
    """Escalate to human review when auto-resolution fails."""
    return escalate_to_human(db, dispute_id)


@router.post("/{dispute_id}/final-resolve")
def api_final_resolve(
    dispute_id: str,
    request: ResolveDisputeRequest,
    db: Session = Depends(get_db),
):
    """Record final dispute resolution."""
    return resolve_dispute(db, dispute_id, request.resolution, request.resolution_method)


@router.get("/{dispute_id}")
def api_get_dispute(dispute_id: str, db: Session = Depends(get_db)):
    """Get dispute details."""
    result = get_dispute(db, dispute_id)
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Dispute not found")
    return result


@router.get("/provider/{npi}")
def api_get_provider_disputes(npi: str, db: Session = Depends(get_db)):
    """List all disputes for a provider."""
    from app.models.dispute import Dispute
    disputes = db.query(Dispute).filter(Dispute.provider_npi == npi).all()
    return [
        {
            "dispute_id": str(d.dispute_id),
            "claim_id": d.claim_id,
            "dispute_type": d.dispute_type.value,
            "status": d.status.value,
            "created_at": d.created_at.isoformat(),
        }
        for d in disputes
    ]
