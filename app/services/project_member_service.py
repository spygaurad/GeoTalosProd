import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.schemas.project_member import ProjectMemberCreate, ProjectMemberUpdate

logger = logging.getLogger(__name__)


class ProjectMemberService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_project_members(
        self,
        limit: int,
        offset: int,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
    ) -> tuple[Sequence[ProjectMember], int]:
        query = select(ProjectMember)
        count_query = select(func.count()).select_from(ProjectMember)

        if organization_id is not None:
            query = query.join(Project, Project.id == ProjectMember.project_id).where(
                Project.organization_id == organization_id
            )
            count_query = count_query.join(Project, Project.id == ProjectMember.project_id).where(
                Project.organization_id == organization_id
            )
        if project_id is not None:
            query = query.where(ProjectMember.project_id == project_id)
            count_query = count_query.where(ProjectMember.project_id == project_id)
        if user_id is not None:
            query = query.where(ProjectMember.user_id == user_id)
            count_query = count_query.where(ProjectMember.user_id == user_id)

        rows = await self.db.scalars(
            query.order_by(ProjectMember.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_project_members organization_id=%s project_id=%s user_id=%s limit=%s offset=%s total=%s",
            organization_id,
            project_id,
            user_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_project_member(
        self, project_id: UUID, user_id: UUID, organization_id: UUID | None = None
    ) -> ProjectMember:
        if organization_id is None:
            member = await self.db.get(ProjectMember, {"project_id": project_id, "user_id": user_id})
        else:
            result = await self.db.execute(
                select(ProjectMember)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == user_id,
                    Project.organization_id == organization_id,
                )
            )
            member = result.scalar_one_or_none()
        if member is None:
            logger.warning(
                "get_project_member_not_found project_id=%s user_id=%s",
                project_id,
                user_id,
            )
            raise not_found("Project member")
        return member

    async def create_project_member(self, payload: ProjectMemberCreate) -> ProjectMember:
        member = ProjectMember(**payload.model_dump())
        self.db.add(member)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning(
                "create_project_member_conflict project_id=%s user_id=%s",
                payload.project_id,
                payload.user_id,
            )
            raise conflict("Project member already exists or references an invalid FK") from exc
        await self.db.refresh(member)
        logger.info(
            "create_project_member_success project_id=%s user_id=%s",
            member.project_id,
            member.user_id,
        )
        return member

    async def update_project_member(
        self, project_id: UUID, user_id: UUID, payload: ProjectMemberUpdate, organization_id: UUID | None = None
    ) -> ProjectMember:
        member = await self.get_project_member(project_id, user_id, organization_id=organization_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(member, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning(
                "update_project_member_conflict project_id=%s user_id=%s",
                project_id,
                user_id,
            )
            raise conflict("Project member update violates constraints") from exc
        await self.db.refresh(member)
        logger.info(
            "update_project_member_success project_id=%s user_id=%s",
            member.project_id,
            member.user_id,
        )
        return member

    async def delete_project_member(
        self, project_id: UUID, user_id: UUID, organization_id: UUID | None = None
    ) -> None:
        member = await self.get_project_member(project_id, user_id, organization_id=organization_id)
        await self.db.delete(member)
        await self.db.commit()
        logger.info(
            "delete_project_member_success project_id=%s user_id=%s",
            project_id,
            user_id,
        )
