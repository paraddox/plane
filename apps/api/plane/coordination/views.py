# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
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
from plane.db.models import AgentRun

from .contracts import (
    AdvanceAuthorizationSerializer,
    ContractInputSerializer,
    ContractSerializer,
    authorize_advance_merge,
    authorized_project,
    edit_contract,
    is_project_admin,
    require_human,
    submit_contract,
)
from .modules import ModuleAssignmentSerializer, assign_modules
from .execution import (
    ClaimSerializer,
    RunSerializer,
    cancel_execution,
    claim_task,
    issue_execution_credential,
    renew_execution,
    require_dispatcher,
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
        if admin:
            require_human(request)
            if not is_project_admin(request.user, project):
                raise PermissionDenied("A human project administrator is required.")
        return project


class ContractEndpoint(CoordinationEndpoint):
    serializer_class = ContractSerializer
    coordination_action = "contract"

    @extend_schema(tags=["Agent Coordination"], responses=ContractSerializer)
    def get(self, request, slug, project_id, issue_id):
        project = self.project(request)
        contract = get_object_or_404(
            WorkItemContract, issue_id=issue_id, project=project, issue__deleted_at__isnull=True
        )
        return Response(ContractSerializer(contract).data)

    @extend_schema(tags=["Agent Coordination"], request=ContractInputSerializer, responses=ContractSerializer)
    def put(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        contract, created = edit_contract(request, project, issue_id, request.data)
        return Response(ContractSerializer(contract).data, status=201 if created else 200)

    @extend_schema(tags=["Agent Coordination"], request=ContractInputSerializer, responses=ContractSerializer)
    def patch(self, request, slug, project_id, issue_id):
        return self.put(request, slug, project_id, issue_id)


class SubmitEndpoint(CoordinationEndpoint):
    serializer_class = ContractSerializer

    @extend_schema(tags=["Agent Coordination"], request=None, responses=ContractSerializer)
    def post(self, request, slug, project_id, issue_id):
        project = self.project(request, write=True)
        contract = submit_contract(request, project, issue_id)
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


class RepositorySerializer(serializers.Serializer):
    repository = serializers.RegexField(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch = serializers.CharField(default="main")
    required_checks = serializers.ListField(child=serializers.CharField(), default=list)
    merge_method = serializers.ChoiceField(choices=["merge"], default="merge")


class ExecutionPolicySerializer(serializers.ModelSerializer):
    repositories = RepositorySerializer(many=True, required=False)
    context_references = serializers.ListField(child=serializers.URLField(), required=False)

    class Meta:
        model = ProjectExecutionPolicy
        fields = ["project", "enabled", "repositories", "context_references", "credential_lifetime", "execution_lease"]
        read_only_fields = ["project"]

    def validate(self, attrs):
        for field in ("credential_lifetime", "execution_lease"):
            if attrs.get(field) is not None and attrs[field] <= timedelta(0):
                raise ValidationError({field: "The operator-selected duration must be positive."})
        repositories = attrs.get("repositories")
        if repositories and len({entry["repository"] for entry in repositories}) != len(repositories):
            raise ValidationError({"repositories": "A repository may appear only once."})
        if attrs.get("enabled", self.instance.enabled if self.instance else False):
            for name in ("repositories", "credential_lifetime", "execution_lease"):
                if not attrs.get(name, getattr(self.instance, name, None)):
                    raise ValidationError({name: "Configure this policy before enabling execution."})
        return attrs


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


class MachineSubmitEndpoint(MachineCoordinationMixin, SubmitEndpoint):
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


class RenewEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = RunSerializer
    coordination_action = "renew"

    @extend_schema(tags=["Agent Execution"], request=None, responses=RunSerializer)
    def post(self, request, slug, project_id, run_id):
        project = self.project(request, write=True)
        return Response(RunSerializer(renew_execution(request, project, run_id)).data)


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
            .filter(
                Q(status="ready")
                | Q(
                    status__in=["in_progress", "awaiting_input", "review"],
                    runs__status="active",
                    runs__deleted_at__isnull=True,
                    runs__lease_expires_at__lte=timezone.now(),
                )
            )
            .distinct()
            .order_by("created_at", "id"),
            on_results=lambda rows: ContractSerializer(rows, many=True).data,
        )
