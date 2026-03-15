import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity_log import ActivityLog


logger = logging.getLogger("audit")


async def log_audit_event(
    *,
    action: str,
    actor_id: str,
    organization_id: str | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    extra: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info(
        "audit action=%s actor_id=%s organization_id=%s entity=%s entity_id=%s ts=%s extra=%s",
        action,
        actor_id,
        organization_id,
        entity,
        entity_id,
        timestamp,
        extra or {},
    )

    if session is None or organization_id is None:
        return

    def _parse_uuid(value: str | None) -> uuid.UUID | None:
        if not value:
            return None
        try:
            return uuid.UUID(value)
        except ValueError:
            return None

    try:
        record = ActivityLog(
            organization_id=_parse_uuid(organization_id),
            user_id=_parse_uuid(actor_id),
            action=action,
            entity_type=entity or "",
            entity_id=_parse_uuid(entity_id),
            changes=extra or None,
        )
        session.add(record)
        await session.commit()
    except Exception:  # pragma: no cover - audit must not break main flow
        logger.exception(
            "audit_persist_failed action=%s actor_id=%s organization_id=%s entity=%s entity_id=%s",
            action,
            actor_id,
            organization_id,
            entity,
            entity_id,
        )
        await session.rollback()
