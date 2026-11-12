# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from plane.db.models import AgentRun, ReviewDecision, ReviewRequirement, User, WorkItemContract
from .contracts import (
    CoordinationConflict,
    check_revision,
    emit_event,
    locked_contract,
    require_human,
    validate_owner,
)
from .workflow import worker_run


class ReviewersInputSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    reviewers = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), many=True)


class ReviewRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewRequirement
        fields = ["id", "reviewer", "requested_at", "is_active"]
        read_only_fields = fields


class ReviewDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewDecision
        fields = [
            "id",
            "run",
            "reviewer",
            "spec_revision",
            "code_revision",
            "head_sha",
            "pull_request",
            "decision",
            "comment",
            "created_at",
        ]
        read_only_fields = fields


@transaction.atomic
def request_reviewers(request, project, issue_id, data):
    require_human(request)
    contract = locked_contract(project, issue_id)
    if request.user.pk != contract.owner_id:
        raise PermissionDenied("Only the owner may change required reviewers.")
    serializer = ReviewersInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    check_revision(contract, serializer.validated_data["expected_revision"])
    if contract.status in ("merging", "merged", "cancelled"):
        raise CoordinationConflict("Review requirements cannot change after merge has started.")
    users = set(serializer.validated_data["reviewers"])
    for user in users:
        validate_owner(user, project)
    contract.review_requirements.exclude(reviewer__in=users).update(is_active=False)
    for reviewer in sorted(users, key=lambda user: user.pk):
        requirement, created = ReviewRequirement.objects.get_or_create(
            project=project,
            contract=contract,
            reviewer=reviewer,
        )
        if not created and not requirement.is_active:
            requirement.is_active, requirement.requested_at = True, timezone.now()
            requirement.save(update_fields=["is_active", "requested_at", "updated_at"])
    emit_event(
        project,
        "task.reviewers_changed",
        actor=request.user,
        issue=contract.issue,
        revision=contract.spec_revision,
        payload={"reviewer_ids": [str(user.pk) for user in sorted(users, key=lambda u: u.pk)]},
    )
    return contract.review_requirements.filter(is_active=True)


class DecisionInputSerializer(serializers.Serializer):
    run = serializers.UUIDField()
    expected_revision = serializers.IntegerField(min_value=1)
    code_revision = serializers.IntegerField(min_value=1)
    head_sha = serializers.RegexField(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    decision = serializers.ChoiceField(choices=["approve", "changes_requested"])
    comment = serializers.CharField(required=False, allow_blank=True, default="")


@transaction.atomic
def decide_review(request, project, issue_id, data):
    require_human(request)
    contract = locked_contract(project, issue_id)
    serializer = DecisionInputSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    values = serializer.validated_data
    check_revision(contract, values.pop("expected_revision"))
    run = get_object_or_404(AgentRun.objects.select_for_update(), pk=values.pop("run"), contract=contract)
    if run.status != "active" or contract.status in ("merging", "merged", "cancelled", "draft"):
        raise CoordinationConflict("This execution is not open for review decisions.")
    if (
        run.spec_revision != contract.spec_revision
        or run.head_sha != values["head_sha"]
        or run.code_revision != values["code_revision"]
    ):
        raise CoordinationConflict("Review the current code revision before deciding.")
    if not run.head_sha or not run.pull_request:
        raise ValidationError("The agent must report a pull request and code revision first.")
    required = ReviewRequirement.objects.filter(contract=contract, reviewer=request.user, is_active=True).exists()
    if request.user.pk != contract.owner_id and not required:
        raise PermissionDenied("Only the task owner or a designated reviewer can decide this review.")
    decision = ReviewDecision.objects.create(
        project=project,
        contract=contract,
        run=run,
        reviewer=request.user,
        spec_revision=contract.spec_revision,
        pull_request=run.pull_request,
        **values,
    )
    emit_event(
        project,
        "review.decided",
        actor=request.user,
        issue=contract.issue,
        revision=contract.spec_revision,
        payload={
            "run_id": str(run.pk),
            "decision_id": str(decision.pk),
            "decision": decision.decision,
            "head_sha": run.head_sha,
            "code_revision": run.code_revision,
        },
    )
    return decision


@transaction.atomic
def mark_review_ready(request, project, run_id):
    run = worker_run(request, project, run_id=run_id)
    if not run.head_sha or not run.pull_request:
        raise ValidationError("Report the pull request and code revision first.")
    if run.contract.questions.filter(spec_revision=run.spec_revision, blocking=True, resolved_at__isnull=True).exists():
        raise CoordinationConflict("Resolve blocking questions before requesting review.")
    run.contract.status = WorkItemContract.Status.REVIEW
    run.contract.save(update_fields=["status", "updated_at"])
    emit_event(
        project,
        "review.requested",
        actor=request.user,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk), "head_sha": run.head_sha, "code_revision": run.code_revision},
    )
    return run


def merge_authorization(run):
    contract = run.contract
    decisions = ReviewDecision.objects.filter(
        run=run,
        spec_revision=run.spec_revision,
        code_revision=run.code_revision,
        head_sha=run.head_sha,
        pull_request=run.pull_request,
    )
    latest = {}
    for decision in decisions:
        latest.setdefault(decision.reviewer_id, decision)
    owner_decision = latest.get(contract.owner_id)
    if owner_decision and owner_decision.decision == "changes_requested":
        raise CoordinationConflict("The owner has requested changes on this code revision.")
    required = list(contract.review_requirements.filter(is_active=True).select_related("reviewer"))
    decision_ids = []
    for requirement in required:
        validate_owner(requirement.reviewer, run.project)
        decision = latest.get(requirement.reviewer_id)
        if not decision or decision.created_at < requirement.requested_at or decision.decision != "approve":
            raise CoordinationConflict("Every requested reviewer must approve this code revision.")
        decision_ids.append(str(decision.pk))
    advance = (
        contract.auto_merge_authorized
        and contract.auto_merge_revision == contract.spec_revision
        and contract.auto_merge_authorized_by_id == contract.owner_id
    )
    owner_approved = owner_decision and owner_decision.decision == "approve"
    if not advance and not owner_approved and not required:
        raise CoordinationConflict("The task owner or designated reviewers must authorize this merge.")
    return {
        "advance": bool(advance),
        "owner_decision_id": str(owner_decision.pk) if owner_decision else None,
        "review_decision_ids": decision_ids,
        "spec_revision": contract.spec_revision,
        "code_revision": run.code_revision,
    }
