# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from unittest.mock import Mock, patch

import pytest
from django.db import transaction

from plane.utils.tasks import delay_on_commit
from plane.db.models import Workspace


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_task_is_published_after_commit():
    task = Mock()
    with transaction.atomic():
        delay_on_commit(task, "object-id", event="created")
        task.delay.assert_not_called()
    task.delay.assert_called_once_with("object-id", event="created")


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_rollback_does_not_publish_task():
    task = Mock()
    with pytest.raises(RuntimeError), transaction.atomic():
        delay_on_commit(task, "object-id")
        raise RuntimeError("The write failed")
    task.delay.assert_not_called()


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_autocommit_preserves_immediate_dispatch():
    task = Mock()
    delay_on_commit(task, "object-id")
    task.delay.assert_called_once_with("object-id")


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_soft_delete_cascade_is_published_after_commit(workspace):
    with patch("plane.db.mixins.soft_delete_related_objects.delay") as publish:
        with transaction.atomic():
            workspace.delete()
            publish.assert_not_called()
        publish.assert_called_once_with("db", "workspace", workspace.pk, using=None)


@pytest.mark.unit
@pytest.mark.django_db(transaction=True)
def test_rolled_back_soft_delete_does_not_publish_cascade(workspace):
    with patch("plane.db.mixins.soft_delete_related_objects.delay") as publish:
        with pytest.raises(RuntimeError), transaction.atomic():
            workspace.delete()
            raise RuntimeError("The mutation failed")
        publish.assert_not_called()
    assert Workspace.objects.filter(pk=workspace.pk).exists()
