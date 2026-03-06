from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.middleware.clerk_auth import ClerkAuthMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In development, seed a dev org and user so the hardcoded dev claims in
    # ClerkAuthMiddleware resolve correctly without a real Clerk account.
    if settings.ENVIRONMENT == "development":
        await _seed_dev_fixtures()
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
                    clerk_user_id=dev_user_clerk_id,
                    email="dev@localhost",
                    name="Dev User",
                )
                .on_conflict_do_nothing(index_elements=["clerk_user_id"])
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

app.include_router(api_router, prefix="/api/v1")
