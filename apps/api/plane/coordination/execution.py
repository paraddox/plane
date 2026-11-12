# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import transaction
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from plane.db.models import (
    AgentProjectAccess,
    AgentRun,
    APIToken,
    ModuleIssue,
    ProjectExecutionPolicy,
    ProjectMember,
    RunTarget,
    User,
    WorkItemContract,
    WorkspaceMember,
)
from plane.db.models.api import generate_token
from .contracts import (
    CoordinationConflict,
    clear_advance_authorization,
    emit_event,
    locked_contract,
    require_editor,
    task_owners,
)


WORKER_CAPABILITIES = [
    "task:read",
    "context:read",
    "task:update",
    "comment:write",
    "artifact:write",
    "subtask:create",
    "run:report",
    "merge:request",
]


def require_agent_access(agent, project, role):
    if (
        not agent.is_bot
        or not agent.is_active
        or not AgentProjectAccess.objects.filter(project=project, agent=agent, role=role, is_active=True).exists()
        or not ProjectMember.objects.filter(project=project, member=agent, role__gte=15, is_active=True).exists()
        or not WorkspaceMember.objects.filter(workspace=project.workspace, member=agent, is_active=True).exists()
    ):
        raise PermissionDenied("This agent does not have active access to this project.")


def require_dispatcher(request, project):
    token = request.auth
    if not isinstance(token, APIToken) or token.purpose != "dispatcher" or token.project_id != project.pk:
        raise PermissionDenied("A project-scoped dispatcher credential is required.")
    require_agent_access(token.user, project, AgentProjectAccess.Role.DISPATCHER)
    return token


def shared_dispatcher_key(token_id):
    # Concurrent runs may hold shared read locks; revocation waits for admitted
    # writes without serializing every worker on one dispatcher's key row.
    return next(
        iter(
            APIToken.objects.raw(
                "SELECT * FROM api_tokens WHERE id = %s AND deleted_at IS NULL FOR SHARE",
                [token_id],
                using="default",
            )
        ),
        None,
    )


def execution_policy(project):
    policy = ProjectExecutionPolicy.objects.filter(project=project, enabled=True).first()
    if not policy:
        raise PermissionDenied("Agent execution is disabled for this project.")
    if not policy.execution_lease or not policy.credential_lifetime:
        raise ValidationError("Configure the execution lease and credential lifetime before starting work.")
    if policy.execution_lease.total_seconds() <= 0 or policy.credential_lifetime.total_seconds() <= 0:
        raise ValidationError("Execution lease and credential lifetime must be positive.")
    return policy


class ClaimSerializer(serializers.Serializer):
    agent = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_bot=True, is_active=True))
    worker_id = serializers.CharField(max_length=255)
    repository = serializers.CharField(required=False)


class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentRun
        fields = [
            "id",
            "contract",
            "agent",
            "worker_id",
            "attempt",
            "spec_revision",
            "spec_snapshot",
            "repository",
            "base_branch",
            "status",
            "lease_expires_at",
            "last_heartbeat",
            "finished_at",
            "head_sha",
            "code_revision",
            "pull_request",
            "last_error",
            "created_at",
        ]
        read_only_fields = fields


def validate_live_run(run, *, allow_expired_merging=False):
    if run.project.deleted_at or run.project.archived_at or run.project.workspace.deleted_at:
        raise PermissionDenied("This project is no longer open for execution.")
    if run.status not in (AgentRun.Status.ACTIVE, AgentRun.Status.MERGING) or (
        run.lease_expires_at <= timezone.now() and not (allow_expired_merging and run.status == AgentRun.Status.MERGING)
    ):
        raise PermissionDenied("This execution is no longer authorized.")
    if run.contract.deleted_at or run.contract.issue.deleted_at or run.contract.spec_revision != run.spec_revision:
        raise PermissionDenied("The execution specification is no longer current.")
    if run.contract.status in (
        WorkItemContract.Status.DRAFT,
        WorkItemContract.Status.CANCELLED,
        WorkItemContract.Status.MERGED,
    ):
        raise PermissionDenied("This task is not open for execution.")
    execution_policy(run.project)
    if not task_owners(run.contract).exists():
        raise PermissionDenied("This task has no active human owner.")
    require_agent_access(run.agent, run.project, AgentProjectAccess.Role.WORKER)
    parent = run.dispatcher
    if (
        not parent
        or parent.deleted_at
        or not parent.is_active
        or parent.purpose != "dispatcher"
        or parent.project_id != run.project_id
        or parent.workspace_id != run.project.workspace_id
        or not isinstance(parent.scopes, list)
        or set(parent.scopes) != {"read", "write"}
        or (parent.expired_at and parent.expired_at <= timezone.now())
    ):
        raise PermissionDenied("The dispatcher's credential has been revoked or expired.")
    require_agent_access(parent.user, run.project, AgentProjectAccess.Role.DISPATCHER)


