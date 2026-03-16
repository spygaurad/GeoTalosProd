import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import not_found
from app.models.job import Job

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_job(self, job_id: UUID, organization_id: UUID) -> Job:
        result = await self.db.execute(
            select(Job).where(
                Job.id == job_id,
                Job.organization_id == organization_id,
            )
        )
        job = result.scalar_one_or_none()
        if job is None:
            logger.warning("get_job_not_found job_id=%s", job_id)
            raise not_found("Job")
        return job
