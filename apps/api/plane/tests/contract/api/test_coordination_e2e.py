# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json

import pytest
from rest_framework.test import APIClient

from plane.db.models import APIToken, Issue, Module, ModuleIssue, ProjectMember, State, User, WorkspaceMember
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import execution as execution, claim, worker_client
from .test_coordination_github import github_http as github_http
from .test_coordination_handoff import handoff, refresh


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handed_off", [False, True])
def test_reviewed_delivery_through_real_auth_database_and_github_http(execution, github_http, settings, handed_off):
    _, human, project, issue, contract, policy, _, _, _, prefix = execution
    session = prefix.replace("/api/v1/", "/api/", 1)
    settings.COORDINATION_GITHUB_INSTALLATIONS = json.dumps({str(project.pk): {"example/product": 42}})
    done = State.objects.create(project=project, name="Done", group="completed", color="#777777")
    reviewer = User.objects.create(email="reviewer@delivery.test", username="reviewer")
    WorkspaceMember.objects.create(workspace=project.workspace, member=reviewer, role=15)
    ProjectMember.objects.create(project=project, member=reviewer, role=15)
    review_client = APIClient()
    review_client.force_login(reviewer)
    assert (
        human.put(
            session + f"work-items/{issue.pk}/reviewers/",
            {"expected_revision": 1, "reviewers": [str(reviewer.pk)]},
            format="json",
        ).status_code
        == 200
    )
    if handed_off:
        assignment, run_id, _ = handoff(execution)
        credential = refresh(execution, assignment, run_id)
        assert credential.status_code == 201, credential.data
        agent = APIClient()
        agent.credentials(HTTP_X_API_KEY=credential.data["token"])
    else:
        run_id = claim(execution)
        agent, _ = worker_client(execution, run_id)
    run_path = prefix + f"runs/{run_id}/"
    task_path = prefix + f"work-items/{issue.pk}/"
    modules = [Module.objects.create(project=project, name=name) for name in ("API", "Web")]
    assert (
        agent.patch(
            task_path + "modules/", {"added": [str(module.pk) for module in modules]}, format="json"
        ).status_code
        == 200
    )
    question = agent.post(task_path + "questions/", {"text": "Include API and web changes?"}, format="json")
    assert question.status_code == 201, question.data
    answered = human.post(
        session + f"work-items/{issue.pk}/questions/{question.data['id']}/answer/", {"text": "Yes"}, format="json"
    )
    assert answered.status_code == 200, answered.data
    assert (
        agent.post(
            task_path + f"questions/{question.data['id']}/resolve/",
            {"answer_comment": str(answered.data["answer_comment"])},
            format="json",
        ).status_code
        == 200
    )
    child = agent.post(
        task_path + "children/",
        {
            "name": "Focused scope",
            "requirements": "Implement the API part",
            "definition_of_done": [{"description": "API contract passes"}],
            "required": True,
        },
        format="json",
    )
    assert child.status_code == 201, child.data
    child_id = child.data["issue"]
    assert ModuleIssue.objects.filter(issue_id=child_id).count() == 2
    work_items = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/"
    assert agent.patch(work_items + f"{child_id}/", {"state": str(done.pk)}, format="json").status_code == 200
    assert agent.patch(work_items + f"{issue.pk}/", {"parent": str(child_id)}, format="json").status_code == 403
    criteria = [contract.definition_of_done[0]["id"], child.data["definition_of_done"][0]["id"]]
    for number, sha in enumerate(["a" * 40, "b" * 40], start=1):
        github_http["head_sha"] = sha
        assert agent.post(run_path + "head/", {"head_sha": sha, "pull_request": 7}, format="json").status_code == 200
        assert (
            agent.post(
                run_path + "evidence/",
                {
                    "head_sha": sha,
                    "kind": "test",
                    "outcome": "passed",
                    "summary": "Acceptance tests pass",
                    "criterion_ids": criteria,
                },
                format="json",
            ).status_code
            == 201
        )
        approval = {
            "run": str(run_id),
            "expected_revision": 1,
            "code_revision": number,
            "head_sha": sha,
            "decision": "approve",
        }
        assert human.post(session + f"work-items/{issue.pk}/decisions/", approval, format="json").status_code == 201
        merge_input = {name: approval[name] for name in ("expected_revision", "code_revision", "head_sha")}
        assert agent.post(run_path + "merge/", merge_input, format="json").status_code == 409
        assert (
            review_client.post(session + f"work-items/{issue.pk}/decisions/", approval, format="json").status_code
            == 201
        )
        if number == 1:
            github_http["merge_status"] = 409
            assert agent.post(run_path + "merge/", merge_input, format="json").status_code == 409
            assert Issue.objects.get(pk=issue.pk).state_id != done.pk
            assert APIToken.objects.filter(agent_run_id=run_id, is_active=True).exists()
    github_http["merge_status"] = 200
    merged = agent.post(run_path + "merge/", merge_input, format="json")
    assert merged.status_code == 200, merged.data
    assert [request for request in github_http["requests"] if request[1].endswith("/merges")] == [
        ("POST", "/repos/example/product/merges", {"base": "main", "head": "a" * 40}),
        ("POST", "/repos/example/product/merges", {"base": "main", "head": "b" * 40}),
    ]
    assert not any(path.endswith(("/check-runs", "/status")) for _, path, _ in github_http["requests"])
    contract.refresh_from_db()
    assert contract.status == "merged" and contract.merged_sha == "f" * 40
    assert Issue.objects.get(pk=issue.pk).state_id == done.pk
    assert not APIToken.objects.filter(agent_run_id=run_id, is_active=True).exists()
    assert agent.get(run_path).status_code == 403
    if handed_off:
        assert refresh(execution, assignment, run_id).status_code == 403
    Issue.all_objects.filter(pk=issue.pk).delete()
    assert not Issue.all_objects.filter(pk__in=[issue.pk, child_id]).exists()
