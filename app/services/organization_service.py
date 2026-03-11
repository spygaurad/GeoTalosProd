import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.organization import Organization
from app.schemas.organization import OrganizationCreate, OrganizationUpdate

logger = logging.getLogger(__name__)


class OrganizationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_organizations(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
    ) -> tuple[Sequence[Organization], int]:
        query = select(Organization)
        count_query = select(func.count()).select_from(Organization)

        if organization_id is not None:
            query = query.where(Organization.id == organization_id)
            count_query = count_query.where(Organization.id == organization_id)

        rows = await self.db.scalars(query.order_by(Organization.created_at.desc()).limit(limit).offset(offset))
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_organizations organization_id=%s limit=%s offset=%s total=%s",
            organization_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_organization(self, organization_id: UUID) -> Organization:
        org = await self.db.get(Organization, organization_id)
        if org is None:
            logger.warning("get_organization_not_found organization_id=%s", organization_id)
            raise not_found("Organization")
        return org

    async def create_organization(self, payload: OrganizationCreate) -> Organization:
        org = Organization(**payload.model_dump())
        self.db.add(org)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning(
                "create_organization_conflict slug=%s clerk_org_id=%s",
                payload.slug,
                payload.clerk_org_id,
            )
            raise conflict("Organization slug or clerk_org_id already exists") from exc
        await self.db.refresh(org)
        logger.info("create_organization_success organization_id=%s", org.id)
        return org

    async def update_organization(
        self, organization_id: UUID, payload: OrganizationUpdate
    ) -> Organization:
        org = await self.get_organization(organization_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(org, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_organization_conflict organization_id=%s", organization_id)
            raise conflict("Organization update violates uniqueness constraints") from exc
        await self.db.refresh(org)
        logger.info("update_organization_success organization_id=%s", org.id)
        return org

    async def delete_organization(self, organization_id: UUID) -> None:
        org = await self.get_organization(organization_id)
        await self.db.delete(org)
        await self.db.commit()
        logger.info("delete_organization_success organization_id=%s", organization_id)
