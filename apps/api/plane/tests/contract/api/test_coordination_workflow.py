# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from plane.db.models import (
    APIToken,
    CoordinationEvent,
    FileAsset,
    Issue,
    IssueAssignee,
    ProjectMember,
    RunTarget,
    User,
    WorkItemContract,
    WorkspaceMember,
)
from .test_coordination_execution import execution as execution, claim, worker_client
from .test_coordination_contracts import coordination as coordination


@pytest.fixture
def guest_access(execution):
    dispatcher, _, project, _, contract, _, _, worker, _, prefix = execution
    project.guest_view_all_features = False
    project.save(update_fields=["guest_view_all_features"])
    guest = User.objects.create(email="guest@coordination.test", username="coordination-guest")
    WorkspaceMember.objects.create(workspace=project.workspace, member=guest, role=5)
    membership = ProjectMember.objects.create(project=project, member=guest, role=5)
    owned = Issue(project=project, name="Guest-created task", created_by=guest)
    owned.save(disable_auto_set_user=True)
    WorkItemContract.objects.update_or_create(
        issue=owned,
        defaults={
            "project": project,
            "owner": contract.owner,
            "status": "ready",
            "requirements": "Guest task requirement",
            "definition_of_done": [{"id": str(uuid4()), "description": "Guest acceptance"}],
        },
    )
    IssueAssignee.objects.create(project=project, issue=owned, assignee=contract.owner)
    hidden_run = claim(execution)
    owned_run = dispatcher.post(
        prefix + f"work-items/{owned.pk}/claim/",
        {"agent": str(worker.pk), "worker_id": "guest-task-worker", "repository": "example/product"},
        format="json",
    )
    assert owned_run.status_code == 201, owned_run.data
    return guest, membership, owned, owned_run.data["id"], hidden_run


def guest_client(guest, transport):
    client = APIClient()
    if transport == "session":
        client.force_login(guest)
    else:
        APIToken.objects.create(user=guest, token="guest-coordination-test-key")
        client.credentials(HTTP_X_API_KEY="guest-coordination-test-key")
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("transport", ["session", "key"])
@pytest.mark.parametrize("endpoint", ["contract", "overview", "run"])
def test_guest_coordination_reads_preserve_issue_visibility(execution, guest_access, transport, endpoint):
    _, _, project, hidden, _, _, _, _, _, machine = execution
    guest, membership, owned, owned_run, hidden_run = guest_access
    client = guest_client(guest, transport)
    prefix = machine.replace("/api/v1/", "/api/", 1) if transport == "session" else machine
    if endpoint == "run":
        visible_url, hidden_url = prefix + f"runs/{owned_run}/", prefix + f"runs/{hidden_run}/"
    else:
        visible_url = prefix + f"work-items/{owned.pk}/{endpoint}/"
        hidden_url = prefix + f"work-items/{hidden.pk}/{endpoint}/"
    assert client.get(visible_url).status_code == 200
    denied = client.get(hidden_url)
    assert denied.status_code == 403, denied.data
    project.guest_view_all_features = True
    project.save(update_fields=["guest_view_all_features"])
    assert client.get(hidden_url).status_code == 200
    project.guest_view_all_features = False
    project.save(update_fields=["guest_view_all_features"])
    membership.role = 15
    membership.save(update_fields=["role"])
    assert client.get(hidden_url).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("transport", ["session", "key"])
def test_guest_event_pages_include_only_visible_tasks(execution, guest_access, transport):
    from django.db import transaction
    from plane.coordination.contracts import emit_event

    _, _, project, hidden, contract, _, _, _, _, machine = execution
    guest, _, owned, _, _ = guest_access
    with transaction.atomic():
        emit_event(project, "synthetic.visible", actor=guest, issue=owned)
        emit_event(project, "synthetic.hidden", actor=contract.owner, issue=hidden)
        emit_event(project, "synthetic.project", actor=contract.owner, payload={"private": "project event"})
    client = guest_client(guest, transport)
    prefix = machine.replace("/api/v1/", "/api/", 1) if transport == "session" else machine
    visible = list(CoordinationEvent.objects.filter(project=project, issue=owned))
    cursor = 0
    for index, event in enumerate(visible):
        response = client.get(prefix + f"events/?after={cursor}&per_page=1")
        assert response.status_code == 200, response.data
        assert [str(row["id"]) for row in response.data["events"]] == [str(event.pk)]
        assert response.data["next_cursor"] == event.sequence > cursor
        assert response.data["has_more"] == (index < len(visible) - 1)
        cursor = response.data["next_cursor"]
    assert client.get(prefix + f"events/?after={cursor}").data["events"] == []
    project.guest_view_all_features = True
    project.save(update_fields=["guest_view_all_features"])
    unrestricted = client.get(prefix + "events/")
    assert len(unrestricted.data["events"]) == CoordinationEvent.objects.filter(project=project).count()