def locked_run(project, run_id):
    candidate = get_object_or_404(AgentRun, pk=run_id, project=project)
    contract = locked_contract(project, candidate.contract.issue_id)
    run = AgentRun.objects.select_for_update().get(pk=run_id)
    run.contract = contract
    return run


def lock_execution_credential(request, run):
    parent = shared_dispatcher_key(run.dispatcher_id)
    current = (
        APIToken.objects.select_for_update()
        .filter(
            pk=request.auth.pk,
            purpose="execution",
            agent_run=run,
            user=run.agent,
            project=run.project,
            is_active=True,
        )
        .filter(Q(expired_at__isnull=True) | Q(expired_at__gt=timezone.now()))
        .first()
    )
    if current is None or parent is None or current.delegated_by_id != parent.pk:
        raise PermissionDenied("This execution credential has been revoked or expired.")
    current.delegated_by = parent
    run.dispatcher = parent
    from plane.api.middleware.api_authentication import APIKeyAuthentication
    from .access import enforce_coordination_access

    APIKeyAuthentication().enforce_scopes(request, current)
    enforce_coordination_access(request, current, locked_execution=run)
    request.auth = current


@transaction.atomic
def claim_task(request, project, issue_id, data):
    parent = require_dispatcher(request, project)
    policy = execution_policy(project)
    serializer = ClaimSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    agent = serializer.validated_data["agent"]
    require_agent_access(agent, project, AgentProjectAccess.Role.WORKER)
    contract = locked_contract(project, issue_id)
    if (
        contract.issue.is_draft
        or contract.issue.archived_at
        or (contract.issue.state_id and contract.issue.state.group in ("completed", "cancelled", "triage"))
    ):
        raise CoordinationConflict("Only an open task can be claimed.")
    if not task_owners(contract).exists():
        raise PermissionDenied("This task has no active human owner.")
    existing = AgentRun.objects.select_for_update().filter(contract=contract, status__in=["active", "merging"]).first()
    if existing:
        # A possibly completed remote merge must be reconciled, never reassigned.
        if existing.status == "merging" or existing.lease_expires_at > timezone.now():
            raise CoordinationConflict("This task is already claimed.")
        existing.status = AgentRun.Status.EXPIRED
        existing.finished_at = timezone.now()
        existing.save(update_fields=["status", "finished_at", "updated_at"])
        existing.credentials.update(is_active=False)
    if contract.status not in (
        WorkItemContract.Status.READY,
        WorkItemContract.Status.IN_PROGRESS,
        WorkItemContract.Status.AWAITING_INPUT,
        WorkItemContract.Status.REVIEW,
    ):
        raise CoordinationConflict("This task is not ready for execution.")
    selected = serializer.validated_data.get("repository")
    if not selected and len(policy.repositories) == 1:
        selected = policy.repositories[0]["repository"]
    repository = next((entry for entry in policy.repositories if entry["repository"] == selected), None)
    if not repository:
        raise ValidationError({"repository": "Select a repository configured for this project."})
    previous = AgentRun.objects.filter(contract=contract).aggregate(last=Max("attempt"))["last"] or 0
    snapshot = {
        "issue_id": str(contract.issue_id),
        "owner_id": str(contract.owner_id),
        "owner_ids": [str(identifier) for identifier in task_owners(contract).values_list("id", flat=True)],
        "name": contract.issue.name,
        "description_html": contract.issue.description_html,
        "requirements": contract.requirements,
        "definition_of_done": contract.definition_of_done,
        "release_id": str(contract.release_id) if contract.release_id else None,
        "module_ids": [
            str(pk) for pk in ModuleIssue.objects.filter(issue_id=issue_id).values_list("module_id", flat=True)
        ],
        "context_references": policy.context_references,
    }
    run = AgentRun.objects.create(
        project=project,
        contract=contract,
        agent=agent,
        dispatcher=parent,
        worker_id=serializer.validated_data["worker_id"],
        attempt=previous + 1,
        spec_revision=contract.spec_revision,
        spec_snapshot=snapshot,
        repository=selected,
        base_branch=repository["base_branch"],
        lease_expires_at=timezone.now() + policy.execution_lease,
    )
    RunTarget.objects.create(run=run, issue_id=issue_id)
    RunTarget.objects.bulk_create(
        [
            RunTarget(run=run, issue_id=child_id)
            for child_id in contract.children.filter(spec_revision=contract.spec_revision).values_list(
                "issue_id", flat=True
            )
        ]
    )
    contract.status = (
        WorkItemContract.Status.AWAITING_INPUT
        if contract.questions.filter(
            spec_revision=contract.spec_revision,
            blocking=True,
            resolved_at__isnull=True,
        ).exists()
        else WorkItemContract.Status.IN_PROGRESS
    )
    contract.save(update_fields=["status", "updated_at"])
    emit_event(
        project,
        "run.claimed",
        actor=request.user,
        issue=contract.issue,
        revision=contract.spec_revision,
        payload={"run_id": str(run.pk), "agent_id": str(agent.pk), "worker_id": run.worker_id},
    )
    return run


