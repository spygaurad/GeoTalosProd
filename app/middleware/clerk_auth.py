"""Clerk JWT authentication middleware.

Validates every incoming Bearer token against the Clerk JWKS endpoint and
stores the decoded claims on request.state.clerk_claims.

Exempt paths (no JWT required):
  - /api/v1/webhooks/clerk  — secured by X-Internal-Key instead
  - /api/v1/health          — public health probe

In development mode (ENVIRONMENT=development) with no token present, a
hardcoded dev claim set is injected so the API can be exercised without a
real Clerk account. Any valid Bearer token in dev mode is still verified
normally — the bypass only fires when the Authorization header is absent.
"""

import ipaddress
import re
import time
from typing import Any

import httpx
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

# Paths that bypass JWT verification entirely.
EXEMPT_PATHS: set[str] = {
    "/api/v1/webhooks/clerk",
    "/api/v1/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}

# Part-upload proxy: PUT /api/v1/datasets/{id}/uploads/{upload_id}/parts/{n}
# The upload_id acts as a capability token — possession proves the caller initiated
# the upload. Auth is enforced inside the endpoint via DB lookup.
_PARTS_UPLOAD_RE = re.compile(
    r"^/api/v1/datasets/[^/]+/uploads/[^/]+/parts/\d+$"
)

# Raster-mask tile proxy: GET /api/v1/tiles/raster-masks/{annotation_set_id}/{z}/{x}/{y}.{fmt}
# Map libraries load tile URLs as image <src> and cannot attach custom headers.
# The annotation_set_id (a random UUID) acts as a capability token — equivalent to
# how titiler serves dataset tiles without requiring auth on each tile request.
# RLS-based org isolation still applies inside the endpoint via the DB session.
_RASTER_MASK_TILE_RE = re.compile(
    r"^/api/v1/tiles/raster-masks/[0-9a-f-]{36}/\d+/\d+/\d+\.\w+$"
)

# Dev claim set injected when no Authorization header is present in development.
_DEV_CLAIMS: dict[str, Any] = {
    "sub": "user_dev",
    "org_id": "org_dev",
    "org_role": "org:admin",
    "email": "dev@localhost",
    "name": "Dev User",
}


class ClerkAuthMiddleware(BaseHTTPMiddleware):
    # Class-level JWKS cache shared across all requests.
    _jwks: dict | None = None
    _jwks_fetched_at: float = 0.0
    _JWKS_TTL: float = 3600.0  # re-fetch at most once per hour

    async def dispatch(self, request: Request, call_next):
        
        if request.method == "OPTIONS":
            return await call_next(request)

        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # Part-upload proxy is exempt — the endpoint verifies the upload_id itself.
        if request.method == "PUT" and _PARTS_UPLOAD_RE.match(request.url.path):
            return await call_next(request)

        # Raster-mask tile proxy is exempt — map libraries cannot attach auth headers
        # to image <src> tile requests.  The annotation_set UUID is the capability token.
        if request.method == "GET" and _RASTER_MASK_TILE_RE.match(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")

        # Development bypass — no token present → inject dev claims, but only from local/private IPs.
        if settings.ENVIRONMENT == "development" and not auth_header:
            if self._is_dev_bypass_allowed(request):
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "DEV BYPASS active for %s %s — using org_id='org_dev' "
                    "(UUID 00000000-0000-0000-0000-000000000001). "
                    "If you get 404s on resources created with a real Clerk token, "
                    "add 'Authorization: Bearer <token>' to your request.",
                    request.method, request.url.path,
                )
                request.state.clerk_claims = _DEV_CLAIMS
                return await call_next(request)
            return JSONResponse(
                {"detail": "Dev auth bypass is restricted to local/private networks"},
                status_code=403,
            )

        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"detail": "Missing or malformed Authorization header"},
                status_code=401,
            )

        token = auth_header[len("Bearer "):]
        try:
            jwks = await self._get_jwks()
            payload = jwt.decode(
                token,
                jwks,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except JWTError as exc:
            return JSONResponse({"detail": f"Invalid token: {exc}"}, status_code=401)

        request.state.clerk_claims = payload
        return await call_next(request)

    @staticmethod
    def _is_dev_bypass_allowed(request: Request) -> bool:
        client_host = request.client.host if request.client else ""
        if not client_host:
            return False
        if client_host in {"localhost"}:
            return True
        try:
            ip = ipaddress.ip_address(client_host)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private

    async def _get_jwks(self) -> dict:
        now = time.monotonic()
        if ClerkAuthMiddleware._jwks and now - ClerkAuthMiddleware._jwks_fetched_at < self._JWKS_TTL:
            return ClerkAuthMiddleware._jwks

        # Strip any protocol prefix so the value works whether the env var
        # is set as "https://foo.clerk.accounts.dev" or "foo.clerk.accounts.dev".
        frontend_api = settings.CLERK_FRONTEND_API.removeprefix("https://").removeprefix("http://")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://{frontend_api}/.well-known/jwks.json"
            )
            resp.raise_for_status()

        ClerkAuthMiddleware._jwks = resp.json()
        ClerkAuthMiddleware._jwks_fetched_at = now
        return ClerkAuthMiddleware._jwks
