# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.html import escape
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from plane.bgtasks.issue_activities_task import issue_activity
from plane.db.models import (
    AgentRun,
    FileAsset,
    Issue,
    IssueComment,
    Module,
    ModuleIssue,
    RunChild,
    RunEvidence,
    RunTarget,
    State,
    User,
    WorkItemContract,
    WorkItemQuestion,
)
from plane.utils.host import base_host
from .contracts import (
    ContractInputSerializer,
    CriterionSerializer,
    CoordinationConflict,
    emit_event,
    locked_contract,
    require_human,
    is_task_owner,
    task_owners,
    validate_owner,
)
from .execution import lock_execution_credential, locked_run


def worker_run(request, project, *, issue_id=None, run_id=None, allow_merging=False):
    grant = getattr(request, "coordination_run", None)
    if not grant or (run_id and grant.pk != run_id) or (issue_id and grant.contract.issue_id != issue_id):
        raise PermissionDenied("The assigned task execution credential is required.")
    run = getattr(request, "_locked_coordination_run", None)
    if run is None:
        run = locked_run(project, grant.pk)
        lock_execution_credential(request, run)
    if run.status != AgentRun.Status.ACTIVE and not (allow_merging and run.status == AgentRun.Status.MERGING):
        raise CoordinationConflict("Reconcile the in-flight merge before changing this execution.")
    return run


def comment(request, contract, text, *, parent=None):
    value = IssueComment.objects.create(
        project=contract.project,
        issue=contract.issue,
        actor=request.user,
        comment_html=f"<p>{escape(text).replace(chr(10), '<br>')}</p>",
        parent=parent,
    )
    transaction.on_commit(
        lambda: issue_activity.delay(
            type="comment.activity.created",
            requested_data=json.dumps({"id": str(value.pk), "comment_html": value.comment_html}, cls=DjangoJSONEncoder),
            actor_id=str(request.user.pk),
            issue_id=str(contract.issue_id),
            project_id=str(contract.project_id),
            current_instance=None,
            epoch=int(value.created_at.timestamp()),
            notification=True,
            origin=base_host(request=request, is_app=True),
        )
    )
    return value


class QuestionInputSerializer(serializers.Serializer):
    text = serializers.CharField()
    blocking = serializers.BooleanField(default=True)
    respondent = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False)


class AnswerInputSerializer(serializers.Serializer):
    text = serializers.CharField()


class ResolveInputSerializer(serializers.Serializer):
    answer_comment = serializers.UUIDField()


class QuestionSerializer(serializers.ModelSerializer):
    answer = serializers.CharField(source="answer_text", read_only=True)

    class Meta:
        model = WorkItemQuestion
        fields = [
            "id",
            "run",
            "spec_revision",
            "text",
            "blocking",
            "respondent",
            "question_comment",
            "answer_comment",
            "answer",
            "resolved_at",
            "resolved_by",
            "created_at",
        ]
        read_only_fields = fields


@transaction.atomic
def ask_question(request, project, issue_id, data):
    run = worker_run(request, project, issue_id=issue_id)
    serializer = QuestionInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    respondent = values.pop("respondent", None)
    if respondent is None:
        owners = task_owners(run.contract)
        respondent = owners.filter(pk=run.contract.owner_id).first() or owners.order_by("id").first()
    if respondent is None:
        raise CoordinationConflict("Assign an active human owner before asking a question.")
    validate_owner(respondent, project)
    question = WorkItemQuestion.objects.create(
        project=project,
        contract=run.contract,
        run=run,
        spec_revision=run.spec_revision,
        respondent=respondent,
        question_comment=comment(request, run.contract, values["text"]),
        **values,
    )
    if question.blocking:
        run.contract.status = WorkItemContract.Status.AWAITING_INPUT
        run.contract.save(update_fields=["status", "updated_at"])
    emit_event(
        project,
        "question.asked",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"question_id": str(question.pk), "respondent_id": str(respondent.pk), "run_id": str(run.pk)},
    )
    return question


