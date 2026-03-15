import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.organization_member import OrganizationMember
from app.schemas.organization_member import OrganizationMemberCreate, OrganizationMemberUpdate

logger = logging.getLogger(__name__)


class MembershipService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_org_memberships(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> tuple[Sequence[OrganizationMember], int]:
        query = select(OrganizationMember)
        count_query = select(func.count()).select_from(OrganizationMember)

        if organization_id is not None:
            query = query.where(OrganizationMember.organization_id == organization_id)
            count_query = count_query.where(OrganizationMember.organization_id == organization_id)
        if user_id is not None:
            query = query.where(OrganizationMember.user_id == user_id)
            count_query = count_query.where(OrganizationMember.user_id == user_id)

        rows = await self.db.scalars(
            query.order_by(OrganizationMember.joined_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_org_memberships organization_id=%s user_id=%s limit=%s offset=%s total=%s",
            organization_id,
            user_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_org_membership(
        self, organization_id: UUID, user_id: UUID
    ) -> OrganizationMember:
        membership = await self.db.get(
            OrganizationMember,
            {"organization_id": organization_id, "user_id": user_id},
        )
        if membership is None:
            logger.warning(
                "get_org_membership_not_found organization_id=%s user_id=%s",
                organization_id,
                user_id,
            )
            raise not_found("Organization membership")
        return membership

    async def create_org_membership(
        self, payload: OrganizationMemberCreate
    ) -> OrganizationMember:
        membership = OrganizationMember(**payload.model_dump())
        self.db.add(membership)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning(
                "create_org_membership_conflict organization_id=%s user_id=%s",
                payload.organization_id,
                payload.user_id,
            )
            raise conflict("Organization membership already exists or references an invalid FK") from exc
        await self.db.refresh(membership)
        logger.info(
            "create_org_membership_success organization_id=%s user_id=%s",
            membership.organization_id,
            membership.user_id,
        )
        return membership

    async def update_org_membership(
        self, organization_id: UUID, user_id: UUID, payload: OrganizationMemberUpdate
    ) -> OrganizationMember:
        membership = await self.get_org_membership(organization_id, user_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(membership, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning(
                "update_org_membership_conflict organization_id=%s user_id=%s",
                organization_id,
                user_id,
            )
            raise conflict("Organization membership update violates constraints") from exc
        await self.db.refresh(membership)
        logger.info(
            "update_org_membership_success organization_id=%s user_id=%s",
            membership.organization_id,
            membership.user_id,
        )
        return membership

    async def delete_org_membership(self, organization_id: UUID, user_id: UUID) -> None:
        membership = await self.get_org_membership(organization_id, user_id)
        await self.db.delete(membership)
        await self.db.commit()
        logger.info(
            "delete_org_membership_success organization_id=%s user_id=%s",
            organization_id,
            user_id,
        )
