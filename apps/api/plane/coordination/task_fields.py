# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

from crum import get_current_request
from django.db import transaction
from rest_framework import serializers

from plane.db.models import Issue, ProjectMember, ProjectRelease, User, WorkItemContract, WorkspaceMember
from .contracts import (
    CoordinationConflict,
    CriterionSerializer,
    edit_contract,
    emit_event,
    ensure_task_contract,
    require_editor,
)


class TaskIntentSerializer(serializers.Serializer):
    requirements = serializers.CharField(required=False, allow_blank=True)
    definition_of_done = CriterionSerializer(many=True, required=False)
    release_id = serializers.PrimaryKeyRelatedField(
        queryset=ProjectRelease.objects.all(), required=False, allow_null=True
    )

    def validate_definition_of_done(self, criteria):
        result = [{"id": str(item.get("id") or uuid4()), "description": item["description"]} for item in criteria]
        if len({item["id"] for item in result}) != len(result):
            raise serializers.ValidationError("Criterion IDs must be unique.")
        return result

    def validate_release_id(self, release):
        project_id = self.context.get("project_id", self.instance.project_id if self.instance else None)
        if release and str(release.project_id) != str(project_id):
            raise serializers.ValidationError("Release must belong to this project.")
        return release

    def validate_human_assignees(self, values):
        identifiers = {getattr(value, "pk", value) for value in values}
        project = self.context.get("project_id", self.instance.project_id if self.instance else None)
        members = ProjectMember.objects.filter(project_id=project, is_active=True, role__gte=15).values("member_id")
        workspace_members = WorkspaceMember.objects.filter(
            workspace__workspace_project__pk=project, is_active=True
        ).values("member_id")
        humans = User.objects.filter(pk__in=identifiers, is_bot=False, is_active=True)
        if humans.filter(pk__in=members).filter(pk__in=workspace_members).count() != len(identifiers):
            raise serializers.ValidationError("Assignees must be active human members of this project.")
        return values

    def validate_assignee_ids(self, values):
        return self.validate_human_assignees(values)

    def validate_assignees(self, values):
        return self.validate_human_assignees(values)


class TaskFieldsSerializer(TaskIntentSerializer):
    spec_revision = serializers.IntegerField(read_only=True)
    expected_revision = serializers.IntegerField(required=False, write_only=True, min_value=1)

    def task_values(self, values):
        intent = {name: values.pop(name) for name in ("requirements", "definition_of_done") if name in values}
        if "release_id" in values:
            intent["release"] = values.pop("release_id")
        return intent

    @transaction.atomic
    def save(self, **kwargs):
        original = self.validated_data
        values = dict(original)
        intent = self.task_values(values)
        expected = values.pop("expected_revision", None)
        creating = self.instance is None
        contract = None
        description_changed = False
        request = self.context.get("request") or get_current_request()
        if not creating:
            self.instance = Issue.objects.select_for_update().get(pk=self.instance.pk)
            contract = WorkItemContract.objects.select_for_update().filter(issue=self.instance).first()
            description_changed = (
                "description_html" in values and values["description_html"] != self.instance.description_html
            )
            if intent or description_changed or "assignee_ids" in values or "assignees" in values:
                if hasattr(self.instance, "agent_child"):
                    raise CoordinationConflict(
                        "This subtask's scope and ownership are managed by its parent execution."
                    )
                if contract and contract.status in ("merging", "merged"):
                    raise CoordinationConflict("A merging or completed task cannot change product intent or ownership.")
                if request is None:
                    raise serializers.ValidationError("Task intent changes require an authenticated request.")
                require_editor(request, self.instance.project, contract=contract, issue=self.instance)
        # Keep task intent out of the model constructor; the versioned specification is its single store.
        self._validated_data = values
        try:
            issue = super().save(**kwargs)
        finally:
            self._validated_data = original
        if creating:
            contract = ensure_task_contract(issue)
            if contract and intent:
                for name, value in intent.items():
                    setattr(contract, name, value)
                contract.save()
        elif intent or description_changed:
            if "release" in intent:
                intent["release"] = str(intent["release"].pk) if intent["release"] else None
            intent["expected_revision"] = expected if intent else (contract.spec_revision if contract else None)
            edit_contract(request, issue.project, issue.pk, intent, material_change=description_changed)
        elif (
            contract
            and not issue.is_draft
            and (
                contract.status == "draft"
                or (
                    contract.status == "cancelled"
                    and "state" in values
                    and values["state"].group in ("backlog", "unstarted", "started")
                )
            )
        ):
            contract.status = "ready"
            contract.save(update_fields=["status", "updated_at"])
            emit_event(issue.project, "task.ready", actor=request.user, issue=issue, revision=contract.spec_revision)
        issue._state.fields_cache.pop("coordination_contract", None)
        return issue
