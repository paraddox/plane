# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from django.conf import settings
from django.urls import resolve
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema
from rest_framework.test import APIRequestFactory

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.views.business import APIKeyBusinessMixin
from plane.app.views.base import BaseAPIView, BaseViewSet
from plane.authentication.session import BaseSessionAuthentication
from plane.db.models import (
    IssueView,
    Label,
    Notification,
    Page,
    PageLabel,
    PageVersion,
    Project,
    ProjectMember,
    ProjectPage,
    User,
    Webhook,
    Workspace,
    WorkspaceMember,
)
from plane.settings.openapi import SPECTACULAR_SETTINGS
from plane.db.models.api_idempotency import APIIdempotency
from plane.utils.openapi.hooks import preprocess_filter_api_v1_paths


PROJECT_ID = uuid4()
RESOURCE_ID = uuid4()
PROJECT_PATH = f"workspaces/example/projects/{PROJECT_ID}"
PAGE_PATH = f"{PROJECT_PATH}/pages/{RESOURCE_ID}"
NOTIFICATION_PATH = f"workspaces/example/users/notifications/{RESOURCE_ID}"

# Every exposed verb must reach a key-only adapter, including custom actions.
BUSINESS_ROUTES = [
    ("workspaces", ["get", "post"]),
    ("workspaces/example", ["get", "put", "patch", "delete"]),
    (f"{PROJECT_PATH}/pages", ["get", "post"]),
    (f"{PROJECT_PATH}/pages-summary", ["get"]),
    (PAGE_PATH, ["get", "patch", "delete"]),
    (f"{PAGE_PATH}/description", ["get", "patch"]),
    (f"{PAGE_PATH}/versions", ["get"]),
    (f"{PAGE_PATH}/versions/{uuid4()}", ["get"]),
    (f"{PAGE_PATH}/duplicate", ["post"]),
    (f"{PAGE_PATH}/archive", ["post", "delete"]),
    (f"{PAGE_PATH}/lock", ["post", "delete"]),
    (f"{PAGE_PATH}/access", ["post"]),
    ("workspaces/example/views", ["get", "post"]),
    (f"workspaces/example/views/{RESOURCE_ID}", ["get", "put", "patch", "delete"]),
    (f"{PROJECT_PATH}/views", ["get", "post"]),
    (f"{PROJECT_PATH}/views/{RESOURCE_ID}", ["get", "put", "patch", "delete"]),
    ("workspaces/example/users/notifications", ["get"]),
    (NOTIFICATION_PATH, ["get", "patch", "delete"]),
    (f"{NOTIFICATION_PATH}/read", ["post", "delete"]),
    (f"{NOTIFICATION_PATH}/archive", ["post", "delete"]),
    ("workspaces/example/users/notifications/unread", ["get"]),
    ("workspaces/example/users/notifications/mark-all-read", ["post"]),
    ("users/me/notification-preferences", ["get", "patch"]),
    ("workspaces/example/webhooks", ["get", "post"]),
    (f"workspaces/example/webhooks/{RESOURCE_ID}", ["get", "patch", "delete"]),
    (f"workspaces/example/webhooks/{RESOURCE_ID}/regenerate", ["post"]),
    (f"workspaces/example/webhook-logs/{RESOURCE_ID}", ["get"]),
]


@pytest.mark.contract
@pytest.mark.parametrize("route,methods", BUSINESS_ROUTES)
def test_business_routes_require_api_key(route, methods):
    url = f"/api/v1/{route}/"
    match = resolve(url)
    assert issubclass(match.func.cls, APIKeyBusinessMixin)
    assert match.func.cls.authentication_classes == [APIKeyAuthentication]
    for method in methods:
        request = getattr(APIRequestFactory(), method)(url, {}, format="json")
        response = match.func(request, **match.kwargs)
        assert response.status_code in (401, 403)


def test_session_bases_still_require_session_authentication():
    assert BaseAPIView.authentication_classes == [BaseSessionAuthentication]
    assert BaseViewSet.authentication_classes == [BaseSessionAuthentication]


