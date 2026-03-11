import logging
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger("audit")


def log_audit_event(
    *,
    action: str,
    actor_id: str,
    organization_id: str | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    extra: dict[str, Any] | None = None,
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
