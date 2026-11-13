import pytest
from rest_framework.test import APIClient

from plane.db.models import APIToken, AgentProjectAccess, Project, ProjectMember, User, WorkspaceMember
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import execution as execution


def create_main(human, prefix, name="Project main agent"):
    response = human.post(prefix + "agents/", {"name": name}, format="json")
    assert response.status_code == 201, getattr(response, "data", None)
    return response


@pytest.mark.django_db
def test_admin_creates_project_main_agent_with_a_one_time_hashed_key(coordination, create_user):
    human, project, _, prefix = coordination
    created = create_main(human, prefix)
    assert created["Cache-Control"] == "no-store"
    agent = created.data["agent"]
    assert agent["name"] == "Project main agent"
    assert agent["role"] == "dispatcher" and agent["is_active"]
    user = User.objects.get(pk=agent["agent"])
    assert user.is_bot and not user.has_usable_password()
    assert ProjectMember.objects.filter(project=project, member=user, role=15, is_active=True).exists()
    assert WorkspaceMember.objects.filter(workspace=project.workspace, member=user, is_active=True).exists()
    access = AgentProjectAccess.objects.get(pk=agent["id"])
    assert access.created_by_id == create_user.pk
    key = APIToken.objects.get(pk=created.data["credential"]["id"])
    raw = created.data["credential"]["token"]
    assert key.token.startswith("sha256$") and key.token != raw
    assert key.project_id == project.pk and key.workspace_id == project.workspace_id
    assert key.user_id == user.pk and key.created_by_id == create_user.pk
    assert key.purpose == "dispatcher" and key.is_service and key.expired_at is None
    assert set(key.scopes) == {"read", "write"}
    listed = human.get(prefix + "agents/")
    assert agent in listed.data
    keys = human.get(prefix + f"agents/{access.pk}/credentials/")
    assert keys.status_code == 200, keys.data
    assert keys["Cache-Control"] == "no-store"
    assert keys.data[0]["id"] == str(key.pk)
    assert "token" not in keys.data[0] and raw not in str(keys.data)


@pytest.mark.django_db
@pytest.mark.parametrize("role", [5, 15])
def test_project_members_cannot_create_main_agents_or_manage_keys(coordination, create_user, role):
    human, project, _, prefix = coordination
    created = create_main(human, prefix)
    access = created.data["agent"]["id"]
    key = created.data["credential"]["id"]
    ProjectMember.objects.filter(project=project, member=create_user).update(role=role)
    assert human.post(prefix + "agents/", {"name": "Denied"}, format="json").status_code == 403
    assert human.get(prefix + f"agents/{access}/credentials/").status_code == 403
    assert human.post(prefix + f"agents/{access}/credentials/", {}, format="json").status_code == 403
    assert human.delete(prefix + f"agents/{access}/credentials/{key}/").status_code == 403
    assert APIToken.objects.get(pk=key).is_active


@pytest.mark.django_db
def test_main_agent_keys_are_usable_but_cannot_administer_agents(execution, api_key_client):
    _, human, project, _, _, _, _, _, _, machine = execution
    prefix = machine.replace("/api/v1/", "/api/")
    created = create_main(human, prefix)
    main = APIClient()
    main.credentials(HTTP_X_API_KEY=created.data["credential"]["token"])
    assert main.get(machine + "candidates/").status_code == 200
    for client in (main, api_key_client):
        assert client.post(machine + "agents/", {"name": "Denied"}, format="json").status_code == 403
    access = created.data["agent"]["id"]
    assert main.post(prefix + f"agents/{access}/credentials/", {}, format="json").status_code in (401, 403)


@pytest.mark.django_db
def test_issued_keys_are_independent_and_revocation_invalidates_only_the_selected_key(coordination):
    human, _, _, prefix = coordination
    created = create_main(human, prefix)
    access = created.data["agent"]["id"]
    first = created.data["credential"]
    issued = human.post(prefix + f"agents/{access}/credentials/", {}, format="json")
    assert issued.status_code == 201, issued.data
    assert issued["Cache-Control"] == "no-store"
    assert issued.data["token"] != first["token"]
    url = prefix + f"agents/{access}/credentials/{first['id']}/"
    assert human.delete(url).status_code == 204
    assert human.delete(url).status_code == 204
    assert not APIToken.objects.get(pk=first["id"]).is_active
    assert APIToken.objects.get(pk=issued.data["id"]).is_active
    machine = prefix.replace("/api/", "/api/v1/")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=first["token"])
    assert client.get(machine + "agents/").status_code == 403
    client.credentials(HTTP_X_API_KEY=issued.data["token"])
    assert client.get(machine + "agents/").status_code == 200


