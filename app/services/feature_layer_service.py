import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.core.geometry import parse_geometry, serialize_geometry
from app.models.feature_layer import FeatureLayer
from app.schemas.feature_layer import FeatureLayerCreate

logger = logging.getLogger(__name__)


class FeatureLayerService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_features(
        self,
        limit: int,
        offset: int,
        organization_id: UUID,
        layer_name: str | None = None,
    ) -> tuple[Sequence[FeatureLayer], int]:
        query = select(FeatureLayer).where(FeatureLayer.organization_id == organization_id)
        count_query = (
            select(func.count())
            .select_from(FeatureLayer)
            .where(FeatureLayer.organization_id == organization_id)
        )
        if layer_name is not None:
            query = query.where(FeatureLayer.layer_name == layer_name)
            count_query = count_query.where(FeatureLayer.layer_name == layer_name)

        rows = await self.db.scalars(
            query.order_by(FeatureLayer.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_feature(self, feature_id: UUID, organization_id: UUID) -> FeatureLayer:
        result = await self.db.execute(
            select(FeatureLayer).where(
                FeatureLayer.id == feature_id,
                FeatureLayer.organization_id == organization_id,
            )
        )
        feature = result.scalar_one_or_none()
        if feature is None:
            raise not_found("FeatureLayer")
        return feature

    async def create_feature(
        self, payload: FeatureLayerCreate, organization_id: UUID, created_by: UUID | None = None
    ) -> FeatureLayer:
        geom = parse_geometry(payload.geometry)
        feature = FeatureLayer(
            organization_id=organization_id,
            layer_name=payload.layer_name,
            geometry=geom,
            properties=payload.properties,
            created_by=created_by,
        )
        self.db.add(feature)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("FeatureLayer creation violates constraints") from exc
        await self.db.refresh(feature)
        return feature

    async def bulk_create_features(
        self,
        features: list[FeatureLayerCreate],
        organization_id: UUID,
        created_by: UUID | None = None,
    ) -> int:
        created = 0
        for payload in features:
            geom = parse_geometry(payload.geometry)
            feature = FeatureLayer(
                organization_id=organization_id,
                layer_name=payload.layer_name,
                geometry=geom,
                properties=payload.properties,
                created_by=created_by,
            )
            self.db.add(feature)
            created += 1
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Bulk feature creation violates constraints") from exc
        return created

    async def delete_feature(self, feature_id: UUID, organization_id: UUID) -> None:
        feature = await self.get_feature(feature_id, organization_id)
        await self.db.delete(feature)
        await self.db.commit()

    @staticmethod
    def to_read_dict(feature: FeatureLayer) -> dict:
        """Convert FeatureLayer ORM object to dict with serialized geometry."""
        geom = serialize_geometry(feature.geometry)
        return {
            "id": feature.id,
            "organization_id": feature.organization_id,
            "layer_name": feature.layer_name,
            "geometry": geom,
            "properties": feature.properties,
            "created_by": feature.created_by,
            "created_at": feature.created_at,
        }
