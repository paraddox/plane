# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from .base import BaseSerializer
from plane.db.models import APIToken, APIActivityLog, Project, ProjectMember, WorkspaceMember
from rest_framework import serializers
from django.utils import timezone


class APITokenSerializer(BaseSerializer):
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=("read", "write")), required=False, allow_empty=False
    )
    project = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), allow_null=True, required=False)

    class Meta:
        model = APIToken
        exclude = ("token",)
        read_only_fields = [
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "deleted_at",
            "user",
            "is_active",
            "last_used",
            "user_type",
            "is_service",
            "allowed_rate_limit",
            "purpose",
            "delegated_by",
            "agent_run",
            "capabilities",
        ]

    def validate(self, attrs):
        user = self.context["request"].user
        workspace = attrs.get("workspace", self.instance.workspace if self.instance else None)
        project = attrs.get("project", self.instance.project if self.instance else None)
        if project:
            if workspace and project.workspace_id != workspace.pk:
                raise serializers.ValidationError({"project": "Project must belong to the selected workspace."})
            workspace = project.workspace
            attrs["workspace"] = workspace
            if not ProjectMember.objects.filter(project=project, member=user, is_active=True).exists():
                raise serializers.ValidationError({"project": "An active project membership is required."})
        if workspace and not WorkspaceMember.objects.filter(workspace=workspace, member=user, is_active=True).exists():
            raise serializers.ValidationError({"workspace": "An active workspace membership is required."})
        return attrs


class APITokenReadSerializer(BaseSerializer):
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = APIToken
        exclude = ("token",)

    def get_is_active(self, obj: APIToken) -> bool:
        return obj.is_active and (obj.expired_at is None or timezone.now() < obj.expired_at)


class APIActivityLogSerializer(BaseSerializer):
    class Meta:
        model = APIActivityLog
        fields = "__all__"