@pytest.mark.django_db
def test_admin_cannot_manage_another_projects_agent_key(coordination, create_user):
    human, project, _, prefix = coordination
    created = create_main(human, prefix)
    other = Project.objects.create(workspace=project.workspace, name="Other", identifier="OTHER")
    ProjectMember.objects.create(project=other, member=create_user, role=20)
    other_prefix = prefix.replace(str(project.pk), str(other.pk))
    access = created.data["agent"]["id"]
    key = created.data["credential"]["id"]
    assert human.get(other_prefix + f"agents/{access}/credentials/").status_code == 404
    assert human.post(other_prefix + f"agents/{access}/credentials/", {}, format="json").status_code == 404
    assert human.delete(other_prefix + f"agents/{access}/credentials/{key}/").status_code == 404
    assert APIToken.objects.get(pk=key).is_active


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["", "   "])
def test_main_agent_needs_a_nonblank_name(coordination, name):
    human, _, _, prefix = coordination
    before = User.objects.count()
    assert human.post(prefix + "agents/", {"name": name}, format="json").status_code == 400
    assert User.objects.count() == before


@pytest.mark.django_db
def test_registration_and_key_issuance_reject_secret_replay_storage(coordination):
    human, _, _, prefix = coordination
    assert (
        human.post(prefix + "agents/", {"name": "Denied"}, format="json", HTTP_IDEMPOTENCY_KEY="secret").status_code
        == 400
    )
    assert not AgentProjectAccess.objects.exists()
    created = create_main(human, prefix)
    access = created.data["agent"]["id"]
    before = APIToken.objects.count()
    response = human.post(prefix + f"agents/{access}/credentials/", {}, format="json", HTTP_IDEMPOTENCY_KEY="secret")
    assert response.status_code == 400
    assert APIToken.objects.count() == before


@pytest.mark.django_db
def test_agent_registration_requires_csrf(coordination, create_user):
    _, _, _, prefix = coordination
    human = APIClient(enforce_csrf_checks=True)
    human.force_login(create_user)
    assert human.post(prefix + "agents/", {"name": "Denied"}, format="json").status_code == 403


@pytest.mark.django_db
def test_ui_main_agent_hands_off_work_and_revocation_fences_its_worker(execution):
    _, human, _, issue, _, _, _, worker, _, machine = execution
    prefix = machine.replace("/api/v1/", "/api/")
    created = create_main(human, prefix)
    main = APIClient()
    main.credentials(HTTP_X_API_KEY=created.data["credential"]["token"])
    claimed = main.post(
        machine + f"work-items/{issue.pk}/claim/",
        {
            "agent": str(worker.pk),
            "worker_id": "ui-created-main",
            "repository": "example/product",
            "base_branch": "main",
        },
        format="json",
    )
    assert claimed.status_code == 201, claimed.data
    run = claimed.data["id"]
    handed_off = main.post(machine + f"runs/{run}/handoff/", {}, format="json")
    assert handed_off.status_code == 201, handed_off.data
    assigned = APIClient()
    assigned.credentials(HTTP_X_API_KEY=handed_off.data["token"])
    execution_key = assigned.post(machine + f"runs/{run}/refresh/", {"expected_revision": 1}, format="json")
    assert execution_key.status_code == 201, execution_key.data
    active_worker = APIClient()
    active_worker.credentials(HTTP_X_API_KEY=execution_key.data["token"])
    assert active_worker.post(machine + f"runs/{run}/heartbeat/", {}, format="json").status_code == 200
    access = created.data["agent"]["id"]
    key = created.data["credential"]["id"]
    assert human.delete(prefix + f"agents/{access}/credentials/{key}/").status_code == 204
    assert active_worker.post(machine + f"runs/{run}/heartbeat/", {}, format="json").status_code == 403
    assert assigned.post(machine + f"runs/{run}/refresh/", {"expected_revision": 1}, format="json").status_code == 403