def test_estimates_and_collection_upsert_are_registered():
    estimate = resolve(f"/api/v1/{PROJECT_PATH}/estimates/")
    assert set(estimate.func.view_initkwargs["http_method_names"]) == {"get", "post", "patch", "delete"}
    upsert = resolve(f"/api/v1/{PROJECT_PATH}/work-items/upsert/")
    assert upsert.func.view_initkwargs["http_method_names"] == ["put"]
    assert set(upsert.kwargs) == {"slug", "project_id"}
    detail = resolve(f"/api/v1/{PROJECT_PATH}/work-items/{RESOURCE_ID}/")
    assert "put" not in detail.func.view_initkwargs["http_method_names"]


def test_schema_hook_keeps_supported_put_and_resource_names():
    endpoints = [
        ("/api/v1/workspaces/server-team/", "", "PUT", None),
        ("/api/workspaces/", "", "GET", None),
        ("/auth/sign-in/", "", "POST", None),
    ]
    assert preprocess_filter_api_v1_paths(endpoints) == endpoints[:1]
    assert SPECTACULAR_SETTINGS["SERVERS"] == [{"url": "/", "description": "Current deployment"}]


@pytest.mark.django_db
@pytest.mark.skipif(not settings.ENABLE_DRF_SPECTACULAR, reason="Schema generation is disabled in these settings.")
def test_generated_schema_includes_business_methods_and_api_key_auth():
    generator = SchemaGenerator()
    schema = generator.get_schema(request=None, public=True)
    validate_schema(schema)
    paths = schema["paths"]
    for path, _, method, callback in generator.endpoints:
        assert method.lower() in paths[path], f"Missing {method} {path}"
        operation = paths[path][method.lower()]
        headers = {
            parameter["name"]: parameter
            for parameter in operation.get("parameters", [])
            if parameter.get("in") == "header"
        }
        action = getattr(callback, "actions", {}).get(method.lower())
        unsupported = action in getattr(
            callback.cls, "idempotency_unsupported_actions", set()
        ) or method.upper() in getattr(callback.cls, "idempotency_unsupported_methods", set())
        if unsupported:
            assert "Idempotency-Key" not in headers
            assert "does not support Idempotency-Key" in operation["description"]
            assert "400" in operation["responses"]
        elif method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            assert "Idempotency-Key" in headers, f"Missing Idempotency-Key on {method} {path}"
            assert headers["Idempotency-Key"]["required"] is False
            assert "application/json" in headers["Idempotency-Key"]["description"]
            assert "415" in operation["responses"]
        else:
            assert "Idempotency-Key" not in headers
    assert "put" in paths["/api/v1/workspaces/{slug}/"]
    assert "put" in paths["/api/v1/workspaces/{slug}/projects/{project_id}/views/{pk}/"]
    assert "put" in paths["/api/v1/workspaces/{slug}/projects/{project_id}/work-items/upsert/"]
    assert "get" in paths["/api/v1/workspaces/{slug}/projects/{project_id}/estimates/"]
    versions = paths["/api/v1/workspaces/{slug}/projects/{project_id}/pages/{page_id}/versions/"]["get"]
    assert versions["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
    summary = paths["/api/v1/workspaces/{slug}/projects/{project_id}/summary/"]["get"]
    assert summary["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/ProjectSummary")
    summary_fields = schema["components"]["schemas"]["ProjectSummary"]["properties"]
    assert set(summary_fields) == {"id", "name", "identifier", "counts"}
    work_items = "/api/v1/workspaces/{slug}/projects/{project_id}/work-items/"
    conditional_writes = {
        (work_items + "{pk}/", "patch"),
        (work_items + "{pk}/", "delete"),
        (work_items + "upsert/", "put"),
    }
    etag_responses = {
        (work_items, "post", "201"),
        (work_items + "{pk}/", "get", "200"),
        (work_items + "{pk}/", "patch", "200"),
        (work_items + "upsert/", "put", "200"),
        (work_items + "upsert/", "put", "201"),
    }
    actual_conditional_writes, actual_etag_responses = set(), set()
    for path, operations in paths.items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "header" and parameter["name"] == "If-Match":
                    actual_conditional_writes.add((path, method))
                    assert parameter["required"] is False
                    assert parameter["schema"]["type"] == "string"
                    assert "412" in operation["responses"]
            for code, response in operation["responses"].items():
                if "ETag" in response.get("headers", {}):
                    actual_etag_responses.add((path, method, code))
                    assert response["headers"]["ETag"]["schema"]["type"] == "string"
    assert actual_conditional_writes == conditional_writes
    assert actual_etag_responses == etag_responses
    assert "drf_spectacular.hooks.postprocess_schema_enums" in SPECTACULAR_SETTINGS["POSTPROCESSING_HOOKS"]
    assert any(component.get("enum") for component in schema["components"]["schemas"].values())
    for path, operations in paths.items():
        if "/pages/" in path or "/webhooks/" in path:
            for operation in operations.values():
                assert {"ApiKeyAuthentication": []} in operation["security"]
    assert not any(path.startswith(("/auth/", "/api/instances/")) for path in paths)


@pytest.fixture
def agent_client(api_client, api_token, monkeypatch):
    # Existing conftest's token fixture uses this plaintext, now stored hashed.
    api_client.credentials(HTTP_X_API_KEY="test-api-token-12345")
    task_paths = [
        "plane.middleware.logger.process_logs.delay",
        "plane.app.views.workspace.base.workspace_seed.delay",
        "plane.app.views.workspace.base.track_event.delay",
        "plane.app.views.page.base.page_transaction.delay",
        "plane.app.views.page.base.track_page_version.delay",
        "plane.app.views.page.base.recent_visited_task.delay",
        "plane.app.views.view.base.recent_visited_task.delay",
        "plane.api.views.project.model_activity.delay",
        "plane.api.views.issue.issue_activity.delay",
        "plane.api.views.issue.model_activity.delay",
    ]
    for task_path in task_paths:
        monkeypatch.setattr(task_path, lambda *args, **kwargs: None)
    return api_client


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(name="Agent project", identifier="AGENT", workspace=workspace)
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def page(workspace, project, create_user):
    page = Page.objects.create(workspace=workspace, owned_by=create_user, name="Agent page")
    ProjectPage.objects.create(workspace=workspace, project=project, page=page)
    return page


def rows(response):
    assert response.status_code == 200, response.data
    return response.data["results"] if isinstance(response.data, dict) else response.data


@pytest.mark.contract
@pytest.mark.django_db
class TestBusinessWorkflowAPI:
    @pytest.mark.parametrize("method", ["post", "patch"])
    @pytest.mark.parametrize("other_workspace", [True, False])
    def test_page_labels_cannot_escape_url_project_or_workspace(
        self, agent_client, api_token, workspace, project, page, create_user, method, other_workspace
    ):
        target_workspace = workspace
        if other_workspace:
            target_workspace = Workspace.objects.create(name="Other", slug="other", owner=create_user)
            WorkspaceMember.objects.create(workspace=target_workspace, member=create_user, role=20)
        target_project = Project.objects.create(name="Other", identifier="OTHER", workspace=target_workspace)
        ProjectMember.objects.create(project=target_project, member=create_user, role=20, is_active=True)
        label = Label.objects.create(name="Foreign label", workspace=target_workspace, project=target_project)
        api_token.workspace, api_token.project = workspace, project
        api_token.save(update_fields=["workspace", "project"])
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/"
        if method == "patch":
            url += f"{page.id}/"
        count = Page.objects.count()
        response = getattr(agent_client, method)(url, {"name": "Attempt", "labels": [str(label.id)]}, format="json")
        assert response.status_code == 400, response.data
        assert Page.objects.count() == count
        assert not PageLabel.objects.filter(label=label).exists()
        page.refresh_from_db()
        assert page.name == "Agent page"

    @pytest.mark.parametrize("method", ["post", "patch"])
    @pytest.mark.parametrize("target", ["other_workspace", "other_project", "private"])
    def test_page_parent_cannot_escape_context_or_visibility(
        self, agent_client, api_token, workspace, project, page, create_user, method, target
    ):
        parent_workspace, parent_project, parent_owner = workspace, project, create_user
        if target == "other_workspace":
            parent_workspace = Workspace.objects.create(name="Other", slug="other", owner=create_user)
        if target != "private":
            parent_project = Project.objects.create(name="Other", identifier="OTHER", workspace=parent_workspace)
        else:
            parent_owner = User.objects.create(email="private-parent@example.test", username="private-parent")
        parent = Page.objects.create(
            workspace=parent_workspace,
            owned_by=parent_owner,
            name="Foreign parent",
            access=Page.PRIVATE_ACCESS if target == "private" else Page.PUBLIC_ACCESS,
        )
        ProjectPage.objects.create(workspace=parent_workspace, project=parent_project, page=parent)
        api_token.workspace, api_token.project = workspace, project
        api_token.save(update_fields=["workspace", "project"])
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/"
        if method == "patch":
            url += f"{page.id}/"
        count = Page.objects.count()
        response = getattr(agent_client, method)(url, {"name": "Attempt", "parent": str(parent.id)}, format="json")
        assert response.status_code == 400, response.data
        assert Page.objects.count() == count
        page.refresh_from_db()
        assert page.parent_id is None
        assert page.name == "Agent page"

    @pytest.mark.parametrize("method", ["post", "patch"])
    @pytest.mark.parametrize("field", ["project_ids", "label_ids"])
    def test_computed_page_relationship_ids_are_not_write_inputs(
        self, agent_client, workspace, project, page, method, field
    ):
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/"
        if method == "patch":
            url += f"{page.id}/"
        count = Page.objects.count()
        response = getattr(agent_client, method)(url, {"name": "Attempt", field: [str(uuid4())]}, format="json")
        assert response.status_code == 400, response.data
        assert field in response.data
        assert Page.objects.count() == count
        assert list(page.projects.values_list("id", flat=True)) == [project.id]

    def test_page_accepts_current_project_and_workspace_labels_and_visible_parent(
        self, agent_client, api_token, workspace, project, page, create_user
    ):
        api_token.workspace, api_token.project = workspace, project
        api_token.save(update_fields=["workspace", "project"])
        project_label = Label.objects.create(name="Project label", project=project, workspace=workspace)
        workspace_label = Label.objects.create(name="Workspace label", workspace=workspace)
        prefix = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/"
        payload = {"name": "Legitimate page", "labels": [str(project_label.id), str(workspace_label.id)]}
        created = agent_client.post(prefix, payload, format="json")
        assert created.status_code == 201, created.data
        child = Page.objects.get(pk=created.data["id"])
        assert set(child.labels.values_list("id", flat=True)) == {project_label.id, workspace_label.id}
        other = User.objects.create(email="public-parent@example.test", username="public-parent")
        page.owned_by = other
        page.save()
        response = agent_client.patch(prefix + f"{child.id}/", {"parent": str(page.id)}, format="json")
        assert response.status_code == 200, response.data
        child.refresh_from_db()
        assert child.parent_id == page.id

    def test_project_summary_documents_and_returns_selected_counts(self, agent_client, workspace, project, page):
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/summary/?fields=members,pages"
        response = agent_client.get(url)
        assert response.status_code == 200, response.data
        assert response.data["counts"] == {"members": 1, "pages": 1}
        assert str(response.data["id"]) == str(project.id)

    def test_guest_cannot_reference_a_parent_hidden_by_project_visibility(
        self, agent_client, workspace, project, page, create_user
    ):
        Project.objects.filter(pk=project.pk).update(guest_view_all_features=False)
        ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=5)
        owner = User.objects.create(email="hidden-parent@example.test", username="hidden-parent")
        parent = Page.objects.create(workspace=workspace, owned_by=owner, name="Hidden public parent")
        ProjectPage.objects.create(project=project, workspace=workspace, page=parent)
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/"
        response = agent_client.patch(url, {"parent": str(parent.id)}, format="json")
        assert response.status_code == 400, response.data
        page.refresh_from_db()
        assert page.parent_id is None

    def test_page_create_list_update_and_archive(self, agent_client, workspace, project):
        prefix = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/"
        response = agent_client.post(prefix, {"name": "Agent-created page"}, format="json")
        assert response.status_code == 201, response.data
        page_id = response.data["id"]
        assert [str(row["id"]) for row in rows(agent_client.get(prefix))] == [str(page_id)]
        url = prefix + f"{page_id}/"
        assert agent_client.patch(url, {"name": "Edited page"}, format="json").status_code == 200
        assert agent_client.get(url + "?track_visit=false").status_code == 200
        assert agent_client.post(url + "archive/", {}, format="json").status_code == 200
        assert agent_client.delete(url).status_code == 204

    def test_workspace_discovery_filters_active_membership_and_key_scope(
        self, agent_client, api_token, workspace, project, create_user
    ):
        other = Workspace.objects.create(name="Other workspace", slug="other-workspace", owner=create_user)
        WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
        hidden = Workspace.objects.create(name="Inactive membership", slug="hidden-workspace", owner=create_user)
        WorkspaceMember.objects.create(workspace=hidden, member=create_user, role=20, is_active=False)
        assert {str(row["id"]) for row in rows(agent_client.get("/api/v1/workspaces/"))} == {
            str(workspace.id),
            str(other.id),
        }
        api_token.project = project
        api_token.save(update_fields=["project"])
        assert [str(row["id"]) for row in rows(agent_client.get("/api/v1/workspaces/"))] == [str(workspace.id)]

    def test_project_discovery_filters_key_project(self, agent_client, api_token, workspace, project, create_user):
        other = Project.objects.create(name="Other project", identifier="OTHER", workspace=workspace)
        ProjectMember.objects.create(project=other, member=create_user, role=20, is_active=True)
        api_token.workspace = workspace
        api_token.project = project
        api_token.save(update_fields=["workspace", "project"])
        response = agent_client.get(f"/api/v1/workspaces/{workspace.slug}/projects/")
        assert [str(row["id"]) for row in rows(response)] == [str(project.id)]

    def test_workspace_crud_with_api_key(self, agent_client, create_user, monkeypatch):
        monkeypatch.setattr("plane.app.views.workspace.base.get_configuration_value", lambda *args: ("0",))
        response = agent_client.post("/api/v1/workspaces/", {"name": "Agent workspace", "slug": "agent-workspace"})
        assert response.status_code == 201, response.data
        workspace = Workspace.objects.get(slug="agent-workspace")
        assert WorkspaceMember.objects.filter(workspace=workspace, member=create_user, role=20).exists()
        url = f"/api/v1/workspaces/{workspace.slug}/"
        assert agent_client.get(url).status_code == 200
        assert agent_client.patch(url, {"name": "Updated"}, format="json").status_code == 200
        assert agent_client.delete(url).status_code == 204

    def test_workspace_member_cannot_put_around_admin_patch_guard(self, agent_client, workspace, create_user):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=15)
        url = f"/api/v1/workspaces/{workspace.slug}/"
        for method in ("put", "patch", "delete"):
            assert getattr(agent_client, method)(url, {"name": "Changed"}, format="json").status_code == 403

    def test_workspace_scoped_key_cannot_access_other_workspace(self, agent_client, api_token, workspace, create_user):
        other = Workspace.objects.create(name="Other", slug="other", owner=create_user)
        WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
        api_token.workspace = workspace
        api_token.save(update_fields=["workspace"])
        assert agent_client.get("/api/v1/workspaces/other/").status_code == 403

    def test_page_description_and_versions(self, agent_client, workspace, project, page, create_user):
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/"
        response = agent_client.patch(url + "description/", {"description_html": "<p>Agent content</p>"}, format="json")
        assert response.status_code == 200, response.data
        page.refresh_from_db()
        assert page.description_html == "<p>Agent content</p>"
        binary = agent_client.get(url + "description/")
        assert binary.status_code == 200
        assert binary["Content-Type"] == "application/octet-stream"
        assert b"".join(binary.streaming_content) == b""
        version = PageVersion.objects.create(workspace=workspace, page=page, owned_by=create_user)
        assert len(rows(agent_client.get(url + "versions/"))) == 1
        assert agent_client.get(url + f"versions/{version.id}/").status_code == 200

    def test_private_page_cannot_be_read_or_changed_by_another_member(self, agent_client, workspace, project, page):
        other = User.objects.create(email="page-owner@example.test", username="page-owner")
        page.owned_by = other
        page.access = Page.PRIVATE_ACCESS
        page.save()
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/"
        assert agent_client.get(url + "description/").status_code == 403
        assert agent_client.patch(url, {"name": "Changed"}, format="json").status_code == 403
        assert agent_client.get(url + "versions/").status_code == 403

    def test_page_cannot_be_addressed_through_another_project(
        self, agent_client, workspace, project, page, create_user
    ):
        other = Project.objects.create(name="Other", identifier="OTHER", workspace=workspace)
        ProjectMember.objects.create(project=other, member=create_user, role=20, is_active=True)
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{other.id}/pages/{page.id}/versions/"
        assert agent_client.get(url).status_code == 404

    @pytest.mark.parametrize("project_level", [False, True])
    def test_saved_view_owner_and_lock_checks_apply_to_put_and_replay(
        self, agent_client, workspace, project, create_user, project_level
    ):
        prefix = f"/api/v1/workspaces/{workspace.slug}"
        if project_level:
            prefix += f"/projects/{project.id}"
        response = agent_client.post(prefix + "/views/", {"name": "Agent view"}, format="json")
        assert response.status_code == 201, response.data
        view = IssueView.objects.get(pk=response.data["id"])
        assert view.created_by_id == create_user.id
        url = prefix + f"/views/{view.id}/"
        headers = {"HTTP_IDEMPOTENCY_KEY": "rename-view"}
        response = agent_client.put(url, {"name": "Renamed"}, format="json", **headers)
        assert response.status_code == 200, response.data
        assert agent_client.put(url, {"name": "Renamed"}, format="json", **headers)["Idempotency-Replayed"] == "true"
        IssueView.objects.filter(pk=view.pk).update(is_locked=True)
        assert agent_client.put(url, {"name": "Renamed"}, format="json", **headers).status_code == 403
        IssueView.objects.filter(pk=view.pk).update(is_locked=False)
        other = User.objects.create(email="view-owner@example.test", username="view-owner")
        IssueView.objects.filter(pk=view.pk).update(owned_by=other)
        assert agent_client.put(url, {"name": "Renamed"}, format="json", **headers).status_code == 403

    def test_notification_access_stays_with_current_user(self, agent_client, workspace, create_user):
        other = User.objects.create(email="notification-owner@example.test", username="notification-owner")
        mine = Notification.objects.create(workspace=workspace, receiver=create_user, title="Mine")
        theirs = Notification.objects.create(workspace=workspace, receiver=other, title="Theirs")
        prefix = f"/api/v1/workspaces/{workspace.slug}/users/notifications/"
        response = agent_client.post(prefix + f"{mine.id}/read/", {}, format="json")
        assert response.status_code == 200, response.data
        assert agent_client.get(prefix + f"{theirs.id}/").status_code == 404
        assert agent_client.post(prefix + f"{theirs.id}/read/", {}, format="json").status_code == 404
        theirs.refresh_from_db()
        assert theirs.read_at is None

    def test_guest_can_create_project_saved_view_but_inactive_member_cannot(
        self, agent_client, workspace, project, create_user
    ):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=5)
        ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/views/"
        response = agent_client.post(url, {"name": "Guest view"}, format="json")
        assert response.status_code == 201, response.data
        ProjectMember.objects.filter(project=project, member=create_user).update(is_active=False)
        assert agent_client.post(url, {"name": "Forbidden view"}, format="json").status_code == 403

    def test_notification_guest_can_mark_own_notifications_read(self, agent_client, workspace, create_user):
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=5)
        notification = Notification.objects.create(workspace=workspace, receiver=create_user, title="Mine")
        url = f"/api/v1/workspaces/{workspace.slug}/users/notifications/{notification.id}/read/"
        assert agent_client.post(url, {}, format="json").status_code == 200

    def test_webhook_admin_check_runs_before_idempotent_replay(self, agent_client, workspace, create_user, monkeypatch):
        monkeypatch.setattr("plane.app.serializers.webhook.validate_url", lambda *args, **kwargs: None)
        url = f"/api/v1/workspaces/{workspace.slug}/webhooks/"
        payload = {"url": "https://example.test/webhook"}
        headers = {"HTTP_IDEMPOTENCY_KEY": "create-webhook"}
        response = agent_client.post(url, payload, format="json", **headers)
        assert response.status_code == 201, response.data
        assert Webhook.objects.filter(workspace=workspace).count() == 1
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=15)
        assert agent_client.post(url, payload, format="json", **headers).status_code == 403
        assert agent_client.get(url).status_code == 403

    def test_read_scoped_key_cannot_write(self, agent_client, api_token, workspace):
        api_token.scopes = ["read"]
        api_token.save(update_fields=["scopes"])
        url = f"/api/v1/workspaces/{workspace.slug}/"
        assert agent_client.get(url).status_code == 200
        assert agent_client.patch(url, {"name": "Changed"}, format="json").status_code == 403

    def test_api_key_does_not_authenticate_session_routes(self, agent_client, workspace):
        assert agent_client.get(f"/api/workspaces/{workspace.slug}/").status_code in (401, 403)

    def test_page_lock_change_is_checked_before_replay(self, agent_client, workspace, project, page):
        url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/pages/{page.id}/description/"
        payload = {"description_html": "<p>Content</p>"}
        headers = {"HTTP_IDEMPOTENCY_KEY": "page-description"}
        assert agent_client.patch(url, payload, format="json", **headers).status_code == 200
        Page.objects.filter(pk=page.pk).update(is_locked=True)
        assert agent_client.patch(url, payload, format="json", **headers).status_code == 403


