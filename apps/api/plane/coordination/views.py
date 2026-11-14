# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from plane.api.idempotency import IdempotencyMixin
from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import APIKeyRateLimitMixin
from plane.app.views.base import BaseAPIView
from plane.db.models import AgentProjectAccess, Issue, ProjectExecutionPolicy, ProjectRelease, WorkItemContract
from plane.db.models import AgentRun, APIToken

from .agents import create_main_agent, create_dispatcher_key, main_agent_access

from .contracts import (
    AdvanceAuthorizationSerializer,
    ContractSerializer,
    authorize_advance_merge,
    authorized_project,
    is_project_admin,
    is_restricted_guest,
    require_human,
)
from .modules import ModuleAssignmentSerializer, assign_modules
from .execution import (
    ClaimSerializer,
    RunSerializer,
    cancel_execution,
    claim_task,
    issue_execution_credential,
    require_dispatcher,
    handoff_execution,
    refresh_execution,
    RefreshSerializer,
)


class MachineCoordinationMixin(IdempotencyMixin, APIKeyRateLimitMixin):
    authentication_classes = [APIKeyAuthentication]


class ModuleAssignmentsResponseSerializer(serializers.Serializer):
    modules = serializers.ListField(child=serializers.UUIDField())


class CoordinationEndpoint(BaseAPIView):
    def check_idempotency_permissions(self, request, *args, **kwargs):
        project = self.project(request, write=True)
        if kwargs.get("issue_id"):
            get_object_or_404(Issue, pk=kwargs["issue_id"], project=project)

    def project(self, request, *, write=False, admin=False):
        project = authorized_project(request, self.kwargs["slug"], self.kwargs["project_id"], write=write)
        issue_id, run_id = self.kwargs.get("issue_id"), self.kwargs.get("run_id")
        if not write and (issue_id or run_id) and is_restricted_guest(request.user, project):
            visible = (
                Issue.objects.filter(project=project, pk=issue_id, created_by=request.user).exists()
                if issue_id
                else AgentRun.objects.filter(
                    project=project,
                    pk=run_id,
                    contract__issue__created_by=request.user,
                    contract__issue__deleted_at__isnull=True,
                ).exists()
            )
            if not visible:
                raise PermissionDenied("You are not allowed to view this work item.")
        if admin:
            require_human(request)
            if not is_project_admin(request.user, project):
                raise PermissionDenied("A human project administrator is required.")
        return project


class ContractEndpoint(CoordinationEndpoint):
    serializer_class = ContractSerializer
    coordination_action = "contract"
    http_method_names = ["get", "head", "options"]

    @extend_schema(tags=["Agent Coordination"], responses=ContractSerializer)
    def get(self, request, slug, project_id, issue_id):
        project = self.project(request)
        contract = get_object_or_404(
            WorkItemContract, issue_id=issue_id, project=project, issue__deleted_at__isnull=True
        )
        return Response(ContractSerializer(contract).data)


class AdvanceMergeEndpoint(CoordinationEndpoint):
    serializer_class = ContractSerializer

    @extend_schema(tags=["Agent Coordination"], request=AdvanceAuthorizationSerializer, responses=ContractSerializer)
    def put(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        contract = authorize_advance_merge(request, project, issue_id, request.data)
        return Response(ContractSerializer(contract).data)


class ReleaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectRelease
        fields = [
            "id",
            "project",
            "identifier",
            "name",
            "description",
            "target_date",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "project", "created_at", "updated_at"]

    def validate_identifier(self, value):
        query = ProjectRelease.objects.filter(project=self.context["project"], identifier=value)
        if self.instance:
            query = query.exclude(pk=self.instance.pk)
        if query.exists():
            raise ValidationError("This release identifier already exists in this project.")
        return value


class ReleaseEndpoint(CoordinationEndpoint):
    serializer_class = ReleaseSerializer
    queryset = ProjectRelease.objects.none()
    coordination_action = "project_context"

    @extend_schema(tags=["Project Releases"], responses=ReleaseSerializer(many=True))
    def get(self, request, slug, project_id):
        project = self.project(request)
        return Response(ReleaseSerializer(ProjectRelease.objects.filter(project=project), many=True).data)

    @extend_schema(tags=["Project Releases"], request=ReleaseSerializer, responses={201: ReleaseSerializer})
    @transaction.atomic
    def post(self, request, slug, project_id):
        project = self.project(request, write=True, admin=True)
        serializer = ReleaseSerializer(data=request.data, context={"project": project})
        serializer.is_valid(raise_exception=True)
        serializer.save(project=project)
        return Response(serializer.data, status=201)


class ReleaseDetailEndpoint(CoordinationEndpoint):
    serializer_class = ReleaseSerializer

    @extend_schema(tags=["Project Releases"], request=ReleaseSerializer, responses=ReleaseSerializer)
    def patch(self, request, slug, project_id, release_id):
        project = self.project(request, write=True, admin=True)
        release = get_object_or_404(ProjectRelease, pk=release_id, project=project)
        serializer = ReleaseSerializer(release, data=request.data, partial=True, context={"project": project})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ExecutionPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectExecutionPolicy
        fields = ["project", "enabled"]
        read_only_fields = ["project"]


class ExecutionPolicyEndpoint(CoordinationEndpoint):
    serializer_class = ExecutionPolicySerializer
    coordination_action = "project_context"

    @extend_schema(tags=["Agent Coordination"], responses=ExecutionPolicySerializer)
    def get(self, request, slug, project_id):
        project = self.project(request)
        policy = ProjectExecutionPolicy.objects.filter(project=project).first() or ProjectExecutionPolicy(
            project=project
        )
        return Response(ExecutionPolicySerializer(policy).data)

    @extend_schema(tags=["Agent Coordination"], request=ExecutionPolicySerializer, responses=ExecutionPolicySerializer)
    @transaction.atomic
    def put(self, request, slug, project_id):
        project = self.project(request, write=True, admin=True)
        policy, _ = ProjectExecutionPolicy.objects.select_for_update().get_or_create(project=project)
        serializer = ExecutionPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class MachineContractEndpoint(MachineCoordinationMixin, ContractEndpoint):
    pass


class MachineAdvanceMergeEndpoint(MachineCoordinationMixin, AdvanceMergeEndpoint):
    pass


class MachineReleaseEndpoint(MachineCoordinationMixin, ReleaseEndpoint):
    pass


class MachineReleaseDetailEndpoint(MachineCoordinationMixin, ReleaseDetailEndpoint):
    pass


class MachineExecutionPolicyEndpoint(MachineCoordinationMixin, ExecutionPolicyEndpoint):
    pass


class AgentSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="agent.display_name", read_only=True)

    class Meta:
        model = AgentProjectAccess
        fields = ["id", "agent", "name", "role", "is_active"]
        read_only_fields = fields


class MainAgentInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)


class AgentCredentialSerializer(serializers.ModelSerializer):
    class Meta:
        model = APIToken
        fields = ["id", "label", "is_active", "expired_at", "created_at", "last_used"]
        read_only_fields = fields


class IssuedAgentCredentialSerializer(AgentCredentialSerializer):
    token = serializers.CharField(read_only=True)

    class Meta(AgentCredentialSerializer.Meta):
        fields = [*AgentCredentialSerializer.Meta.fields, "token"]


class MainAgentResponseSerializer(serializers.Serializer):
    agent = AgentSerializer()
    credential = IssuedAgentCredentialSerializer()


def reject_credential_replay(request):
    if "Idempotency-Key" in request.headers:
        raise ValidationError({"Idempotency-Key": "This operation does not store replay responses."})


def credential_response(credential, raw):
    return {**AgentCredentialSerializer(credential).data, "token": raw}


class AgentsEndpoint(CoordinationEndpoint):
    serializer_class = AgentSerializer
    queryset = AgentProjectAccess.objects.none()
    coordination_action = "project_context"

    @extend_schema(tags=["Agent Execution"], responses=AgentSerializer(many=True))
    def get(self, request, slug, project_id):
        project = self.project(request)
        return Response(
            AgentSerializer(AgentProjectAccess.objects.filter(project=project).select_related("agent"), many=True).data
        )

    @extend_schema(
        tags=["Agent Execution"], request=MainAgentInputSerializer, responses={201: MainAgentResponseSerializer}
    )
    def post(self, request, slug, project_id):
        project = self.project(request, write=True, admin=True)
        reject_credential_replay(request)
        serializer = MainAgentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        agent, credential, raw = create_main_agent(project, request.user, serializer.validated_data["name"])
        response = Response(
            {"agent": AgentSerializer(agent).data, "credential": credential_response(credential, raw)}, status=201
        )
        response["Cache-Control"] = "no-store"
        return response


class AgentCredentialsEndpoint(CoordinationEndpoint):
    serializer_class = AgentCredentialSerializer

    @extend_schema(tags=["Agent Execution"], responses=AgentCredentialSerializer(many=True))
    def get(self, request, slug, project_id, access_id):
        project = self.project(request, admin=True)
        access = main_agent_access(project, access_id)
        credentials = APIToken.objects.filter(project=project, user=access.agent, purpose="dispatcher")
        response = Response(AgentCredentialSerializer(credentials, many=True).data)
        response["Cache-Control"] = "no-store"
        return response

    @extend_schema(tags=["Agent Execution"], request=None, responses={201: IssuedAgentCredentialSerializer})
    @transaction.atomic
    def post(self, request, slug, project_id, access_id):
        project = self.project(request, write=True, admin=True)
        reject_credential_replay(request)
        access = main_agent_access(project, access_id, lock=True)
        credential, raw = create_dispatcher_key(access, request.user)
        response = Response(credential_response(credential, raw), status=201)
        response["Cache-Control"] = "no-store"
        return response