@pytest.mark.django_db
def test_events_are_replayable_and_catch_up_from_committed_cursor(execution):
    dispatcher, _, _, issue, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    first = dispatcher.get(prefix + "events/?after=0")
    assert first.status_code == 200, first.data
    assert [event["kind"] for event in first.data["events"]] == [
        "task.specification_created",
        "task.ready",
        "run.claimed",
    ]
    cursor = first.data["next_cursor"]
    assert dispatcher.get(prefix + "events/?after=0").data == first.data
    worker_client(execution, run_id)
    next_page = dispatcher.get(prefix + f"events/?after={cursor}")
    assert [event["kind"] for event in next_page.data["events"]] == ["run.credential_issued"]
    assert dispatcher.get(prefix + "events/?after=-1").status_code == 400
    assert dispatcher.get(prefix + "events/?after=not-a-number").status_code == 400


@pytest.mark.django_db
def test_only_explicit_answer_and_resolution_resume_blocked_task(execution):
    _, human, _, issue, contract, _, _, _, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    url = prefix + f"work-items/{issue.pk}/questions/"
    asked = agent.post(url, {"text": "Which behavior?", "blocking": True}, format="json")
    assert asked.status_code == 201, asked.data
    question_id = asked.data["id"]
    contract.refresh_from_db()
    assert contract.status == "awaiting_input"
    assert agent.post(url + f"{question_id}/resolve/", {}, format="json").status_code == 409
    assert agent.post(url + f"{question_id}/answer/", {"text": "I approve"}, format="json").status_code == 403
    session = url.replace("/api/v1/", "/api/", 1)
    answered = human.post(session + f"{question_id}/answer/", {"text": "Use behavior A"}, format="json")
    assert answered.status_code == 200, answered.data
    contract.refresh_from_db()
    assert contract.status == "awaiting_input"
    assert agent.post(url + f"{question_id}/resolve/", {}, format="json").status_code == 400
    assert (
        agent.post(
            url + f"{question_id}/resolve/", {"answer_comment": str(answered.data["answer_comment"])}, format="json"
        ).status_code
        == 200
    )
    contract.refresh_from_db()
    assert contract.status == "in_progress"
    assert issue.issue_comments.count() == 2


@pytest.mark.django_db
def test_scoped_decomposition_grants_only_new_child_and_preserves_owner(execution):
    _, _, project, issue, contract, _, _, _, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    created = agent.post(
        prefix + f"work-items/{issue.pk}/children/",
        {
            "name": "Focused subtask",
            "requirements": "Smaller requirement",
            "definition_of_done": [{"description": "Child test passes"}],
            "required": True,
        },
        format="json",
    )
    assert created.status_code == 201, created.data
    child = Issue.objects.get(pk=created.data["issue"])
    assert child.parent_id == issue.pk
    assert created.json()["owner"] == str(contract.owner_id)
    assert RunTarget.objects.filter(run_id=run_id, issue=child).exists()
    base = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/"
    assert agent.patch(base + f"{child.pk}/", {"name": "Refined subtask"}, format="json").status_code == 200
    assert (
        agent.post(
            prefix + f"work-items/{issue.pk}/children/",
            {
                "name": "Invalid",
                "requirements": "Spec",
                "definition_of_done": [{"description": "Test"}],
                "modules": [str(uuid4())],
            },
            format="json",
        ).status_code
        == 400
    )
    assert Issue.objects.filter(parent=issue).count() == 1


