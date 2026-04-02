"""Offset pagination helpers.

Usage in endpoints:
    @router.get("/items")
    async def list_items(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
        session: AsyncSession = Depends(get_session),
    ) -> PaginatedResponse[ItemRead]:
        offset = (page - 1) * page_size
        total = await session.scalar(select(func.count()).select_from(Item))
        rows  = (await session.scalars(select(Item).offset(offset).limit(page_size))).all()
        return paginate(rows, total, page, page_size)
"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int

    model_config = {"arbitrary_types_allowed": True}


def paginate(items: list, total: int, page: int, page_size: int) -> PaginatedResponse:
    """Wrap a list of serialisable items in a PaginatedResponse envelope."""
    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)
