import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.core.geometry import parse_geometry
from app.core.ranges import parse_tstzrange
from app.models.dataset import Dataset
from app.schemas.dataset import DatasetCreate, DatasetUpdate

logger = logging.getLogger(__name__)


class DatasetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_datasets(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
    ) -> tuple[Sequence[Dataset], int]:
        query = select(Dataset).where(Dataset.deleted_at.is_(None))
        count_query = select(func.count()).select_from(Dataset).where(Dataset.deleted_at.is_(None))

        if organization_id is not None:
            query = query.where(Dataset.organization_id == organization_id)
            count_query = count_query.where(Dataset.organization_id == organization_id)

        rows = await self.db.scalars(
            query.order_by(Dataset.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_datasets organization_id=%s limit=%s offset=%s total=%s",
            organization_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_dataset(self, dataset_id: UUID, organization_id: UUID | None = None) -> Dataset:
        if organization_id is None:
            dataset = await self.db.get(Dataset, dataset_id)
        else:
            result = await self.db.execute(
                select(Dataset).where(
                    Dataset.id == dataset_id, Dataset.organization_id == organization_id
                )
            )
            dataset = result.scalar_one_or_none()
        if dataset is None:
            logger.warning("get_dataset_not_found dataset_id=%s", dataset_id)
            raise not_found("Dataset")
        return dataset

    async def create_dataset(self, payload: DatasetCreate, organization_id: UUID) -> Dataset:
        data = payload.model_dump_db()
        data["organization_id"] = organization_id
        if data.get("geometry") is not None:
            data["geometry"] = parse_geometry(data["geometry"])
        if "temporal_extent" in data:
            data["temporal_extent"] = parse_tstzrange(data["temporal_extent"])
        dataset = Dataset(**data)
        self.db.add(dataset)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_dataset_conflict organization_id=%s", organization_id)
            raise conflict("Dataset creation violates uniqueness or FK constraints") from exc
        await self.db.refresh(dataset)
        logger.info("create_dataset_success dataset_id=%s", dataset.id)
        return dataset

    async def update_dataset(
        self, dataset_id: UUID, payload: DatasetUpdate, organization_id: UUID | None = None
    ) -> Dataset:
        dataset = await self.get_dataset(dataset_id, organization_id=organization_id)
        data = payload.model_dump_db(exclude_unset=True)

        if "geometry" in data:
            data["geometry"] = parse_geometry(data["geometry"])
        if "temporal_extent" in data:
            data["temporal_extent"] = parse_tstzrange(data["temporal_extent"])

        for key, value in data.items():
            setattr(dataset, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_dataset_conflict dataset_id=%s", dataset_id)
            raise conflict("Dataset update violates uniqueness or FK constraints") from exc
        await self.db.refresh(dataset)
        logger.info("update_dataset_success dataset_id=%s", dataset.id)
        return dataset

    async def delete_dataset(self, dataset_id: UUID, organization_id: UUID | None = None) -> None:
        dataset = await self.get_dataset(dataset_id, organization_id=organization_id)
        dataset.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
        logger.info("delete_dataset_success dataset_id=%s", dataset_id)
