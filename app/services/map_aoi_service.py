import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.map import Map
from app.models.map_aoi import MapAOI
from app.models.project import Project
from app.schemas.map_aoi import MapAOICreate, MapAOIUpdate

logger = logging.getLogger(__name__)


class MapAOIService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_map_for_org(self, map_id: UUID, organization_id: UUID) -> Map:
        result = await self.db.execute(
            select(Map)
            .join(Project, Project.id == Map.project_id)
            .where(
                Map.id == map_id,
                Map.deleted_at.is_(None),
                Project.organization_id == organization_id,
            )
        )
        map_row = result.scalar_one_or_none()
        if map_row is None:
            raise not_found("Map")
        return map_row

    async def list_aois(
        self,
        map_id: UUID,
        *,
        organization_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[MapAOI], int]:
        await self.get_map_for_org(map_id, organization_id)
        query = select(MapAOI).where(
            MapAOI.map_id == map_id,
            MapAOI.organization_id == organization_id,
            MapAOI.deleted_at.is_(None),
        )
        count_query = select(func.count()).select_from(MapAOI).where(
            MapAOI.map_id == map_id,
            MapAOI.organization_id == organization_id,
            MapAOI.deleted_at.is_(None),
        )
        rows = await self.db.scalars(
            query.order_by(MapAOI.z_index.asc(), MapAOI.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_aoi(self, map_id: UUID, aoi_id: UUID, *, organization_id: UUID) -> MapAOI:
        result = await self.db.execute(
            select(MapAOI).where(
                MapAOI.id == aoi_id,
                MapAOI.map_id == map_id,
                MapAOI.organization_id == organization_id,
                MapAOI.deleted_at.is_(None),
            )
        )
        aoi = result.scalar_one_or_none()
        if aoi is None:
            raise not_found("Map AOI")
        return aoi

    async def create_aoi(
        self,
        map_id: UUID,
        payload: MapAOICreate,
        *,
        organization_id: UUID,
        created_by: UUID | None,
    ) -> MapAOI:
        await self.get_map_for_org(map_id, organization_id)
        data = payload.model_dump()
        data["map_id"] = map_id
        data["organization_id"] = organization_id
        data["created_by"] = created_by
        aoi = MapAOI(**data)
        self.db.add(aoi)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_map_aoi_conflict map_id=%s org_id=%s", map_id, organization_id)
            raise conflict("Map AOI creation violates uniqueness or FK constraints") from exc
        await self.db.refresh(aoi)
        return aoi

    async def update_aoi(
        self,
        map_id: UUID,
        aoi_id: UUID,
        payload: MapAOIUpdate,
        *,
        organization_id: UUID,
    ) -> MapAOI:
        aoi = await self.get_aoi(map_id, aoi_id, organization_id=organization_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(aoi, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_map_aoi_conflict map_id=%s aoi_id=%s", map_id, aoi_id)
            raise conflict("Map AOI update violates uniqueness or FK constraints") from exc
        await self.db.refresh(aoi)
        return aoi

    async def delete_aoi(self, map_id: UUID, aoi_id: UUID, *, organization_id: UUID) -> None:
        aoi = await self.get_aoi(map_id, aoi_id, organization_id=organization_id)
        aoi.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
