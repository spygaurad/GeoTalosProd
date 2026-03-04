import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import conflict, not_found
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_users(self, limit: int, offset: int) -> tuple[Sequence[User], int]:
        rows = await self.db.scalars(
            select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
        total = await self.db.scalar(select(func.count()).select_from(User))
        logger.debug("list_users limit=%s offset=%s total=%s", limit, offset, total or 0)
        return rows.all(), int(total or 0)

    async def get_user(self, user_id: UUID) -> User:
        user = await self.db.get(User, user_id)
        if user is None:
            logger.warning("get_user_not_found user_id=%s", user_id)
            raise not_found("User")
        return user

    async def create_user(self, payload: UserCreate) -> User:
        user = User(**payload.model_dump())
        self.db.add(user)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("create_user_conflict clerk_user_id=%s", payload.clerk_user_id)
            raise conflict("User with same clerk_user_id already exists") from exc
        await self.db.refresh(user)
        logger.info("create_user_success user_id=%s", user.id)
        return user

    async def update_user(self, user_id: UUID, payload: UserUpdate) -> User:
        user = await self.get_user(user_id)
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(user, key, value)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            logger.warning("update_user_conflict user_id=%s", user_id)
            raise conflict("User update violates a uniqueness constraint") from exc
        await self.db.refresh(user)
        logger.info("update_user_success user_id=%s", user.id)
        return user

    async def delete_user(self, user_id: UUID) -> None:
        user = await self.get_user(user_id)
        await self.db.delete(user)
        await self.db.commit()
        logger.info("delete_user_success user_id=%s", user_id)
