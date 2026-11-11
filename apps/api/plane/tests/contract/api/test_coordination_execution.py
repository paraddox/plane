# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.db import close_old_connections, connection, connections, OperationalError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import (
    AgentProjectAccess,
    AgentRun,
    APIToken,
    FileAsset,
    Issue,
    Module,
    ProjectExecutionPolicy,
    ProjectMember,
    State,
    User,
    WorkItemContract,
    WorkspaceMember,
)
from .test_coordination_contracts import coordination as coordination


@pytest.fixture
def execution(coordination, create_user):
    human, project, issue, prefix = coordination
    State.objects.create(project=project, name="Todo", group="unstarted", default=True, color="#777777")
    policy = ProjectExecutionPolicy.objects.create(
        project=project,
        enabled=True,
        repositories=[{"repository": "example/product", "base_branch": "main", "required_checks": []}],
        credential_lifetime=timedelta(minutes=5),
        execution_lease=timedelta(minutes=10),
    )
    owner = WorkItemContract.objects.create(
        project=project,
        issue=issue,
        owner=create_user,
        status="ready",
        requirements="Implement it",
        definition_of_done=[{"id": str(uuid4()), "description": "Tests pass"}],
    )
    agents = []
    for name, role in (("dispatcher", "dispatcher"), ("worker", "worker")):
        user = User.objects.create(email=f"{name}@execution.test", username=name, is_bot=True)
        user.set_unusable_password()
        user.save()
        WorkspaceMember.objects.create(workspace=project.workspace, member=user, role=15)
        ProjectMember.objects.create(project=project, member=user, role=15)
        AgentProjectAccess.objects.create(project=project, agent=user, role=role)
        agents.append(user)
    dispatcher, worker = agents
    token = APIToken.objects.create(
        user=dispatcher,
        project=project,
        workspace=project.workspace,
        token="coordination-dispatcher-secret",
        purpose="dispatcher",
    )
    client = APIClient()
    client.credentials(HTTP_X_API_KEY="coordination-dispatcher-secret")
    machine = prefix.replace("/api/", "/api/v1/", 1)
    return client, human, project, issue, owner, policy, dispatcher, worker, token, machine


def claim(execution):
    client, _, _, issue, _, _, _, worker, _, prefix = execution
    response = client.post(
        prefix + f"work-items/{issue.pk}/claim/",
        {"agent": str(worker.pk), "worker_id": "worker-instance", "repository": "example/product"},
        format="json",
    )
    assert response.status_code == 201, response.data
    return response.data["id"]


def worker_client(execution, run_id):
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    response = dispatcher.post(prefix + f"runs/{run_id}/credentials/", {}, format="json")
    assert response.status_code == 201, response.data
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=response.data["token"])
    return client, response


@pytest.fixture
def execution_child(execution):
    _, _, _, issue, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    response = agent.post(
        prefix + f"work-items/{issue.pk}/children/",
        {
            "name": "Agent child",
            "requirements": "Focused requirement",
            "definition_of_done": [{"description": "Child test passes"}],
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    return agent, run_id, Issue.objects.get(pk=response.data["issue"])


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["root", "child"])
def test_execution_patch_cannot_soft_delete_work_items(execution, execution_child, target):
    _, _, project, root, _, _, _, _, _, prefix = execution
    agent, run_id, child = execution_child
    issue = root if target == "root" else child
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/"
    response = agent.patch(url, {"deleted_at": timezone.now().isoformat()}, format="json")
    assert response.status_code == 403, response.data
    assert Issue.all_objects.get(pk=issue.pk).deleted_at is None
    assert agent.get(prefix + f"runs/{run_id}/").status_code == 200
    assert agent.patch(url, {"name": "Permitted update"}, format="json").status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["patch", "delete", "bulk", "upsert", "modules"])
