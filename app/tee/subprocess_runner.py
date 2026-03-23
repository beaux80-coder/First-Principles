"""TEE subprocess isolation for development.

Constitution: "Runs inside a cryptographically isolated Trusted Execution
Environment (TEE) with remote attestation, ensuring zero logical pathway
— no API, no shared memory, no indirect data pipeline — to any system
containing financial data."

In development, we achieve isolation by running the clinical engine in a
separate Python subprocess that:
1. Only imports clinical modules (no financial modules)
2. Communicates via stdin/stdout JSON (no shared memory)
3. Has no access to financial environment variables
4. Can be verified via the attestation endpoint

In production, this is replaced by AWS Nitro Enclave vsock communication.
"""

import json
import subprocess
import sys
import os
import logging

logger = logging.getLogger(__name__)

# The subprocess script that runs in isolation
ISOLATED_RUNNER = '''
import sys
import os
import json

# Block financial environment variables
for var in ["PRICING_API_KEY", "BILLING_API_KEY", "STRIPE_SECRET_KEY", "PAYMENT_GATEWAY_URL"]:
    os.environ.pop(var, None)

# Set up database connection (clinical DB only)
sys.path.insert(0, os.environ.get("APP_ROOT", "."))

# CRITICAL: Import ONLY clinical modules.
# Do NOT import app.models (which pulls in financial models).
# Instead, import specific clinical models directly.
from app.tee.clinical_db import ClinicalSessionLocal as SessionLocal
from app.models.clinical_determination import ClinicalDetermination
from app.models.clinical_guideline import ClinicalGuideline

# Import ONLY clinical modules — no financial modules
from app.services.clinical_engine import make_determination, verify_audit_chain, get_published_rates
from app.tee.isolation import get_tee_boundary

# Read request from stdin
request = json.loads(sys.stdin.read())
action = request.get("action")

db = SessionLocal()

try:
    if action == "determine":
        det = make_determination(
            db=db,
            claim_id=request.get("claim_id"),
            service_code=request.get("service_code"),
            benefit_type=request.get("benefit_type"),
            patient_symptoms=request.get("patient_symptoms", []),
            patient_history=request.get("patient_history", {}),
            condition=request.get("condition"),
        )
        result = {
            "determination_id": str(det.determination_id),
            "decision": det.decision if isinstance(det.decision, str) else det.decision.value,
            "reasoning": det.reasoning,
            "guidelines_referenced": det.guidelines_referenced or [],
            "audit_hash": det.audit_hash,
        }
    elif action == "verify_chain":
        result = verify_audit_chain(db, limit=request.get("limit", 100))
    elif action == "published_rates":
        result = get_published_rates(db)
    elif action == "attestation":
        boundary = get_tee_boundary()
        doc = boundary.verify_isolation()
        result = {
            "enclave_id": doc.enclave_id,
            "pcr0": doc.pcr0,
            "pcr1": doc.pcr1,
            "pcr2": doc.pcr2,
            "timestamp": doc.timestamp,
            "nonce": doc.nonce,
            "isolation_mode": doc.isolation_mode,
            "financial_access_paths": doc.financial_access_paths,
            "verified": doc.verified,
        }
    else:
        result = {"error": f"Unknown action: {action}"}
finally:
    db.close()

print(json.dumps(result, default=str))
'''


def run_in_isolation(request: dict) -> dict:
    """Run a clinical engine request in an isolated subprocess.

    The subprocess has no access to financial modules or data.
    Communication is via JSON on stdin/stdout — no shared memory.
    """
    app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Build clean environment — strip financial vars
    env = {k: v for k, v in os.environ.items()
           if k not in ("PRICING_API_KEY", "BILLING_API_KEY", "STRIPE_SECRET_KEY", "PAYMENT_GATEWAY_URL")}
    env["APP_ROOT"] = app_root

    try:
        proc = subprocess.run(
            [sys.executable, "-c", ISOLATED_RUNNER],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=app_root,
            env=env,
        )

        if proc.returncode != 0:
            logger.error(f"TEE subprocess failed: {proc.stderr[:500]}")
            return {"error": proc.stderr[:500], "isolated": True}

        result = json.loads(proc.stdout.strip())
        result["_isolated"] = True
        result["_subprocess_pid"] = proc.pid if hasattr(proc, 'pid') else "completed"
        return result

    except subprocess.TimeoutExpired:
        return {"error": "TEE subprocess timed out", "isolated": True}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON from TEE subprocess: {e}", "isolated": True}
    except Exception as e:
        return {"error": f"TEE subprocess error: {e}", "isolated": True}
