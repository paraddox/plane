# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from plane.bgtasks.deletion_task import hard_delete, soft_delete_related_objects
from plane.db.models import Issue, IssueComment, IssueLink, RunChild, WorkItemQuestion
from .test_coordination_contracts import coordination as coordination
from .test_coordination_execution import execution as execution, execution_child as execution_child


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
