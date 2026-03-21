import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.basemap import Basemap
from app.schemas.basemap import BasemapCreate, BasemapUpdate

logger = logging.getLogger(__name__)


class BasemapService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_basemaps(
        self,
        limit: int,
        offset: int,
        organization_id: UUID,
    ) -> tuple[Sequence[Basemap], int]:
        query = select(Basemap).where(
            Basemap.organization_id == organization_id,
            Basemap.deleted_at.is_(None),
        )
        count_query = (
            select(func.count())
            .select_from(Basemap)
            .where(Basemap.organization_id == organization_id, Basemap.deleted_at.is_(None))
        )
        rows = await self.db.scalars(
            query.order_by(Basemap.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_basemap(self, basemap_id: UUID, organization_id: UUID) -> Basemap:
        result = await self.db.execute(
            select(Basemap).where(
                Basemap.id == basemap_id,
                Basemap.organization_id == organization_id,
                Basemap.deleted_at.is_(None),
            )
        )
        basemap = result.scalar_one_or_none()
        if basemap is None:
            raise not_found("Basemap")
        return basemap

    async def create_basemap(
        self, payload: BasemapCreate, organization_id: UUID, created_by: UUID | None = None
    ) -> Basemap:
        data = payload.model_dump()
        data["organization_id"] = organization_id
        data["created_by"] = created_by
        basemap = Basemap(**data)
        self.db.add(basemap)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Basemap creation violates constraints") from exc
        await self.db.refresh(basemap)
        return basemap

    async def update_basemap(
        self, basemap_id: UUID, payload: BasemapUpdate, organization_id: UUID
    ) -> Basemap:
        basemap = await self.get_basemap(basemap_id, organization_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(basemap, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Basemap update violates constraints") from exc
        await self.db.refresh(basemap)
        return basemap

    async def delete_basemap(self, basemap_id: UUID, organization_id: UUID) -> None:
        basemap = await self.get_basemap(basemap_id, organization_id)
        basemap.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
