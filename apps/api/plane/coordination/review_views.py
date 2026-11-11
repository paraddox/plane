# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from plane.db.models import MergeAttempt, ReviewRequirement
from .execution import RunSerializer
from .merges import MergeInputSerializer, reconcile_merge, request_merge
from .reviews import (
    DecisionInputSerializer,
    ReviewDecisionSerializer,
    ReviewRequirementSerializer,
    ReviewersInputSerializer,
    decide_review,
    mark_review_ready,
    request_reviewers,
)
from .views import CoordinationEndpoint, MachineCoordinationMixin


class MergeAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = MergeAttempt
        fields = [
            "id",
            "run",
            "head_sha",
            "code_revision",
            "pull_request",
            "status",
            "merge_sha",
            "error",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields


class ReviewersEndpoint(CoordinationEndpoint):
    serializer_class = ReviewRequirementSerializer
    queryset = ReviewRequirement.objects.none()

    @extend_schema(
        tags=["Agent Reviews"], request=ReviewersInputSerializer, responses=ReviewRequirementSerializer(many=True)
    )
    def put(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        return Response(
            ReviewRequirementSerializer(request_reviewers(request, project, issue_id, request.data), many=True).data
        )


class MachineReviewersEndpoint(MachineCoordinationMixin, ReviewersEndpoint):
    pass


class DecisionEndpoint(CoordinationEndpoint):
    serializer_class = ReviewDecisionSerializer

    @extend_schema(tags=["Agent Reviews"], request=DecisionInputSerializer, responses={201: ReviewDecisionSerializer})
    def post(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        return Response(
            ReviewDecisionSerializer(decide_review(request, project, issue_id, request.data)).data, status=201
        )


class MachineDecisionEndpoint(MachineCoordinationMixin, DecisionEndpoint):
    pass


class ReviewReadyEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "review_ready"
    serializer_class = RunSerializer

    @extend_schema(tags=["Agent Reviews"], request=None, responses=RunSerializer)
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        return Response(RunSerializer(mark_review_ready(request, project, run_id)).data)


class MergeEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "merge"
    serializer_class = RunSerializer
    idempotency_unsupported_methods = {"POST"}

    @extend_schema(
        tags=["Agent Reviews"], request=MergeInputSerializer, responses={200: RunSerializer, 202: RunSerializer}
    )
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        run = request_merge(request, project, run_id)
        return Response(RunSerializer(run).data, status=200 if run.status == "merged" else 202)


class ReconcileEndpoint(CoordinationEndpoint):
    coordination_action = "reconcile"
    serializer_class = RunSerializer
    idempotency_unsupported_methods = {"POST"}

    @extend_schema(tags=["Agent Reviews"], request=None, responses={200: RunSerializer, 202: RunSerializer})
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        run = reconcile_merge(request, project, run_id)
        return Response(RunSerializer(run).data, status=200 if run.status == "merged" else 202)


class MachineReconcileEndpoint(MachineCoordinationMixin, ReconcileEndpoint):
    pass
