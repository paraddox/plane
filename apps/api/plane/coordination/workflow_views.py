# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from plane.db.models import AgentRun, CoordinationEvent, WorkItemContract
from .contracts import ContractSerializer
from .execution import RunSerializer
from .reviews import ReviewDecisionSerializer, ReviewRequirementSerializer
from .review_views import MergeAttemptSerializer
from .views import CoordinationEndpoint, MachineCoordinationMixin
from .workflow import (
    AnswerInputSerializer,
    ChildInputSerializer,
    ChildSerializer,
    EvidenceInputSerializer,
    EvidenceSerializer,
    HeadInputSerializer,
    QuestionInputSerializer,
    QuestionSerializer,
    ResolveInputSerializer,
    answer_question,
    ask_question,
    create_child,
    report_evidence,
    report_head,
    resolve_question,
)


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoordinationEvent
        fields = ["id", "sequence", "kind", "issue", "actor", "spec_revision", "payload", "created_at"]
        read_only_fields = fields


class EventQuerySerializer(serializers.Serializer):
    after = serializers.IntegerField(min_value=0, default=0)


class EventPageSerializer(serializers.Serializer):
    events = EventSerializer(many=True)
    next_cursor = serializers.IntegerField()
    has_more = serializers.BooleanField()


class EventsEndpoint(CoordinationEndpoint):
    coordination_action = "events"
    serializer_class = EventPageSerializer

    @extend_schema(tags=["Agent Coordination"], parameters=[EventQuerySerializer], responses=EventPageSerializer)
    def get(self, request, slug, project_id):
        project = self.project(request)
        query = EventQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        after = query.validated_data["after"]
        per_page = self.get_per_page(request)
        if per_page <= 0:
            raise ValidationError({"per_page": "A positive page size is required."})
        rows = list(CoordinationEvent.objects.filter(project=project, sequence__gt=after)[: per_page + 1])
        page = rows[:per_page]
        return Response(
            {
                "events": EventSerializer(page, many=True).data,
                "next_cursor": page[-1].sequence if page else after,
                "has_more": len(rows) > per_page,
            }
        )


class MachineEventsEndpoint(MachineCoordinationMixin, EventsEndpoint):
    pass


class OverviewSerializer(serializers.Serializer):
    contract = ContractSerializer()
    runs = RunSerializer(many=True)
    questions = QuestionSerializer(many=True)
    children = ChildSerializer(many=True)
    evidence = EvidenceSerializer(many=True)
    reviewers = ReviewRequirementSerializer(many=True)
    decisions = ReviewDecisionSerializer(many=True)
    merges = MergeAttemptSerializer(many=True)


class OverviewEndpoint(CoordinationEndpoint):
    coordination_action = "overview"
    serializer_class = OverviewSerializer

    @extend_schema(tags=["Agent Coordination"], responses=OverviewSerializer)
    def get(self, request, slug, project_id, issue_id):
        project = self.project(request)
        contract = get_object_or_404(
            WorkItemContract, project=project, issue_id=issue_id, issue__deleted_at__isnull=True
        )
        runs = AgentRun.objects.filter(contract=contract)
        return Response(
            {
                "contract": ContractSerializer(contract).data,
                "runs": RunSerializer(runs, many=True).data,
                "questions": QuestionSerializer(contract.questions.select_related("answer_comment"), many=True).data,
                "children": ChildSerializer(contract.children.select_related("issue__state"), many=True).data,
                "reviewers": ReviewRequirementSerializer(
                    contract.review_requirements.filter(is_active=True), many=True
                ).data,
                "decisions": ReviewDecisionSerializer(contract.review_decisions.all(), many=True).data,
                "merges": MergeAttemptSerializer(
                    [
                        attempt
                        for run in runs.prefetch_related("merge_attempts")
                        for attempt in run.merge_attempts.all()
                    ],
                    many=True,
                ).data,
                "evidence": EvidenceSerializer(
                    [row for run in runs.prefetch_related("evidence__asset") for row in run.evidence.all()],
                    many=True,
                ).data,
            }
        )


class MachineOverviewEndpoint(MachineCoordinationMixin, OverviewEndpoint):
    pass


class QuestionsEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "question"
    serializer_class = QuestionSerializer

    @extend_schema(tags=["Agent Coordination"], request=QuestionInputSerializer, responses={201: QuestionSerializer})
    def post(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        return Response(QuestionSerializer(ask_question(request, project, issue_id, request.data)).data, status=201)


class AnswerEndpoint(CoordinationEndpoint):
    serializer_class = QuestionSerializer
    coordination_action = "answer"

    @extend_schema(tags=["Agent Coordination"], request=AnswerInputSerializer, responses=QuestionSerializer)
    def post(self, request, slug, project_id, issue_id, question_id):
        project = self.project(request, write=True)
        return Response(QuestionSerializer(answer_question(request, project, issue_id, question_id, request.data)).data)


class MachineAnswerEndpoint(MachineCoordinationMixin, AnswerEndpoint):
    pass


class ResolveEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "resolve"
    serializer_class = QuestionSerializer

    @extend_schema(tags=["Agent Coordination"], request=ResolveInputSerializer, responses=QuestionSerializer)
    def post(self, request, slug, project_id, issue_id, question_id):
        project = self.project(request, write=True)
        return Response(
            QuestionSerializer(resolve_question(request, project, issue_id, question_id, request.data)).data
        )


class ChildrenEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "children"
    serializer_class = ChildSerializer

    @extend_schema(tags=["Agent Coordination"], request=ChildInputSerializer, responses={201: ChildSerializer})
    def post(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        return Response(ChildSerializer(create_child(request, project, issue_id, request.data)).data, status=201)


class HeadEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "head"
    serializer_class = RunSerializer

    @extend_schema(tags=["Agent Execution"], request=HeadInputSerializer, responses=RunSerializer)
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        return Response(RunSerializer(report_head(request, project, run_id, request.data)).data)


class EvidenceEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    coordination_action = "evidence"
    serializer_class = EvidenceSerializer

    @extend_schema(tags=["Agent Execution"], request=EvidenceInputSerializer, responses={201: EvidenceSerializer})
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        return Response(EvidenceSerializer(report_evidence(request, project, run_id, request.data)).data, status=201)