@transaction.atomic
def answer_question(request, project, issue_id, question_id, data):
    require_human(request)
    contract = locked_contract(project, issue_id)
    question = get_object_or_404(
        WorkItemQuestion.objects.select_for_update(),
        pk=question_id,
        contract=contract,
        spec_revision=contract.spec_revision,
    )
    if request.user.pk != question.respondent_id and not is_task_owner(request.user, contract):
        raise PermissionDenied("Only the intended respondent or task owner may answer this question.")
    if contract.status in ("merging", "merged", "cancelled", "draft") or question.resolved_at:
        raise CoordinationConflict("This question is no longer open for answers.")
    serializer = AnswerInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    question.answer_comment = comment(
        request, contract, serializer.validated_data["text"], parent=question.question_comment
    )
    question.answer_text = serializer.validated_data["text"]
    question.save(update_fields=["answer_comment", "answer_text", "updated_at"])
    emit_event(
        project,
        "question.answered",
        actor=request.user,
        issue=contract.issue,
        revision=contract.spec_revision,
        payload={"question_id": str(question.pk), "run_id": str(question.run_id)},
    )
    return question


@transaction.atomic
def resolve_question(request, project, issue_id, question_id, data):
    run = worker_run(request, project, issue_id=issue_id)
    question = get_object_or_404(
        WorkItemQuestion.objects.select_for_update(),
        pk=question_id,
        contract=run.contract,
        spec_revision=run.spec_revision,
    )
    if not question.answer_comment_id:
        raise CoordinationConflict("A human answer is required before resolving this question.")
    serializer = ResolveInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    if serializer.validated_data["answer_comment"] != question.answer_comment_id:
        raise CoordinationConflict("The answer changed. Read and acknowledge the current answer.")
    if question.resolved_at:
        return question
    question.resolved_at, question.resolved_by = timezone.now(), request.user
    question.save(update_fields=["resolved_at", "resolved_by", "updated_at"])
    if not WorkItemQuestion.objects.filter(
        contract=run.contract, spec_revision=run.spec_revision, blocking=True, resolved_at__isnull=True
    ).exists():
        run.contract.status = WorkItemContract.Status.IN_PROGRESS
        run.contract.save(update_fields=["status", "updated_at"])
    emit_event(
        project,
        "question.resolved",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"question_id": str(question.pk), "run_id": str(run.pk)},
    )
    return question


class ChildInputSerializer(ContractInputSerializer):
    owner = None
    release = None
    expected_revision = None
    name = serializers.CharField(max_length=255)
    requirements = serializers.CharField()
    definition_of_done = CriterionSerializer(many=True, allow_empty=False)
    modules = serializers.ListField(child=serializers.UUIDField(), required=False)
    required = serializers.BooleanField(default=True)


class ChildSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="issue.name", read_only=True)
    state_group = serializers.CharField(source="issue.state.group", read_only=True, default="backlog")

    class Meta:
        model = RunChild
        fields = [
            "id",
            "issue",
            "run",
            "owner",
            "release",
            "spec_revision",
            "requirements",
            "definition_of_done",
            "required",
            "name",
            "state_group",
        ]
        read_only_fields = fields