@transaction.atomic
def issue_execution_credential(request, project, run_id):
    parent = require_dispatcher(request, project)
    run = locked_run(project, run_id)
    if run.dispatcher_id != parent.pk:
        raise PermissionDenied("This run belongs to another dispatcher.")
    validate_live_run(run)
    policy = execution_policy(project)
    run.credentials.update(is_active=False)
    raw = generate_token()
    credential = APIToken.objects.create(
        user=run.agent,
        user_type=1,
        workspace=project.workspace,
        project=project,
        label=f"{run.worker_id}: {run.id}",
        purpose="execution",
        agent_run=run,
        delegated_by=parent,
        capabilities=WORKER_CAPABILITIES.copy(),
        token=raw,
        expired_at=min(timezone.now() + policy.credential_lifetime, run.lease_expires_at),
    )
    emit_event(
        project,
        "run.credential_issued",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk), "credential_id": str(credential.pk)},
    )
    return credential, raw


@transaction.atomic
def renew_execution(request, project, run_id):
    parent = require_dispatcher(request, project)
    run = locked_run(project, run_id)
    if run.dispatcher_id != parent.pk:
        raise PermissionDenied("This run belongs to another dispatcher.")
    # Pending merges cannot be reassigned; their dispatcher can restore the same
    # agent's lease without reviving an expired, reclaimable active execution.
    validate_live_run(run, allow_expired_merging=True)
    run.lease_expires_at = timezone.now() + execution_policy(project).execution_lease
    run.save(update_fields=["lease_expires_at", "updated_at"])
    return run


@transaction.atomic
def cancel_execution(request, project, run_id):
    run = locked_run(project, run_id)
    contract = run.contract
    require_editor(request, project, contract=contract)
    if run.status == AgentRun.Status.MERGING:
        raise CoordinationConflict("Reconcile the in-flight repository merge before cancelling.")
    if run.status == AgentRun.Status.MERGED:
        raise CoordinationConflict("This execution has already merged.")
    if run.status == AgentRun.Status.CANCELLED:
        return run
    if run.status == AgentRun.Status.EXPIRED:
        raise CoordinationConflict("This execution has expired. Cancel the current execution instead.")
    run.status = AgentRun.Status.CANCELLED
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at", "updated_at"])
    run.credentials.update(is_active=False)
    contract.status = WorkItemContract.Status.CANCELLED
    clear_advance_authorization(contract)
    contract.save()
    emit_event(
        project,
        "run.cancelled",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk)},
    )
    return run
