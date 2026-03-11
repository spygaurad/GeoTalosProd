import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.schemas.project import ProjectCreate, ProjectUpdate

logger = logging.getLogger(__name__)


class ProjectService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_projects(
        self,
        limit: int,
        offset: int,
        organization_id: UUID | None = None,
        user_id: UUID | None = None,
    ) -> tuple[Sequence[Project], int]:
        query = select(Project)
        count_query = select(func.count()).select_from(Project)

        if organization_id is not None:
            query = query.where(Project.organization_id == organization_id)
            count_query = count_query.where(Project.organization_id == organization_id)
        if user_id is not None:
            query = query.join(ProjectMember, ProjectMember.project_id == Project.id).where(
                ProjectMember.user_id == user_id
            )
            count_query = count_query.join(ProjectMember, ProjectMember.project_id == Project.id).where(
                ProjectMember.user_id == user_id
            )

        rows = await self.db.scalars(
            query.order_by(Project.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(count_query)
        logger.debug(
            "list_projects organization_id=%s user_id=%s limit=%s offset=%s total=%s",
            organization_id,
            user_id,
            limit,
            offset,
            total or 0,
        )
        return rows.all(), int(total or 0)

    async def get_project(self, project_id: UUID, organization_id: UUID | None = None) -> Project:
        if organization_id is None:
            project = await self.db.get(Project, project_id)
        else:
            result = await self.db.execute(
                select(Project).where(
                    Project.id == project_id, Project.organization_id == organization_id
                )
            )
            project = result.scalar_one_or_none()
        if project is None:
            logger.warning("get_project_not_found project_id=%s", project_id)
            raise not_found("Project")
        return project

    async def create_project(self, payload: ProjectCreate) -> Project:
        data = payload.model_dump()
        data["metadata_"] = data.pop("metadata")
        project = Project(**data)
        self.db.add(project)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning(
                "create_project_conflict organization_id=%s slug=%s",
                payload.organization_id,
                payload.slug,
            )
            raise conflict("Project slug already exists within the organization") from exc
        await self.db.refresh(project)
        logger.info("create_project_success project_id=%s", project.id)
        return project

    async def update_project(
        self, project_id: UUID, payload: ProjectUpdate, organization_id: UUID | None = None
    ) -> Project:
        project = await self.get_project(project_id, organization_id=organization_id)
        data = payload.model_dump(exclude_unset=True)

        if "metadata" in data:
            data["metadata_"] = data.pop("metadata")
        if data.get("status") == "archived" and data.get("archived_at") is None:
            # Keep archive metadata consistent when caller sets archived status.
            data["archived_at"] = datetime.now(timezone.utc)

        for key, value in data.items():
            setattr(project, key, value)

        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_project_conflict project_id=%s", project_id)
            raise conflict("Project update violates uniqueness or FK constraints") from exc
        await self.db.refresh(project)
        logger.info("update_project_success project_id=%s", project.id)
        return project

    async def delete_project(self, project_id: UUID, organization_id: UUID | None = None) -> None:
        project = await self.get_project(project_id, organization_id=organization_id)
        await self.db.delete(project)
        await self.db.commit()
        logger.info("delete_project_success project_id=%s", project_id)
