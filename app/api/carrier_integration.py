"""Carrier Integration API (Function 6A).

Constitution: "One-click API connection to employer's current carrier/TPA."

Endpoints:
  POST /carrier/connect          -- Connect to a carrier/TPA
  POST /carrier/fetch-claims     -- Fetch claims from a connected carrier
  POST /carrier/ingest-claim     -- Ingest a single raw carrier claim
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/carrier", tags=["Carrier Integration"])


# -- Request Models -----------------------------------------------------------

class ConnectCarrierRequest(BaseModel):
    """Connect to a carrier/TPA."""
    employer_id: str = Field(description="Employer UUID")
    carrier_name: str = Field(description="Carrier name (e.g., 'mock', 'anthem', 'cigna')")
    connection_type: str = Field(
        default="mock",
        description="Connection type: oauth, sftp, edi, api_key, mock",
    )
    credentials: Optional[dict] = Field(
        default=None,
        description="Carrier credentials (OAuth tokens, SFTP keys, etc.)",
    )


class FetchClaimsRequest(BaseModel):
    """Fetch claims from a connected carrier."""
    carrier_name: str = Field(description="Carrier name")
    credentials: Optional[dict] = Field(default=None, description="Carrier credentials")
    limit: int = Field(default=100, description="Max claims to fetch")


class IngestClaimRequest(BaseModel):
    """Ingest a single raw carrier claim."""
    raw_data: dict = Field(description="Raw carrier claim data")


# -- Endpoints ----------------------------------------------------------------

@router.post("/connect")
def connect_carrier(request: ConnectCarrierRequest):
    """Connect to a carrier/TPA.

    Constitution: "One-click API connection to employer's current carrier/TPA."
    """
    from app.services.carrier_integration import connect_carrier as _connect

    try:
        return _connect(
            employer_id=request.employer_id,
            carrier_name=request.carrier_name,
            connection_type=request.connection_type,
            credentials=request.credentials,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fetch-claims")
def fetch_claims(request: FetchClaimsRequest):
    """Fetch claims from a connected carrier."""
    from app.services.carrier_integration import fetch_carrier_claims as _fetch

    try:
        claims = _fetch(
            carrier_name=request.carrier_name,
            credentials=request.credentials,
            limit=request.limit,
        )
        return {
            "carrier_name": request.carrier_name,
            "claims_fetched": len(claims),
            "claims": [c.to_dict() for c in claims],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ingest-claim")
def ingest_claim(request: IngestClaimRequest):
    """Ingest a single raw carrier claim into normalised format."""
    from app.services.carrier_integration import ingest_carrier_claim as _ingest

    try:
        normalised = _ingest(request.raw_data)
        return {
            "status": "ingested",
            "normalised_claim": normalised.to_dict(),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
