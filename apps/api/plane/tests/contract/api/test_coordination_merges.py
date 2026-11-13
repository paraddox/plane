# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

import pytest
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import (
    AgentRun,
    APIToken,
    CoordinationEvent,
    Issue,
    MergeAttempt,
    ProjectMember,
    RunChild,
    State,
    User,
    WorkItemContract,
    WorkItemQuestion,
    WorkspaceMember,
)
from plane.coordination.github import MergeRejected, ProviderUnavailable
from .test_coordination_execution import execution as execution, claim, worker_client
from .test_coordination_contracts import coordination as coordination

MERGE_INPUT = {"expected_revision": 1, "code_revision": 1, "head_sha": "a" * 40}


class FakeGitHub:
    def __init__(self):
        self.head_sha = "a" * 40
        self.base_branch = "main"
        self.repository = "example/product"
        self.merged = False
        self.merge_calls = []
        self.failure = None
        self.merged_branch = None
        self.merged_head = None
        self.actual_merges = 0

    def pull_request(self, number):
        return {
            "head_sha": self.head_sha,
            "base_branch": self.base_branch,
            "repository": self.repository,
            "merged": self.merged,
            "merge_sha": "f" * 40 if self.merged else "",
            "state": "open",
            "draft": False,
            "mergeable": True,
        }

    def target_status(self, base, sha):
        merged = self.merged and sha == (self.merged_head or self.head_sha) and base == (self.merged_branch or "main")
        return {
            "head_sha": sha,
            "base_branch": base,
            "repository": self.repository,
            "merged": merged,
            "merge_sha": "f" * 40 if merged else "",
        }

    def merge(self, base, sha):
        self.merge_calls.append((base, sha))
        if self.failure:
            raise self.failure
        assert sha == self.head_sha
        if not self.merged:
            self.actual_merges += 1
        self.merged = True
        self.merged_branch, self.merged_head = base, sha


@pytest.fixture
def merge_flow(execution, monkeypatch):
    _, human, project, issue, contract, policy, _, _, _, prefix = execution
    done = State.objects.create(project=project, name="Done", group="completed", color="#00aa00")
    run_id = claim(execution)
    agent, credential = worker_client(execution, run_id)
    provider = FakeGitHub()
    monkeypatch.setattr("plane.coordination.merges.GitHubClient", lambda *args: provider)
    head = agent.post(
        prefix + f"runs/{run_id}/head/", {"head_sha": provider.head_sha, "pull_request": 7}, format="json"
    )
    assert head.status_code == 200, head.data
    evidence = agent.post(
        prefix + f"runs/{run_id}/evidence/",
        {
            "head_sha": provider.head_sha,
            "kind": "test",
            "outcome": "passed",
            "summary": "Acceptance passed",
            "criterion_ids": [contract.definition_of_done[0]["id"]],
        },
        format="json",
    )
    assert evidence.status_code == 201, evidence.data
    session = prefix.replace("/api/v1/", "/api/", 1)
    return execution, agent, run_id, provider, done, session, credential


def advance(flow):
    execution, _, _, _, _, session, _ = flow
    _, human, _, issue, _, _, _, _, _, _ = execution
    response = human.put(
        session + f"work-items/{issue.pk}/auto-merge/", {"enabled": True, "expected_revision": 1}, format="json"
    )
    assert response.status_code == 200, response.data