def test_personal_keys_cannot_mutate_agent_children(execution, execution_child, api_key_client, action):
    _, _, project, _, _, _, _, _, _, _ = execution
    agent, _, child = execution_child
    Issue.objects.filter(pk=child.pk).update(external_id="child-external", external_source="integration")
    done = State.objects.create(project=project, name="Done", group="completed", color="#777777")
    base = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/"
    detail = base + f"work-items/{child.pk}/"
    assert api_key_client.get(detail).status_code == 200
    if action == "patch":
        response = api_key_client.patch(detail, {"state": str(done.pk)}, format="json")
    elif action == "delete":
        response = api_key_client.delete(detail)
    elif action == "bulk":
        response = api_key_client.post(
            base + "work-items/bulk/",
            {"updates": [{"id": str(child.pk), "data": {"state": str(done.pk)}}]},
            format="json",
        )
    elif action == "upsert":
        response = api_key_client.put(
            base + "work-items/upsert/",
            {"external_id": "child-external", "external_source": "integration", "state": str(done.pk)},
            format="json",
        )
    else:
        module = Module.objects.create(project=project, name="Module")
        response = api_key_client.post(
            base + f"modules/{module.pk}/module-issues/", {"issues": [str(child.pk)]}, format="json"
        )
    assert response.status_code == 403, getattr(response, "data", None)
    saved = Issue.all_objects.get(pk=child.pk)
    assert saved.deleted_at is None and saved.state_id != done.pk
    assert agent.patch(detail, {"state": str(done.pk)}, format="json").status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["patch", "delete", "bulk"])
@pytest.mark.parametrize("idempotent", [False, True])
def test_personal_mutations_recheck_contracts_added_after_authentication(
    execution, api_key_client, monkeypatch, action, idempotent
):
    from plane.api.middleware.api_authentication import APIKeyAuthentication

    _, _, project, _, owner_contract, _, _, _, _, _ = execution
    first = Issue.objects.create(project=project, name="First ordinary")
    protected = Issue.objects.create(project=project, name="Newly coordinated")
    record_usage = APIKeyAuthentication.record_usage

    def add_contract(self, token):
        record_usage(self, token)
        WorkItemContract.objects.create(project=project, issue=protected, owner=owner_contract.owner)

    monkeypatch.setattr(APIKeyAuthentication, "record_usage", add_contract)
    base = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/"
    headers = {"HTTP_IDEMPOTENCY_KEY": "coordination-race"} if idempotent else {}
    if action == "bulk":
        response = api_key_client.post(
            base + "bulk/",
            {
                "updates": [
                    {"id": str(first.pk), "data": {"name": "Changed first"}},
                    {"id": str(protected.pk), "data": {"name": "Unauthorized"}},
                ]
            },
            format="json",
            **headers,
        )
    elif action == "patch":
        response = api_key_client.patch(base + f"{protected.pk}/", {"name": "Unauthorized"}, format="json", **headers)
    else:
        response = api_key_client.delete(base + f"{protected.pk}/", **headers)
    assert response.status_code == 403, response.data
    saved = Issue.all_objects.get(pk=protected.pk)
    assert saved.name == "Newly coordinated" and saved.deleted_at is None
    assert Issue.objects.get(pk=first.pk).name == "First ordinary"


@pytest.mark.django_db
def test_claim_is_exclusive_and_credentials_are_separate_hashed_grants(execution):
    client, _, project, issue, contract, _, _, worker, parent, prefix = execution
    run_id = claim(execution)
    assert (
        client.post(
            prefix + f"work-items/{issue.pk}/claim/", {"agent": str(worker.pk), "worker_id": "second"}, format="json"
        ).status_code
        == 409
    )
    child, created = worker_client(execution, run_id)
    token = APIToken.objects.get(pk=created.data["id"])
    assert token.token != created.data["token"] and token.token.startswith("sha256$")
    assert token.delegated_by_id == parent.pk
    assert str(token.agent_run_id) == str(run_id)
    assert child.get(prefix + f"work-items/{issue.pk}/contract/").status_code == 200
    detail = client.get(prefix + f"runs/{run_id}/")
    assert detail.status_code == 200
    assert "token" not in detail.data


@pytest.mark.django_db
def test_worker_cannot_touch_unrelated_task_or_delegate_credentials(execution):
    _, _, project, issue, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    client, _ = worker_client(execution, run_id)
    other = Issue.objects.create(project=project, name="Unrelated")
    base = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/"
    assert client.get(base + f"{issue.pk}/").status_code == 200
    assert client.patch(base + f"{issue.pk}/", {"name": "Scoped update"}, format="json").status_code == 200
    assert client.get(base + f"{other.pk}/").status_code == 403
    assert client.patch(base + f"{other.pk}/", {"name": "Forbidden"}, format="json").status_code == 403
    assert client.post(prefix + f"runs/{run_id}/credentials/", {}, format="json").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["root", "child"])
