import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.tile_source import TileSource
from app.schemas.tile_source import TileSourceCreate, TileSourceUpdate

logger = logging.getLogger(__name__)


class TileSourceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_tile_sources(
        self,
        limit: int,
        offset: int,
        organization_id: UUID,
    ) -> tuple[Sequence[TileSource], int]:
        query = select(TileSource).where(
            TileSource.organization_id == organization_id,
            TileSource.deleted_at.is_(None),
        )
        count_query = (
            select(func.count())
            .select_from(TileSource)
            .where(TileSource.organization_id == organization_id, TileSource.deleted_at.is_(None))
        )
        rows = await self.db.scalars(
            query.order_by(TileSource.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_tile_source(self, tile_source_id: UUID, organization_id: UUID) -> TileSource:
        result = await self.db.execute(
            select(TileSource).where(
                TileSource.id == tile_source_id,
                TileSource.organization_id == organization_id,
                TileSource.deleted_at.is_(None),
            )
        )
        ts = result.scalar_one_or_none()
        if ts is None:
            raise not_found("TileSource")
        return ts

    async def create_tile_source(
        self, payload: TileSourceCreate, organization_id: UUID, created_by: UUID | None = None
    ) -> TileSource:
        data = payload.model_dump()
        data["organization_id"] = organization_id
        data["created_by"] = created_by
        ts = TileSource(**data)
        self.db.add(ts)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("TileSource creation violates constraints") from exc
        await self.db.refresh(ts)
        return ts

    async def update_tile_source(
        self, tile_source_id: UUID, payload: TileSourceUpdate, organization_id: UUID
    ) -> TileSource:
        ts = await self.get_tile_source(tile_source_id, organization_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(ts, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("TileSource update violates constraints") from exc
        await self.db.refresh(ts)
        return ts

    async def delete_tile_source(self, tile_source_id: UUID, organization_id: UUID) -> None:
        ts = await self.get_tile_source(tile_source_id, organization_id)
        ts.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
