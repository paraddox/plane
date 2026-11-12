# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework.test import APIClient

from plane.db.models import (
    Issue,
    Module,
    ModuleIssue,
    Project,
    ProjectMember,
    ProjectRelease,
    User,
    WorkItemContract,
    WorkspaceMember,
)


@pytest.fixture
def coordination(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Coordination", identifier="COORD")
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    issue = Issue(project=project, name="Product task", created_by=create_user)
    issue.save(disable_auto_set_user=True)
    client = APIClient()
    client.force_login(create_user)
    prefix = f"/api/workspaces/{workspace.slug}/projects/{project.pk}/coordination/"
    return client, project, issue, prefix


def specification(**overrides):
    return {
        "requirements": "Implement the product requirement.",
        "definition_of_done": [{"description": "The acceptance test passes."}],
        **overrides,
    }


@pytest.mark.django_db
def test_creator_owns_a_ready_task_without_a_configuration_step(coordination, create_user):
    client, project, issue, prefix = coordination
    response = client.get(prefix + f"work-items/{issue.pk}/contract/")
    assert response.status_code == 200
    assert str(response.data["owner"]) == str(create_user.pk)
    assert response.data["owner_ids"] == [str(create_user.pk)]
    assert response.data["status"] == "ready"
    assert response.data["requirements"] == ""
    assert response.data["definition_of_done"] == []
    assert client.put(prefix + f"work-items/{issue.pk}/contract/", specification(), format="json").status_code == 405
    assert client.post(prefix + f"work-items/{issue.pk}/submit/", {}, format="json").status_code == 404


@pytest.mark.django_db
def test_stale_native_edits_conflict_and_material_changes_revoke_advance_authorization(coordination):
    client, project, issue, prefix = coordination
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    created = client.patch(url, specification(expected_revision=1), format="json")
    assert created.status_code == 200, created.data
    assert created.data["spec_revision"] == 2
    authorization = client.put(
        prefix + f"work-items/{issue.pk}/auto-merge/", {"enabled": True, "expected_revision": 2}, format="json"
    )
    assert authorization.status_code == 200, authorization.data
    unchanged = client.patch(url, {"expected_revision": 2, "requirements": created.data["requirements"]}, format="json")
    assert unchanged.status_code == 200
    assert WorkItemContract.objects.get(issue=issue).auto_merge_authorized is True
    changed = client.patch(url, {"expected_revision": 2, "requirements": "Changed scope"}, format="json")
    assert changed.status_code == 200 and changed.data["spec_revision"] == 3
    assert WorkItemContract.objects.get(issue=issue).auto_merge_authorized is False
    assert client.patch(url, {"expected_revision": 2, "requirements": "Stale"}, format="json").status_code == 409


@pytest.mark.django_db
def test_human_owned_api_key_cannot_rewrite_native_intent_or_approve_merge(coordination, api_key_client):
    client, project, issue, prefix = coordination
    native = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    assert client.patch(native, specification(expected_revision=1), format="json").status_code == 200
    machine = prefix.replace("/api/", "/api/v1/", 1)
    assert api_key_client.get(machine + f"work-items/{issue.pk}/contract/").status_code == 200
    task = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/"
    assert (
        api_key_client.patch(
            task, {"requirements": "Machine replacement", "expected_revision": 2}, format="json"
        ).status_code
        == 403
    )
    assert (
        api_key_client.put(
            machine + f"work-items/{issue.pk}/auto-merge/", {"enabled": True, "expected_revision": 2}, format="json"
        ).status_code
        == 403
    )
    assert WorkItemContract.objects.get(issue=issue).requirements == specification()["requirements"]


@pytest.mark.django_db
def test_native_task_mutations_require_csrf(coordination, create_user):
    _, project, issue, _ = coordination
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(create_user)
    url = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    assert client.patch(url, specification(expected_revision=1), format="json").status_code == 403


@pytest.mark.django_db
def test_release_identifiers_are_project_local_and_machine_cannot_administer_them(
    coordination, workspace, api_key_client
):
    client, project, issue, prefix = coordination
    first = client.post(prefix + "releases/", {"name": "First", "identifier": "R1"}, format="json")
    assert first.status_code == 201, first.data
    other = Project.objects.create(workspace=workspace, name="Second", identifier="SECOND")
    ProjectRelease.objects.create(project=other, name="Second", identifier="R1")
    assert [entry["id"] for entry in client.get(prefix + "releases/").data] == [first.data["id"]]
    assert (
        client.post(prefix + "releases/", {"name": "Duplicate", "identifier": "R1"}, format="json").status_code == 400
    )
    assert (
        api_key_client.post(
            prefix.replace("/api/", "/api/v1/", 1) + "releases/", {"name": "Denied", "identifier": "R2"}, format="json"
        ).status_code
        == 403
    )


@pytest.mark.django_db
def test_membership_revocation_blocks_contract_reads(coordination, create_user):
    client, project, issue, prefix = coordination
    WorkspaceMember.objects.filter(workspace=project.workspace, member=create_user).update(is_active=False)
    assert client.get(prefix + f"work-items/{issue.pk}/contract/").status_code == 403


@pytest.mark.django_db
def test_non_owner_member_cannot_take_over_an_existing_task(coordination, workspace):
    client, project, issue, _ = coordination
    other = User.objects.create(email="other-contract@example.test", username="other-contract")
    WorkspaceMember.objects.create(workspace=workspace, member=other, role=15)
    ProjectMember.objects.create(project=project, member=other, role=15)
    client.force_login(other)
    url = f"/api/workspaces/{workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    assert client.patch(url, {"assignee_ids": [str(other.pk)]}, format="json").status_code == 403


@pytest.mark.django_db
def test_owner_can_assign_multiple_modules_without_moving_the_task(coordination, api_key_client):
    client, project, issue, prefix = coordination
    modules = [Module.objects.create(project=project, name=name) for name in ("Frontend", "Backend")]
    url = prefix + f"work-items/{issue.pk}/modules/"
    response = client.patch(url, {"added": [str(module.pk) for module in modules]}, format="json")
    assert response.status_code == 200, response.data
    assert set(ModuleIssue.objects.filter(issue=issue).values_list("module_id", flat=True)) == {
        module.pk for module in modules
    }
    machine = url.replace("/api/", "/api/v1/", 1)
    assert api_key_client.patch(machine, {"removed": [str(modules[0].pk)]}, format="json").status_code == 403


@pytest.mark.django_db
def test_module_assignment_rejects_other_project_and_rolls_back(coordination, workspace):
    client, project, issue, prefix = coordination
    other = Project.objects.create(workspace=workspace, name="Foreign", identifier="FOREIGN")
    foreign = Module.objects.create(project=other, name="Foreign module")
    local = Module.objects.create(project=project, name="Local module")
    response = client.patch(
        prefix + f"work-items/{issue.pk}/modules/", {"added": [str(local.pk), str(foreign.pk)]}, format="json"
    )
    assert response.status_code == 400, response.data
    assert not ModuleIssue.objects.filter(issue=issue).exists()
