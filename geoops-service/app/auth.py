import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


async def require_bearer_token(authorization: str | None = Header(default=None)) -> None:
    """Every route but /health requires this. Matches the ai_models.auth_config
    bearer_token convention AwakeForestProd's ModelManager/geoops services
    already use for calling OUT to a model endpoint — here we're the callee,
    so we verify the same header shape instead of sending it.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # Constant-time compare — this is a real auth boundary, not a dev toggle.
    if not hmac.compare_digest(token, settings.API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
