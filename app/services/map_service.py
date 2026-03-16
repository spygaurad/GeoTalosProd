import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.map import Map
from app.models.project import Project
from app.schemas.map import MapCreate, MapUpdate

logger = logging.getLogger(__name__)


class MapService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_maps(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> tuple[Sequence[Map], int]:
        query = select(Map).where(Map.deleted_at.is_(None))
        count_query = select(func.count()).select_from(Map).where(Map.deleted_at.is_(None))

        if project_id is not None:
            query = query.where(Map.project_id == project_id)
            count_query = count_query.where(Map.project_id == project_id)
        if organization_id is not None:
            query = query.join(Project, Project.id == Map.project_id).where(
                Project.organization_id == organization_id
            )
            count_query = count_query.join(Project, Project.id == Map.project_id).where(
                Project.organization_id == organization_id
            )

        rows = await self.db.scalars(
            query.order_by(Map.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_maps organization_id=%s project_id=%s limit=%s offset=%s total=%s",
            organization_id,
            project_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_map(
        self, map_id: UUID, organization_id: UUID | None = None, project_id: UUID | None = None
    ) -> Map:
        if organization_id is None and project_id is None:
            map_row = await self.db.get(Map, map_id)
        else:
            query = select(Map).where(Map.id == map_id)
            if project_id is not None:
                query = query.where(Map.project_id == project_id)
            if organization_id is not None:
                query = query.join(Project, Project.id == Map.project_id).where(
                    Project.organization_id == organization_id
                )
            result = await self.db.execute(query)
            map_row = result.scalar_one_or_none()
        if map_row is None:
            logger.warning("get_map_not_found map_id=%s", map_id)
            raise not_found("Map")
        return map_row

    async def create_map(self, payload: MapCreate, created_by: UUID | None = None) -> Map:
        data = payload.model_dump()
        data["created_by"] = created_by
        map_row = Map(**data)
        self.db.add(map_row)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_map_conflict project_id=%s", payload.project_id)
            raise conflict("Map creation violates uniqueness or FK constraints") from exc
        await self.db.refresh(map_row)
        logger.info("create_map_success map_id=%s", map_row.id)
        return map_row

    async def update_map(
        self,
        map_id: UUID,
        payload: MapUpdate,
        organization_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> Map:
        map_row = await self.get_map(map_id, organization_id=organization_id, project_id=project_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(map_row, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_map_conflict map_id=%s", map_id)
            raise conflict("Map update violates uniqueness or FK constraints") from exc
        await self.db.refresh(map_row)
        logger.info("update_map_success map_id=%s", map_row.id)
        return map_row

    async def delete_map(
        self,
        map_id: UUID,
        organization_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> None:
        map_row = await self.get_map(map_id, organization_id=organization_id, project_id=project_id)
        map_row.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
        logger.info("delete_map_success map_id=%s", map_row.id)