class AgentCredentialDetailEndpoint(CoordinationEndpoint):
    @extend_schema(tags=["Agent Execution"], request=None, responses={204: None})
    @transaction.atomic
    def delete(self, request, slug, project_id, access_id, credential_id):
        project = self.project(request, write=True, admin=True)
        access = main_agent_access(project, access_id, lock=True)
        credential = get_object_or_404(
            APIToken.objects.select_for_update(),
            pk=credential_id,
            project=project,
            user=access.agent,
            purpose="dispatcher",
        )
        credential.is_active = False
        credential.updated_by = request.user
        credential.save(disable_auto_set_user=True, update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=204)


class MachineAgentsEndpoint(MachineCoordinationMixin, AgentsEndpoint):
    pass


class ModuleAssignmentsEndpoint(CoordinationEndpoint):
    serializer_class = ModuleAssignmentSerializer
    coordination_action = "modules"

    @extend_schema(
        tags=["Agent Coordination"], request=ModuleAssignmentSerializer, responses=ModuleAssignmentsResponseSerializer
    )
    def patch(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        serializer = ModuleAssignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        modules = assign_modules(request, project, issue_id, **serializer.validated_data)
        return Response({"modules": modules})


class MachineModuleAssignmentsEndpoint(MachineCoordinationMixin, ModuleAssignmentsEndpoint):
    pass


class ClaimEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = RunSerializer
    coordination_action = "claim"

    @extend_schema(tags=["Agent Execution"], request=ClaimSerializer, responses={201: RunSerializer})
    def post(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        return Response(RunSerializer(claim_task(request, project, issue_id, request.data)).data, status=201)


class CredentialResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    token = serializers.CharField()
    expired_at = serializers.DateTimeField()


class CredentialEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = CredentialResponseSerializer
    coordination_action = "credential"
    idempotency_unsupported_methods = {"POST"}

    @extend_schema(tags=["Agent Execution"], request=None, responses={201: CredentialResponseSerializer})
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        credential, raw = issue_execution_credential(request, project, run_id)
        response = Response({"id": str(credential.pk), "token": raw, "expired_at": credential.expired_at}, status=201)
        response["Cache-Control"] = "no-store"
        return response


class HandoffResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    token = serializers.CharField()
    run = RunSerializer()
    event_cursor = serializers.IntegerField()


class HandoffEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = HandoffResponseSerializer
    coordination_action = "handoff"
    idempotency_unsupported_methods = {"POST"}

    @extend_schema(tags=["Agent Execution"], request=None, responses={201: HandoffResponseSerializer})
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        run, credential, raw, cursor = handoff_execution(request, project, run_id)
        response = Response(
            {
                "id": str(credential.pk),
                "token": raw,
                "run": RunSerializer(run).data,
                "event_cursor": cursor,
            },
            status=201,
        )
        response["Cache-Control"] = "no-store"
        return response


class RefreshEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = RefreshSerializer
    coordination_action = "refresh"
    idempotency_unsupported_methods = {"POST"}

    @extend_schema(tags=["Agent Execution"], request=RefreshSerializer, responses={201: CredentialResponseSerializer})
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        credential, raw = refresh_execution(request, project, run_id)
        response = Response(
            {
                "id": str(credential.pk),
                "token": raw,
                "expired_at": credential.expired_at,
            },
            status=201,
        )
        response["Cache-Control"] = "no-store"
        return response


class RunEndpoint(CoordinationEndpoint):
    serializer_class = RunSerializer
    coordination_action = "run"

    @extend_schema(tags=["Agent Execution"], responses=RunSerializer)
    def get(self, request, slug, project_id, run_id):
        project = self.project(request)
        run = get_object_or_404(AgentRun, pk=run_id, project=project)
        return Response(RunSerializer(run).data)


class MachineRunEndpoint(MachineCoordinationMixin, RunEndpoint):
    pass


class HeartbeatEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = RunSerializer
    coordination_action = "heartbeat"

    @extend_schema(tags=["Agent Execution"], request=None, responses=RunSerializer)
    def post(self, request, slug, project_id, run_id):
        self.project(request, write=True)
        from django.utils import timezone

        run = request.coordination_run
        run.last_heartbeat = timezone.now()
        run.save(update_fields=["last_heartbeat", "updated_at"])
        return Response(RunSerializer(run).data)


class CancelRunEndpoint(CoordinationEndpoint):
    serializer_class = RunSerializer

    @extend_schema(tags=["Agent Execution"], request=None, responses=RunSerializer)
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        return Response(RunSerializer(cancel_execution(request, project, run_id)).data)


class CandidatesEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = ContractSerializer
    queryset = WorkItemContract.objects.none()
    coordination_action = "candidates"

    @extend_schema(tags=["Agent Execution"], responses=ContractSerializer(many=True))
    def get(self, request, slug, project_id):
        project = self.project(request)
        require_dispatcher(request, project)
        return self.paginate(
            request=request,
            queryset=WorkItemContract.objects.filter(project=project)
            .filter(issue__is_draft=False, issue__archived_at__isnull=True, issue__agent_child__isnull=True)
            .exclude(issue__state__group__in=["completed", "cancelled", "triage"])
            .filter(status="ready")
            .distinct()
            .order_by("created_at", "id"),
            on_results=lambda rows: ContractSerializer(rows, many=True).data,
        )
