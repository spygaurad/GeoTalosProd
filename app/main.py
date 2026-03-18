import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.core.logging import configure_logging
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.organization_member import OrganizationMember

configure_logging(settings.LOG_LEVEL)
logger = logging.getLogger(__name__)

if settings.ENVIRONMENT != "development":
    if not settings.BACKEND_CORS_ORIGINS or "*" in settings.BACKEND_CORS_ORIGINS:
        raise RuntimeError(
            "BACKEND_CORS_ORIGINS must be an explicit allowlist in non-development environments."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In development, seed a dev org and user so the hardcoded dev claims in
    # ClerkAuthMiddleware resolve correctly without a real Clerk account.
    if settings.ENVIRONMENT == "development":
        try:
            await _seed_dev_fixtures()
        except Exception:
            logger.warning("dev_seed_failed database not ready", exc_info=True)
    yield


async def _seed_dev_fixtures() -> None:
    """Upsert the dev org and dev user referenced by the dev claim bypass."""
    import uuid

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.session import AsyncSessionLocal
    from app.models.organization import Organization
    from app.models.user import User

    dev_org_clerk_id = "org_dev"
    dev_user_clerk_id = "user_dev"

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                pg_insert(Organization.__table__)
                .values(
                    id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    clerk_org_id=dev_org_clerk_id,
                    name="Dev Organization",
                    slug="dev-organization",
                )
                .on_conflict_do_nothing(index_elements=["clerk_org_id"])
            )
            await session.execute(
                pg_insert(User.__table__)
                .values(
                    id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
                    clerk_id=dev_user_clerk_id,
                    email="dev@localhost",
                    name="Dev User",
                )
                .on_conflict_do_nothing(index_elements=["clerk_id"])
            )

            # Upsert membership (dev user is admin of dev org)
            await session.execute(
                pg_insert(OrganizationMember.__table__)
                .values(
                    organization_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
                    role="admin",  # or "member" – admin is highest
                )
                .on_conflict_do_nothing(index_elements=["organization_id", "user_id"])
            )


app = FastAPI(title="AwakeForest API", lifespan=lifespan)

# Middleware stack (last added = outermost = runs first).
# Order: CORS → ClerkAuth → route handler
app.add_middleware(ClerkAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid4()))
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_failed method=%s path=%s request_id=%s",
            request.method,
            request.url.path,
            request_id,
        )
        raise
    latency_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request method=%s path=%s status_code=%s latency_ms=%.2f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        latency_ms,
        request_id,
    )
    return response


app.include_router(api_router, prefix="/api/v1")
