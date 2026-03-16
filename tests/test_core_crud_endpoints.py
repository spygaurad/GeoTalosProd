"""
Smoke tests for core CRUD endpoints.

Key constraints these tests work around:
- Endpoints call `str(result.id)` on service return values inside log_audit_event args,
  so service mocks must return SimpleNamespace objects (attribute access), not plain dicts.
- Several endpoints guard with `if path_org_id != org_id: raise 403`, so path/payload
  org UUIDs must match _ORG_ID (the value returned by the overridden get_current_org_id
  in conftest).
"""
from types import SimpleNamespace
from uuid import UUID, uuid4

import app.api.v1.endpoints.organization_members as org_memberships_ep
import app.api.v1.endpoints.organizations as organizations_ep
import app.api.v1.endpoints.projects as projects_ep
import app.api.v1.endpoints.users as users_ep

# Must match conftest._ORG_ID so `if org_id != payload.organization_id` guards pass.
_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


def _assert_status(resp, expected: int) -> None:
    """Assert HTTP status and include response body in the failure message."""
    assert resp.status_code == expected, (
        f"Expected HTTP {expected}, got {resp.status_code}\n"
        f"Response body: {resp.text}"
    )


def _ns(**kwargs) -> SimpleNamespace:
    """Wrap a dict as a SimpleNamespace so endpoints can do result.id etc."""
    return SimpleNamespace(**kwargs)


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_users_crud_endpoints(client, monkeypatch):
    user_id = str(uuid4())
    # Matches UserRead: id, clerk_id, email, name, avatar_url, created_at, updated_at
    user_obj = _ns(
        id=user_id,
        clerk_id="user_123",
        email="user@example.com",
        name="User One",
        avatar_url=None,
        created_at="2026-03-04T00:00:00Z",
        updated_at="2026-03-04T00:00:00Z",
    )

    async def _list_users(*_a, **_kw):
        return [user_obj], 1

    async def _get_user(*_a, **_kw):
        return user_obj

    async def _create_user(*_a, **_kw):
        return user_obj

    async def _update_user(*_a, **_kw):
        return user_obj

    async def _delete_user(*_a, **_kw):
        return None

    monkeypatch.setattr(users_ep.UserService, "list_users", _list_users)
    monkeypatch.setattr(users_ep.UserService, "get_user", _get_user)
    monkeypatch.setattr(users_ep.UserService, "create_user", _create_user)
    monkeypatch.setattr(users_ep.UserService, "update_user", _update_user)
    monkeypatch.setattr(users_ep.UserService, "delete_user", _delete_user)

    _assert_status(client.get("/api/v1/users?limit=10&offset=0"), 200)
    _assert_status(client.get(f"/api/v1/users/{user_id}"), 200)
    _assert_status(
        client.post("/api/v1/users", json={"clerk_id": "user_123", "email": "user@example.com"}),
        201,
    )
    _assert_status(client.patch(f"/api/v1/users/{user_id}", json={"name": "Updated"}), 200)
    _assert_status(client.delete(f"/api/v1/users/{user_id}"), 204)


def test_organizations_crud_endpoints(client, monkeypatch):
    # organization_id MUST equal _ORG_ID because GET/PATCH/DELETE /{id} check
    # `if organization_id != org_id: raise 403`.
    organization_id = str(_ORG_ID)
    # Matches OrganizationRead: id, clerk_org_id, name, slug, description, owner_id,
    # settings, created_at, updated_at
    org_obj = _ns(
        id=organization_id,
        clerk_org_id="org_123",
        name="Org One",
        slug="org-one",
        description="desc",
        owner_id=None,
        settings={},
        created_at="2026-03-04T00:00:00Z",
        updated_at="2026-03-04T00:00:00Z",
    )

    async def _list_orgs(*_a, **_kw):
        return [org_obj], 1

    async def _get_org(*_a, **_kw):
        return org_obj

    async def _create_org(*_a, **_kw):
        return org_obj

    async def _update_org(*_a, **_kw):
        return org_obj

    async def _delete_org(*_a, **_kw):
        return None

    monkeypatch.setattr(organizations_ep.OrganizationService, "list_organizations", _list_orgs)
    monkeypatch.setattr(organizations_ep.OrganizationService, "get_organization", _get_org)
    monkeypatch.setattr(organizations_ep.OrganizationService, "create_organization", _create_org)
    monkeypatch.setattr(organizations_ep.OrganizationService, "update_organization", _update_org)
    monkeypatch.setattr(organizations_ep.OrganizationService, "delete_organization", _delete_org)

    _assert_status(client.get("/api/v1/organizations?limit=10&offset=0"), 200)
    _assert_status(client.get(f"/api/v1/organizations/{organization_id}"), 200)
    _assert_status(
        client.post(
            "/api/v1/organizations",
            json={"clerk_org_id": "org_123", "name": "Org One", "slug": "org-one"},
        ),
        201,
    )
    _assert_status(
        client.patch(f"/api/v1/organizations/{organization_id}", json={"description": "Updated"}),
        200,
    )
    _assert_status(client.delete(f"/api/v1/organizations/{organization_id}"), 204)


