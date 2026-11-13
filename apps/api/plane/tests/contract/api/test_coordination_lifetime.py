from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import APIToken, AgentRun, State
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import claim, execution as execution, worker_client
from .test_coordination_handoff import handoff, refresh


@pytest.mark.django_db
def test_project_policy_needs_no_repository_or_durations(execution):
    _, human, _, _, _, _, _, _, _, prefix = execution
    url = prefix.replace("/api/v1/", "/api/") + "policy/"
    response = human.put(url, {"enabled": True, "required_checks": ["tests"]}, format="json")
    assert response.status_code == 200, response.data
    assert set(response.data) == {"project", "enabled", "context_references", "required_checks"}
    assert response.data["required_checks"] == ["tests"]


@pytest.mark.django_db
def test_claim_uses_agent_repository_and_target_without_project_setup(execution):
    dispatcher, _, _, issue, _, _, _, worker, _, prefix = execution
    response = dispatcher.post(
        prefix + f"work-items/{issue.pk}/claim/",
        {"agent": str(worker.pk), "worker_id": "external", "repository": "other/project", "base_branch": "develop"},
        format="json",
    )
    assert response.status_code == 201, response.data
    assert response.data["repository"] == "other/project"
    assert response.data["base_branch"] == "develop"
    assert "lease_expires_at" not in response.data


@pytest.mark.django_db
def test_execution_key_stays_valid_while_work_item_is_open(execution, monkeypatch):
    run_id = claim(execution)
    worker, response = worker_client(execution, run_id)
    assert APIToken.objects.get(pk=response.data["id"]).expired_at is None
    # Advance past the old configured expiry and lease without refreshing keys.
    later = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    prefix = execution[-1]
    assert worker.get(prefix + f"runs/{run_id}/").status_code == 200
    assert worker.post(prefix + f"runs/{run_id}/heartbeat/", {}, format="json").status_code == 200
    assert execution[0].get(prefix + "candidates/").data["results"] == []


@pytest.mark.django_db
def test_closing_task_revokes_assignment_and_execution_permanently(execution):
    assignment, run_id, grant = handoff(execution)
    issued = refresh(execution, assignment, run_id)
    assert issued.status_code == 201, issued.data
    worker = APIClient()
    worker.credentials(HTTP_X_API_KEY=issued.data["token"])
    _, human, project, issue, _, _, _, _, _, prefix = execution
    cancelled = State.objects.create(project=project, name="Cancelled", group="cancelled", color="#777777")
    detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    response = human.patch(detail, {"state": str(cancelled.pk)}, format="json")
    assert response.status_code == 200, response.data
    assert not APIToken.objects.filter(pk__in=[grant.data["id"], issued.data["id"]], is_active=True).exists()
    assert AgentRun.objects.get(pk=run_id).status == "cancelled"
    assert worker.get(prefix + f"runs/{run_id}/").status_code == 403
    assert refresh(execution, assignment, run_id).status_code == 403
    todo = State.objects.get(project=project, group="unstarted")
    assert human.patch(detail, {"state": str(todo.pk)}, format="json").status_code == 200
    assert worker.get(prefix + f"runs/{run_id}/").status_code == 403
    assert refresh(execution, assignment, run_id).status_code == 403
