from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import AgentRun, APIToken, Issue, State, WorkItemContract
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import claim, execution as execution, worker_client


def handoff(execution):
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    response = dispatcher.post(prefix + f"runs/{run_id}/handoff/", {}, format="json")
    assert response.status_code == 201, getattr(response, "data", None)
    worker = APIClient()
    worker.credentials(HTTP_X_API_KEY=response.data["token"])
    return worker, run_id, response


def refresh(execution, worker, run_id, revision=1):
    prefix = execution[-1]
    return worker.post(prefix + f"runs/{run_id}/refresh/", {"expected_revision": revision}, format="json")


@pytest.mark.django_db
def test_handed_off_worker_refreshes_after_idle_without_dispatcher(execution, monkeypatch):
    worker, run_id, grant = handoff(execution)
    later = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    response = refresh(execution, worker, run_id)
    assert response.status_code == 201, response.data
    assert response["Cache-Control"] == "no-store"
    assert APIToken.objects.get(pk=grant.data["id"]).purpose == "assignment"
    token = APIToken.objects.get(pk=response.data["id"])
    assert token.purpose == "execution" and str(token.agent_run_id) == str(run_id)
    assert token.expired_at is None
    assert execution[0].get(execution[-1] + "candidates/").data["results"] == []


@pytest.mark.django_db
def test_assignment_reads_context_but_cannot_execute_or_delegate(execution):
    worker, run_id, _ = handoff(execution)
    _, _, project, issue, _, _, _, _, _, prefix = execution
    assert worker.get(prefix + f"work-items/{issue.pk}/overview/").status_code == 200
    assert worker.get(prefix + "events/").status_code == 403
    assert worker.post(prefix + f"runs/{run_id}/credentials/", {}, format="json").status_code == 403
    assert worker.post(prefix + f"runs/{run_id}/merge/", {}, format="json").status_code == 403
    comments = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/comments/"
    assert worker.post(comments, {"comment_html": "<p>Denied</p>"}, format="json").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["requirements", "definition_of_done", "description_html"])
def test_spec_edit_preserves_assignment_and_requires_explicit_revision_refresh(execution, field):
    worker, run_id, _ = handoff(execution)
    response = refresh(execution, worker, run_id)
    assert response.status_code == 201, response.data
    old_execution = APIClient()
    old_execution.credentials(HTTP_X_API_KEY=response.data["token"])
    _, human, project, issue, _, _, _, _, _, prefix = execution
    detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    value = [{"description": "Revised acceptance criterion"}] if field == "definition_of_done" else "Revised scope"
    edited = human.patch(detail, {field: value, "expected_revision": 1}, format="json")
    assert edited.status_code == 200, edited.data
    assert AgentRun.objects.get(pk=run_id).status == "active"
    assert WorkItemContract.objects.get(issue=issue).status == "in_progress"
    assert old_execution.get(prefix + f"runs/{run_id}/").status_code == 403
    assert worker.get(prefix + f"work-items/{issue.pk}/overview/").status_code == 200
    assert refresh(execution, worker, run_id, revision=1).status_code == 409
    updated = refresh(execution, worker, run_id, revision=2)
    assert updated.status_code == 201, updated.data
    run = AgentRun.objects.get(pk=run_id)
    assert run.spec_revision == 2
    if field == "definition_of_done":
        assert run.spec_snapshot[field][0]["description"] == "Revised acceptance criterion"
    else:
        assert run.spec_snapshot[field] == value
    assert run.head_sha == "" and run.pull_request is None


@pytest.mark.django_db
@pytest.mark.parametrize("revocation", ["cancel", "parent"])
def test_assignment_cannot_refresh_after_revocation(execution, revocation):
    worker, run_id, _ = handoff(execution)
    _, human, _, _, _, _, _, _, parent, prefix = execution
    if revocation == "cancel":
        response = human.post(prefix.replace("/api/v1/", "/api/") + f"runs/{run_id}/cancel/", {}, format="json")
        assert response.status_code == 200, response.data
    else:
        APIToken.objects.filter(pk=parent.pk).update(is_active=False)
    assert refresh(execution, worker, run_id).status_code == 403


@pytest.mark.django_db
def test_handoff_and_refresh_never_store_plaintext_in_idempotency_replays(execution):
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    run_id = claim(execution)
    assert (
        dispatcher.post(
            prefix + f"runs/{run_id}/handoff/", {}, format="json", HTTP_IDEMPOTENCY_KEY="secret-handoff"
        ).status_code
        == 400
    )


