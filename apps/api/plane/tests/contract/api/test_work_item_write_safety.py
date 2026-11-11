# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.db import close_old_connections, connections
from rest_framework.test import APIRequestFactory, force_authenticate

from plane.api.idempotency import IdempotencyMixin
from plane.api.views.bulk import BulkWorkItemAPIEndpoint
from plane.api.views.issue import IssueDetailAPIEndpoint, IssueListCreateAPIEndpoint
from plane.db.models import Issue, Project, ProjectMember, State
from plane.db.models.api_idempotency import APIIdempotency


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Agent project", identifier="AGENT")
    ProjectMember.objects.create(project=project, member=create_user, role=15)
    State.objects.create(project=project, name="Backlog", group="backlog", default=True)
    return project


@pytest.fixture
def tasks(monkeypatch, settings):
    settings.WEB_URL = "http://testserver"
    activity, webhook = Mock(), Mock()
    monkeypatch.setattr("plane.api.views.issue.issue_activity.delay", activity)
    monkeypatch.setattr("plane.api.views.issue.model_activity.delay", webhook)
    return activity, webhook


def api_view(view):
    if not issubclass(view, IdempotencyMixin):
        view = type("Idempotent" + view.__name__, (IdempotencyMixin, view), {})
    return view.as_view()


def invoke(view, method, project, user, token, data=None, pk=None, **headers):
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/"
    if pk:
        path += f"{pk}/"
    if view is BulkWorkItemAPIEndpoint:
        path += "bulk/"
    request = getattr(APIRequestFactory(), method)(path, data, format="json", **headers)
    force_authenticate(request, user=user, token=token)
    kwargs = {"slug": project.workspace.slug, "project_id": project.pk}
    if pk is not None:
        kwargs["pk"] = pk
    return api_view(view)(request, **kwargs)


@pytest.mark.django_db
def test_normal_create_and_update_publish_expected_events(
    project, create_user, api_token, tasks, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        created = invoke(IssueListCreateAPIEndpoint, "post", project, create_user, api_token, {"name": "Original"})
    assert created.status_code == 201
    assert tasks[0].call_args.kwargs["type"] == "issue.activity.created"
    assert tasks[1].call_count == 1

    with django_capture_on_commit_callbacks(execute=True):
        updated = invoke(
            IssueDetailAPIEndpoint,
            "patch",
            project,
            create_user,
            api_token,
            {"name": "Updated"},
            pk=created.data["id"],
            HTTP_IF_MATCH=created["ETag"],
        )
    assert updated.status_code == 200
    assert updated.data["name"] == "Updated"
    assert updated["ETag"] != created["ETag"]
    assert tasks[0].call_args.kwargs["type"] == "issue.activity.updated"
    assert tasks[1].call_count == 2


@pytest.mark.django_db
def test_stale_revision_rejects_write_and_header_free_clients_still_work(project, create_user, api_token, tasks):
    issue = Issue.objects.create(project=project, name="Original")
    current = invoke(IssueDetailAPIEndpoint, "get", project, create_user, api_token, pk=issue.pk)
    assert current.status_code == 200
    changed = invoke(IssueDetailAPIEndpoint, "patch", project, create_user, api_token, {"name": "New"}, pk=issue.pk)
    assert changed.status_code == 200
    stale = invoke(
        IssueDetailAPIEndpoint,
        "patch",
        project,
        create_user,
        api_token,
        {"name": "Stale"},
        pk=issue.pk,
        HTTP_IF_MATCH=current["ETag"],
    )
    assert stale.status_code == 412
    issue.refresh_from_db()
    assert issue.name == "New"


@pytest.mark.django_db
def test_idempotent_create_replays_without_duplicate_activities(
    project, create_user, api_token, tasks, django_capture_on_commit_callbacks
):
    headers = {"HTTP_IDEMPOTENCY_KEY": "create-work-item"}
    with django_capture_on_commit_callbacks(execute=True):
        created = invoke(
            IssueListCreateAPIEndpoint, "post", project, create_user, api_token, {"name": "Created"}, **headers
        )
    with django_capture_on_commit_callbacks(execute=True):
        replay = invoke(
            IssueListCreateAPIEndpoint, "post", project, create_user, api_token, {"name": "Created"}, **headers
        )
    assert created.status_code == replay.status_code == 201
    assert json.loads(created.render().content) == json.loads(replay.render().content)
    assert Issue.objects.filter(project=project).count() == 1
    assert [task.call_count for task in tasks] == [1, 1]


@pytest.mark.django_db
def test_bulk_update_has_single_write_validation_and_events(
    project, create_user, api_token, tasks, django_capture_on_commit_callbacks
):
    issues = [Issue.objects.create(project=project, name=name) for name in ("First", "Second")]
    updates = [{"id": str(issue.pk), "data": {"name": issue.name + " updated"}} for issue in issues]
    headers = {"HTTP_IDEMPOTENCY_KEY": "bulk-update"}
    with django_capture_on_commit_callbacks(execute=True):
        response = invoke(
            BulkWorkItemAPIEndpoint, "post", project, create_user, api_token, {"updates": updates}, **headers
        )
    assert response.status_code == 200
    assert [item["name"] for item in response.data] == ["First updated", "Second updated"]
    assert [task.call_count for task in tasks] == [2, 2]
    assert {call.kwargs["type"] for call in tasks[0].call_args_list} == {"issue.activity.updated"}
    assert {call.kwargs["model_id"] for call in tasks[1].call_args_list} == {str(issue.pk) for issue in issues}
    with django_capture_on_commit_callbacks(execute=True):
        replay = invoke(
            BulkWorkItemAPIEndpoint, "post", project, create_user, api_token, {"updates": updates}, **headers
        )
    assert json.loads(response.render().content) == json.loads(replay.render().content)
    assert [task.call_count for task in tasks] == [2, 2]


@pytest.mark.django_db
@pytest.mark.parametrize("second_change", [{"name": ""}, {"priority": "not-a-priority"}])
def test_bulk_failure_rolls_back_all_changes_claims_and_events(
    project, create_user, api_token, tasks, django_capture_on_commit_callbacks, second_change
):
    first = Issue.objects.create(project=project, name="First")
    second = Issue.objects.create(project=project, name="Second")
    updates = [{"id": str(first.pk), "data": {"name": "Changed"}}, {"id": str(second.pk), "data": second_change}]
    with django_capture_on_commit_callbacks(execute=True):
        response = invoke(
            BulkWorkItemAPIEndpoint,
            "post",
            project,
            create_user,
            api_token,
            {"updates": updates},
            HTTP_IDEMPOTENCY_KEY="bulk-failure",
        )
    assert response.status_code == 400
    assert response.data["index"] == 1
    first.refresh_from_db()
    assert first.name == "First"
    assert not APIIdempotency.objects.exists()
    assert [task.call_count for task in tasks] == [0, 0]


@pytest.mark.django_db
def test_bulk_cannot_bypass_single_item_project_role(project, create_user, api_token, tasks):
    issue = Issue.objects.create(project=project, name="Original")
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    single = invoke(IssueDetailAPIEndpoint, "patch", project, create_user, api_token, {"name": "No"}, pk=issue.pk)
    bulk = invoke(
        BulkWorkItemAPIEndpoint,
        "post",
        project,
        create_user,
        api_token,
        {"updates": [{"id": str(issue.pk), "data": {"name": "No"}}]},
    )
    assert single.status_code == bulk.status_code == 403
    issue.refresh_from_db()
    assert issue.name == "Original"


@pytest.mark.django_db
def test_bulk_stale_entry_rolls_back_earlier_updates(
    project, create_user, api_token, tasks, django_capture_on_commit_callbacks
):
    first = Issue.objects.create(project=project, name="First")
    second = Issue.objects.create(project=project, name="Second")
    with django_capture_on_commit_callbacks(execute=True):
        response = invoke(
            BulkWorkItemAPIEndpoint,
            "post",
            project,
            create_user,
            api_token,
            {
                "updates": [
                    {"id": str(first.pk), "data": {"name": "Changed"}},
                    {"id": str(second.pk), "data": {"name": "Stale"}, "if_match": '"old-revision"'},
                ]
            },
        )
    assert response.status_code == 412
    first.refresh_from_db()
    assert first.name == "First"
    assert [task.call_count for task in tasks] == [0, 0]


@pytest.mark.django_db(transaction=True)
def test_concurrent_idempotent_external_id_creation_writes_once(project, create_user, api_token, tasks):
    start = Barrier(2)

    def call():
        close_old_connections()
        try:
            start.wait()
            return invoke(
                IssueListCreateAPIEndpoint,
                "post",
                project,
                create_user,
                api_token,
                {"name": "External", "external_id": "external-identifier", "external_source": "agent"},
                HTTP_IDEMPOTENCY_KEY="create-external-work-item",
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call) for _ in range(2)]
        responses = [future.result() for future in futures]
    assert [response.status_code for response in responses] == [201, 201]
    assert json.loads(responses[0].render().content) == json.loads(responses[1].render().content)
    assert (
        Issue.objects.filter(project=project, external_id="external-identifier", external_source="agent").count() == 1
    )
    assert [task.call_count for task in tasks] == [1, 1]


