from uuid import uuid4

import app.api.v1.endpoints.org_memberships as org_memberships_ep
import app.api.v1.endpoints.organizations as organizations_ep
import app.api.v1.endpoints.project_members as project_members_ep
import app.api.v1.endpoints.projects as projects_ep
import app.api.v1.endpoints.users as users_ep


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_users_crud_endpoints(client, monkeypatch):
    user_id = str(uuid4())
    user_payload = {
        "id": user_id,
        "clerk_user_id": "user_123",
        "email": "user@example.com",
        "name": "User One",
        "is_active": True,
        "created_at": "2026-03-04T00:00:00Z",
        "updated_at": "2026-03-04T00:00:00Z",
    }

    async def _list_users(*_args, **_kwargs):
        return [user_payload], 1

    async def _get_user(*_args, **_kwargs):
        return user_payload

    async def _create_user(*_args, **_kwargs):
        return user_payload

    async def _update_user(*_args, **_kwargs):
        return user_payload

    async def _delete_user(*_args, **_kwargs):
        return None

    monkeypatch.setattr(users_ep.UserService, "list_users", _list_users)
    monkeypatch.setattr(users_ep.UserService, "get_user", _get_user)
    monkeypatch.setattr(users_ep.UserService, "create_user", _create_user)
    monkeypatch.setattr(users_ep.UserService, "update_user", _update_user)
    monkeypatch.setattr(users_ep.UserService, "delete_user", _delete_user)

    assert client.get("/api/v1/users?limit=10&offset=0").status_code == 200
    assert client.get(f"/api/v1/users/{user_id}").status_code == 200
    assert client.post("/api/v1/users", json={"clerk_user_id": "user_123"}).status_code == 201
    assert client.patch(f"/api/v1/users/{user_id}", json={"name": "Updated"}).status_code == 200
    assert client.delete(f"/api/v1/users/{user_id}").status_code == 204


def test_organizations_crud_endpoints(client, monkeypatch):
    organization_id = str(uuid4())
    org_payload = {
        "id": organization_id,
        "clerk_org_id": "org_123",
        "name": "Org One",
        "slug": "org-one",
        "description": "desc",
        "settings": {},
        "created_at": "2026-03-04T00:00:00Z",
        "updated_at": "2026-03-04T00:00:00Z",
    }

    async def _list_orgs(*_args, **_kwargs):
        return [org_payload], 1

    async def _get_org(*_args, **_kwargs):
        return org_payload

    async def _create_org(*_args, **_kwargs):
        return org_payload

    async def _update_org(*_args, **_kwargs):
        return org_payload

    async def _delete_org(*_args, **_kwargs):
        return None

    monkeypatch.setattr(organizations_ep.OrganizationService, "list_organizations", _list_orgs)
    monkeypatch.setattr(organizations_ep.OrganizationService, "get_organization", _get_org)
    monkeypatch.setattr(organizations_ep.OrganizationService, "create_organization", _create_org)
    monkeypatch.setattr(organizations_ep.OrganizationService, "update_organization", _update_org)
    monkeypatch.setattr(organizations_ep.OrganizationService, "delete_organization", _delete_org)

    assert client.get("/api/v1/organizations?limit=10&offset=0").status_code == 200
    assert client.get(f"/api/v1/organizations/{organization_id}").status_code == 200
    assert (
        client.post(
            "/api/v1/organizations",
            json={"clerk_org_id": "org_123", "name": "Org One", "slug": "org-one"},
        ).status_code
        == 201
    )
    assert (
        client.patch(f"/api/v1/organizations/{organization_id}", json={"description": "Updated"}).status_code
        == 200
    )
    assert client.delete(f"/api/v1/organizations/{organization_id}").status_code == 204


