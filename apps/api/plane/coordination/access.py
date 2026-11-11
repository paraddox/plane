# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import UUID
from functools import wraps

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS

from plane.db.models import APIToken, AgentRun, FileAsset, RunTarget, WorkItemContract
from .execution import lock_execution_credential, locked_run, require_agent_access, validate_live_run


TASK_VIEWS = {
    "IssueDetailAPIEndpoint": ("pk", "task"),
    "IssueCommentListCreateAPIEndpoint": ("issue_id", "comment"),
    "IssueCommentDetailAPIEndpoint": ("issue_id", "comment_detail"),
    "IssueAttachmentListCreateAPIEndpoint": ("issue_id", "artifact"),
    "IssueAttachmentDetailAPIEndpoint": ("issue_id", "artifact_detail"),
    "IssueActivityListAPIEndpoint": ("issue_id", "activity"),
    "IssueActivityDetailAPIEndpoint": ("issue_id", "activity"),
    "IssueRelationListCreateAPIEndpoint": ("issue_id", "relation"),
    "IssueLinkListCreateAPIEndpoint": ("issue_id", "link"),
    "IssueLinkDetailAPIEndpoint": ("issue_id", "link_detail"),
}

CONTEXT_VIEWS = {
    "ProjectDetailAPIEndpoint",
    "StateListCreateAPIEndpoint",
    "StateDetailAPIEndpoint",
    "LabelListCreateAPIEndpoint",
    "LabelDetailAPIEndpoint",
    "ModuleListCreateAPIEndpoint",
    "ModuleDetailAPIEndpoint",
    "CycleListCreateAPIEndpoint",
    "CycleDetailAPIEndpoint",
    "PageAPIViewSet",
    "PageDescriptionAPIViewSet",
    "PageVersionAPIEndpoint",
    "PageVersionDetailAPIEndpoint",
}

DISPATCHER_ACTIONS = {
    ("reconcile", "POST"),
    ("claim", "POST"),
    ("credential", "POST"),
    ("renew", "POST"),
    ("run", "GET"),
    ("candidates", "GET"),
    ("events", "GET"),
    ("contract", "GET"),
    ("project_context", "GET"),
}


