# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from plane.app.permissions import ProjectEntityPermission
from plane.db.models import Issue
from .base import BaseAPIView
from .issue import IssueSerializer, update_work_item


class BulkWorkItemUpdateSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    data = serializers.DictField()
    if_match = serializers.CharField(required=False)


class BulkWorkItemRequestSerializer(serializers.Serializer):
    updates = BulkWorkItemUpdateSerializer(many=True, allow_empty=False)

    def validate_updates(self, updates):
        identifiers = [entry["id"] for entry in updates]
        if len(set(identifiers)) != len(identifiers):
            raise serializers.ValidationError("Each work item may appear only once.")
        return updates


class BulkWorkItemAPIEndpoint(BaseAPIView):
    permission_classes = [ProjectEntityPermission]
    serializer_class = BulkWorkItemRequestSerializer
    queryset = Issue.objects.none()

    @extend_schema(
        operation_id="bulk_update_work_items",
        tags=["Work Items"],
        summary="Update multiple work items atomically",
        description=(
            "Apply partial updates with the same project permissions, validation, activities and webhooks as PATCH. "
            "All updates commit together; any invalid or stale item rolls back the entire request. Each entry may "
            "include if_match from the work item's ETag. The request supports Idempotency-Key."
        ),
        request=BulkWorkItemRequestSerializer,
        responses={200: IssueSerializer(many=True)},
    )
    @transaction.atomic
    def post(self, request, slug, project_id):
        serializer = BulkWorkItemRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updates = serializer.validated_data["updates"]
        # Consistent lock ordering avoids deadlocks between overlapping batches.
        list(
            Issue.objects.select_for_update()
            .filter(workspace__slug=slug, project_id=project_id, pk__in=[entry["id"] for entry in updates])
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        results = []
        for index, entry in enumerate(updates):
            response = update_work_item(request, slug, project_id, entry["id"], entry["data"], entry.get("if_match"))
            if response.status_code != 200:
                transaction.set_rollback(True)
                return Response({"index": index, "error": response.data}, status=response.status_code)
            results.append(response.data)
        return Response(results)
