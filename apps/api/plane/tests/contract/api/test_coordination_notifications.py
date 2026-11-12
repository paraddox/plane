import base64
import hashlib
import hmac
import ipaddress
import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.db import transaction
from django.utils import timezone

from plane.db.models import (
    AgentEventDelivery,
    AgentSubscription,
    CoordinationEvent,
    FileAsset,
    Issue,
    Module,
    ModuleIssue,
)
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import execution as execution
from .test_coordination_handoff import handoff


@pytest.fixture
def callback_network(settings):
    settings.WEBHOOK_ALLOWED_IPS = [ipaddress.ip_network("127.0.0.1/32")]


def subscribe(client, prefix, **overrides):
    response = client.post(
        prefix + "subscriptions/",
        {
            "url": "http://127.0.0.1:18131/callback",
            **overrides,
        },
        format="json",
    )
    assert response.status_code == 201, getattr(response, "data", None)
    return response


@pytest.mark.django_db
def test_main_and_worker_subscriptions_are_independent_and_task_scoped(execution, callback_network):
    dispatcher, _, project, issue, contract, _, _, _, _, prefix = execution
    main = subscribe(dispatcher, prefix)
    worker, run_id, grant = handoff(execution)
    subscription = subscribe(worker, prefix, after=grant.data["event_cursor"])
    assert "signing_secret" in subscription.data
    assert "signing_secret" not in worker.get(prefix + "subscriptions/").data[0]
    assert len(worker.get(prefix + "subscriptions/").data) == 1
    assert worker.get(prefix + f"subscriptions/{main.data['id']}/").status_code == 404
    from plane.coordination.contracts import emit_event

    emit_event(project, "question.answered", actor=contract.owner, issue=issue, revision=1)
    other = Issue.objects.create(project=project, name="Unrelated")
    emit_event(project, "question.answered", actor=contract.owner, issue=other, revision=1)
    deliveries = AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"])
    assert list(deliveries.values_list("event__issue_id", flat=True)) == [issue.pk]
    assert not AgentEventDelivery.objects.filter(
        subscription_id=main.data["id"], event__kind="question.answered"
    ).exists()
    events = worker.get(prefix + f"runs/{run_id}/events/?after={grant.data['event_cursor']}")
    assert events.status_code == 200, events.data
    assert [str(row["issue"]) for row in events.data["events"]] == [str(issue.pk)]


@pytest.mark.django_db
def test_subscription_catches_up_events_between_handoff_and_registration(execution, callback_network):
    worker, run_id, grant = handoff(execution)
    from plane.coordination.contracts import emit_event

    _, _, project, issue, contract, _, _, _, _, prefix = execution
    event = emit_event(project, "comment.created", actor=contract.owner, issue=issue, revision=1)
    created = subscribe(worker, prefix, after=grant.data["event_cursor"])
    assert AgentEventDelivery.objects.get(subscription_id=created.data["id"]).event_id == event.pk


