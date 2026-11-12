# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from uuid import UUID
from functools import wraps

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS

from plane.db.models import APIToken, AgentRun, FileAsset, Issue, RunChild, RunTarget, WorkItemContract
from plane.utils.cycle_transfer_issues import transferable_cycle_issues
from .contracts import CoordinationConflict
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


def personal_write_targets(request, *, resolved_issue_id=None):
    if resolved_issue_id is not None:
        return [resolved_issue_id]
    context = request.parser_context or {}
    view = context.get("view")
    view_name = view.__class__.__name__ if view else ""
    kwargs = context.get("kwargs", {})
    target_field, _ = TASK_VIEWS.get(view_name, ("issue_id", None))
    target_id = kwargs.get(target_field)
    if view_name == "BulkWorkItemAPIEndpoint":
        updates = request.data.get("updates", [])
        return (
            parsed_ids([item.get("id") for item in updates if isinstance(item, dict)])
            if isinstance(updates, list)
            else []
        )
    if view_name in ("ModuleIssueListCreateAPIEndpoint", "CycleIssueListCreateAPIEndpoint"):
        return parsed_ids(request.data.get("issues", []))
    if view_name == "GenericAssetEndpoint" and kwargs.get("asset_id"):
        return list(
            FileAsset.objects.filter(
                pk=kwargs["asset_id"],
                workspace__slug=kwargs.get("slug"),
                is_deleted=False,
                issue_id__isnull=False,
            ).values_list("issue_id", flat=True)
        )
    if view_name == "TransferCycleIssueAPIEndpoint":
        selected = getattr(request, "_coordination_transfer_issue_ids", None)
        if selected is not None:
            return selected
        return list(
            transferable_cycle_issues(kwargs.get("slug"), kwargs.get("project_id"), kwargs.get("cycle_id")).values_list(
                "issue_id", flat=True
            )
        )
    if view_name == "IssueRelationListCreateAPIEndpoint":
        return ([target_id] if target_id else []) + parsed_ids(request.data.get("issues", []))
    if (
        view_name == "IssueDetailAPIEndpoint"
        and request.method == "PUT"
        and not target_id
        and isinstance(request.data, dict)
        and request.data.get("external_id")
        and request.data.get("external_source")
    ):
        return list(
            Issue.objects.filter(
                project_id=kwargs.get("project_id"),
                workspace__slug=kwargs.get("slug"),
                external_id=request.data["external_id"],
                external_source=request.data["external_source"],
            ).values_list("id", flat=True)
        )
    return [target_id] if target_id else []


def enforce_coordination_access(request, token, *, locked_execution=None, resolved_issue_id=None):
    context = request.parser_context or {}
    view = context.get("view")
    kwargs = context.get("kwargs", {})
    action = getattr(view, "coordination_action", None)
    view_name = view.__class__.__name__ if view else ""
    target_field, category = TASK_VIEWS.get(view_name, ("issue_id", None))
    target_id = resolved_issue_id or kwargs.get(target_field)

    if token.purpose == "personal":
        if token.user.is_bot:
            raise PermissionDenied("Agents require dispatcher or execution credentials.")
        if request.method not in SAFE_METHODS:
            targets = personal_write_targets(request, resolved_issue_id=resolved_issue_id)
            if targets and (
                WorkItemContract.objects.filter(issue_id__in=targets).exists()
                or RunChild.objects.filter(issue_id__in=targets).exists()
            ):
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
        if (
            category == "artifact_detail"
            and not FileAsset.objects.filter(
                pk=kwargs.get("pk"), issue_id=target_id, project_id=run.project_id
            ).exists()
        ):
            raise PermissionDenied("This attachment does not belong to the granted work item.")
        return
    if request.method not in SAFE_METHODS:
        if not isinstance(request.data, dict):
            raise ValidationError("A JSON object is required.")
        if set(request.data) & {"created_by", "created_at", "actor", "owner", "project", "workspace"}:
            raise PermissionDenied("Execution credentials cannot rewrite actor or ownership metadata.")
        if category == "task" and request.method == "PATCH":
            require_capability(token, "task:update")
            if "deleted_at" in request.data:
                raise PermissionDenied("Execution credentials cannot delete work items.")
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


def guard_coordination_mutation(view, request):
    token = request.auth
    if (
        not isinstance(token, APIToken)
        or token.purpose not in ("personal", "execution")
        or request.method in SAFE_METHODS
        or getattr(view, "coordination_action", None) in ("merge", "reconcile")
    ):
        return
    handler = getattr(view, request.method.lower(), None)
    if handler is None:
        return
    cycle_transfer = view.__class__.__name__ == "TransferCycleIssueAPIEndpoint"
    generic_asset = view.__class__.__name__ == "GenericAssetEndpoint" and request.method == "PATCH"
    relation_write = view.__class__.__name__ == "IssueRelationListCreateAPIEndpoint"
    if token.purpose == "personal" and not cycle_transfer and not generic_asset and not personal_write_targets(request):
        return

    @wraps(handler)
    def guarded(request, *args, **kwargs):
        with transaction.atomic():
            if token.purpose == "personal":
                scope = (request.parser_context or {}).get("kwargs", {})
                # Contract creation takes the same issue lock. Recheck after
                # acquiring all targets, in the same order used by bulk writes.
                requested_targets = personal_write_targets(request)
                issues = Issue.all_objects.select_for_update(of=("self",)).filter(
                    pk__in=requested_targets,
                    workspace__slug=scope.get("slug"),
                )
                if scope.get("project_id") and not relation_write:
                    issues = issues.filter(project_id=scope["project_id"])
                targets = list(issues.order_by("id").values_list("id", flat=True))
                if relation_write and set(targets) != set(requested_targets):
                    raise ValidationError({"issues": "All relation targets must belong to this workspace."})
                if generic_asset:
                    # Freeze the binding after issue locks; reject changes rather
                    # than acquiring an unexpected issue lock out of order.
                    asset = (
                        FileAsset.objects.select_for_update(of=("self",))
                        .filter(
                            pk=scope["asset_id"],
                            workspace__slug=scope["slug"],
                            is_deleted=False,
                        )
                        .first()
                    )
                    current_targets = [asset.issue_id] if asset and asset.issue_id else []
                    if set(current_targets) != set(targets):
                        raise CoordinationConflict("The asset's work item changed. Retry the request.")
                if cycle_transfer:
                    # Implicit selections must not expand after authorization,
                    # including when the source cycle was initially empty.
                    request._coordination_transfer_issue_ids = targets
                enforce_coordination_access(request, token)
                return handler(request, *args, **kwargs)
            run = locked_run(token.project, token.agent_run_id)
            lock_execution_credential(request, run)
            request._locked_coordination_run = run
            if run.status == "merging" and getattr(view, "coordination_action", None) != "heartbeat":
                raise PermissionDenied("Reconcile the in-flight merge before making further changes.")
            return handler(request, *args, **kwargs)

    setattr(view, request.method.lower(), guarded)