@pytest.mark.django_db
def test_reissued_handoff_fences_old_assignment_key(execution):
    worker, run_id, _ = handoff(execution)
    previous = refresh(execution, worker, run_id)
    assert previous.status_code == 201
    executor = APIClient()
    executor.credentials(HTTP_X_API_KEY=previous.data["token"])
    dispatcher, _, _, _, _, _, _, _, _, prefix = execution
    replacement = dispatcher.post(prefix + f"runs/{run_id}/handoff/", {}, format="json")
    assert replacement.status_code == 201, replacement.data
    assert refresh(execution, worker, run_id).status_code == 403
    assert executor.get(prefix + f"runs/{run_id}/").status_code == 403
    successor = APIClient()
    successor.credentials(HTTP_X_API_KEY=replacement.data["token"])
    assert refresh(execution, successor, run_id).status_code == 201


@pytest.mark.django_db
def test_assignment_cannot_refresh_or_read_events_for_another_run(execution):
    from uuid import uuid4

    worker, _, _ = handoff(execution)
    foreign_id = uuid4()
    assert refresh(execution, worker, foreign_id).status_code == 403
    assert worker.get(execution[-1] + f"runs/{foreign_id}/events/").status_code == 403


@pytest.mark.django_db
def test_refresh_rotation_preserves_assignment_but_fences_previous_execution(execution):
    worker, run_id, _ = handoff(execution)
    first = refresh(execution, worker, run_id)
    assert first.status_code == 201
    old_execution = APIClient()
    old_execution.credentials(HTTP_X_API_KEY=first.data["token"])
    assert refresh(execution, worker, run_id).status_code == 201
    assert old_execution.get(execution[-1] + f"runs/{run_id}/").status_code == 403
    assert worker.get(execution[-1] + f"runs/{run_id}/").status_code == 200


@pytest.mark.django_db
def test_cancelling_through_normal_task_state_closes_assignment(execution):
    worker, run_id, _ = handoff(execution)
    _, human, project, issue, _, _, _, _, _, _ = execution
    cancelled = State.objects.create(project=project, name="Cancelled", group="cancelled", color="#777777")
    detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    response = human.patch(detail, {"state": str(cancelled.pk)}, format="json")
    assert response.status_code == 200, response.data
    assert refresh(execution, worker, run_id).status_code == 403
    assert not APIToken.objects.filter(agent_run_id=run_id, is_active=True).exists()


@pytest.mark.django_db
def test_cancelling_unassigned_task_preserves_same_request_spec_edits(execution):
    _, human, project, issue, contract, _, _, _, _, _ = execution
    cancelled = State.objects.create(project=project, name="Cancelled", group="cancelled", color="#777777")
    detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    response = human.patch(
        detail,
        {
            "state": str(cancelled.pk),
            "expected_revision": 1,
            "requirements": "Final scope before cancellation",
            "definition_of_done": [{"description": "Retain the edited acceptance criterion"}],
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    contract.refresh_from_db()
    assert contract.spec_revision == 2
    assert contract.status == "cancelled"
    assert contract.requirements == "Final scope before cancellation"
    assert contract.definition_of_done[0]["description"] == "Retain the edited acceptance criterion"


@pytest.mark.django_db
@pytest.mark.parametrize("transport", ["session", "execution"])
def test_null_state_patch_uses_default_without_crashing_cancellation_logic(execution, transport):
    _, human, project, issue, _, _, _, _, _, _ = execution
    client = human
    if transport == "execution":
        client, _ = worker_client(execution, claim(execution))
        detail = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.pk}/work-items/{issue.pk}/"
    else:
        detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    client.raise_request_exception = False
    response = client.patch(detail, {"state": None}, format="json")
    assert response.status_code == 200, getattr(response, "data", None)
    assert Issue.objects.get(pk=issue.pk).state.group == "unstarted"


@pytest.mark.django_db
def test_clearing_cancelled_state_reopens_the_task_with_its_default_state(execution):
    _, human, project, issue, contract, _, _, _, _, _ = execution
    cancelled = State.objects.create(project=project, name="Cancelled", group="cancelled", color="#777777")
    detail = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/{issue.pk}/"
    assert human.patch(detail, {"state": str(cancelled.pk)}, format="json").status_code == 200
    contract.refresh_from_db()
    assert contract.status == "cancelled"
    response = human.patch(detail, {"state": None}, format="json")
    assert response.status_code == 200, response.data
    contract.refresh_from_db()
    assert contract.status == "ready"
    assert Issue.objects.get(pk=issue.pk).state.group == "unstarted"


@pytest.mark.django_db(transaction=True)
def test_concurrent_refreshes_leave_only_one_current_execution_key(execution):
    _, run_id, grant = handoff(execution)
    barrier = Barrier(2)

    def refresh_worker():
        close_old_connections()
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=grant.data["token"])
        barrier.wait()
        try:
            return refresh(execution, client, run_id).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: refresh_worker(), range(2)))
    assert statuses == [201, 201]
    assert APIToken.objects.filter(agent_run_id=run_id, purpose="assignment", is_active=True).count() == 1
    assert APIToken.objects.filter(agent_run_id=run_id, purpose="execution", is_active=True).count() == 1
