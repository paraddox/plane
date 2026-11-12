# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework.test import APIClient

from plane.db.models import (
    DraftIssue,
    CoordinationEvent,
    Issue,
    IssueAssignee,
    Project,
    ProjectMember,
    ProjectRelease,
    User,
    WorkItemContract,
    WorkspaceMember,
)
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import execution as execution, claim, worker_client


@pytest.mark.django_db
def test_tasks_have_an_empty_ready_specification_from_creation(coordination, create_user):
    _, project, issue, _ = coordination
    contract = WorkItemContract.objects.get(issue=issue)
    assert contract.owner_id == create_user.pk
    assert contract.requirements == "" and contract.definition_of_done == []
    assert contract.status == "ready"
    assert CoordinationEvent.objects.filter(project=project, issue=issue, kind="task.ready").count() == 1


@pytest.mark.django_db
def test_drafts_preserve_native_intent_until_published(coordination):
    client, project, _, _ = coordination
    release = ProjectRelease.objects.create(project=project, identifier="DRAFT", name="Draft release")
    prefix = f"/api/workspaces/{project.workspace.slug}/"
    created = client.post(
        prefix + "draft-issues/",
        {
            "project_id": str(project.pk),
            "name": "Draft with intent",
            "requirements": "Stored requirements",
            "definition_of_done": [{"description": "Stored acceptance criterion"}],
            "release_id": str(release.pk),
        },
        format="json",
    )
    assert created.status_code == 201, created.data
    draft = DraftIssue.objects.get(pk=created.data["id"])
    assert draft.requirements == "Stored requirements"
    detail = client.get(prefix + f"draft-issues/{draft.pk}/")
    assert detail.status_code == 200, detail.data
    assert str(detail.data["release_id"]) == str(release.pk)
    assert detail.data["definition_of_done"][0]["description"] == "Stored acceptance criterion"
    published = client.post(prefix + f"draft-to-issue/{draft.pk}/", {"name": draft.name}, format="json")
    assert published.status_code == 201, published.data
    contract = WorkItemContract.objects.get(issue_id=published.data["id"])
    assert contract.requirements == draft.requirements
    assert contract.definition_of_done == draft.definition_of_done
    assert contract.release_id == release.pk and contract.status == "ready"


