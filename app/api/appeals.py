"""Appeals API — Constitution F1 Q9-Q11.

Full appeals process compliant with ERISA 503, ACA 2719, and state mandates.
Every step that is legally automatable is fully automated.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.appeal_management import (
    file_appeal,
    process_internal_review,
    escalate_to_external_review,
    request_expedited_review,
    resolve_appeal,
    get_appeal_metrics,
)

router = APIRouter(prefix="/appeals", tags=["appeals"])


class AppealFileRequest(BaseModel):
    claim_id: str = Field(description="ID of the denied claim to appeal")
    employee_id: str = Field(description="ID of the employee filing the appeal")
    appeal_rationale: str = Field(description="Reason for appealing the denial")
    new_evidence: dict | None = Field(default=None, description="New clinical evidence")
    appeal_type: str = Field(default="standard", description="standard, expedited, or external_iro")


class ExpediteRequest(BaseModel):
    urgency_reason: str = Field(description="Reason this qualifies for expedited review")


class ResolveRequest(BaseModel):
    decision: str = Field(description="overturned, partially_overturned, or upheld")
    decision_reasoning: str = Field(description="Full reasoning for the decision")
    guidelines_referenced: list[str] | None = Field(default=None)


@router.post("/file")
def api_file_appeal(request: AppealFileRequest, db: Session = Depends(get_db)):
    """File an appeal on a denied claim. ERISA 503 compliant."""
    return file_appeal(
        db=db,
        claim_id=request.claim_id,
        employee_id=request.employee_id,
        appeal_rationale=request.appeal_rationale,
        new_evidence=request.new_evidence,
        appeal_type=request.appeal_type,
    )


@router.post("/{appeal_id}/review")
def api_internal_review(appeal_id: str, db: Session = Depends(get_db)):
    """Process internal appeal review by independent clinical professional."""
    return process_internal_review(db, appeal_id)


@router.post("/{appeal_id}/expedite")
def api_expedite(appeal_id: str, request: ExpediteRequest, db: Session = Depends(get_db)):
    """Request expedited review for urgent/emergent situations (72-hr turnaround)."""
    return request_expedited_review(db, appeal_id, request.urgency_reason)


@router.post("/{appeal_id}/external-review")
def api_external_review(appeal_id: str, db: Session = Depends(get_db)):
    """Escalate to external Independent Review Organization (IRO). ACA 2719."""
    return escalate_to_external_review(db, appeal_id)


@router.post("/{appeal_id}/resolve")
def api_resolve(appeal_id: str, request: ResolveRequest, db: Session = Depends(get_db)):
    """Record appeal decision with full audit trail."""
    return resolve_appeal(
        db=db,
        appeal_id=appeal_id,
        decision=request.decision,
        decision_reasoning=request.decision_reasoning,
        guidelines_referenced=request.guidelines_referenced,
    )


@router.get("/{appeal_id}")
def api_get_appeal(appeal_id: str, db: Session = Depends(get_db)):
    """Get appeal status and history."""
    from app.models.appeal import Appeal
    appeal = db.query(Appeal).filter(Appeal.appeal_id == appeal_id).first()
    if not appeal:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Appeal not found")
    return {
        "appeal_id": str(appeal.appeal_id),
        "claim_id": str(appeal.claim_id),
        "appeal_type": appeal.appeal_type.value,
        "stage": appeal.stage.value,
        "status": appeal.status.value,
        "deadline_at": appeal.deadline_at.isoformat(),
        "decision": appeal.decision,
        "decision_reasoning": appeal.decision_reasoning,
        "audit_hash": appeal.audit_hash,
    }


@router.get("/claim/{claim_id}")
def api_get_claim_appeals(claim_id: str, db: Session = Depends(get_db)):
    """List all appeals for a claim."""
    from app.models.appeal import Appeal
    appeals = db.query(Appeal).filter(Appeal.claim_id == claim_id).all()
    return [
        {
            "appeal_id": str(a.appeal_id),
            "appeal_type": a.appeal_type.value,
            "stage": a.stage.value,
            "status": a.status.value,
            "requested_at": a.requested_at.isoformat(),
        }
        for a in appeals
    ]


@router.get("/metrics/summary")
def api_appeal_metrics(db: Session = Depends(get_db)):
    """Appeal metrics: overturn rates, resolution times, compliance status."""
    return get_appeal_metrics(db)