@pytest.mark.parametrize("foreign_asset", ["other_issue", "project_asset"])
def test_attachment_downloads_are_bound_to_the_granted_issue(execution, monkeypatch, target, foreign_asset):
    _, _, project, issue, contract, _, _, _, _, prefix = execution
    run_id = claim(execution)
    client, _ = worker_client(execution, run_id)
    if target == "child":
        child = client.post(
            prefix + f"work-items/{issue.pk}/children/",
            {
                "name": "Granted child",
                "requirements": "Implement the focused scope",
                "definition_of_done": [{"description": "Child tests pass"}],
            },
            format="json",
        )
        assert child.status_code == 201, child.data
        issue = Issue.objects.get(pk=child.data["issue"])
    other = Issue.objects.create(project=project, name="Unrelated attachment owner")
    assets = []
    for related_issue, name in (
        (issue, "allowed.txt"),
        (other if foreign_asset == "other_issue" else None, "private.txt"),
    ):
        asset = FileAsset(
            project=project,
            workspace=project.workspace,
            issue=related_issue,
            asset=f"synthetic/{name}",
            attributes={"name": name},
            is_uploaded=True,
            created_by=contract.owner,
        )
        asset.save(disable_auto_set_user=True)
        assets.append(asset)
    allowed, forbidden = assets
    storage = Mock()
    storage.generate_presigned_url.return_value = "https://storage.example/allowed.txt"
    storage_factory = Mock(return_value=storage)
    monkeypatch.setattr("plane.api.views.issue.S3Storage", storage_factory)
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/attachments/"
    denied = client.get(url + f"{forbidden.pk}/")
    assert denied.status_code == 403, dict(denied.items())
    assert "Location" not in denied
    storage_factory.assert_not_called()
    downloaded = client.get(url + f"{allowed.pk}/")
    assert downloaded.status_code == 302
    assert downloaded["Location"] == "https://storage.example/allowed.txt"
    storage.generate_presigned_url.assert_called_once_with(
        object_name=allowed.asset.name, disposition="attachment", filename="allowed.txt"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("revocation", ["parent", "cancel", "expired", "spec", "policy", "identity"])
def test_execution_grants_fail_closed_after_revocation(execution, revocation):
    dispatcher, human, project, issue, contract, policy, _, worker, parent, prefix = execution
    run_id = claim(execution)
    client, _ = worker_client(execution, run_id)
    if revocation == "parent":
        APIToken.objects.filter(pk=parent.pk).update(is_active=False)
    elif revocation == "cancel":
        AgentRun.objects.filter(pk=run_id).update(status="cancelled")
    elif revocation == "expired":
        AgentRun.objects.filter(pk=run_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    elif revocation == "spec":
        WorkItemContract.objects.filter(pk=contract.pk).update(spec_revision=2)
    elif revocation == "policy":
        ProjectExecutionPolicy.objects.filter(pk=policy.pk).update(enabled=False)
    else:
        AgentProjectAccess.objects.filter(project=project, agent=worker).update(is_active=False)
    assert client.get(prefix + f"work-items/{issue.pk}/contract/").status_code == 403


@pytest.mark.django_db
def test_personal_key_cannot_mutate_coordinated_task_or_claim_a_run(execution, api_key_client):
    _, _, project, issue, _, _, _, worker, _, prefix = execution
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/"
    assert api_key_client.patch(url, {"name": "Unscoped"}, format="json").status_code == 403
    assert (
        api_key_client.post(
            prefix + f"work-items/{issue.pk}/claim/", {"agent": str(worker.pk), "worker_id": "x"}, format="json"
        ).status_code
        == 403
    )


@pytest.mark.django_db
@pytest.mark.parametrize("idempotent", [False, True])
def test_personal_upsert_cannot_update_a_coordinated_task(execution, api_key_client, idempotent):
    _, _, project, issue, _, _, _, _, _, _ = execution
    Issue.objects.filter(pk=issue.pk).update(external_id="product-task", external_source="integration")
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/upsert/"
    headers = {"HTTP_IDEMPOTENCY_KEY": "coordinated-upsert"} if idempotent else {}
    response = api_key_client.put(
        url,
        {"external_id": "product-task", "external_source": "integration", "name": "Unauthorized update"},
        format="json",
        **headers,
    )
    assert response.status_code == 403, response.data
    issue.refresh_from_db()
    assert issue.name == "Product task"


@pytest.mark.django_db
def test_personal_upsert_replay_is_denied_after_a_contract_is_added(execution, api_key_client):
    _, _, project, _, contract, _, _, _, _, _ = execution
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/upsert/"
    payload = {"external_id": "ordinary-task", "external_source": "integration", "name": "Imported"}
    created = api_key_client.put(url, payload, format="json")
    assert created.status_code == 201, created.data
    payload["name"] = "Synced"
    headers = {"HTTP_IDEMPOTENCY_KEY": "ordinary-upsert"}
    updated = api_key_client.put(url, payload, format="json", **headers)
    assert updated.status_code == 200, updated.data
    WorkItemContract.objects.create(project=project, issue_id=created.data["id"], owner=contract.owner)
    replay = api_key_client.put(url, payload, format="json", **headers)
    assert replay.status_code == 403, replay.data
    assert Issue.objects.get(pk=created.data["id"]).name == "Synced"


@pytest.mark.django_db
def test_personal_upsert_rechecks_a_contract_added_after_authentication(execution, api_key_client, monkeypatch):
    from plane.api.middleware.api_authentication import APIKeyAuthentication

    _, _, project, _, contract, _, _, _, _, _ = execution
    issue = Issue.objects.create(
        project=project, name="Original", external_id="new-contract", external_source="integration"
    )
    record_usage = APIKeyAuthentication.record_usage

    def add_contract(self, token):
        record_usage(self, token)
        WorkItemContract.objects.create(project=project, issue=issue, owner=contract.owner)

    monkeypatch.setattr(APIKeyAuthentication, "record_usage", add_contract)
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/upsert/"
    response = api_key_client.put(
        url,
        {"external_id": issue.external_id, "external_source": issue.external_source, "name": "Unauthorized"},
        format="json",
    )
    assert response.status_code == 403, response.data
    issue.refresh_from_db()
    assert issue.name == "Original"


@pytest.mark.django_db
def test_credential_issuance_does_not_cache_plaintext_secrets(execution):
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    response = dispatcher.post(prefix + f"runs/{run_id}/credentials/", {}, format="json", HTTP_IDEMPOTENCY_KEY="secret")
    assert response.status_code == 400
    assert not APIToken.objects.filter(agent_run_id=run_id).exists()


@pytest.mark.django_db
def test_new_credentials_share_the_dispatchers_rate_budget(execution):
    dispatcher, _, _, _, _, _, _, _, parent, prefix = execution
    run_id = claim(execution)
    first, _ = worker_client(execution, run_id)
    APIToken.objects.filter(pk=parent.pk).update(allowed_rate_limit="2/minute")
    from plane.settings.redis import redis_instance

    redis_instance().delete(f"api_key:{parent.pk}")
    assert first.get(prefix + f"runs/{run_id}/").status_code == 200
    second, _ = worker_client(execution, run_id)
    assert second.get(prefix + f"runs/{run_id}/").status_code == 429


@pytest.mark.django_db(transaction=True)
def test_concurrent_dispatchers_cannot_claim_the_same_task_twice(execution):
    _, _, _, issue, _, _, _, worker, _, prefix = execution
    assert connection.vendor == "postgresql"
    start = Barrier(2)

    def attempt(number):
        close_old_connections()
        try:
            client = APIClient()
            client.credentials(HTTP_X_API_KEY="coordination-dispatcher-secret")
            start.wait()
            return client.post(
                prefix + f"work-items/{issue.pk}/claim/",
                {"agent": str(worker.pk), "worker_id": f"worker-{number}"},
                format="json",
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [1, 2])) == [201, 409]
    assert AgentRun.objects.filter(contract__issue=issue, status="active").count() == 1


@pytest.mark.django_db
def test_operator_registers_workers_without_long_lived_credentials(coordination, create_user):
    from io import StringIO
    import json
    from django.core.management import call_command

    _, project, _, _ = coordination
    output = StringIO()
    call_command(
        "register_coordination_agent",
        str(project.pk),
        "--email",
        "worker@registration.test",
        "--name",
        "Implementation",
        "--role",
        "worker",
        "--owner",
        str(create_user.pk),
        stdout=output,
    )
    registered = json.loads(output.getvalue())
    assert "token" not in registered
    agent = User.objects.get(pk=registered["agent_id"])
    assert agent.is_bot and not agent.has_usable_password()
    assert not APIToken.objects.filter(user=agent).exists()
    assert AgentProjectAccess.objects.get(agent=agent, project=project).created_by_id == create_user.pk


@pytest.mark.django_db
def test_registered_agent_cannot_use_human_session_endpoints(execution):
    _, _, _, _, _, _, _, worker, _, _ = execution
    client = APIClient()
    client.force_login(worker)
    assert client.get("/api/users/me/").status_code == 403


@pytest.mark.django_db
def test_spec_edit_closes_old_run_and_can_be_submitted_and_claimed_again(execution):
    _, human, _, issue, _, _, _, _, _, prefix = execution
    first = claim(execution)
    old, _ = worker_client(execution, first)
    session_prefix = prefix.replace("/api/v1/", "/api/", 1)
    changed = human.patch(
        session_prefix + f"work-items/{issue.pk}/contract/",
        {"expected_revision": 1, "requirements": "Revised requirement"},
        format="json",
    )
    assert changed.status_code == 200, changed.data
    assert old.get(prefix + f"runs/{first}/").status_code == 403
    assert human.post(session_prefix + f"work-items/{issue.pk}/submit/", {}, format="json").status_code == 200
    second = claim(execution)
    assert first != second
    assert AgentRun.objects.get(pk=first).status == "cancelled"


@pytest.mark.django_db
def test_mutation_rechecks_revocation_after_initial_authentication(execution, monkeypatch):
    from plane.api.middleware.api_authentication import APIKeyAuthentication

    _, _, project, issue, _, _, _, _, _, _ = execution
    run_id = claim(execution)
    agent, credential = worker_client(execution, run_id)

    def revoke_after_auth(self, token):
        APIToken.objects.filter(pk=token.pk).update(is_active=False)

    monkeypatch.setattr(APIKeyAuthentication, "record_usage", revoke_after_auth)
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/"
    assert agent.patch(url, {"name": "Stale worker write"}, format="json").status_code == 403
    issue.refresh_from_db()
    assert issue.name == "Product task"


@pytest.mark.django_db
def test_audit_record_preserves_run_and_delegation_without_secrets(execution, monkeypatch):
    from plane.middleware import logger

    _, _, _, _, _, _, _, _, parent, prefix = execution
    run_id = claim(execution)
    agent, credential = worker_client(execution, run_id)
    records = []
    monkeypatch.setattr(logger.process_logs, "delay", lambda **kwargs: records.append(kwargs["log_data"]))
    assert agent.get(prefix + f"runs/{run_id}/").status_code == 200
    assert records[-1]["agent_run_id"] == str(run_id)
    assert records[-1]["delegated_by_id"] == str(parent.pk)
    assert records[-1]["worker_id"] == "worker-instance"
    assert credential.data["token"] not in str(records)


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["in_progress", "awaiting_input", "review"])
def test_expired_runs_are_discoverable_and_reclaimable_in_every_working_status(execution, status):
    dispatcher, _, _, issue, contract, _, _, _, _, prefix = execution
    first = claim(execution)
    old, _ = worker_client(execution, first)
    WorkItemContract.objects.filter(pk=contract.pk).update(status=status)
    AgentRun.objects.filter(pk=first).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    candidates = dispatcher.get(prefix + "candidates/")
    assert candidates.status_code == 200, candidates.data
    assert str(contract.pk) in [str(row["id"]) for row in candidates.data["results"]]
    second = claim(execution)
    assert AgentRun.objects.get(pk=second).attempt == 2
    assert old.get(prefix + f"runs/{first}/").status_code == 403