@pytest.mark.django_db
def test_creation_accepts_native_task_intent_and_release(coordination):
    client, project, _, _ = coordination
    release = ProjectRelease.objects.create(project=project, identifier="R1", name="Release 1")
    response = client.post(
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/",
        {
            "name": "Native task",
            "requirements": "One standalone HTML page.",
            "definition_of_done": [{"description": "Opens without a build step."}],
            "release_id": str(release.pk),
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    assert response.data["requirements"] == "One standalone HTML page."
    assert str(response.data["release_id"]) == str(release.pk)
    assert response.data["spec_revision"] == 1
    issue = Issue.objects.get(pk=response.data["id"])
    contract = WorkItemContract.objects.get(issue=issue)
    assert issue.requirements == "One standalone HTML page."
    assert issue.definition_of_done[0]["description"] == "Opens without a build step."
    assert issue.definition_of_done[0]["id"]
    assert issue.release_id == release.pk
    assert contract.status == "ready" and contract.spec_revision == 1


@pytest.mark.django_db
def test_normal_task_patch_updates_versioned_intent_and_rejects_stale_edits(coordination):
    client, project, issue, _ = coordination
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    response = client.patch(url, {"requirements": "Updated requirement", "expected_revision": 1}, format="json")
    assert response.status_code == 200, response.data
    contract = WorkItemContract.objects.get(issue=issue)
    assert contract.requirements == "Updated requirement" and contract.spec_revision == 2
    assert contract.status == "ready"
    stale = client.patch(url, {"requirements": "Stale overwrite", "expected_revision": 1}, format="json")
    assert stale.status_code == 409, stale.data
    contract.refresh_from_db()
    assert contract.requirements == "Updated requirement"


@pytest.mark.django_db
def test_release_cannot_cross_projects(coordination):
    client, project, issue, _ = coordination
    other = Project.objects.create(workspace=project.workspace, name="Other", identifier="OTHER")
    release = ProjectRelease.objects.create(project=other, name="Other release", identifier="R1")
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    response = client.patch(url, {"release_id": str(release.pk), "expected_revision": 1}, format="json")
    assert response.status_code == 400, response.data
    assert WorkItemContract.objects.get(issue=issue).release_id is None


@pytest.mark.django_db
def test_human_assignees_are_owners_but_members_cannot_self_grant_ownership(coordination):
    creator, project, issue, _ = coordination
    member = User.objects.create(email="native-owner@example.test", username="native-owner")
    WorkspaceMember.objects.create(workspace=project.workspace, member=member, role=15)
    ProjectMember.objects.create(project=project, member=member, role=15)
    client = APIClient()
    client.force_login(member)
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    response = client.patch(url, {"assignee_ids": [str(member.pk)]}, format="json")
    assert response.status_code == 403, response.data
    assert not IssueAssignee.objects.filter(issue=issue, assignee=member).exists()
    assigned = creator.patch(url, {"assignee_ids": [str(member.pk)]}, format="json")
    assert assigned.status_code == 200, assigned.data
    edited = client.patch(url, {"requirements": "Human assignee intent", "expected_revision": 1}, format="json")
    assert edited.status_code == 200, edited.data
    assert WorkItemContract.objects.get(issue=issue).requirements == "Human assignee intent"
    removed = creator.patch(url, {"assignee_ids": []}, format="json")
    assert removed.status_code == 200
    denied = client.patch(url, {"requirements": "Former owner", "expected_revision": 2}, format="json")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_bot_identities_cannot_be_assigned_as_human_owners(execution):
    _, human, project, issue, _, _, _, worker, _, _ = execution
    response = human.patch(
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/",
        {"assignee_ids": [str(worker.pk)]},
        format="json",
    )
    assert response.status_code == 400, response.data
    assert not IssueAssignee.objects.filter(issue=issue, assignee=worker).exists()


@pytest.mark.django_db
def test_agent_subtask_exposes_its_scope_without_a_separate_root_workflow(execution):
    _, human, project, issue, _, _, _, _, _, prefix = execution
    agent, _ = worker_client(execution, claim(execution))
    created = agent.post(
        prefix + f"work-items/{issue.pk}/children/",
        {
            "name": "Scoped child",
            "requirements": "Implement just the welcome heading.",
            "definition_of_done": [{"description": "Heading is visible."}],
        },
        format="json",
    )
    assert created.status_code == 201, created.data
    child_id = created.data["issue"]
    overview = human.get(prefix.replace("/api/v1/", "/api/") + f"work-items/{child_id}/overview/")
    assert overview.status_code == 200, overview.data
    assert overview.data["inherited_from"] == str(execution[3].pk)
    assert overview.data["contract"]["requirements"] == "Implement just the welcome heading."
    assert not WorkItemContract.objects.filter(issue_id=child_id).exists()
    rewrite = human.patch(
        f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{child_id}/",
        {"requirements": "Changed scope", "expected_revision": 1},
        format="json",
    )
    assert rewrite.status_code == 409, rewrite.data


@pytest.mark.django_db
def test_agent_can_claim_incomplete_tasks_and_ask_but_cannot_request_review(execution):
    _, _, _, issue, contract, _, _, _, _, prefix = execution
    contract.requirements = ""
    contract.definition_of_done = []
    contract.save()
    run_id = claim(execution)
    agent, _ = worker_client(execution, run_id)
    overview = agent.get(prefix + f"work-items/{issue.pk}/overview/")
    assert overview.status_code == 200
    assert overview.data["runs"][0]["spec_snapshot"]["description_html"] == issue.description_html
    asked = agent.post(
        prefix + f"work-items/{issue.pk}/questions/",
        {"text": "What are the required welcome content and acceptance criteria?", "blocking": True},
        format="json",
    )
    assert asked.status_code == 201, asked.data
    contract.refresh_from_db()
    assert contract.status == "awaiting_input"
    review = agent.post(prefix + f"runs/{run_id}/review-ready/", {}, format="json")
    assert review.status_code == 409


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value", [("requirements", "Changed"), ("definition_of_done", []), ("release_id", None), ("assignees", [])]
)
def test_worker_cannot_rewrite_native_intent_or_owner_assignments(execution, field, value):
    _, _, project, issue, _, _, _, _, _, _ = execution
    agent, _ = worker_client(execution, claim(execution))
    url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/"
    response = agent.patch(url, {field: value, "expected_revision": 1}, format="json")
    assert response.status_code == 403, response.data