def test_org_memberships_crud_endpoints(client, monkeypatch):
    # organization_id MUST equal _ORG_ID because every path and POST payload is checked
    # against org_id from get_current_org_id.
    organization_id = str(_ORG_ID)
    user_id = str(uuid4())
    # Matches OrganizationMemberRead: organization_id, user_id, role, joined_at
    membership_obj = _ns(
        organization_id=organization_id,
        user_id=user_id,
        role="org:member",
        joined_at="2026-03-04T00:00:00Z",
    )

    async def _list_memberships(*_a, **_kw):
        return [membership_obj], 1

    async def _get_membership(*_a, **_kw):
        return membership_obj

    async def _create_membership(*_a, **_kw):
        return membership_obj

    async def _update_membership(*_a, **_kw):
        return membership_obj

    async def _delete_membership(*_a, **_kw):
        return None

    monkeypatch.setattr(org_memberships_ep.MembershipService, "list_org_memberships", _list_memberships)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "get_org_membership", _get_membership)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "create_org_membership", _create_membership)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "update_org_membership", _update_membership)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "delete_org_membership", _delete_membership)

    _assert_status(client.get("/api/v1/organization-members?limit=10&offset=0"), 200)
    _assert_status(client.get(f"/api/v1/organization-members/{organization_id}/{user_id}"), 200)
    _assert_status(
        client.post(
            "/api/v1/organization-members",
            json={"organization_id": organization_id, "user_id": user_id, "role": "org:member"},
        ),
        201,
    )
    _assert_status(
        client.patch(
            f"/api/v1/organization-members/{organization_id}/{user_id}",
            json={"role": "org:admin"},
        ),
        200,
    )
    _assert_status(client.delete(f"/api/v1/organization-members/{organization_id}/{user_id}"), 204)


def test_projects_crud_endpoints(client, monkeypatch):
    project_id = str(uuid4())
    # organization_id MUST equal _ORG_ID because POST checks
    # `if payload.organization_id != org_id: raise 403`.
    organization_id = str(_ORG_ID)
    # Matches ProjectRead: id, organization_id, name, description, created_by,
    # created_at, updated_at, deleted_at
    project_obj = _ns(
        id=project_id,
        organization_id=organization_id,
        name="Project One",
        description="desc",
        created_by=None,
        deleted_at=None,
        created_at="2026-03-04T00:00:00Z",
        updated_at="2026-03-04T00:00:00Z",
    )

    async def _list_projects(*_a, **_kw):
        return [project_obj], 1

    async def _get_project(*_a, **_kw):
        return project_obj

    async def _create_project(*_a, **_kw):
        return project_obj

    async def _update_project(*_a, **_kw):
        return project_obj

    async def _delete_project(*_a, **_kw):
        return None

    monkeypatch.setattr(projects_ep.ProjectService, "list_projects", _list_projects)
    monkeypatch.setattr(projects_ep.ProjectService, "get_project", _get_project)
    monkeypatch.setattr(projects_ep.ProjectService, "create_project", _create_project)
    monkeypatch.setattr(projects_ep.ProjectService, "update_project", _update_project)
    monkeypatch.setattr(projects_ep.ProjectService, "delete_project", _delete_project)

    _assert_status(client.get("/api/v1/projects?limit=10&offset=0"), 200)
    _assert_status(client.get(f"/api/v1/projects/{project_id}"), 200)
    _assert_status(
        client.post(
            "/api/v1/projects",
            json={"organization_id": organization_id, "name": "Project One"},
        ),
        201,
    )
    _assert_status(client.patch(f"/api/v1/projects/{project_id}", json={"description": "Updated"}), 200)
    _assert_status(client.delete(f"/api/v1/projects/{project_id}"), 204)
