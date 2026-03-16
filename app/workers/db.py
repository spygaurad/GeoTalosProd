from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# Synchronous engine for Celery workers.
# Uses the celery_worker role which has BYPASSRLS — never expose this
# session or these credentials to any API-facing code path.
_sync_engine = create_engine(settings.CELERY_DATABASE_URL, pool_size=5)

WorkerSession = sessionmaker(bind=_sync_engine, expire_on_commit=False)