def test_org_memberships_crud_endpoints(client, monkeypatch):
    organization_id = str(uuid4())
    user_id = str(uuid4())
    payload = {
        "organization_id": organization_id,
        "user_id": user_id,
        "role": "org:member",
        "invited_by": None,
        "status": "active",
        "created_at": "2026-03-04T00:00:00Z",
        "synced_at": "2026-03-04T00:00:00Z",
        "updated_at": "2026-03-04T00:00:00Z",
    }

    async def _list_memberships(*_args, **_kwargs):
        return [payload], 1

    async def _get_membership(*_args, **_kwargs):
        return payload

    async def _create_membership(*_args, **_kwargs):
        return payload

    async def _update_membership(*_args, **_kwargs):
        return payload

    async def _delete_membership(*_args, **_kwargs):
        return None

    monkeypatch.setattr(org_memberships_ep.MembershipService, "list_org_memberships", _list_memberships)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "get_org_membership", _get_membership)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "create_org_membership", _create_membership)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "update_org_membership", _update_membership)
    monkeypatch.setattr(org_memberships_ep.MembershipService, "delete_org_membership", _delete_membership)

    assert client.get("/api/v1/org-memberships?limit=10&offset=0").status_code == 200
    assert client.get(f"/api/v1/org-memberships/{organization_id}/{user_id}").status_code == 200
    assert (
        client.post(
            "/api/v1/org-memberships",
            json={"organization_id": organization_id, "user_id": user_id},
        ).status_code
        == 201
    )
    assert (
        client.patch(
            f"/api/v1/org-memberships/{organization_id}/{user_id}",
            json={"role": "org:admin"},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/org-memberships/{organization_id}/{user_id}").status_code == 204


def test_projects_crud_endpoints(client, monkeypatch):
    project_id = str(uuid4())
    organization_id = str(uuid4())
    payload = {
        "id": project_id,
        "organization_id": organization_id,
        "name": "Project One",
        "slug": "project-one",
        "description": "desc",
        "created_by": None,
        "metadata_": {},
        "status": "active",
        "archived_at": None,
        "archived_by": None,
        "created_at": "2026-03-04T00:00:00Z",
        "updated_at": "2026-03-04T00:00:00Z",
    }

    async def _list_projects(*_args, **_kwargs):
        return [payload], 1

    async def _get_project(*_args, **_kwargs):
        return payload

    async def _create_project(*_args, **_kwargs):
        return payload

    async def _update_project(*_args, **_kwargs):
        return payload

    async def _delete_project(*_args, **_kwargs):
        return None

    monkeypatch.setattr(projects_ep.ProjectService, "list_projects", _list_projects)
    monkeypatch.setattr(projects_ep.ProjectService, "get_project", _get_project)
    monkeypatch.setattr(projects_ep.ProjectService, "create_project", _create_project)
    monkeypatch.setattr(projects_ep.ProjectService, "update_project", _update_project)
    monkeypatch.setattr(projects_ep.ProjectService, "delete_project", _delete_project)

    assert client.get("/api/v1/projects?limit=10&offset=0").status_code == 200
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200
    assert (
        client.post(
            "/api/v1/projects",
            json={"organization_id": organization_id, "name": "Project One", "slug": "project-one"},
        ).status_code
        == 201
    )
    assert client.patch(f"/api/v1/projects/{project_id}", json={"description": "Updated"}).status_code == 200
    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204


def test_project_members_crud_endpoints(client, monkeypatch):
    project_id = str(uuid4())
    user_id = str(uuid4())
    payload = {
        "project_id": project_id,
        "user_id": user_id,
        "role": "viewer",
        "added_by": None,
        "status": "active",
        "created_at": "2026-03-04T00:00:00Z",
        "updated_at": "2026-03-04T00:00:00Z",
    }

    async def _list_project_members(*_args, **_kwargs):
        return [payload], 1

    async def _get_project_member(*_args, **_kwargs):
        return payload

    async def _create_project_member(*_args, **_kwargs):
        return payload

    async def _update_project_member(*_args, **_kwargs):
        return payload

    async def _delete_project_member(*_args, **_kwargs):
        return None

    monkeypatch.setattr(project_members_ep.ProjectMemberService, "list_project_members", _list_project_members)
    monkeypatch.setattr(project_members_ep.ProjectMemberService, "get_project_member", _get_project_member)
    monkeypatch.setattr(project_members_ep.ProjectMemberService, "create_project_member", _create_project_member)
    monkeypatch.setattr(project_members_ep.ProjectMemberService, "update_project_member", _update_project_member)
    monkeypatch.setattr(project_members_ep.ProjectMemberService, "delete_project_member", _delete_project_member)

    assert client.get("/api/v1/project-members?limit=10&offset=0").status_code == 200
    assert client.get(f"/api/v1/project-members/{project_id}/{user_id}").status_code == 200
    assert (
        client.post(
            "/api/v1/project-members",
            json={"project_id": project_id, "user_id": user_id},
        ).status_code
        == 201
    )
    assert (
        client.patch(
            f"/api/v1/project-members/{project_id}/{user_id}",
            json={"role": "editor"},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/project-members/{project_id}/{user_id}").status_code == 204