def parsed_ids(values):
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        try:
            result.append(UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            continue
    return result


def require_capability(token, capability):
    if capability not in token.capabilities:
        raise PermissionDenied("This execution credential does not permit the operation.")


def require_targets(run, identifiers):
    identifiers = set(identifiers)
    if not identifiers or RunTarget.objects.filter(run=run, issue_id__in=identifiers).count() != len(identifiers):
        raise PermissionDenied("This execution is not authorized for those work items.")


def enforce_coordination_access(request, token, *, locked_execution=None):
    context = request.parser_context or {}
    view = context.get("view")
    kwargs = context.get("kwargs", {})
    action = getattr(view, "coordination_action", None)
    view_name = view.__class__.__name__ if view else ""
    target_field, category = TASK_VIEWS.get(view_name, ("issue_id", None))
    target_id = kwargs.get(target_field)

    if token.purpose == "personal":
        if token.user.is_bot:
            raise PermissionDenied("Agents require dispatcher or execution credentials.")
        if request.method not in SAFE_METHODS:
            targets = [target_id] if target_id else []
            if view_name == "BulkWorkItemAPIEndpoint":
                updates = request.data.get("updates", [])
                if isinstance(updates, list):
                    targets = parsed_ids([item.get("id") for item in updates if isinstance(item, dict)])
            if view_name == "ModuleIssueListCreateAPIEndpoint":
                targets = parsed_ids(request.data.get("issues", []))
            if targets and WorkItemContract.objects.filter(issue_id__in=targets).exists():
                raise PermissionDenied("Coordinated work requires a task-run credential.")
        return

    if token.purpose == "dispatcher":
        if not token.project_id:
            raise PermissionDenied("Dispatcher credentials must be project scoped.")
        require_agent_access(token.user, token.project, "dispatcher")
        if (action, request.method) not in DISPATCHER_ACTIONS:
            raise PermissionDenied("This dispatcher may dispatch work, not perform worker actions.")
        if kwargs.get("run_id") and not AgentRun.objects.filter(pk=kwargs["run_id"], dispatcher=token).exists():
            raise PermissionDenied("This run belongs to another dispatcher.")
        return

    if token.purpose != "execution" or not token.agent_run_id or not token.delegated_by_id:
        raise PermissionDenied("Invalid execution credential.")
    run = locked_execution or AgentRun.objects.select_related(
        "contract__issue", "project__workspace", "agent", "dispatcher__user"
    ).get(pk=token.agent_run_id)
    if (
        run.agent_id != token.user_id
        or run.dispatcher_id != token.delegated_by_id
        or run.project_id != token.project_id
    ):
        raise PermissionDenied("Execution credential bindings are inconsistent.")
    token.agent_run = run
    token.delegated_by = run.dispatcher
    validate_live_run(run)
    request.coordination_run = run
    if kwargs.get("run_id") and kwargs["run_id"] != run.pk:
        raise PermissionDenied("This credential belongs to another run.")

    if action == "run" and request.method == "GET":
        require_capability(token, "task:read")
        return
    if action in ("heartbeat", "head", "evidence", "review_ready") and request.method == "POST":
        require_capability(token, "run:report")
        return
    if action in ("merge", "reconcile") and request.method == "POST":
        require_capability(token, "merge:request")
        return
    if action == "project_context" and request.method in SAFE_METHODS:
        require_capability(token, "context:read")
        return
    if view_name in CONTEXT_VIEWS and request.method in SAFE_METHODS:
        require_capability(token, "context:read")
        return

    if target_id:
        require_targets(run, [target_id])
    else:
        raise PermissionDenied("This endpoint is not part of the execution grant.")
    if request.method in SAFE_METHODS and (category or action in ("contract", "overview")):
        require_capability(token, "task:read")
        return
    if request.method not in SAFE_METHODS:
        if not isinstance(request.data, dict):
            raise ValidationError("A JSON object is required.")
        if set(request.data) & {"created_by", "created_at", "actor", "owner", "project", "workspace"}:
            raise PermissionDenied("Execution credentials cannot rewrite actor or ownership metadata.")
        if category == "task" and request.method == "PATCH":
            require_capability(token, "task:update")
            if "parent" in request.data and (
                target_id == run.contract.issue_id or str(request.data["parent"]) != str(run.contract.issue_id)
            ):
                raise PermissionDenied("Executions may not reparent their root or move children outside it.")
            return
        if category == "comment" and request.method == "POST":
            require_capability(token, "comment:write")
            return
        if category in ("artifact", "artifact_detail") and request.method in ("POST", "PATCH"):
            require_capability(token, "artifact:write")
            if (
                kwargs.get("pk")
                and not FileAsset.objects.filter(
                    pk=kwargs["pk"], issue_id=target_id, created_by=token.user, agent_run=run
                ).exists()
            ):
                raise PermissionDenied("This upload is not owned by the executing agent.")
            return
        if action == "modules" and request.method == "PATCH":
            require_capability(token, "task:update")
            return
        if action in ("question", "resolve", "children") and request.method == "POST":
            require_capability(token, "subtask:create" if action == "children" else "run:report")
            return
        if category == "relation" and request.method == "POST":
            require_capability(token, "task:update")
            require_targets(run, parsed_ids(request.data.get("issues", [])))
            return
    raise PermissionDenied("This operation is not part of the execution grant.")


def guard_execution_mutation(view, request):
    token = request.auth
    if (
        not isinstance(token, APIToken)
        or token.purpose != "execution"
        or request.method in SAFE_METHODS
        or getattr(view, "coordination_action", None) in ("merge", "reconcile")
    ):
        return
    handler = getattr(view, request.method.lower(), None)
    if handler is None:
        return

    @wraps(handler)
    def guarded(request, *args, **kwargs):
        with transaction.atomic():
            run = locked_run(token.project, token.agent_run_id)
            lock_execution_credential(request, run)
            request._locked_coordination_run = run
            if run.status == "merging" and getattr(view, "coordination_action", None) != "heartbeat":
                raise PermissionDenied("Reconcile the in-flight merge before making further changes.")
            return handler(request, *args, **kwargs)

    setattr(view, request.method.lower(), guarded)