@pytest.mark.django_db
def test_evidence_is_bound_to_current_head_criteria_and_owned_completed_upload(execution):
    _, _, project, issue, contract, _, _, worker, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    sha = "a" * 40
    head = agent.post(prefix + f"runs/{run_id}/head/", {"head_sha": sha, "pull_request": 7}, format="json")
    assert head.status_code == 200, head.data
    asset = FileAsset(
        project=project,
        workspace=project.workspace,
        issue=issue,
        created_by=worker,
        agent_run_id=run_id,
        is_uploaded=False,
        asset="synthetic/screenshot.png",
    )
    asset.save(disable_auto_set_user=True)
    payload = {
        "kind": "test",
        "summary": "Tests pass",
        "head_sha": sha,
        "outcome": "passed",
        "criterion_ids": [contract.definition_of_done[0]["id"]],
        "asset": str(asset.pk),
    }
    evidence_url = prefix + f"runs/{run_id}/evidence/"
    assert agent.post(evidence_url, payload, format="json").status_code == 400
    FileAsset.objects.filter(pk=asset.pk).update(is_uploaded=True)
    response = agent.post(evidence_url, payload, format="json")
    assert response.status_code == 201, response.data
    assert response.data["spec_revision"] == 1
    assert agent.post(evidence_url, {**payload, "head_sha": "b" * 40}, format="json").status_code == 409
    assert agent.post(evidence_url, {**payload, "criterion_ids": [str(uuid4())]}, format="json").status_code == 400
    overview = agent.get(prefix + f"work-items/{issue.pk}/overview/")
    assert overview.status_code == 200, overview.data
    assert len(overview.data["evidence"]) == 1
    assert "token" not in str(overview.data)


@pytest.mark.django_db
def test_machine_cannot_answer_questions_using_human_owned_key(execution, api_key_client):
    _, _, _, issue, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    asked = agent.post(prefix + f"work-items/{issue.pk}/questions/", {"text": "Human decision?"}, format="json")
    assert asked.status_code == 201, asked.data
    answer = prefix + f"work-items/{issue.pk}/questions/{asked.data['id']}/answer/"
    assert api_key_client.post(answer, {"text": "Yes"}, format="json").status_code == 403


@pytest.mark.django_db
def test_event_and_business_mutation_roll_back_together(execution):
    from django.db import transaction

    _, _, project, issue, _, _, _, _, _, _ = execution
    from plane.coordination.contracts import emit_event

    with pytest.raises(RuntimeError), transaction.atomic():
        WorkItemContract.objects.filter(issue=issue).update(status="review")
        emit_event(project, "synthetic.rollback", actor=None, issue=issue)
        raise RuntimeError("rollback")
    assert not CoordinationEvent.objects.filter(kind="synthetic.rollback").exists()
    assert WorkItemContract.objects.get(issue=issue).status == "ready"


@pytest.mark.django_db
def test_question_answer_is_a_snapshot_not_mutable_comment_text(execution):
    _, human, project, issue, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    question = agent.post(prefix + f"work-items/{issue.pk}/questions/", {"text": "Which behavior?"}, format="json")
    session = prefix.replace("/api/v1/", "/api/", 1)
    answer = human.post(
        session + f"work-items/{issue.pk}/questions/{question.data['id']}/answer/",
        {"text": "Approved answer"},
        format="json",
    )
    assert answer.status_code == 200, answer.data
    comment = (
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
        f"comments/{answer.data['answer_comment']}/"
    )
    changed = human.patch(comment, {"comment_html": "<p>Edited ordinary comment</p>"}, format="json")
    assert changed.status_code == 200, changed.data
    overview = agent.get(prefix + f"work-items/{issue.pk}/overview/")
    assert overview.data["questions"][0]["answer"] == "Approved answer"


@pytest.mark.django_db
def test_resolution_must_acknowledge_the_current_answer(execution):
    _, human, _, issue, contract, _, _, _, _, prefix = execution
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    url = prefix + f"work-items/{issue.pk}/questions/"
    question = agent.post(url, {"text": "Which behavior?"}, format="json")
    session = url.replace("/api/v1/", "/api/", 1)
    first = human.post(session + f"{question.data['id']}/answer/", {"text": "First answer"}, format="json")
    second = human.post(session + f"{question.data['id']}/answer/", {"text": "Revised answer"}, format="json")
    assert first.status_code == second.status_code == 200
    resolved = agent.post(
        url + f"{question.data['id']}/resolve/", {"answer_comment": str(first.data["answer_comment"])}, format="json"
    )
    assert resolved.status_code == 409, resolved.data
    contract.refresh_from_db()
    assert contract.status == "awaiting_input"
    assert (
        agent.post(
            url + f"{question.data['id']}/resolve/",
            {"answer_comment": str(second.data["answer_comment"])},
            format="json",
        ).status_code
        == 200
    )