@pytest.mark.django_db(transaction=True)
def test_merge_does_not_fetch_project_ci_checks(merge_flow, monkeypatch):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    advance(merge_flow)

    def read_checks(sha):
        pytest.fail("Repository CI is not configured or polled by Plane project settings.")

    monkeypatch.setattr(provider, "checks", read_checks, raising=False)
    response = agent.post(execution[-1] + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 200, response.data


def decision(flow, client=None, outcome="approve"):
    execution, _, run_id, provider, _, session, _ = flow
    _, human, _, issue, _, _, _, _, _, _ = execution
    run = AgentRun.objects.get(pk=run_id)
    return (client or human).post(
        session + f"work-items/{issue.pk}/decisions/",
        {
            "run": str(run_id),
            "expected_revision": 1,
            "head_sha": provider.head_sha,
            "code_revision": run.code_revision,
            "decision": outcome,
        },
        format="json",
    )


def reviewer(flow):
    execution, _, _, _, _, session, _ = flow
    _, human, project, issue, _, _, _, _, _, _ = execution
    user = User.objects.create(email="reviewer@coordination.test", username="reviewer")
    WorkspaceMember.objects.create(workspace=project.workspace, member=user, role=15)
    ProjectMember.objects.create(project=project, member=user, role=15)
    response = human.put(
        session + f"work-items/{issue.pk}/reviewers/",
        {"expected_revision": 1, "reviewers": [str(user.pk)]},
        format="json",
    )
    assert response.status_code == 200, response.data
    client = APIClient()
    client.force_login(user)
    return client


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("authorization", ["advance", "owner", "reviewer", "owner_then_reviewer"])
def test_assigned_agent_merges_and_credentials_end_only_after_confirmation(merge_flow, authorization):
    execution, agent, run_id, provider, done, _, credential = merge_flow
    _, _, _, issue, contract, _, _, _, _, prefix = execution
    if authorization == "advance":
        advance(merge_flow)
    elif authorization == "owner":
        assert decision(merge_flow).status_code == 201
    else:
        client = reviewer(merge_flow)
        if authorization == "owner_then_reviewer":
            assert decision(merge_flow).status_code == 201
            assert agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json").status_code == 409
        assert decision(merge_flow, client).status_code == 201
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 200, response.data
    assert response.data["status"] == "merged"
    assert provider.merge_calls == [("main", "a" * 40)]
    issue.refresh_from_db()
    contract.refresh_from_db()
    assert issue.state_id == done.pk and issue.completed_at is not None
    assert contract.status == "merged" and contract.merged_sha == "f" * 40
    assert not APIToken.objects.filter(agent_run_id=run_id, is_active=True).exists()
    assert agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json").status_code == 403
    assert CoordinationEvent.objects.filter(kind="task.merged", issue=issue).count() == 1


@pytest.mark.django_db(transaction=True)
def test_advance_permission_does_not_waive_requested_reviews_or_owner_veto(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    advance(merge_flow)
    client = reviewer(merge_flow)
    url = prefix + f"runs/{run_id}/merge/"
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 409
    assert decision(merge_flow, client).status_code == 201
    assert decision(merge_flow, outcome="changes_requested").status_code == 201
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 409
    assert not provider.merge_calls


@pytest.mark.django_db(transaction=True)
def test_code_changes_require_fresh_approval_and_fresh_evidence(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    assert decision(merge_flow).status_code == 201
    provider.head_sha = "b" * 40
    assert (
        agent.post(
            prefix + f"runs/{run_id}/head/", {"head_sha": provider.head_sha, "pull_request": 7}, format="json"
        ).status_code
        == 200
    )
    assert (
        agent.post(
            prefix + f"runs/{run_id}/merge/", {**MERGE_INPUT, "head_sha": "b" * 40, "code_revision": 2}, format="json"
        ).status_code
        == 409
    )
    # Returning to an earlier SHA must not reactivate its old review decision.
    provider.head_sha = "a" * 40
    assert (
        agent.post(
            prefix + f"runs/{run_id}/head/", {"head_sha": provider.head_sha, "pull_request": 7}, format="json"
        ).status_code
        == 200
    )
    assert (
        agent.post(prefix + f"runs/{run_id}/merge/", {**MERGE_INPUT, "code_revision": 3}, format="json").status_code
        == 409
    )
    assert not provider.merge_calls


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "failed_gate", ["no_approval", "remote_head", "remote_base", "question", "child", "failed_test"]
)
def test_failed_gates_never_call_provider_merge(merge_flow, failed_gate):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    _, _, _, issue, contract, _, _, _, _, prefix = execution
    if failed_gate != "no_approval":
        advance(merge_flow)
    if failed_gate == "remote_head":
        provider.head_sha = "b" * 40
    elif failed_gate == "remote_base":
        provider.base_branch = "elsewhere"
    elif failed_gate == "question":
        assert (
            agent.post(prefix + f"work-items/{issue.pk}/questions/", {"text": "Blocking"}, format="json").status_code
            == 201
        )
    elif failed_gate == "child":
        assert (
            agent.post(
                prefix + f"work-items/{issue.pk}/children/",
                {
                    "name": "Required child",
                    "requirements": "Smaller",
                    "definition_of_done": [{"description": "Test"}],
                },
                format="json",
            ).status_code
            == 201
        )
    elif failed_gate == "failed_test":
        assert (
            agent.post(
                prefix + f"runs/{run_id}/evidence/",
                {
                    "head_sha": "a" * 40,
                    "kind": "test",
                    "outcome": "failed",
                    "summary": "Regression",
                    "criterion_ids": [contract.definition_of_done[0]["id"]],
                },
                format="json",
            ).status_code
            == 201
        )
    assert agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json").status_code == 409
    assert not provider.merge_calls
    assert not MergeAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("completed", [False, True])
def test_session_child_deletion_cannot_remove_required_merge_gates(merge_flow, monkeypatch, completed):
    from plane.bgtasks.deletion_task import soft_delete_related_objects

    execution, agent, run_id, provider, done, _, _ = merge_flow
    _, human, project, issue, _, _, _, _, _, prefix = execution
    monkeypatch.setattr(soft_delete_related_objects, "delay", soft_delete_related_objects.run)
    advance(merge_flow)
    child = agent.post(
        prefix + f"work-items/{issue.pk}/children/",
        {
            "name": "Required child",
            "requirements": "Focused implementation",
            "definition_of_done": [{"description": "Child acceptance must pass"}],
        },
        format="json",
    )
    assert child.status_code == 201, child.data
    if completed:
        Issue.objects.filter(pk=child.data["issue"]).update(state=done)
    deleted = human.delete(
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{child.data['issue']}/"
    )
    assert deleted.status_code == 204, getattr(deleted, "data", None)
    assert Issue.all_objects.get(pk=child.data["issue"]).deleted_at is not None
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 409, response.data
    assert RunChild.objects.filter(pk=child.data["id"]).exists()
    assert not provider.merge_calls and not MergeAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("source", ["question", "answer"])
def test_session_comment_deletion_preserves_blocking_question_until_acknowledged(merge_flow, monkeypatch, source):
    from plane.bgtasks.deletion_task import soft_delete_related_objects

    execution, agent, run_id, provider, _, session, _ = merge_flow
    _, human, project, issue, _, _, _, _, _, prefix = execution
    monkeypatch.setattr(soft_delete_related_objects, "delay", soft_delete_related_objects.run)
    advance(merge_flow)
    question = agent.post(prefix + f"work-items/{issue.pk}/questions/", {"text": "Blocking decision"}, format="json")
    assert question.status_code == 201, question.data
    answer_url = session + f"work-items/{issue.pk}/questions/{question.data['id']}/answer/"
    if source == "answer":
        answer = human.post(answer_url, {"text": "Canonical decision"}, format="json")
        assert answer.status_code == 200, answer.data
        comment_id = answer.data["answer_comment"]
    else:
        comment_id = question.data["question_comment"]
    deleted = human.delete(
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/comments/{comment_id}/"
    )
    assert deleted.status_code == 204, getattr(deleted, "data", None)
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 409, response.data
    assert not provider.merge_calls and not MergeAttempt.objects.exists()
    assert WorkItemQuestion.objects.filter(pk=question.data["id"]).exists()
    if source == "question":
        answer = human.post(answer_url, {"text": "Canonical decision"}, format="json")
        assert answer.status_code == 200, answer.data
    resolved = agent.post(
        prefix + f"work-items/{issue.pk}/questions/{question.data['id']}/resolve/",
        {"answer_comment": str(answer.data["answer_comment"])},
        format="json",
    )
    assert resolved.status_code == 200, resolved.data
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 200, response.data
    assert provider.merge_calls == [("main", "a" * 40)]


@pytest.mark.django_db(transaction=True)
def test_unknown_provider_outcome_is_fenced_until_reconciled(merge_flow):
    execution, agent, run_id, provider, _, session, _ = merge_flow
    dispatcher, human, _, issue, contract, _, _, worker, _, prefix = execution
    advance(merge_flow)
    provider.failure = ProviderUnavailable()
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 202, response.data
    assert response.data["status"] == "merging"
    assert APIToken.objects.filter(agent_run_id=run_id, is_active=True).exists()
    assert (
        dispatcher.post(
            prefix + f"work-items/{issue.pk}/claim/",
            {
                "agent": str(worker.pk),
                "worker_id": "replacement",
                "repository": "example/product",
                "base_branch": "main",
            },
            format="json",
        ).status_code
        == 409
    )
    assert human.post(session + f"runs/{run_id}/cancel/", {}, format="json").status_code == 409
    assert (
        human.patch(
            f"/api/workspaces/{contract.project.workspace.slug}/projects/{contract.project_id}/issues/{issue.pk}/",
            {"expected_revision": 1, "requirements": "Changed"},
            format="json",
        ).status_code
        == 409
    )
    assert agent.post(prefix + f"runs/{run_id}/reconcile/", {}, format="json").status_code == 202
    provider.merged = True
    assert agent.post(prefix + f"runs/{run_id}/reconcile/", {}, format="json").status_code == 200
    assert dispatcher.post(prefix + f"runs/{run_id}/reconcile/", {}, format="json").status_code == 200
    assert len(provider.merge_calls) == 1
    assert CoordinationEvent.objects.filter(kind="task.merged", issue=issue).count() == 1


@pytest.mark.django_db(transaction=True)
def test_definite_rejection_keeps_run_open_for_fixes_and_retry(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    advance(merge_flow)
    provider.failure = MergeRejected()
    url = prefix + f"runs/{run_id}/merge/"
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 409
    assert AgentRun.objects.get(pk=run_id).status == "active"
    provider.failure = None
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_completion_cannot_be_bypassed_by_bulk_orm_or_reclassifying_state(merge_flow):
    execution, agent, run_id, provider, done, _, _ = merge_flow
    _, human, project, issue, _, _, _, _, _, _ = execution
    issue.refresh_from_db()
    with pytest.raises(IntegrityError), transaction.atomic():
        Issue.objects.filter(pk=issue.pk).update(state=done)
    state = issue.state or State.objects.create(project=project, name="Working", group="started", color="#ff0000")
    if issue.state_id is None:
        Issue.objects.filter(pk=issue.pk).update(state=state)
    with pytest.raises(IntegrityError), transaction.atomic():
        State.objects.filter(pk=state.pk).update(group="completed")
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    assert human.patch(url, {"state": str(done.pk)}, format="json").status_code == 400
    assert not provider.merge_calls


def completed_required_child(flow):
    execution, agent, run_id, _, _, _, _ = flow
    _, _, project, issue, _, _, _, _, _, prefix = execution
    state = State.objects.create(project=project, name="Child accepted", group="completed", color="#00aa00")
    child = agent.post(
        prefix + f"work-items/{issue.pk}/children/",
        {
            "name": "Required child",
            "requirements": "Focused implementation",
            "definition_of_done": [{"description": "Child tests pass"}],
        },
        format="json",
    )
    assert child.status_code == 201, child.data
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{child.data['issue']}/"
    assert agent.patch(url, {"state": str(state.pk)}, format="json").status_code == 200
    evidence = agent.post(
        prefix + f"runs/{run_id}/evidence/",
        {
            "head_sha": "a" * 40,
            "kind": "test",
            "outcome": "passed",
            "summary": "Child acceptance passed",
            "criterion_ids": [child.data["definition_of_done"][0]["id"]],
        },
        format="json",
    )
    assert evidence.status_code == 201, evidence.data
    return state


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("change", ["group", "deleted_at"])
def test_required_child_state_is_fenced_until_merge_confirmation(merge_flow, change):
    execution, agent, run_id, provider, done, _, _ = merge_flow
    dispatcher, _, project, _, _, _, _, _, _, prefix = execution
    child_state = completed_required_child(merge_flow)
    advance(merge_flow)
    provider.failure = ProviderUnavailable()
    assert agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json").status_code == 202
    assert MergeAttempt.objects.get(run_id=run_id).completion_state_id == done.pk != child_state.pk
    unrelated = State.objects.create(project=project, name="Unrelated", group="completed", color="#777777")
    State.objects.filter(pk=unrelated.pk).update(group="started")
    State.objects.filter(pk=child_state.pk).update(name="Child accepted, renamed")
    changes = {"group": "started"} if change == "group" else {"deleted_at": timezone.now()}
    with pytest.raises(IntegrityError, match="outstanding coordinated merge"), transaction.atomic():
        State.objects.filter(pk=child_state.pk).update(**changes)
    child_state.refresh_from_db()
    assert child_state.group == "completed" and child_state.deleted_at is None
    provider.merged = True
    assert dispatcher.post(prefix + f"runs/{run_id}/reconcile/", {}, format="json").status_code == 200
    State.objects.filter(pk=child_state.pk).update(**changes)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("archived", [False, True])
def test_merge_admission_locks_required_child_state_before_checking_gates(merge_flow, monkeypatch, archived):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    child_state = completed_required_child(merge_flow)
    if archived:
        child_id = RunChild.objects.get(run_id=run_id).issue_id
        Issue.all_objects.filter(pk=child_id).update(archived_at=timezone.localdate())
        assert Issue.objects.filter(pk=child_id).exists()
    advance(merge_flow)
    target_status = provider.target_status
    inspected, locked = False, False

    def inspect_state_lock(base, sha):
        nonlocal inspected, locked
        if not inspected:
            inspected = True
            probe = connection.copy(alias="coordination-child-state-probe")
            try:
                with probe.cursor() as cursor:
                    cursor.execute("SELECT id FROM states WHERE id = %s FOR UPDATE NOWAIT", [child_state.pk])
            except OperationalError as error:
                assert error.__cause__.sqlstate == "55P03"
                locked = True
            finally:
                probe.close()
        return target_status(base, sha)

    monkeypatch.setattr(provider, "target_status", inspect_state_lock)
    provider.failure = MergeRejected()
    assert agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json").status_code == 409
    assert inspected and locked


@pytest.mark.django_db(transaction=True)
def test_machine_keys_cannot_impersonate_human_approval_or_wrap_remote_side_effect(merge_flow, api_key_client):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    _, _, _, issue, _, _, _, _, _, prefix = execution
    assert (
        api_key_client.post(
            prefix + f"work-items/{issue.pk}/decisions/",
            {
                "run": str(run_id),
                "expected_revision": 1,
                "head_sha": "a" * 40,
                "code_revision": 1,
                "decision": "approve",
            },
            format="json",
        ).status_code
        == 403
    )
    advance(merge_flow)
    assert (
        agent.post(
            prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json", HTTP_IDEMPOTENCY_KEY="unsafe-wrapper"
        ).status_code
        == 400
    )
    assert not provider.merge_calls and not MergeAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("stale", [{"expected_revision": 2}, {"code_revision": 2}, {"head_sha": "b" * 40}])
def test_stale_merge_commands_do_not_merge_the_current_revision(merge_flow, stale):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    advance(merge_flow)
    response = agent.post(prefix + f"runs/{run_id}/merge/", {**MERGE_INPUT, **stale}, format="json")
    assert response.status_code == 409, response.data
    assert not provider.merge_calls and not MergeAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_credential_revoked_after_authentication_cannot_start_a_merge(merge_flow, monkeypatch):
    from plane.api.middleware.api_authentication import APIKeyAuthentication

    execution, agent, run_id, provider, _, _, credential = merge_flow
    *_, prefix = execution
    advance(merge_flow)

    def revoke(self, token):
        APIToken.objects.filter(pk=token.pk).update(is_active=False)

    monkeypatch.setattr(APIKeyAuthentication, "record_usage", revoke)
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 403
    assert not provider.merge_calls and not MergeAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_pr_retargeting_cannot_change_the_explicit_merge_destination(merge_flow, monkeypatch):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    advance(merge_flow)
    merge = provider.merge

    def retarget(base, sha):
        provider.base_branch = "another-branch"
        return merge(base, sha)

    monkeypatch.setattr(provider, "merge", retarget)
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 200, response.data
    assert provider.merged_branch == "main" and provider.actual_merges == 1


@pytest.mark.django_db(transaction=True)
def test_same_agent_can_retry_unknown_write_without_another_attempt_or_completion(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    advance(merge_flow)
    url = prefix + f"runs/{run_id}/merge/"
    provider.failure = ProviderUnavailable()
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 202
    provider.failure = None
    response = agent.post(url, MERGE_INPUT, format="json")
    assert response.status_code == 200, response.data
    assert MergeAttempt.objects.filter(run_id=run_id).count() == 1
    assert provider.actual_merges == 1 and len(provider.merge_calls) == 2
    assert CoordinationEvent.objects.filter(kind="task.merged", payload__run_id=str(run_id)).count() == 1


@pytest.mark.django_db(transaction=True)
def test_retry_rejection_does_not_discard_an_earlier_unknown_write(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    *_, prefix = execution
    advance(merge_flow)
    url = prefix + f"runs/{run_id}/merge/"
    provider.failure = ProviderUnavailable()
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 202
    provider.failure = MergeRejected()
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 202
    assert AgentRun.objects.get(pk=run_id).status == "merging"
    assert MergeAttempt.objects.filter(run_id=run_id).get().status == "uncertain"


@pytest.mark.django_db(transaction=True)
def test_task_deletion_cannot_erase_an_in_flight_merge(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    _, _, _, issue, _, _, _, _, _, prefix = execution
    advance(merge_flow)
    provider.failure = ProviderUnavailable()
    assert agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json").status_code == 202
    with pytest.raises(IntegrityError, match="in-flight merge"), transaction.atomic():
        Issue.all_objects.filter(pk=issue.pk).delete()
    assert Issue.objects.filter(pk=issue.pk).exists()
    assert MergeAttempt.objects.get(run_id=run_id).status == "uncertain"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("reject_before_retry_result", [False, True])
def test_earlier_rejection_cannot_clear_an_overlapping_retry_fence(merge_flow, monkeypatch, reject_before_retry_result):
    from plane.coordination.merges import reject_merge

    execution, agent, run_id, provider, _, session, _ = merge_flow
    _, human, project, _, contract, _, _, _, _, prefix = execution
    advance(merge_flow)
    url = prefix + f"runs/{run_id}/merge/"
    entered = False

    def overlapping_merge(base, sha):
        nonlocal entered
        if not entered:
            entered = True
            retry = agent.post(url, MERGE_INPUT, format="json")
            assert retry.status_code == 202, retry.data
            raise MergeRejected()
        attempt = MergeAttempt.objects.get(run_id=run_id)
        assert attempt.status == "uncertain"
        if reject_before_retry_result:
            assert reject_merge(project, run_id, attempt.pk).status == "merging"
        raise ProviderUnavailable()

    monkeypatch.setattr(provider, "merge", overlapping_merge)
    response = agent.post(url, MERGE_INPUT, format="json")
    assert response.status_code == 202, response.data
    assert AgentRun.objects.get(pk=run_id).status == "merging"
    contract.refresh_from_db()
    assert contract.status == "merging"
    assert MergeAttempt.objects.get(run_id=run_id).status == "uncertain"
    assert human.post(session + f"runs/{run_id}/cancel/", {}, format="json").status_code == 409


@pytest.mark.django_db(transaction=True)
def test_pending_merge_can_finish_after_idle_without_dispatcher_renewal(merge_flow, monkeypatch):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    advance(merge_flow)
    url = prefix + f"runs/{run_id}/merge/"
    provider.failure = ProviderUnavailable()
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 202
    later = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    provider.failure = None
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 200
    assert AgentRun.objects.filter(contract_id=AgentRun.objects.get(pk=run_id).contract_id).count() == 1
    assert MergeAttempt.objects.filter(run_id=run_id).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("retry_in_flight", [False, True])
def test_stale_reconciliation_preserves_a_failed_attempt_and_allows_retry(merge_flow, monkeypatch, retry_in_flight):
    from plane.coordination.merges import reject_merge

    execution, agent, run_id, provider, done, _, _ = merge_flow
    dispatcher, _, project, _, contract, _, _, _, _, prefix = execution
    advance(merge_flow)
    run = AgentRun.objects.get(pk=run_id)
    attempt = MergeAttempt.objects.create(
        project=project,
        run=run,
        completion_state=done,
        head_sha=run.head_sha,
        code_revision=run.code_revision,
        pull_request=run.pull_request,
    )
    AgentRun.objects.filter(pk=run_id).update(status="merging")
    WorkItemContract.objects.filter(pk=contract.pk).update(status="merging")
    merge_url = prefix + f"runs/{run_id}/merge/"
    target_status = provider.target_status
    rejected_state = {}

    def finish_rejection_during_observation(base, sha):
        # Reconciliation has already selected the prepared attempt. Complete
        # the original rejection before its old unmerged observation returns.
        observation = target_status(base, sha)
        rejected = reject_merge(project, run_id, attempt.pk)
        failed = MergeAttempt.objects.get(pk=attempt.pk)
        rejected_state.update(error=failed.error, finished_at=failed.finished_at, last_error=rejected.last_error)
        if retry_in_flight:
            provider.failure = ProviderUnavailable()
            with monkeypatch.context() as retry_patch:
                retry_patch.setattr(provider, "target_status", target_status)
                retry = agent.post(merge_url, MERGE_INPUT, format="json")
            assert retry.status_code == 202, retry.data
            provider.failure = None
        return observation

    monkeypatch.setattr(provider, "target_status", finish_rejection_during_observation)
    response = dispatcher.post(prefix + f"runs/{run_id}/reconcile/", {}, format="json")
    assert response.status_code == 202, response.data
    attempt.refresh_from_db()
    assert attempt.status == "failed"
    assert attempt.error == rejected_state["error"]
    assert attempt.finished_at == rejected_state["finished_at"]
    run.refresh_from_db()
    contract.refresh_from_db()
    assert run.status == ("merging" if retry_in_flight else "active")
    assert contract.status == ("merging" if retry_in_flight else "review")
    assert MergeAttempt.objects.filter(run=run, status__in=["prepared", "uncertain"]).count() == int(retry_in_flight)
    if not retry_in_flight:
        assert run.last_error == rejected_state["last_error"]
    monkeypatch.setattr(provider, "target_status", target_status)
    assert agent.post(merge_url, MERGE_INPUT, format="json").status_code == 200
    assert MergeAttempt.objects.filter(run=run).count() == 2
    assert MergeAttempt.objects.filter(run=run, status="failed").count() == 1
    assert MergeAttempt.objects.filter(run=run, status="confirmed").count() == 1
