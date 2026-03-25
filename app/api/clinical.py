"""Clinical Quality Engine API endpoints (Function 1).

Constitution: "Complete source code is open-source and publicly available.
Any person can independently verify the engine operates on clinical inputs only."

All endpoints in this module are part of the open-source F1 engine.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/clinical", tags=["F1 Clinical Quality Engine"])


class DeterminationRequest(BaseModel):
    """Request for a clinical quality determination.

    Constitution: "Sole inputs: patient symptoms, patient medical history,
    current peer-reviewed evidence-based clinical guidelines."
    """
    claim_id: str
    service_code: str
    benefit_type: str  # health, dental, vision, mental_health, life, std, ltd
    patient_symptoms: list[str]
    patient_history: dict  # age, sex, diagnoses, risk_factors, medications
    condition: Optional[str] = None


class DeterminationResponse(BaseModel):
    determination_id: str
    decision: str  # approved, denied, modified
    reasoning: str
    guidelines_referenced: list[str]
    audit_hash: str
    benefit_type: Optional[str] = None
    risk_score: Optional[float] = None
    risk_factors: Optional[dict] = None
    latency_ms: Optional[float] = None


@router.post("/determine", response_model=DeterminationResponse)
def make_clinical_determination(
    request: DeterminationRequest,
    db: Session = Depends(get_db),
):
    """Make a clinical quality determination for a service request.

    This endpoint processes clinical inputs ONLY. It has zero access
    to financial data, pricing, or cost information.

    The determination is recorded in an immutable, cryptographically
    secured audit log.
    """
    from app.services.clinical_engine import make_determination

    det = make_determination(
        db=db,
        claim_id=request.claim_id,
        service_code=request.service_code,
        benefit_type=request.benefit_type,
        patient_symptoms=request.patient_symptoms,
        patient_history=request.patient_history,
        condition=request.condition,
    )

    return DeterminationResponse(
        determination_id=str(det.determination_id),
        decision=det.decision if isinstance(det.decision, str) else det.decision.value,
        reasoning=det.reasoning,
        guidelines_referenced=det.guidelines_referenced or [],
        audit_hash=det.audit_hash,
        benefit_type=det.benefit_type,
        risk_score=det.risk_score,
        risk_factors=det.risk_factors,
        latency_ms=det.latency_ms,
    )


@router.get("/rates")
def published_rates(db: Session = Depends(get_db)):
    """Published approval and denial rates vs guideline predictions.

    Constitution: "Publishes approval and denial rates across all benefit
    types compared against rates that evidence-based guidelines would predict
    for the same patient population."

    Public endpoint — freely accessible.
    """
    from app.services.clinical_engine import get_published_rates
    return get_published_rates(db)


@router.get("/audit/verify")
def verify_audit_chain(db: Session = Depends(get_db)):
    """Verify the integrity of the determination audit chain.

    Constitution: "Available for independent audit at any time."

    Walks the cryptographic hash chain and verifies no entries
    have been tampered with.
    """
    from app.services.clinical_engine import verify_audit_chain
    return verify_audit_chain(db)


@router.get("/attestation")
def tee_attestation():
    """Remote attestation endpoint.

    Constitution: "Any external party can verify the integrity of the
    isolation at any time via remote attestation."

    Returns an attestation document proving:
    1. The exact code running inside the TEE (PCR2 hash)
    2. That zero financial data pathways exist
    3. The isolation mode (Nitro Enclave or process isolation)

    On Nitro hardware: returns a COSE Sign1 document signed by AWS PKI.
    On development: returns a simulated attestation with PCR hashes.
    """
    from app.tee.vsock_runner import get_enclave_attestation
    return get_enclave_attestation()


@router.get("/guidelines")
def list_guidelines(db: Session = Depends(get_db)):
    """List all clinical guidelines in the knowledge base.

    Constitution: "Complete source code is open-source and publicly available."
    Guidelines are part of the engine's decision basis and must be transparent.
    """
    from app.services.clinical_guidelines_ingester import get_guideline_stats
    return get_guideline_stats(db)


@router.get("/nlp/info")
def nlp_model_info(db: Session = Depends(get_db)):
    """NLP model information for the clinical engine.

    Shows the current state of the symptom-to-guideline matching model
    used to improve determination accuracy.
    """
    from app.services.clinical_nlp import get_nlp_model_info
    return get_nlp_model_info(db)


@router.post("/nlp/match")
def nlp_match(request: DeterminationRequest, db: Session = Depends(get_db)):
    """Test NLP symptom matching without creating a determination.

    Returns ranked guideline matches for the given symptoms.
    Useful for debugging and transparency.
    """
    from app.services.clinical_nlp import match_symptoms_to_guidelines
    return match_symptoms_to_guidelines(
        db,
        request.patient_symptoms,
        request.patient_history,
        request.benefit_type,
        request.condition,
    )


class OutcomeRequest(BaseModel):
    determination_id: str
    actual_outcome: str  # "correct", "incorrect", "inconclusive"


@router.post("/outcome")
def record_outcome(request: OutcomeRequest, db: Session = Depends(get_db)):
    """Record the actual outcome of a clinical determination for accuracy tracking.

    Constitution (accuracy): "Is there any physically possible, legally permitted
    method to increase accuracy that has not been implemented?"

    Outcome feedback enables the accuracy measurement loop.
    """
    from app.services.clinical_engine import record_determination_outcome
    return record_determination_outcome(db, request.determination_id, request.actual_outcome)


@router.post("/guidelines/ingest")
def ingest_guidelines(db: Session = Depends(get_db)):
    """Trigger ingestion of clinical guidelines from all recognized authorities.

    Constitution: "Updates determination models as new peer-reviewed evidence
    is published. Latency between guideline publication and engine incorporation
    must be measured and minimized."

    Sources: CMS NCDs (hardcoded baseline), USPSTF (live API), PubMed (live API).
    """
    from app.services.clinical_guidelines_ingester import ingest_all_dynamic_sources
    return ingest_all_dynamic_sources(db)


@router.post("/nlp/fine-tune")
def fine_tune_nlp(db: Session = Depends(get_db)):
    """Fine-tune NLP model on the current guideline corpus.

    Constitution: "Is there any physically possible, legally permitted method
    to increase accuracy that has not been implemented?"

    This analyzes the corpus to identify high-information clinical terms
    and optimizes TF-IDF feature weights for better matching accuracy.
    """
    from app.services.clinical_nlp import fine_tune_on_corpus
    return fine_tune_on_corpus(db)


@router.post("/nlp/retrain")
def retrain_from_outcomes(db: Session = Depends(get_db)):
    """Active learning: retrain model from outcome feedback.

    Constitution: "Is there any physically possible, legally permitted method
    to increase accuracy that has not been implemented?"

    When clinicians record outcomes (correct/incorrect via POST /clinical/outcome),
    this retrains the model to boost weights for guideline-symptom pairs that
    led to correct determinations and penalize incorrect ones.
    """
    from app.services.clinical_nlp import retrain_from_outcomes
    return retrain_from_outcomes(db)
