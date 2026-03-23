"""Auth0 JWT verification middleware.

Validates JWTs from Auth0. In development mode, allows bypass for testing.
"""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
import httpx

from app.config import settings

security = HTTPBearer(auto_error=False)

_jwks_cache: dict | None = None


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://{settings.auth0_domain}/.well-known/jwks.json")
            _jwks_cache = resp.json()
    return _jwks_cache


def _decode_token(token: str, jwks: dict) -> dict:
    unverified_header = jwt.get_unverified_header(token)
    rsa_key = {}
    for key in jwks.get("keys", []):
        if key["kid"] == unverified_header.get("kid"):
            rsa_key = {
                "kty": key["kty"],
                "kid": key["kid"],
                "use": key["use"],
                "n": key["n"],
                "e": key["e"],
            }
    if not rsa_key:
        raise HTTPException(status_code=401, detail="Unable to find signing key")

    return jwt.decode(
        token,
        rsa_key,
        algorithms=["RS256"],
        audience=settings.auth0_api_audience,
        issuer=f"https://{settings.auth0_domain}/",
    )


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Extract and validate the current user from the JWT.

    In development mode with no Auth0 config, returns a test user.
    """
    if settings.app_env == "development" and not settings.auth0_domain:
        return {"sub": "dev-user", "email": "dev@beneflex.local", "roles": ["admin"]}

    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        jwks = await _get_jwks()
        payload = _decode_token(credentials.credentials, jwks)
        return payload
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Token validation failed: {e}")


async def optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """Returns user if authenticated, None otherwise. For public endpoints."""
    if credentials is None:
        return None
    try:
        jwks = await _get_jwks()
        return _decode_token(credentials.credentials, jwks)
    except (JWTError, HTTPException):
        return None
