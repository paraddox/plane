# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from plane.db.models import (
    AgentRun,
    APIToken,
    CoordinationEvent,
    CoordinationSequence,
    Issue,
    IssueAssignee,
    Project,
    ProjectMember,
    ProjectRelease,
    User,
    WorkItemContract,
    WorkspaceMember,
)


class CoordinationConflict(APIException):
    status_code = 409
    default_detail = "The coordination state has changed. Refresh before continuing."
    default_code = "coordination_conflict"


def authorized_project(request, slug, project_id, *, write=False):
    project = get_object_or_404(
        Project, pk=project_id, workspace__slug=slug, workspace__deleted_at__isnull=True, archived_at__isnull=True
    )
    membership = ProjectMember.objects.filter(project=project, member=request.user, is_active=True)
    if write:
        membership = membership.filter(role__gte=15)
    if (
        not membership.exists()
        or not WorkspaceMember.objects.filter(workspace=project.workspace, member=request.user, is_active=True).exists()
    ):
        raise PermissionDenied("An active project and workspace membership is required.")
    return project


def require_human(request):
    if not request.user.is_authenticated or request.user.is_bot or isinstance(getattr(request, "auth", None), APIToken):
        raise PermissionDenied("This operation requires an authenticated human session.")


def is_restricted_guest(user, project):
    return (
        not project.guest_view_all_features
        and ProjectMember.objects.filter(project=project, member=user, is_active=True, role=5).exists()
    )


def is_project_admin(user, project):
    return ProjectMember.objects.filter(project=project, member=user, is_active=True, role=20).exists()


def task_owners(contract):
    assignees = IssueAssignee.objects.filter(issue_id=contract.issue_id).values_list("assignee_id", flat=True)
    members = ProjectMember.objects.filter(project=contract.project, is_active=True, role__gte=15).values("member_id")
    workspace_members = WorkspaceMember.objects.filter(workspace=contract.project.workspace, is_active=True).values(
        "member_id"
    )
    return User.objects.filter(
        Q(pk=contract.issue.created_by_id or contract.owner_id) | Q(pk__in=assignees),
        pk__in=members,
        is_bot=False,
        is_active=True,
    ).filter(pk__in=workspace_members)


def is_task_owner(user, contract):
    return task_owners(contract).filter(pk=user.pk).exists()


def validate_owner(owner, project):
    if (
        owner.is_bot
        or not owner.is_active
        or not ProjectMember.objects.filter(project=project, member=owner, is_active=True, role__gte=15).exists()
        or not WorkspaceMember.objects.filter(workspace=project.workspace, member=owner, is_active=True).exists()
    ):
        raise ValidationError({"owner": "The owner must be an active human member of this project."})


def require_editor(request, project, contract=None, issue=None):
    require_human(request)
    owns = is_task_owner(request.user, contract) if contract else request.user.pk == issue.created_by_id
    if not owns and not is_project_admin(request.user, project):
        raise PermissionDenied("Only the task owner or a project admin can edit its contract.")


def emit_event(project, kind, *, actor, issue=None, revision=None, payload=None):
    # The project sequence lock is held until commit: cursors cannot skip a
    # lower sequence allocated by an earlier, not-yet-committed transaction.
    CoordinationSequence.objects.get_or_create(project=project)
    sequence = CoordinationSequence.objects.select_for_update().get(project=project)
    sequence.last_value += 1
    sequence.save(update_fields=["last_value"])
    return CoordinationEvent.objects.create(
        project=project,
        sequence=sequence.last_value,
        issue=issue,
        actor=actor,
        kind=kind,
        spec_revision=revision,
        payload=payload or {},
    )


@transaction.atomic
def ensure_task_contract(issue):
    Issue.objects.select_for_update().get(pk=issue.pk)
    existing = WorkItemContract.objects.filter(issue=issue).first()
    if existing:
        return existing
    if not issue.created_by_id or issue.created_by.is_bot:
        return None
    contract = WorkItemContract(
        issue=issue,
        project=issue.project,
        owner=issue.created_by,
        created_by=issue.created_by,
        status=WorkItemContract.Status.DRAFT if issue.is_draft else WorkItemContract.Status.READY,
    )
    contract.save(disable_auto_set_user=True)
    emit_event(issue.project, "task.specification_created", actor=issue.created_by, issue=issue, revision=1)
    if not issue.is_draft:
        emit_event(issue.project, "task.ready", actor=issue.created_by, issue=issue, revision=1)
    return contract


class CriterionSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    description = serializers.CharField()


