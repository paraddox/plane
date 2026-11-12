# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from plane.bgtasks.deletion_task import _hard_delete_unrestricted, hard_delete, soft_delete_related_objects
from plane.coordination.github import ProviderUnavailable
from plane.db.models import (
    AgentRun,
    Issue,
    IssueComment,
    IssueLink,
    MergeAttempt,
    Project,
    RunChild,
    WorkItemContract,
    WorkItemQuestion,
    Workspace,
)
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import execution as execution, execution_child as execution_child
from .test_coordination_merges import MERGE_INPUT, advance, merge_flow as merge_flow


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("source", ["child", "question", "answer"])
def test_retention_preserves_coordination_references_without_blocking_other_rows(
    execution, execution_child, monkeypatch, source
):
    _, human, project, root, contract, _, _, _, _, prefix = execution
    agent, _, child = execution_child
    monkeypatch.setattr(soft_delete_related_objects, "delay", soft_delete_related_objects.run)
    base = f"/api/workspaces/{project.workspace.slug}/projects/{project.pk}/issues/"
    if source == "child":
        model, retained_id = Issue, child.pk
        deleted = human.delete(base + f"{child.pk}/")
        ordinary = Issue.objects.create(project=project, name="Expired ordinary task")
        canonical = RunChild.objects.get(issue=child)
    else:
        question = agent.post(
            prefix + f"work-items/{root.pk}/questions/", {"text": "Canonical question"}, format="json"
        )
        assert question.status_code == 201, question.data
        canonical = WorkItemQuestion.objects.get(pk=question.data["id"])
        retained_id = canonical.question_comment_id
        if source == "answer":
            answer = human.post(
                prefix.replace("/api/v1/", "/api/", 1) + f"work-items/{root.pk}/questions/{canonical.pk}/answer/",
                {"text": "Canonical answer"},
                format="json",
            )
            assert answer.status_code == 200, answer.data
            retained_id = answer.data["answer_comment"]
        model = IssueComment
        deleted = human.delete(base + f"{root.pk}/comments/{retained_id}/")
        ordinary = IssueComment.objects.create(
            project=project, issue=root, actor=contract.owner, comment_html="<p>Expired</p>"
        )
    assert deleted.status_code == 204, getattr(deleted, "data", None)
    expired = timezone.now() - timedelta(days=settings.HARD_DELETE_AFTER_DAYS + 1)
    ordinary.delete()
    model.all_objects.filter(pk__in=[retained_id, ordinary.pk]).update(deleted_at=expired)
    later = IssueLink.objects.create(project=project, issue=root, url="https://example.com/expired")
    IssueLink.objects.filter(pk=later.pk).update(deleted_at=expired)

    hard_delete.run()

    assert model.all_objects.filter(pk=retained_id).exists()
    assert not model.all_objects.filter(pk=ordinary.pk).exists()
    assert not IssueLink.all_objects.filter(pk=later.pk).exists()
    canonical.refresh_from_db()
    assert canonical.deleted_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("source", ["workspace", "project", "contract"])
def test_retention_preserves_pending_merges_without_blocking_unrelated_cleanup(merge_flow, source):
    execution, agent, run_id, provider, _, _, _ = merge_flow
    _, _, project, root, contract, _, _, _, _, prefix = execution
    advance(merge_flow)
    provider.failure = ProviderUnavailable()
    response = agent.post(prefix + f"runs/{run_id}/merge/", MERGE_INPUT, format="json")
    assert response.status_code == 202, response.data
    if source == "workspace":
        model, retained = Workspace, project.workspace
        ordinary = Workspace.objects.create(
            name="Expired ordinary workspace", slug="expired-workspace", owner=contract.owner
        )
    elif source == "project":
        model, retained = Project, project
        ordinary = Project.objects.create(
            workspace=project.workspace, name="Expired ordinary project", identifier="EXPIRED"
        )
    else:
        model, retained = WorkItemContract, contract
        ordinary_issue = Issue.objects.create(project=project, name="Ordinary draft task")
        ordinary = WorkItemContract.objects.create(project=project, issue=ordinary_issue, owner=contract.owner)
    expired = timezone.now() - timedelta(days=settings.HARD_DELETE_AFTER_DAYS + 1)
    model.all_objects.filter(pk__in=[retained.pk, ordinary.pk]).update(deleted_at=expired)
    later = IssueLink.objects.create(project=project, issue=root, url="https://example.com/expired-pending")
    IssueLink.objects.filter(pk=later.pk).update(deleted_at=expired)

    hard_delete.run()

    assert model.all_objects.filter(pk=retained.pk).exists()
    assert not model.all_objects.filter(pk=ordinary.pk).exists()
    assert not IssueLink.all_objects.filter(pk=later.pk).exists()
    assert AgentRun.all_objects.get(pk=run_id).status == "merging"
    assert MergeAttempt.all_objects.get(run_id=run_id).status == "uncertain"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("sqlstate", "message"),
    [
        ("23514", "Unrelated retention invariant"),
        ("23503", "Reconcile the in-flight merge before deleting task history"),
    ],
)
def test_retention_does_not_suppress_unrelated_database_errors(sqlstate, message):
    cause = Exception(message)
    cause.sqlstate = sqlstate
    cause.diag = SimpleNamespace(message_primary=message)
    error = IntegrityError(message)
    error.__cause__ = cause
    queryset = Mock()
    queryset.delete.side_effect = error
    with pytest.raises(IntegrityError, match=message):
        _hard_delete_unrestricted(queryset)
    queryset.delete.assert_called_once()
