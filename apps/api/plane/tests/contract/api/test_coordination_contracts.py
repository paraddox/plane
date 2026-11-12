# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework.test import APIClient

from plane.db.models import (
    CoordinationEvent,
    Issue,
    Module,
    ModuleIssue,
    Project,
    ProjectExecutionPolicy,
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
def test_contract_owner_defaults_to_creator_and_draft_is_not_dispatched(coordination, create_user):
    client, project, issue, prefix = coordination
    response = client.put(prefix + f"work-items/{issue.pk}/contract/", specification(), format="json")
    assert response.status_code == 201, response.data
    assert response.json()["owner"] == str(create_user.pk)
    assert response.data["status"] == "draft"
    assert response.data["spec_revision"] == 1
    assert response.data["definition_of_done"][0]["id"]
    assert not CoordinationEvent.objects.filter(kind="task.ready").exists()


@pytest.mark.django_db
def test_release_and_owner_must_belong_to_the_task_project(coordination, workspace, create_user):
    client, project, issue, prefix = coordination
    other = Project.objects.create(workspace=workspace, name="Other", identifier="OTHER")
    release = ProjectRelease.objects.create(project=other, name="Other release", identifier="REL")
    outsider = User.objects.create(email="outside-contract@example.test", username="outside-contract")
    url = prefix + f"work-items/{issue.pk}/contract/"
    for payload in ({"release": str(release.pk)}, {"owner": str(outsider.pk)}):
        response = client.put(url, specification(**payload), format="json")
        assert response.status_code == 400, response.data
    assert not WorkItemContract.objects.exists()


@pytest.mark.django_db
def test_submit_requires_complete_spec_and_enabled_project_context(coordination):
    client, project, issue, prefix = coordination
    url = prefix + f"work-items/{issue.pk}/"
    assert client.put(url + "contract/", {"requirements": ""}, format="json").status_code == 201
    assert client.post(url + "submit/", {}, format="json").status_code == 400
    contract = WorkItemContract.objects.get(issue=issue)
    assert (
        client.patch(
            url + "contract/", specification(expected_revision=contract.spec_revision), format="json"
        ).status_code
        == 200
    )
    assert client.post(url + "submit/", {}, format="json").status_code == 400
    ProjectExecutionPolicy.objects.create(
        project=project,
        enabled=True,
        repositories=[{"repository": "example/product", "base_branch": "main", "required_checks": []}],
    )
    submitted = client.post(url + "submit/", {}, format="json")
    assert submitted.status_code == 200, submitted.data
    assert submitted.data["status"] == "ready"
    assert CoordinationEvent.objects.filter(project=project, kind="task.ready", issue=issue).count() == 1
    assert client.post(url + "submit/", {}, format="json").status_code == 200
    assert CoordinationEvent.objects.filter(project=project, kind="task.ready", issue=issue).count() == 1


@pytest.mark.django_db
def test_stale_edits_conflict_and_material_changes_revoke_advance_authorization(coordination):
    client, project, issue, prefix = coordination
    url = prefix + f"work-items/{issue.pk}/"
    created = client.put(url + "contract/", specification(), format="json").data
    authorized = client.put(url + "auto-merge/", {"enabled": True, "expected_revision": 1}, format="json")
    assert authorized.status_code == 200, authorized.data
    assert authorized.data["auto_merge_revision"] == 1
    unchanged = client.patch(
        url + "contract/", {"expected_revision": 1, "requirements": created["requirements"]}, format="json"
    )
    assert unchanged.status_code == 200
    assert unchanged.data["spec_revision"] == 1
    assert unchanged.data["auto_merge_authorized"] is True
    changed = client.patch(url + "contract/", {"expected_revision": 1, "requirements": "Changed scope"}, format="json")
    assert changed.status_code == 200, changed.data
    assert changed.data["spec_revision"] == 2
    assert changed.data["auto_merge_authorized"] is False
    assert (
        client.patch(url + "contract/", {"expected_revision": 1, "requirements": "Stale"}, format="json").status_code
        == 409
    )


@pytest.mark.django_db
def test_human_owned_api_key_cannot_create_edit_or_authorize_contract(coordination, api_key_client):
    client, project, issue, prefix = coordination
    url = prefix + f"work-items/{issue.pk}/"
    assert client.put(url + "contract/", specification(), format="json").status_code == 201
    machine = url.replace("/api/", "/api/v1/", 1)
    assert api_key_client.get(machine + "contract/").status_code == 200
    for suffix, method, payload in (
        ("contract/", "patch", {"expected_revision": 1, "requirements": "Machine replacement"}),
        ("auto-merge/", "put", {"enabled": True, "expected_revision": 1}),
        ("submit/", "post", {}),
    ):
        response = getattr(api_key_client, method)(machine + suffix, payload, format="json")
        assert response.status_code == 403, response.data
    contract = WorkItemContract.objects.get(issue=issue)
    assert contract.requirements == specification()["requirements"]
    assert contract.auto_merge_authorized is False


@pytest.mark.django_db
def test_session_mutations_require_csrf(coordination, create_user):
    _, project, issue, prefix = coordination
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(create_user)
    assert client.put(prefix + f"work-items/{issue.pk}/contract/", specification(), format="json").status_code == 403


@pytest.mark.django_db
def test_release_identifiers_are_project_local_and_machine_cannot_administer_them(
    coordination, workspace, api_key_client
):
    client, project, issue, prefix = coordination
    first = client.post(prefix + "releases/", {"name": "First", "identifier": "R1"}, format="json")
    assert first.status_code == 201, first.data
    other = Project.objects.create(workspace=workspace, name="Second", identifier="SECOND")
    ProjectRelease.objects.create(project=other, name="Second", identifier="R1")
    response = client.get(prefix + "releases/")
    assert [entry["id"] for entry in response.data] == [first.data["id"]]
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
    url = prefix + f"work-items/{issue.pk}/contract/"
    assert client.put(url, specification(), format="json").status_code == 201
    WorkspaceMember.objects.filter(workspace=project.workspace, member=create_user).update(is_active=False)
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_non_owner_member_cannot_take_over_existing_contract(coordination, workspace):
    client, project, issue, prefix = coordination
    url = prefix + f"work-items/{issue.pk}/contract/"
    assert client.put(url, specification(), format="json").status_code == 201
    other = User.objects.create(email="other-contract@example.test", username="other-contract")
    WorkspaceMember.objects.create(workspace=workspace, member=other, role=15)
    ProjectMember.objects.create(project=project, member=other, role=15)
    client.force_login(other)
    assert client.patch(url, {"expected_revision": 1, "owner": str(other.pk)}, format="json").status_code == 403


@pytest.mark.django_db
def test_agent_can_add_multiple_modules_without_moving_the_task(coordination, api_key_client, monkeypatch):
    client, project, issue, prefix = coordination
    monkeypatch.setattr("plane.bgtasks.issue_activities_task.issue_activity.delay", lambda **kwargs: None)
    modules = [Module.objects.create(project=project, name=name) for name in ("Frontend", "Backend")]
    for module in modules:
        url = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/modules/{module.pk}/module-issues/"
        assert api_key_client.post(url, {"issues": [str(issue.pk)]}, format="json").status_code == 200
    assert set(ModuleIssue.objects.filter(issue=issue).values_list("module_id", flat=True)) == {m.pk for m in modules}
    url = prefix.replace("/api/", "/api/v1/", 1) + f"work-items/{issue.pk}/modules/"
    response = api_key_client.patch(url, {"removed": [str(modules[0].pk)]}, format="json")
    assert response.status_code == 200, response.data
    assert ModuleIssue.objects.get(issue=issue).module_id == modules[1].pk


@pytest.mark.django_db
def test_module_assignment_rejects_other_project_and_rolls_back(coordination, workspace, api_key_client):
    client, project, issue, prefix = coordination
    other = Project.objects.create(workspace=workspace, name="Foreign", identifier="FOREIGN")
    foreign = Module.objects.create(project=other, name="Foreign module")
    local = Module.objects.create(project=project, name="Local module")
    url = prefix.replace("/api/", "/api/v1/", 1) + f"work-items/{issue.pk}/modules/"
    response = api_key_client.patch(url, {"added": [str(local.pk), str(foreign.pk)]}, format="json")
    assert response.status_code == 400, response.data
    assert not ModuleIssue.objects.filter(issue=issue).exists()