@pytest.mark.django_db
def test_cancelled_work_can_be_explicitly_resubmitted_without_changing_the_spec(execution):
    _, human, _, issue, contract, _, _, _, _, prefix = execution
    first = claim(execution)
    session = prefix.replace("/api/v1/", "/api/", 1)
    assert (
        human.put(
            session + f"work-items/{issue.pk}/auto-merge/", {"expected_revision": 1, "enabled": True}, format="json"
        ).status_code
        == 200
    )
    assert human.post(session + f"runs/{first}/cancel/", {}, format="json").status_code == 200
    contract.refresh_from_db()
    assert contract.status == "cancelled" and contract.auto_merge_authorized is False
    assert human.post(session + f"work-items/{issue.pk}/submit/", {}, format="json").status_code == 200
    second = claim(execution)
    assert AgentRun.objects.get(pk=second).attempt == 2
    assert human.post(session + f"runs/{first}/cancel/", {}, format="json").status_code == 200
    contract.refresh_from_db()
    assert contract.status == "in_progress"
    assert AgentRun.objects.get(pk=second).status == "active"


@pytest.mark.django_db
def test_authentication_and_revocation_never_use_replica_state(execution):
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory
    from rest_framework.exceptions import AuthenticationFailed
    from plane.api.middleware.api_authentication import APIKeyAuthentication
    from plane.api.views.issue import IssueDetailAPIEndpoint
    from plane.utils.core import set_use_read_replica, should_use_read_replica, clear_read_replica_context

    _, _, project, issue, _, _, _, _, _, _ = execution
    run_id = claim(execution)
    _, issued = worker_client(execution, run_id)
    request = Request(
        APIRequestFactory().get("/", HTTP_X_API_KEY=issued.data["token"]),
        parser_context={
            "view": IssueDetailAPIEndpoint(),
            "kwargs": {"slug": project.workspace.slug, "project_id": project.pk, "pk": issue.pk},
        },
    )

    def assert_primary(execute, sql, params, many, context):
        assert should_use_read_replica() is False
        return execute(sql, params, many, context)

    set_use_read_replica(True)
    try:
        with connection.execute_wrapper(assert_primary):
            assert APIKeyAuthentication().authenticate(request)[0].is_bot
        assert should_use_read_replica() is True
        APIToken.objects.filter(pk=issued.data["id"]).update(is_active=False)
        with connection.execute_wrapper(assert_primary), pytest.raises(AuthenticationFailed):
            APIKeyAuthentication().authenticate(request)
        assert should_use_read_replica() is True
    finally:
        clear_read_replica_context()