class ContractInputSerializer(serializers.Serializer):
    release = serializers.PrimaryKeyRelatedField(queryset=ProjectRelease.objects.all(), required=False, allow_null=True)
    requirements = serializers.CharField(required=False, allow_blank=True)
    definition_of_done = CriterionSerializer(many=True, required=False)
    expected_revision = serializers.IntegerField(min_value=1, required=False)

    def validate_definition_of_done(self, criteria):
        normalized = [{"id": str(item.get("id") or uuid4()), "description": item["description"]} for item in criteria]
        if len({item["id"] for item in normalized}) != len(normalized):
            raise ValidationError("Criterion IDs must be unique.")
        return normalized

    def validate(self, attrs):
        allowed = set(self.fields)
        unknown = set(self.initial_data) - allowed
        if unknown:
            raise ValidationError({name: "This field is not a writable contract input." for name in sorted(unknown)})
        project = self.context["project"]
        release = attrs.get("release")
        if release and release.project_id != project.pk:
            raise ValidationError({"release": "Release must belong to this project."})
        return attrs


class ContractSerializer(serializers.ModelSerializer):
    owner_ids = serializers.SerializerMethodField()

    def get_owner_ids(self, contract):
        return [str(identifier) for identifier in task_owners(contract).values_list("id", flat=True)]

    class Meta:
        model = WorkItemContract
        fields = [
            "id",
            "issue",
            "project",
            "owner",
            "owner_ids",
            "release",
            "requirements",
            "definition_of_done",
            "spec_revision",
            "status",
            "auto_merge_authorized",
            "auto_merge_revision",
            "auto_merge_authorized_by",
            "auto_merge_authorized_at",
            "merged_sha",
            "merged_at",
            "updated_at",
        ]
        read_only_fields = fields


def check_revision(contract, expected):
    if expected is None:
        raise ValidationError({"expected_revision": "The current specification revision is required."})
    if contract.spec_revision != expected:
        raise CoordinationConflict()


def clear_advance_authorization(contract):
    contract.auto_merge_authorized = False
    contract.auto_merge_revision = None
    contract.auto_merge_authorized_by = None
    contract.auto_merge_authorized_at = None


def locked_contract(project, issue_id):
    # Every coordination mutation locks issue -> contract -> run -> event sequence.
    get_object_or_404(Issue.objects.select_for_update(), pk=issue_id, project=project)
    return get_object_or_404(WorkItemContract.objects.select_for_update(), issue_id=issue_id, project=project)


@transaction.atomic
def edit_contract(request, project, issue_id, data, *, material_change=False):
    issue = get_object_or_404(Issue.objects.select_for_update(), pk=issue_id, project=project)
    contract = WorkItemContract.objects.select_for_update().filter(issue=issue).first()
    require_editor(request, project, contract=contract, issue=issue)
    serializer = ContractInputSerializer(data=data, context={"project": project})
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    expected = values.pop("expected_revision", None)
    created = contract is None
    if created:
        owner = issue.created_by or request.user
        validate_owner(owner, project)
        contract = WorkItemContract(issue=issue, project=project, owner=owner, **values)
    else:
        check_revision(contract, expected)
        if contract.status in (WorkItemContract.Status.MERGING, WorkItemContract.Status.MERGED):
            raise CoordinationConflict("A merging or completed specification cannot be edited.")
        changed = material_change or any(getattr(contract, name) != value for name, value in values.items())
        if not changed:
            return contract, False
        for name, value in values.items():
            setattr(contract, name, value)
        contract.spec_revision += 1
        contract.status = WorkItemContract.Status.READY
        clear_advance_authorization(contract)
        stale_runs = AgentRun.objects.select_for_update().filter(contract=contract, status="active")
        for run in stale_runs:
            run.status = AgentRun.Status.CANCELLED
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at", "updated_at"])
            run.credentials.update(is_active=False)
    contract.save()
    emit_event(
        project,
        "task.specification_created" if created else "task.specification_changed",
        actor=request.user,
        issue=issue,
        revision=contract.spec_revision,
    )
    if contract.status == WorkItemContract.Status.READY:
        emit_event(project, "task.ready", actor=request.user, issue=issue, revision=contract.spec_revision)
    return contract, created


class AdvanceAuthorizationSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    expected_revision = serializers.IntegerField(min_value=1)


@transaction.atomic
def authorize_advance_merge(request, project, issue_id, data):
    require_human(request)
    contract = locked_contract(project, issue_id)
    if not is_task_owner(request.user, contract):
        raise PermissionDenied("Only the task owner can change advance merge authorization.")
    serializer = AdvanceAuthorizationSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    check_revision(contract, serializer.validated_data["expected_revision"])
    if contract.status in (WorkItemContract.Status.MERGING, WorkItemContract.Status.MERGED):
        raise CoordinationConflict("Merge authorization cannot change after merge has started.")
    if serializer.validated_data["enabled"]:
        if not contract.requirements.strip() or not contract.definition_of_done:
            raise ValidationError("Complete the specification before authorizing an automatic merge.")
        contract.auto_merge_authorized = True
        contract.auto_merge_revision = contract.spec_revision
        contract.auto_merge_authorized_by = request.user
        contract.auto_merge_authorized_at = timezone.now()
    else:
        clear_advance_authorization(contract)
    contract.save()
    emit_event(
        project,
        "task.auto_merge_authorization_changed",
        actor=request.user,
        issue=contract.issue,
        revision=contract.spec_revision,
        payload={"enabled": contract.auto_merge_authorized},
    )
    return contract
