from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    health,
    org_memberships,
    organizations,
    project_members,
    projects,
    users,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(webhooks.router)
api_router.include_router(organizations.router)
api_router.include_router(users.router)
api_router.include_router(org_memberships.router)
api_router.include_router(projects.router)
api_router.include_router(project_members.router)
