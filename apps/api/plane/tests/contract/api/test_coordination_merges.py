# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import (
    AgentRun,
    APIToken,
    CoordinationEvent,
    Issue,
    MergeAttempt,
    ProjectMember,
    State,
    User,
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
        self.check_results = {"tests": [True]}
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

    def checks(self, sha):
        return self.check_results

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
    policy.repositories[0]["required_checks"] = ["tests"]
    policy.save()
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
    "failed_gate", ["no_approval", "ci", "missing_ci", "remote_head", "remote_base", "question", "child", "failed_test"]
)
def test_failed_gates_never_call_provider_merge(merge_flow, failed_gate):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    _, _, _, issue, contract, _, _, _, _, prefix = execution
    if failed_gate != "no_approval":
        advance(merge_flow)
    if failed_gate == "ci":
        provider.check_results = {"tests": [False]}
    elif failed_gate == "missing_ci":
        provider.check_results = {}
    elif failed_gate == "remote_head":
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
            {"agent": str(worker.pk), "worker_id": "replacement"},
            format="json",
        ).status_code
        == 409
    )
    assert human.post(session + f"runs/{run_id}/cancel/", {}, format="json").status_code == 409
    assert (
        human.patch(
            session + f"work-items/{issue.pk}/contract/",
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
def test_expired_pending_merge_can_be_renewed_only_by_its_dispatcher(merge_flow):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    advance(merge_flow)
    url = prefix + f"runs/{run_id}/merge/"
    provider.failure = ProviderUnavailable()
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 202
    AgentRun.objects.filter(pk=run_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert agent.post(url, MERGE_INPUT, format="json").status_code == 403
    renewed = dispatcher.post(prefix + f"runs/{run_id}/renew/", {}, format="json")
    assert renewed.status_code == 200, renewed.data
    assert renewed.data["status"] == "merging"
    assert AgentRun.objects.get(pk=run_id).lease_expires_at > timezone.now()
    resumed, _ = worker_client(execution, run_id)
    provider.failure = None
    assert resumed.post(url, MERGE_INPUT, format="json").status_code == 200
    assert AgentRun.objects.filter(contract_id=AgentRun.objects.get(pk=run_id).contract_id).count() == 1
    assert MergeAttempt.objects.filter(run_id=run_id).count() == 1


@pytest.mark.django_db(transaction=True)
def test_expired_active_run_cannot_be_revived_by_renewal(merge_flow):
    execution, _, run_id, _, _, _, _ = merge_flow
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    expired_at = timezone.now() - timedelta(seconds=1)
    AgentRun.objects.filter(pk=run_id).update(lease_expires_at=expired_at)
    assert dispatcher.post(prefix + f"runs/{run_id}/renew/", {}, format="json").status_code == 403
    assert AgentRun.objects.get(pk=run_id).lease_expires_at == expired_at