@transaction.atomic
def create_child(request, project, issue_id, data):
    run = worker_run(request, project, issue_id=issue_id)
    serializer = ChildInputSerializer(data=data, context={"project": project})
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    used_criteria = {item["id"] for item in run.spec_snapshot["definition_of_done"]}
    for previous in run.contract.children.filter(spec_revision=run.spec_revision):
        used_criteria.update(item["id"] for item in previous.definition_of_done)
    if any(item["id"] in used_criteria for item in values["definition_of_done"]):
        raise ValidationError({"definition_of_done": "Subtask criterion IDs must be distinct from existing criteria."})
    modules = set(
        values.pop("modules", ModuleIssue.objects.filter(issue_id=issue_id).values_list("module_id", flat=True))
    )
    if Module.objects.filter(project=project, pk__in=modules).count() != len(modules):
        raise ValidationError({"modules": "Every module must belong to this project."})
    name = values.pop("name")
    description = (
        values["requirements"]
        + "\n\nDefinition of done:\n"
        + "\n".join(item["description"] for item in values["definition_of_done"])
    )
    initial_state = (
        State.objects.filter(project=project, group__in=["backlog", "unstarted", "started"])
        .order_by("sequence", "id")
        .first()
    )
    if not initial_state:
        raise ValidationError("Configure an open project state before decomposing work.")
    issue = Issue.objects.create(
        project=project,
        state=initial_state,
        parent=run.contract.issue,
        name=name,
        description_html=f"<p>{escape(description).replace(chr(10), '<br>')}</p>",
    )
    for module_id in modules:
        ModuleIssue.objects.create(project=project, issue=issue, module_id=module_id)
    child = RunChild.objects.create(
        project=project,
        contract=run.contract,
        run=run,
        issue=issue,
        owner=run.contract.owner,
        release=run.contract.release,
        spec_revision=run.spec_revision,
        **values,
    )
    RunTarget.objects.create(run=run, issue=issue)
    emit_event(
        project,
        "task.decomposed",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk), "child_id": str(child.pk), "issue_id": str(issue.pk)},
    )
    return child


class HeadInputSerializer(serializers.Serializer):
    head_sha = serializers.RegexField(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    pull_request = serializers.IntegerField(min_value=1)


@transaction.atomic
def report_head(request, project, run_id, data):
    run = worker_run(request, project, run_id=run_id)
    serializer = HeadInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    if run.head_sha == values["head_sha"] and run.pull_request == values["pull_request"]:
        return run
    run.head_sha, run.pull_request = values["head_sha"], values["pull_request"]
    run.code_revision += 1
    run.contract.status = WorkItemContract.Status.IN_PROGRESS
    run.contract.save(update_fields=["status", "updated_at"])
    run.save(update_fields=["head_sha", "pull_request", "code_revision", "updated_at"])
    emit_event(
        project,
        "run.head_changed",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk), "head_sha": run.head_sha, "pull_request": run.pull_request},
    )
    return run


class EvidenceInputSerializer(serializers.ModelSerializer):
    criterion_ids = serializers.ListField(child=serializers.UUIDField(), required=False)

    class Meta:
        model = RunEvidence
        fields = ["head_sha", "kind", "outcome", "summary", "criterion_ids", "url", "asset"]


class EvidenceSerializer(serializers.ModelSerializer):
    asset_url = serializers.CharField(source="asset.asset_url", read_only=True, default=None)

    class Meta:
        model = RunEvidence
        fields = [
            "id",
            "run",
            "spec_revision",
            "head_sha",
            "kind",
            "outcome",
            "summary",
            "criterion_ids",
            "url",
            "asset",
            "asset_url",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


@transaction.atomic
def report_evidence(request, project, run_id, data):
    run = worker_run(request, project, run_id=run_id)
    serializer = EvidenceInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    if not run.head_sha or values["head_sha"] != run.head_sha:
        raise CoordinationConflict("Evidence must reference the execution's current code revision.")
    criteria = [str(pk) for pk in values.get("criterion_ids", [])]
    valid = {item["id"] for item in run.spec_snapshot["definition_of_done"]}
    for child in run.contract.children.filter(spec_revision=run.spec_revision):
        valid.update(item["id"] for item in child.definition_of_done)
    if not set(criteria) <= valid:
        raise ValidationError({"criterion_ids": "Every criterion must belong to this specification."})
    values["criterion_ids"] = criteria
    asset = values.get("asset")
    if (
        asset
        and not FileAsset.objects.filter(
            pk=asset.pk,
            agent_run=run,
            project=project,
            issue_id__in=run.targets.values("issue_id"),
            created_by=request.user,
            is_uploaded=True,
            is_deleted=False,
        ).exists()
    ):
        raise ValidationError({"asset": "Evidence requires an uploaded artifact owned by this execution's agent."})
    evidence = RunEvidence.objects.create(project=project, run=run, spec_revision=run.spec_revision, **values)
    emit_event(
        project,
        "run.evidence_added",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk), "evidence_id": str(evidence.pk), "head_sha": run.head_sha},
    )
    return evidence
