from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    datasets,
    health,
    jobs,
    map_layers,
    maps,
    models,
    organization_members,
    organizations,
    projects,
    stac,
    tiles,
    users,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(webhooks.router)
api_router.include_router(organizations.router)
api_router.include_router(users.router)
api_router.include_router(organization_members.router)
api_router.include_router(projects.router)
api_router.include_router(maps.router)
api_router.include_router(map_layers.router)
api_router.include_router(datasets.router)
api_router.include_router(models.router)
api_router.include_router(jobs.router)
api_router.include_router(stac.router)
api_router.include_router(tiles.router)
