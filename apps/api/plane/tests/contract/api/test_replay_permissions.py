# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock

import pytest
from crum import impersonate
from rest_framework.test import APIRequestFactory, force_authenticate

from plane.api.views.asset import UserAssetEndpoint, UserServerAssetEndpoint
from plane.api.views.cycle import CycleDetailAPIEndpoint
from plane.api.views.intake import IntakeIssueDetailAPIEndpoint
from plane.api.views.module import ModuleDetailAPIEndpoint
from plane.app.permissions import ROLE
from plane.db.models import (
    APIToken,
    Cycle,
    FileAsset,
    Intake,
    IntakeIssue,
    Issue,
    Module,
    Project,
    ProjectMember,
    State,
    User,
)
from plane.db.models.intake import IntakeIssueStatus


@pytest.fixture
def replay_context(workspace, create_user, api_token, monkeypatch, settings):
    settings.WEB_URL = "http://testserver"
    project = Project.objects.create(
        workspace=workspace, name="Replay permissions", identifier="REPLAY", intake_view=True
    )
    membership = ProjectMember.objects.create(project=project, member=create_user, role=ROLE.ADMIN.value)
    State.objects.create(project=project, name="Backlog", group="backlog", default=True)
    other = User.objects.create(email="other-replay-owner@plane.so", username="other-replay-owner")
    activity, webhook, cascade = Mock(), Mock(), Mock()
    monkeypatch.setattr("plane.bgtasks.issue_activities_task.issue_activity.delay", activity)
    monkeypatch.setattr("plane.bgtasks.webhook_task.model_activity.delay", webhook)
    monkeypatch.setattr("plane.db.mixins.soft_delete_related_objects.delay", cascade)
    return project, membership, other, (activity, webhook, cascade)


def call(view, method, project, user, token, path, data=None, **kwargs):
    request = getattr(APIRequestFactory(), method)(
        path, data, format="json", HTTP_IDEMPOTENCY_KEY="replay-permission-key"
    )
    force_authenticate(request, user=user, token=token)
    # RequestFactory bypasses the middleware that supplies audit-model actors.
    with impersonate(user):
        return view.as_view()(request, **kwargs)


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["cycle", "module"])
@pytest.mark.parametrize("is_owner", [False, True])
def test_cycle_module_delete_replay_rechecks_admin_role(
    replay_context, create_user, api_token, django_capture_on_commit_callbacks, kind, is_owner
):
    project, membership, other, tasks = replay_context
    owner = create_user if is_owner else other
    if kind == "cycle":
        item = Cycle.objects.create(project=project, name="Cycle", owned_by=owner)
        view = CycleDetailAPIEndpoint
    else:
        item = Module(project=project, name="Module", created_by=owner)
        item.save(disable_auto_set_user=True)
        view = ModuleDetailAPIEndpoint
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/{kind}s/{item.pk}/"
    kwargs = {"slug": project.workspace.slug, "project_id": project.pk, "pk": item.pk}
    with django_capture_on_commit_callbacks(execute=True):
        deleted = call(view, "delete", project, create_user, api_token, path, **kwargs)
    assert deleted.status_code == 204
    assert not type(item).objects.filter(pk=item.pk).exists()
    event_counts = [task.call_count for task in tasks]

    membership.role = ROLE.MEMBER.value
    membership.save()
    with django_capture_on_commit_callbacks(execute=True):
        replay = call(view, "delete", project, create_user, api_token, path, **kwargs)
    assert replay.status_code == (204 if is_owner else 403)
    assert (replay.get("Idempotency-Replayed") == "true") is is_owner
    assert [task.call_count for task in tasks] == event_counts


def create_intake_item(project, creator, status=IntakeIssueStatus.PENDING):
    intake = Intake.objects.create(project=project, name="Intake")
    issue = Issue(project=project, name="Intake work item", created_by=creator)
    issue.save(disable_auto_set_user=True)
    item = IntakeIssue(project=project, intake=intake, issue=issue, created_by=creator, status=status)
    item.save(disable_auto_set_user=True)
    return item


@pytest.mark.django_db
@pytest.mark.parametrize("is_creator", [False, True])
def test_personal_keys_cannot_patch_agent_first_intake_tasks(
    replay_context, create_user, api_token, django_capture_on_commit_callbacks, is_creator
):
    project, membership, other, tasks = replay_context
    item = create_intake_item(project, create_user if is_creator else other)
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/intake-issues/{item.issue_id}/"
    kwargs = {"slug": project.workspace.slug, "project_id": project.pk, "issue_id": item.issue_id}
    payload = {"issue": {"name": "Updated intake work item"}}
    with django_capture_on_commit_callbacks(execute=True):
        updated = call(IntakeIssueDetailAPIEndpoint, "patch", project, create_user, api_token, path, payload, **kwargs)
    assert updated.status_code == 403
    event_counts = [task.call_count for task in tasks]

    membership.role = ROLE.GUEST.value
    membership.save()
    with django_capture_on_commit_callbacks(execute=True):
        replay = call(IntakeIssueDetailAPIEndpoint, "patch", project, create_user, api_token, path, payload, **kwargs)
    assert replay.status_code == (403 if is_creator else 400)
    assert "Idempotency-Replayed" not in replay
    assert [task.call_count for task in tasks] == event_counts


@pytest.mark.django_db
@pytest.mark.parametrize(
    "is_creator,status",
    [
        (False, IntakeIssueStatus.PENDING),
        (True, IntakeIssueStatus.PENDING),
        (False, IntakeIssueStatus.ACCEPTED),
    ],
)
def test_personal_keys_cannot_delete_agent_first_intake_tasks(
    replay_context, create_user, api_token, django_capture_on_commit_callbacks, is_creator, status
):
    project, membership, other, tasks = replay_context
    item = create_intake_item(project, create_user if is_creator else other, status)
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/intake-issues/{item.issue_id}/"
    kwargs = {"slug": project.workspace.slug, "project_id": project.pk, "issue_id": item.issue_id}
    with django_capture_on_commit_callbacks(execute=True):
        deleted = call(IntakeIssueDetailAPIEndpoint, "delete", project, create_user, api_token, path, **kwargs)
    assert deleted.status_code == 403
    assert IntakeIssue.objects.filter(pk=item.pk).exists()
    event_counts = [task.call_count for task in tasks]

    membership.role = ROLE.MEMBER.value
    membership.save()
    with django_capture_on_commit_callbacks(execute=True):
        replay = call(IntakeIssueDetailAPIEndpoint, "delete", project, create_user, api_token, path, **kwargs)
    assert replay.status_code == 403
    assert "Idempotency-Replayed" not in replay
    assert [task.call_count for task in tasks] == event_counts


@pytest.mark.django_db
@pytest.mark.parametrize("view", [UserAssetEndpoint, UserServerAssetEndpoint])
def test_user_asset_replay_cannot_cross_authenticated_key_ownership(replay_context, create_user, api_token, view):
    project, membership, other, tasks = replay_context
    asset = FileAsset.objects.create(
        user=create_user,
        asset="replay-test/avatar.png",
        storage_metadata={"checked": True},
        entity_type=FileAsset.EntityTypeContext.USER_AVATAR,
    )
    path = f"/api/v1/assets/user-assets/{asset.pk}/"
    assert call(view, "patch", project, create_user, api_token, path, {}, asset_id=asset.pk).status_code == 204
    other_token = APIToken.objects.create(user=other)
    denied = call(view, "patch", project, other, other_token, path, {}, asset_id=asset.pk)
    assert denied.status_code == 404
    assert "Idempotency-Replayed" not in denied
