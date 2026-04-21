"""Discovery endpoints for the output adapter registry.

The admin UI uses these to populate the "output format" dropdown on the
model-registration form and render a JSON-schema-driven form for each
adapter's ``adapter_config``.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user, require_org_role
from app.automation.adapters import ADAPTER_REGISTRY
from app.models.user import User

router = APIRouter(prefix="/inference/adapters", tags=["inference"])


@router.get("")
async def list_adapters(
    _org_id=Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
):
    return {
        "items": [
            {
                "name": adapter.name,
                "label": adapter.label,
                "description": adapter.description,
                "supported_formats": adapter.supported_formats,
                "config_schema": adapter.config_schema,
            }
            for adapter in ADAPTER_REGISTRY.values()
        ],
        "total": len(ADAPTER_REGISTRY),
    }


@router.get("/{name}/schema")
async def get_adapter_schema(
    name: str,
    _org_id=Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
):
    adapter = ADAPTER_REGISTRY.get(name)
    if adapter is None:
        raise HTTPException(status_code=404, detail="Adapter not found")
    return {
        "name": adapter.name,
        "label": adapter.label,
        "config_schema": adapter.config_schema,
    }