@pytest.fixture
def delete_target(request, workspace, project, create_user):
    kind = request.param
    prefix = f"/api/v1/workspaces/{workspace.slug}"
    if kind == "webhook":
        obj = Webhook.objects.create(workspace=workspace, url="https://example.test/webhook")
        return kind, obj, prefix + f"/webhooks/{obj.pk}/"
    if kind == "notification":
        obj = Notification.objects.create(workspace=workspace, receiver=create_user, title="Delete me")
        return kind, obj, prefix + f"/users/notifications/{obj.pk}/"

    owner = User.objects.create(email=f"delete-owner-{uuid4().hex}@example.test", username=uuid4().hex)
    if kind == "page":
        obj = Page.objects.create(
            workspace=workspace, owned_by=owner, name="Delete me", archived_at=timezone.now().date()
        )
        ProjectPage.objects.create(workspace=workspace, project=project, page=obj)
        return kind, obj, prefix + f"/projects/{project.pk}/pages/{obj.pk}/"
    obj = IssueView.objects.create(
        workspace=workspace,
        project=project if kind == "project_view" else None,
        owned_by=owner,
        created_by=owner,
        name="Delete me",
    )
    if kind == "project_view":
        prefix += f"/projects/{project.pk}"
    return kind, obj, prefix + f"/views/{obj.pk}/"


