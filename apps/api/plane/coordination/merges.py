# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import re

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from plane.db.models import AgentRun, APIToken, Issue, MergeAttempt, State, WorkItemContract
from .contracts import CoordinationConflict, emit_event, require_editor
from .execution import execution_policy, locked_run, validate_live_run
from .github import GitHubClient, MergeRejected, ProviderUnavailable
from .reviews import merge_authorization
from .workflow import worker_run


class MergeInputSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    code_revision = serializers.IntegerField(min_value=1)
    head_sha = serializers.RegexField(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def check_pull_request(run, pull_request):
    if (
        pull_request["head_sha"] != run.head_sha
        or pull_request["base_branch"] != run.base_branch
        or pull_request["repository"].lower() != run.repository.lower()
    ):
        raise CoordinationConflict("The repository, base branch or head no longer matches this execution.")


def check_task_gates(run):
    if not run.head_sha or not run.pull_request:
        raise ValidationError("Report the pull request and head SHA before requesting a merge.")
    if run.contract.questions.filter(spec_revision=run.spec_revision, blocking=True, resolved_at__isnull=True).exists():
        raise CoordinationConflict("Resolve all blocking questions before merging.")
    children = run.contract.children.filter(spec_revision=run.spec_revision, required=True)
    if (
        children.exclude(issue__state__group="completed").exists()
        or children.filter(issue__deleted_at__isnull=False).exists()
    ):
        raise CoordinationConflict("Complete every required subtask before merging.")
    latest = {}
    reports = run.evidence.filter(spec_revision=run.spec_revision, head_sha=run.head_sha, kind="test").order_by(
        "-created_at", "-id"
    )
    for evidence in reports:
        for criterion in evidence.criterion_ids:
            latest.setdefault(criterion, evidence.outcome)
    criteria = list(run.spec_snapshot["definition_of_done"])
    for child in children:
        criteria.extend(child.definition_of_done)
    if any(latest.get(item["id"]) != "passed" for item in criteria):
        raise CoordinationConflict("Each acceptance criterion needs passing test evidence for this head SHA.")
    return merge_authorization(run)


def prepare_merge(request, project, run_id, expected):
    # The fence must commit before the remote side effect, even if the request
    # dies. This operation deliberately does not support response-cache wrapping.
    with transaction.atomic(durable=True):
        run = worker_run(request, project, run_id=run_id, allow_merging=True)
        pending = run.status == AgentRun.Status.MERGING
        if (
            expected["expected_revision"] != run.spec_revision
            or expected["code_revision"] != run.code_revision
            or expected["head_sha"] != run.head_sha
        ):
            raise CoordinationConflict("The merge request does not match the current specification and code revision.")
        list(
            Issue.objects.select_for_update()
            .filter(
                id__in=run.contract.children.filter(spec_revision=run.spec_revision, required=True).values("issue_id"),
            )
            .order_by("id")
        )
        authorization = check_task_gates(run)
        repositories = execution_policy(project).repositories
        repository = next(
            (
                entry
                for entry in repositories
                if entry["repository"] == run.repository and entry["base_branch"] == run.base_branch
            ),
            None,
        )
        if repository is None:
            raise CoordinationConflict("This repository and base are no longer allowed by project policy.")
        if repository.get("merge_method", "merge") != "merge":
            raise ValidationError("Guarded target-branch delivery requires merge commits.")
        done = (
            State.objects.select_for_update()
            .filter(project=project, group="completed")
            .order_by("sequence", "id")
            .first()
        )
        if not done:
            raise ValidationError("Configure a completed state for this project before merging.")
        provider = GitHubClient(project.pk, run.repository)
        confirmation = provider.target_status(run.base_branch, run.head_sha)
        if not pending or not confirmation["merged"]:
            pull_request = provider.pull_request(run.pull_request)
            check_pull_request(run, pull_request)
        if not confirmation["merged"]:
            if pull_request["state"] != "open" or pull_request["draft"] or pull_request["mergeable"] is not True:
                raise CoordinationConflict("The pull request is closed, a draft, conflicting, or not yet mergeable.")
            checks = provider.checks(run.head_sha)
            if any(name not in checks or not all(checks[name]) for name in repository["required_checks"]):
                raise CoordinationConflict("Required CI checks are missing, pending, or failing.")
        validate_live_run(
            AgentRun.objects.select_related(
                "contract__issue",
                "contract__owner",
                "project__workspace",
                "agent",
                "dispatcher__user",
            ).get(pk=run.pk)
        )
        if request.auth.expired_at and request.auth.expired_at <= timezone.now():
            raise PermissionDenied("This execution credential has expired.")
        authorization = merge_authorization(run)
        if pending:
            attempt = get_object_or_404(
                MergeAttempt.objects.select_for_update(), run=run, status__in=["prepared", "uncertain"]
            )
            if (
                attempt.head_sha != run.head_sha
                or attempt.code_revision != run.code_revision
                or attempt.pull_request != run.pull_request
            ):
                raise CoordinationConflict("The pending merge does not match this execution.")
            # A retry may outlive the original call. Its admission makes a later
            # rejection of that original call insufficient to clear the fence.
            if attempt.status == "prepared":
                attempt.status = "uncertain"
                attempt.save(update_fields=["status", "updated_at"])
            return run, attempt, confirmation, provider, True
        attempt = MergeAttempt.objects.create(
            project=project,
            run=run,
            head_sha=run.head_sha,
            code_revision=run.code_revision,
            pull_request=run.pull_request,
            authorization=authorization,
            completion_state=done,
        )
        run.status, run.last_error = AgentRun.Status.MERGING, ""
        run.save(update_fields=["status", "last_error", "updated_at"])
        run.contract.status = WorkItemContract.Status.MERGING
        run.contract.save(update_fields=["status", "updated_at"])
        emit_event(
            project,
            "merge.requested",
            actor=request.user,
            issue=run.contract.issue,
            revision=run.spec_revision,
            payload={"run_id": str(run.pk), "attempt_id": str(attempt.pk), "head_sha": run.head_sha},
        )
        return run, attempt, confirmation, provider, False


@transaction.atomic
def record_uncertain(project, run_id, attempt_id):
    run = locked_run(project, run_id)
    attempt = MergeAttempt.objects.select_for_update().get(pk=attempt_id, run=run)
    # An old observation cannot revive a terminal attempt or overwrite a run
    # that has already moved on to fixes, cancellation, or another attempt.
    if attempt.status not in ("prepared", "uncertain") or run.status != AgentRun.Status.MERGING:
        return run
    attempt.status, attempt.error = "uncertain", "Awaiting authoritative GitHub merge confirmation."
    attempt.save(update_fields=["status", "error", "updated_at"])
    run.last_error = attempt.error
    run.save(update_fields=["last_error", "updated_at"])
    return run


@transaction.atomic
def reject_merge(project, run_id, attempt_id):
    run = locked_run(project, run_id)
    attempt = MergeAttempt.objects.select_for_update().get(pk=attempt_id, run=run)
    if attempt.status in ("confirmed", "uncertain"):
        return run
    attempt.status, attempt.error, attempt.finished_at = "failed", str(MergeRejected.default_detail), timezone.now()
    attempt.save(update_fields=["status", "error", "finished_at", "updated_at"])
    run.status, run.last_error = AgentRun.Status.ACTIVE, attempt.error
    run.save(update_fields=["status", "last_error", "updated_at"])
    run.contract.status = WorkItemContract.Status.REVIEW
    run.contract.save(update_fields=["status", "updated_at"])
    emit_event(
        project,
        "merge.rejected",
        actor=run.agent,
        issue=run.contract.issue,
        revision=run.spec_revision,
        payload={"run_id": str(run.pk), "attempt_id": str(attempt.pk)},
    )
    return run


@transaction.atomic
def confirm_merge(project, run_id, attempt_id, pull_request):
    run = locked_run(project, run_id)
    attempt = MergeAttempt.objects.select_for_update().get(pk=attempt_id, run=run)
    if attempt.status == "confirmed":
        return run
    check_pull_request(run, pull_request)
    if (
        not pull_request["merged"]
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", pull_request["merge_sha"])
        or attempt.head_sha != run.head_sha
        or attempt.code_revision != run.code_revision
        or attempt.pull_request != run.pull_request
        or attempt.status not in ("prepared", "uncertain")
    ):
        raise CoordinationConflict("The provider has not confirmed this merge attempt.")
    if attempt.completion_state.group != "completed":
        raise CoordinationConflict("Restore the project's completed state before finalizing this merge.")
    now = timezone.now()
    contract = run.contract
    contract.status, contract.merged_sha, contract.merged_at = "merged", pull_request["merge_sha"], now
    contract.save(update_fields=["status", "merged_sha", "merged_at", "updated_at"])
    Issue.objects.filter(pk=contract.issue_id).update(state=attempt.completion_state, completed_at=now)
    attempt.status, attempt.merge_sha, attempt.finished_at, attempt.error = "confirmed", contract.merged_sha, now, ""
    attempt.save(update_fields=["status", "merge_sha", "finished_at", "error", "updated_at"])
    run.status, run.finished_at, run.last_error = AgentRun.Status.MERGED, now, ""
    run.save(update_fields=["status", "finished_at", "last_error", "updated_at"])
    APIToken.objects.filter(agent_run=run).update(is_active=False)
    emit_event(
        project,
        "task.merged",
        actor=run.agent,
        issue=contract.issue,
        revision=run.spec_revision,
        payload={
            "run_id": str(run.pk),
            "attempt_id": str(attempt.pk),
            "merge_sha": contract.merged_sha,
            "head_sha": run.head_sha,
            "repository": run.repository,
            "pull_request": run.pull_request,
        },
    )
    return run


def request_merge(request, project, run_id):
    serializer = MergeInputSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    run, attempt, confirmation, provider, pending = prepare_merge(request, project, run_id, serializer.validated_data)
    if confirmation["merged"]:
        return confirm_merge(project, run_id, attempt.pk, confirmation)
    try:
        provider.merge(run.base_branch, run.head_sha)
    except MergeRejected:
        try:
            confirmation = provider.target_status(run.base_branch, run.head_sha)
        except ProviderUnavailable:
            return record_uncertain(project, run_id, attempt.pk)
        if confirmation["merged"]:
            return confirm_merge(project, run_id, attempt.pk, confirmation)
        if pending:
            return record_uncertain(project, run_id, attempt.pk)
        result = reject_merge(project, run_id, attempt.pk)
        if result.status != AgentRun.Status.ACTIVE:
            return result
        raise
    except ProviderUnavailable:
        return record_uncertain(project, run_id, attempt.pk)
    try:
        confirmation = provider.target_status(run.base_branch, run.head_sha)
        if confirmation["merged"]:
            return confirm_merge(project, run_id, attempt.pk, confirmation)
    except (ProviderUnavailable, CoordinationConflict):
        pass
    return record_uncertain(project, run_id, attempt.pk)


def reconcile_merge(request, project, run_id):
    candidate = get_object_or_404(AgentRun, project=project, pk=run_id)
    token = request.auth
    if isinstance(token, APIToken):
        if token.pk != candidate.dispatcher_id and token.agent_run_id != candidate.pk:
            raise PermissionDenied("Only this execution or its dispatcher may observe this merge.")
    else:
        require_editor(request, project, contract=candidate.contract)
    attempt = candidate.merge_attempts.filter(status__in=["prepared", "uncertain", "confirmed"]).first()
    if not attempt:
        raise CoordinationConflict("There is no merge attempt to reconcile.")
    if attempt.status == "confirmed":
        return candidate
    provider = GitHubClient(project.pk, candidate.repository)
    confirmation = provider.target_status(candidate.base_branch, attempt.head_sha)
    if confirmation["merged"]:
        return confirm_merge(project, run_id, attempt.pk, confirmation)
    # An unmerged read cannot prove a timed-out write will not still complete.
    # Keep the fence; never reassign or silently discard an unknown outcome.
    return record_uncertain(project, run_id, attempt.pk)
