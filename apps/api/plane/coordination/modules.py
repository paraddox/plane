# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from plane.bgtasks.issue_activities_task import issue_activity
from plane.db.models import Issue, Module, ModuleIssue
from plane.utils.host import base_host
from plane.utils.tasks import delay_on_commit


class ModuleAssignmentSerializer(serializers.Serializer):
    added = serializers.ListField(child=serializers.UUIDField(), default=list)
    removed = serializers.ListField(child=serializers.UUIDField(), default=list)

    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise ValidationError({name: "This field is not a module assignment input." for name in sorted(unknown)})
        if set(attrs["added"]) & set(attrs["removed"]):
            raise ValidationError("A module cannot be added and removed in the same operation.")
        return attrs


@transaction.atomic
def assign_modules(request, project, issue_id, added, removed):
    issue = get_object_or_404(Issue.objects.select_for_update(), pk=issue_id, project=project)
    target_ids = set(added) | set(removed)
    module_names = dict(Module.objects.filter(project=project, pk__in=target_ids).values_list("id", "name"))
    if len(module_names) != len(target_ids):
        raise ValidationError("All modules must belong to this project.")
    existing = set(ModuleIssue.objects.filter(issue=issue).values_list("module_id", flat=True))
    additions = set(added) - existing
    removals = set(removed) & existing
    ModuleIssue.objects.filter(issue=issue, module_id__in=removals).delete()
    ModuleIssue.objects.bulk_create(
        [
            ModuleIssue(
                issue=issue,
                module_id=module_id,
                project=project,
                workspace=project.workspace,
                created_by=request.user,
                updated_by=request.user,
            )
            for module_id in additions
        ]
    )
    for module_ids, kind in ((additions, "created"), (removals, "deleted")):
        for module_id in module_ids:
            delay_on_commit(
                issue_activity,
                type=f"module.activity.{kind}",
                requested_data=json.dumps({"module_id": str(module_id)}),
                actor_id=str(request.user.pk),
                issue_id=str(issue.pk),
                project_id=str(project.pk),
                current_instance=None if kind == "created" else json.dumps({"module_name": module_names[module_id]}),
                epoch=int(timezone.now().timestamp()),
                notification=True,
                origin=base_host(request=request, is_app=True),
            )
    return list(ModuleIssue.objects.filter(issue=issue).order_by("module_id").values_list("module_id", flat=True))
