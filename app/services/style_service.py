import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.style import Style
from app.schemas.style import StyleCreate, StyleUpdate

logger = logging.getLogger(__name__)


class StyleService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_styles(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
    ) -> tuple[Sequence[Style], int]:
        query = select(Style).where(Style.deleted_at.is_(None))
        count_query = select(func.count()).select_from(Style).where(Style.deleted_at.is_(None))
        if organization_id is not None:
            query = query.where(Style.organization_id == organization_id)
            count_query = count_query.where(Style.organization_id == organization_id)

        rows = await self.db.scalars(
            query.order_by(Style.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        return rows.all(), int(total or 0)

    async def get_style(self, style_id: UUID, organization_id: UUID | None = None) -> Style:
        query = select(Style).where(Style.id == style_id, Style.deleted_at.is_(None))
        if organization_id is not None:
            query = query.where(Style.organization_id == organization_id)
        result = await self.db.execute(query)
        style = result.scalar_one_or_none()
        if style is None:
            raise not_found("Style")
        return style

    async def create_style(
        self, payload: StyleCreate, organization_id: UUID, created_by: UUID | None = None
    ) -> Style:
        data = payload.model_dump()
        data["organization_id"] = organization_id
        data["created_by"] = created_by
        style = Style(**data)
        self.db.add(style)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_style_conflict organization_id=%s", organization_id)
            raise conflict("Style creation violates uniqueness or FK constraints") from exc
        await self.db.refresh(style)
        return style

    async def update_style(
        self, style_id: UUID, payload: StyleUpdate, organization_id: UUID | None = None
    ) -> Style:
        style = await self.get_style(style_id, organization_id=organization_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(style, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise conflict("Style update violates constraints") from exc
        await self.db.refresh(style)
        return style

    async def delete_style(self, style_id: UUID, organization_id: UUID | None = None) -> None:
        style = await self.get_style(style_id, organization_id=organization_id)
        style.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        await self.db.commit()