DELETE_TARGETS = ["page", "workspace_view", "project_view", "notification", "webhook"]


@pytest.mark.contract
@pytest.mark.django_db
class TestBusinessDeleteReplay:
    @pytest.mark.parametrize("delete_target", ["project_view"], indirect=True)
    def test_workspace_admin_cannot_replay_after_losing_project_admin_role(
        self, agent_client, delete_target, workspace, project, create_user
    ):
        _, obj, url = delete_target
        headers = {"HTTP_IDEMPOTENCY_KEY": f"project-admin-delete-{obj.pk}"}
        assert agent_client.delete(url, {}, format="json", **headers).status_code == 204
        ProjectMember.objects.filter(project=project, member=create_user).update(role=15)
        assert WorkspaceMember.objects.get(workspace=workspace, member=create_user).role == 20
        denied = agent_client.delete(url, {}, format="json", **headers)
        assert denied.status_code == 403
        assert "Idempotency-Replayed" not in denied

    @pytest.mark.parametrize("delete_target", ["workspace_view", "project_view"], indirect=True)
    def test_admin_can_replay_deleting_another_owners_private_view(self, agent_client, delete_target):
        _, obj, url = delete_target
        IssueView.objects.filter(pk=obj.pk).update(access=0)
        headers = {"HTTP_IDEMPOTENCY_KEY": f"private-view-delete-{obj.pk}"}
        assert agent_client.delete(url, {}, format="json", **headers).status_code == 204
        replay = agent_client.delete(url, {}, format="json", **headers)
        assert replay.status_code == 204
        assert replay["Idempotency-Replayed"] == "true"

    @pytest.mark.parametrize("delete_target", DELETE_TARGETS, indirect=True)
    def test_delete_replays_and_current_access_is_rechecked(
        self, agent_client, delete_target, workspace, project, create_user
    ):
        kind, obj, url = delete_target
        headers = {"HTTP_IDEMPOTENCY_KEY": f"delete-{obj.pk}"}
        first = agent_client.delete(url, {}, format="json", **headers)
        assert first.status_code == 204, first.data
        assert first.content == b""
        deleted_at = type(obj).all_objects.get(pk=obj.pk).deleted_at
        assert deleted_at is not None

        if kind == "page":
            # The deletion worker also soft-deletes the page/project association.
            ProjectPage.objects.filter(page=obj).delete()
        replay = agent_client.delete(url, {}, format="json", **headers)
        assert replay.status_code == 204, replay.data
        assert replay.content == b""
        assert replay["Idempotency-Replayed"] == "true"
        assert type(obj).all_objects.get(pk=obj.pk).deleted_at == deleted_at
        assert APIIdempotency.objects.count() == 1

        if kind == "notification":
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
        else:
            WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=15)
            ProjectMember.objects.filter(project=project, member=create_user).update(role=15)
        denied = agent_client.delete(url, {}, format="json", **headers)
        assert denied.status_code == 403, denied.data
        assert "Idempotency-Replayed" not in denied

    @pytest.mark.parametrize("delete_target", DELETE_TARGETS, indirect=True)
    def test_delete_replay_fails_closed_without_retained_metadata(self, agent_client, delete_target):
        _, obj, url = delete_target
        headers = {"HTTP_IDEMPOTENCY_KEY": f"delete-{obj.pk}"}
        assert agent_client.delete(url, {}, format="json", **headers).status_code == 204
        type(obj).all_objects.filter(pk=obj.pk).delete()
        response = agent_client.delete(url, {}, format="json", **headers)
        assert response.status_code in (403, 404), response.data
        assert "Idempotency-Replayed" not in response

    @pytest.mark.parametrize("delete_target", ["page", "workspace_view", "project_view"], indirect=True)
    def test_non_admin_owner_can_replay_own_delete(self, agent_client, delete_target, workspace, project, create_user):
        _, obj, url = delete_target
        type(obj).objects.filter(pk=obj.pk).update(owned_by=create_user, created_by=create_user)
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=15)
        ProjectMember.objects.filter(project=project, member=create_user).update(role=15)
        headers = {"HTTP_IDEMPOTENCY_KEY": f"owner-delete-{obj.pk}"}
        assert agent_client.delete(url, {}, format="json", **headers).status_code == 204
        response = agent_client.delete(url, {}, format="json", **headers)
        assert response.status_code == 204, response.data
        assert response["Idempotency-Replayed"] == "true"
        ProjectMember.objects.filter(project=project, member=create_user).update(is_active=False)
        WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(is_active=False)
        assert agent_client.delete(url, {}, format="json", **headers).status_code == 403

    @pytest.mark.parametrize("delete_target", ["page", "workspace_view", "project_view", "notification"], indirect=True)
    def test_deleted_object_cannot_be_mutated_with_an_unmatched_key(self, agent_client, delete_target):
        _, obj, url = delete_target
        assert agent_client.delete(url, {}, format="json", HTTP_IDEMPOTENCY_KEY=f"delete-{obj.pk}").status_code == 204
        if isinstance(obj, (Page, Notification)):
            assert agent_client.get(url).status_code in (403, 404)
        response = agent_client.delete(url, {}, format="json", HTTP_IDEMPOTENCY_KEY="unmatched-key")
        assert response.status_code in (403, 404), response.data
        assert "Idempotency-Replayed" not in response
        assert APIIdempotency.objects.count() == 1

    def test_workspace_delete_rejects_idempotency_before_mutation(self, agent_client, workspace):
        url = f"/api/v1/workspaces/{workspace.slug}/"
        response = agent_client.delete(url, {}, format="json", HTTP_IDEMPOTENCY_KEY="delete-workspace")
        assert response.status_code == 400, response.data
        assert "Idempotency-Key" in response.data
        assert workspace.__class__.objects.filter(pk=workspace.pk).exists()
        assert not APIIdempotency.objects.exists()
        assert agent_client.delete(url).status_code == 204
