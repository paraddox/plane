"""Task feedback events shared by browser and machine write paths."""

from crum import get_current_user
from django.db.models import Q


def emit_task_change(issue_id, kind, *, actor=None, payload=None):
    from plane.db.models import WorkItemContract
    from .contracts import emit_event

    contract = (
        WorkItemContract.objects.filter(Q(issue_id=issue_id) | Q(children__issue_id=issue_id))
        .select_related("project", "issue")
        .first()
    )
    if not contract or contract.issue.is_draft or contract.status == "draft":
        return
    current_user = get_current_user()
    if current_user is not None and current_user.is_authenticated:
        actor = current_user
    from plane.db.models import Issue

    issue = contract.issue if contract.issue_id == issue_id else Issue.all_objects.get(pk=issue_id)
    return emit_event(
        contract.project, kind, actor=actor, issue=issue, revision=contract.spec_revision, payload=payload
    )