@pytest.mark.django_db
def test_delete_replay_rechecks_admin_role_on_soft_deleted_item(
    project, create_user, api_token, tasks, monkeypatch, django_capture_on_commit_callbacks
):
    # The model-level task is owned by main; isolate its external delivery here.
    monkeypatch.setattr("plane.db.mixins.soft_delete_related_objects.delay", Mock())
    issue = Issue.objects.create(project=project, name="Other creator")
    membership = ProjectMember.objects.get(project=project, member=create_user)
    membership.role = 20
    membership.save()
    headers = {"HTTP_IDEMPOTENCY_KEY": "delete-work-item"}
    with django_capture_on_commit_callbacks(execute=True):
        deleted = invoke(IssueDetailAPIEndpoint, "delete", project, create_user, api_token, pk=issue.pk, **headers)
    assert deleted.status_code == 204
    replay = invoke(IssueDetailAPIEndpoint, "delete", project, create_user, api_token, pk=issue.pk, **headers)
    assert replay.status_code == 204
    assert replay.render().content == b""
    assert replay["Idempotency-Replayed"] == "true"
    membership.role = 15
    membership.save()
    replay = invoke(IssueDetailAPIEndpoint, "delete", project, create_user, api_token, pk=issue.pk, **headers)
    assert replay.status_code == 403
    assert "Idempotency-Replayed" not in replay


@pytest.mark.django_db(transaction=True)
def test_concurrent_if_match_updates_allow_one_winner(project, create_user, api_token, tasks):
    issue = Issue.objects.create(project=project, name="Original")
    revision = invoke(IssueDetailAPIEndpoint, "get", project, create_user, api_token, pk=issue.pk)["ETag"]
    start = Barrier(2)

    def call(name):
        close_old_connections()
        try:
            start.wait()
            return invoke(
                IssueDetailAPIEndpoint,
                "patch",
                project,
                create_user,
                api_token,
                {"name": name},
                pk=issue.pk,
                HTTP_IF_MATCH=revision,
            )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call, name) for name in ("First", "Second")]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 412]
    winner = next(response.data["name"] for response in responses if response.status_code == 200)
    issue.refresh_from_db()
    assert issue.name == winner
    assert [task.call_count for task in tasks] == [1, 1]