@pytest.mark.django_db
def test_callback_signature_and_replay_keep_stable_delivery_identity(execution, callback_network, monkeypatch):
    from plane.coordination import notifications

    worker, _, grant = handoff(execution)
    _, _, project, issue, contract, _, _, _, _, prefix = execution
    created = subscribe(worker, prefix, after=grant.data["event_cursor"])
    from plane.coordination.contracts import emit_event

    emit_event(project, "question.answered", actor=contract.owner, issue=issue, revision=1)
    delivery = AgentEventDelivery.objects.get(subscription_id=created.data["id"])
    sender = Mock(return_value=503)
    monkeypatch.setattr(notifications, "send_callback", sender)
    notifications.deliver(delivery.pk)
    body, headers = sender.call_args.args[1:]
    secret = base64.b64decode(created.data["signing_secret"].removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = "v1," + base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
    assert hmac.compare_digest(headers["webhook-signature"], expected)
    event = json.loads(body)
    assert event["specversion"] == "1.0" and event["type"] == "question.answered"
    assert "token" not in body.decode() and "signing_secret" not in body.decode()
    delivery.refresh_from_db()
    assert delivery.status == "pending" and delivery.attempts == 1
    replay = worker.post(
        prefix + f"subscriptions/{created.data['id']}/deliveries/{delivery.pk}/replay/", {}, format="json"
    )
    assert replay.status_code == 202, replay.data
    sender.return_value = 204
    notifications.deliver(delivery.pk)
    assert sender.call_args.args[2]["webhook-id"] == str(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.status == "delivered" and delivery.attempts == 2


@pytest.mark.django_db
def test_rolled_back_task_change_never_creates_delivery(execution, callback_network):
    worker, _, _ = handoff(execution)
    _, _, project, issue, contract, _, _, _, _, prefix = execution
    subscription = subscribe(worker, prefix)
    from plane.coordination.contracts import emit_event

    with pytest.raises(RuntimeError), transaction.atomic():
        event = emit_event(project, "comment.created", actor=contract.owner, issue=issue, revision=1)
        raise RuntimeError("Rollback")
    assert not CoordinationEvent.objects.filter(pk=event.pk).exists()
    assert not AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"]).exists()


@pytest.mark.django_db
def test_callback_private_destinations_require_operator_allowlist(execution, settings):
    worker, _, _ = handoff(execution)
    settings.WEBHOOK_ALLOWED_IPS = []
    response = worker.post(execution[-1] + "subscriptions/", {"url": "http://127.0.0.1/callback"}, format="json")
    assert response.status_code == 400, response.data
    assert not AgentSubscription.objects.exists()


@pytest.mark.django_db
def test_human_comments_edits_and_completed_uploads_notify_assigned_worker(execution, callback_network):
    worker, _, grant = handoff(execution)
    _, human, project, issue, _, _, _, _, _, prefix = execution
    subscription = subscribe(worker, prefix, after=grant.data["event_cursor"])
    comments = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/comments/"
    created = human.post(comments, {"comment_html": "<p>Use this additional feedback.</p>"}, format="json")
    assert created.status_code == 201, created.data
    changed = human.patch(
        comments + str(created.data["id"]) + "/", {"comment_html": "<p>Revised feedback.</p>"}, format="json"
    )
    assert changed.status_code == 200, changed.data
    asset = FileAsset.objects.create(
        project=project,
        workspace=project.workspace,
        issue=issue,
        asset="feedback.png",
        entity_type="ISSUE_ATTACHMENT",
    )
    deliveries = AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"])
    assert set(deliveries.values_list("event__kind", flat=True)) == {"comment.created", "comment.updated"}
    asset.is_uploaded = True
    asset.save(update_fields=["is_uploaded"])
    asset.save(update_fields=["is_uploaded"])
    assert deliveries.filter(event__kind="attachment.uploaded").count() == 1
    assert deliveries.get(event__kind="attachment.uploaded").event.payload["asset_id"] == str(asset.pk)


@pytest.mark.django_db
def test_native_task_properties_and_spec_changes_notify_without_reassigning(execution, callback_network):
    worker, _, _ = handoff(execution)
    _, human, project, issue, _, _, _, _, _, prefix = execution
    subscription = subscribe(worker, prefix)
    detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    assert human.patch(detail, {"priority": "urgent"}, format="json").status_code == 200
    assert (
        human.patch(detail, {"requirements": "Updated intent", "expected_revision": 1}, format="json").status_code
        == 200
    )
    events = list(AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"]).select_related("event"))
    assert any(row.event.kind == "task.updated" and "priority" in row.event.payload["changed_fields"] for row in events)
    assert any(row.event.kind == "task.specification_changed" and row.event.spec_revision == 2 for row in events)
    assert not any(row.event.kind == "task.ready" for row in events)


@pytest.mark.django_db
def test_failed_and_interrupted_deliveries_remain_recoverable(execution, callback_network, monkeypatch, settings):
    from plane.coordination import notifications
    from plane.coordination.contracts import emit_event

    worker, _, _ = handoff(execution)
    _, _, project, issue, contract, _, _, _, _, prefix = execution
    subscription = subscribe(worker, prefix)
    emit_event(project, "comment.created", actor=contract.owner, issue=issue, revision=1)
    delivery = AgentEventDelivery.objects.get(subscription_id=subscription.data["id"])
    sender = Mock(return_value=503)
    monkeypatch.setattr(notifications, "send_callback", sender)
    for _ in range(settings.COORDINATION_WEBHOOK_MAX_RETRIES + 1):
        AgentEventDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
        notifications.deliver(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.status == "failed"
    assert delivery.attempts == settings.COORDINATION_WEBHOOK_MAX_RETRIES + 1
    assert (
        worker.post(
            prefix + f"subscriptions/{subscription.data['id']}/deliveries/{delivery.pk}/replay/", {}, format="json"
        ).status_code
        == 202
    )
    AgentEventDelivery.objects.filter(pk=delivery.pk).update(
        status="sending", locked_until=timezone.now() - timedelta(microseconds=1)
    )
    enqueue = Mock()
    monkeypatch.setattr(notifications, "enqueue", enqueue)
    notifications.dispatch_due()
    enqueue.assert_called_once_with(delivery.pk)
    sender.return_value = 204
    notifications.deliver(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.status == "delivered"


@pytest.mark.django_db
def test_parent_revocation_stops_pending_callbacks(execution, callback_network, monkeypatch):
    from plane.coordination import notifications
    from plane.coordination.contracts import emit_event

    worker, _, _ = handoff(execution)
    _, _, project, issue, contract, _, _, _, parent, prefix = execution
    subscription = subscribe(worker, prefix)
    emit_event(project, "comment.created", actor=contract.owner, issue=issue, revision=1)
    delivery = AgentEventDelivery.objects.get(subscription_id=subscription.data["id"])
    parent.is_active = False
    parent.save(update_fields=["is_active"])
    sender = Mock()
    monkeypatch.setattr(notifications, "send_callback", sender)
    notifications.deliver(delivery.pk)
    delivery.refresh_from_db()
    assert delivery.status == "cancelled"
    sender.assert_not_called()


@pytest.mark.django_db
def test_worker_gets_child_feedback_and_only_downloads_granted_uploaded_assets(
    execution, callback_network, monkeypatch
):
    from plane.db.models import IssueComment
    from rest_framework.test import APIClient
    from .test_coordination_handoff import refresh

    worker, run_id, _ = handoff(execution)
    _, _, project, issue, contract, _, _, _, _, prefix = execution
    grant = refresh(execution, worker, run_id)
    assert grant.status_code == 201
    executor = APIClient()
    executor.credentials(HTTP_X_API_KEY=grant.data["token"])
    child = executor.post(
        prefix + f"work-items/{issue.pk}/children/",
        {
            "name": "Focused child",
            "requirements": "Implement this part",
            "definition_of_done": [{"description": "Child works"}],
        },
        format="json",
    )
    assert child.status_code == 201, child.data
    subscription = subscribe(worker, prefix)
    feedback = IssueComment.objects.create(
        project=project, issue_id=child.data["issue"], actor=contract.owner, comment_html="<p>Child feedback</p>"
    )
    asset = FileAsset.objects.create(
        project=project,
        workspace=project.workspace,
        comment=feedback,
        asset="feedback.png",
        is_uploaded=True,
        entity_type="COMMENT_DESCRIPTION",
    )
    pending = FileAsset.objects.create(project=project, workspace=project.workspace, issue=issue, asset="pending.png")
    unrelated = Issue.objects.create(project=project, name="Other task")
    foreign = FileAsset.objects.create(
        project=project, workspace=project.workspace, issue=unrelated, asset="private.png", is_uploaded=True
    )
    from plane.coordination import notification_views

    storage = Mock()
    signed = Mock(return_value="https://files.example.test/feedback?signature=fixture")
    storage.generate_presigned_url = signed
    monkeypatch.setattr(notification_views, "S3Storage", Mock(return_value=storage))
    url = prefix + f"runs/{run_id}/assets/"
    downloaded = worker.get(url + f"{asset.pk}/")
    assert downloaded.status_code == 200
    assert downloaded.data["download_url"] == "https://files.example.test/feedback?signature=fixture"
    assert downloaded["Cache-Control"] == "no-store"
    assert executor.get(url + f"{asset.pk}/").status_code == 200
    assert worker.get(url + f"{pending.pk}/").status_code == 404
    assert worker.get(url + f"{foreign.pk}/").status_code == 404
    deliveries = AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"])
    assert set(deliveries.values_list("event__kind", flat=True)) == {"comment.created", "attachment.uploaded"}
    assert set(deliveries.values_list("event__issue_id", flat=True)) == {feedback.issue_id}


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["task_add", "module_add", "task_remove", "module_remove"])
def test_browser_module_changes_notify_assigned_worker(execution, callback_network, operation):
    worker, _, _ = handoff(execution)
    _, human, project, issue, _, _, _, _, _, prefix = execution
    module = Module.objects.create(project=project, name="Feature scope")
    if operation.endswith("remove"):
        ModuleIssue.objects.create(project=project, issue=issue, module=module)
    subscription = subscribe(worker, prefix)
    base = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/"
    if operation.startswith("task_"):
        data = {"modules" if operation == "task_add" else "removed_modules": [str(module.pk)]}
        response = human.post(base + f"issues/{issue.pk}/modules/", data, format="json")
        assert response.status_code == 201, response.data
    elif operation == "module_add":
        response = human.post(base + f"modules/{module.pk}/issues/", {"issues": [str(issue.pk)]}, format="json")
        assert response.status_code == 201, response.data
    else:
        response = human.delete(base + f"modules/{module.pk}/issues/{issue.pk}/")
        assert response.status_code == 204, getattr(response, "data", None)
    delivery = AgentEventDelivery.objects.filter(
        subscription_id=subscription.data["id"], event__kind="task.modules_changed"
    )
    assert delivery.count() == 1
    event = delivery.get().event
    expected = "added" if operation.endswith("add") else "removed"
    assert event.payload[expected] == [str(module.pk)]


@pytest.mark.django_db
@pytest.mark.parametrize("enabled", [True, False])
def test_merge_authorization_changes_notify_the_assigned_worker(execution, callback_network, enabled):
    worker, _, _ = handoff(execution)
    dispatcher, human, _, issue, _, _, _, _, _, prefix = execution
    main = subscribe(dispatcher, prefix)
    subscription = subscribe(worker, prefix)
    response = human.put(
        prefix.replace("/api/v1/", "/api/") + f"work-items/{issue.pk}/auto-merge/",
        {"enabled": enabled, "expected_revision": 1},
        format="json",
    )
    assert response.status_code == 200, response.data
    delivery = AgentEventDelivery.objects.get(
        subscription_id=subscription.data["id"], event__kind="task.auto_merge_authorization_changed"
    )
    assert delivery.event.payload == {"enabled": enabled}
    assert not AgentEventDelivery.objects.filter(subscription_id=main.data["id"]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("entity_type", ["COMMENT_DESCRIPTION", "ISSUE_DESCRIPTION"])
@pytest.mark.parametrize("uploaded", [True, False])
def test_binding_editor_upload_notifies_worker_once_it_is_ready(execution, callback_network, entity_type, uploaded):
    from plane.db.models import IssueComment

    worker, _, _ = handoff(execution)
    _, human, project, issue, contract, _, _, _, _, prefix = execution
    target = issue
    if entity_type == "COMMENT_DESCRIPTION":
        target = IssueComment.objects.create(
            project=project, issue=issue, actor=contract.owner, comment_html="<p>Screenshot feedback</p>"
        )
    asset = FileAsset.objects.create(
        project=project, workspace=project.workspace, asset="editor.png", entity_type=entity_type, is_uploaded=uploaded
    )
    subscription = subscribe(worker, prefix)
    url = f"/api/assets/v2/workspaces/{project.workspace.slug}/projects/{project.pk}/{target.pk}/bulk/"
    for _ in range(2):
        response = human.post(url, {"asset_ids": [str(asset.pk)]}, format="json")
        assert response.status_code == 204, getattr(response, "data", None)
    asset.refresh_from_db()
    assert (asset.comment_id if entity_type == "COMMENT_DESCRIPTION" else asset.issue_id) == target.pk
    if not uploaded:
        assert not AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"]).exists()
        asset.is_uploaded = True
        asset.save(update_fields=["is_uploaded"])
    delivery = AgentEventDelivery.objects.get(
        subscription_id=subscription.data["id"], event__kind="attachment.uploaded"
    )
    assert delivery.event.payload["asset_id"] == str(asset.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("transport", ["browser", "api"])
def test_deleting_a_module_notifies_its_assigned_worker(execution, callback_network, api_key_client, transport):
    worker, _, _ = handoff(execution)
    _, human, project, issue, _, _, _, _, _, prefix = execution
    module = Module.objects.create(project=project, name="Removed feature")
    ModuleIssue.objects.create(project=project, issue=issue, module=module)
    subscription = subscribe(worker, prefix)
    client = human if transport == "browser" else api_key_client
    base = "/api/" if transport == "browser" else "/api/v1/"
    response = client.delete(base + f"workspaces/{project.workspace.slug}/projects/{project.pk}/modules/{module.pk}/")
    assert response.status_code == 204, getattr(response, "data", None)
    delivery = AgentEventDelivery.objects.get(
        subscription_id=subscription.data["id"], event__kind="task.modules_changed"
    )
    assert delivery.event.payload == {"added": [], "removed": [str(module.pk)]}


@pytest.mark.django_db
def test_restoring_a_confirmed_attachment_notifies_worker_that_it_is_available_again(execution, callback_network):
    worker, _, _ = handoff(execution)
    _, human, project, issue, _, _, _, _, _, prefix = execution
    subscription = subscribe(worker, prefix)
    asset = FileAsset.objects.create(
        project=project,
        workspace=project.workspace,
        issue=issue,
        asset="feedback.png",
        is_uploaded=True,
        entity_type="ISSUE_ATTACHMENT",
    )
    workspace = f"/api/assets/v2/workspaces/{project.workspace.slug}/"
    assert (
        human.delete(workspace + f"projects/{project.pk}/issues/{issue.pk}/attachments/{asset.pk}/").status_code == 204
    )
    assert human.post(workspace + f"restore/{asset.pk}/", {}, format="json").status_code == 204
    events = AgentEventDelivery.objects.filter(subscription_id=subscription.data["id"]).values_list(
        "event__kind", flat=True
    )
    assert list(events) == ["attachment.uploaded", "attachment.deleted", "attachment.uploaded"]


@pytest.mark.django_db
@pytest.mark.parametrize("entity_type", ["COMMENT_DESCRIPTION", "ISSUE_DESCRIPTION"])
@pytest.mark.parametrize("scope", ["project", "workspace"])
def test_editor_upload_accepts_an_empty_entity_identifier_before_posting(execution, monkeypatch, entity_type, scope):
    from plane.app.views.asset import v2

    _, human, project, _, _, _, _, _, _, _ = execution
    storage = Mock()
    storage.generate_presigned_post.return_value = {"url": "http://storage.test/upload", "fields": {}}
    monkeypatch.setattr(v2, "S3Storage", Mock(return_value=storage))
    url = f"/api/assets/v2/workspaces/{project.workspace.slug}/"
    if scope == "project":
        url += f"projects/{project.pk}/"
    response = human.post(
        url,
        {
            "name": "composed-feedback.png",
            "type": "image/png",
            "size": 100,
            "entity_type": entity_type,
            "entity_identifier": "",
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    asset = FileAsset.objects.get(pk=response.data["asset_id"])
    assert asset.comment_id is None and asset.issue_id is None


@pytest.mark.django_db
def test_task_asset_binding_changes_preserve_new_project_cover_uploads(execution):
    _, human, project, _, _, _, _, _, _, _ = execution
    asset = FileAsset.objects.create(
        workspace=project.workspace, asset="new-cover.png", entity_type="PROJECT_COVER", is_uploaded=True
    )
    url = f"/api/assets/v2/workspaces/{project.workspace.slug}/projects/{project.pk}/{project.pk}/bulk/"
    response = human.post(url, {"asset_ids": [str(asset.pk)]}, format="json")
    assert response.status_code == 204, getattr(response, "data", None)
    project.refresh_from_db()
    assert project.cover_image_asset_id == asset.pk