@pytest.mark.django_db(transaction=True)
def test_dispatcher_read_locks_allow_parallel_workers_but_serialize_revocation(execution):
    from plane.coordination.execution import shared_dispatcher_key

    *_, parent, _ = execution
    ready, finish = Event(), Event()

    def hold_worker_lock():
        close_old_connections()
        try:
            with transaction.atomic():
                assert shared_dispatcher_key(parent.pk).pk == parent.pk
                ready.set()
                finish.wait()
        finally:
            ready.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(hold_worker_lock)
        ready.wait()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT id FROM api_tokens WHERE id = %s FOR SHARE NOWAIT", [parent.pk])
                assert cursor.fetchone()[0] == parent.pk
            with pytest.raises(OperationalError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT id FROM api_tokens WHERE id = %s FOR UPDATE NOWAIT", [parent.pk])
        finally:
            finish.set()
        pending.result()


@pytest.mark.django_db
def test_claim_replays_recheck_project_and_issue_availability(execution):
    from plane.db.models import Project

    dispatcher, _, project, issue, _, _, _, worker, _, prefix = execution
    url = prefix + f"work-items/{issue.pk}/claim/"
    payload = {"agent": str(worker.pk), "worker_id": "replay-worker"}
    assert dispatcher.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="claim").status_code == 201
    Project.objects.filter(pk=project.pk).update(archived_at=timezone.now())
    assert dispatcher.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="claim").status_code == 404
    Project.objects.filter(pk=project.pk).update(archived_at=None)
    Issue.objects.filter(pk=issue.pk).update(deleted_at=timezone.now())
    assert dispatcher.post(url, payload, format="json", HTTP_IDEMPOTENCY_KEY="claim").status_code == 404
