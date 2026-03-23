"""Enclave server — runs INSIDE the Nitro Enclave.

Constitution: "Runs inside a cryptographically isolated Trusted Execution
Environment (TEE) with remote attestation, ensuring zero logical pathway
to any system containing financial data."

This file is the entry point for the Docker image that becomes the Enclave
Image File (EIF). It:
1. Listens on vsock for determination requests from the parent instance
2. Imports ONLY clinical modules (no financial modules exist in the image)
3. Processes determinations using the same logic as clinical_engine.py
4. Returns results via vsock

The Dockerfile.enclave packages ONLY this file and its clinical dependencies.
No financial modules, no pricing data, no employer data.
"""

import json
import logging
import socket
import struct
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("enclave")

VSOCK_PORT = 5001


def handle_request(request_data: bytes) -> bytes:
    """Process a determination request inside the enclave.

    Only clinical modules are available — financial modules are not
    packaged in the enclave image.
    """
    request = json.loads(request_data.decode("utf-8"))
    action = request.get("action")

    if action == "determine":
        # Import clinical engine (the ONLY business logic in the enclave)
        from app.services.clinical_engine import make_determination
        from app.tee.clinical_db import get_clinical_session

        db = get_clinical_session()
        try:
            det = make_determination(
                db=db,
                claim_id=request["claim_id"],
                service_code=request["service_code"],
                benefit_type=request["benefit_type"],
                patient_symptoms=request["patient_symptoms"],
                patient_history=request["patient_history"],
                condition=request.get("condition"),
            )

            result = {
                "determination_id": str(det.determination_id),
                "decision": det.decision.value if hasattr(det.decision, "value") else str(det.decision),
                "reasoning": det.reasoning,
                "guidelines_referenced": det.guidelines_referenced or [],
                "audit_hash": det.audit_hash,
                "benefit_type": det.benefit_type,
                "risk_score": det.risk_score,
                "risk_factors": det.risk_factors,
                "latency_ms": det.latency_ms,
            }
        finally:
            db.close()

    elif action == "attest":
        from app.tee.isolation import get_tee_boundary
        boundary = get_tee_boundary()
        doc = boundary.verify_isolation()
        result = doc.to_dict()

    elif action == "verify_chain":
        from app.services.clinical_engine import verify_audit_chain
        from app.tee.clinical_db import get_clinical_session
        db = get_clinical_session()
        try:
            result = verify_audit_chain(db)
        finally:
            db.close()

    else:
        result = {"error": f"Unknown action: {action}"}

    return json.dumps(result, default=str).encode("utf-8")


def serve():
    """Main vsock server loop."""
    logger.info(f"Enclave server starting on vsock port {VSOCK_PORT}")

    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.bind((socket.VMADDR_CID_ANY, VSOCK_PORT))
    sock.listen(5)

    logger.info("Enclave server ready — listening for determination requests")

    while True:
        try:
            conn, addr = sock.accept()
            logger.info(f"Connection from CID {addr[0]}")

            # Read length-prefixed request
            length_bytes = _recv_exact(conn, 4)
            req_length = struct.unpack("!I", length_bytes)[0]
            req_data = _recv_exact(conn, req_length)

            # Process
            resp_data = handle_request(req_data)

            # Send length-prefixed response
            conn.sendall(struct.pack("!I", len(resp_data)))
            conn.sendall(resp_data)
            conn.close()

        except Exception as e:
            logger.error(f"Error handling request: {e}")
            try:
                conn.close()
            except Exception:
                pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes from a socket."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("Socket closed prematurely")
        data += chunk
    return data


if __name__ == "__main__":
    serve()
