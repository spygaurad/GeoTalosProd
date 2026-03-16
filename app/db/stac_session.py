from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# pgSTAC write pool — pgstac_ingest role (INSERT / UPDATE on pgSTAC tables)
_engine_ingest = create_async_engine(
    settings.STAC_DATABASE_URL,
    future=True,
    echo=False,
    pool_size=5,
    max_overflow=5,
)

# pgSTAC read-only pool — pgstac_read role (SELECT only)
_engine_read = create_async_engine(
    settings.STAC_READ_URL,
    future=True,
    echo=False,
    pool_size=5,
    max_overflow=5,
)

AsyncSTACIngestSession = async_sessionmaker(
    bind=_engine_ingest,
    class_=AsyncSession,
    expire_on_commit=False,
)

AsyncSTACReadSession = async_sessionmaker(
    bind=_engine_read,
    class_=AsyncSession,
    expire_on_commit=False,
)
