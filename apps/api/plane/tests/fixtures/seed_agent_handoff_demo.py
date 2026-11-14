"""Create an isolated local demo project without changing existing user data."""

import json
import os
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.db import transaction

from plane.db.models import (
    AgentProjectAccess,
    APIToken,
    Project,
    ProjectExecutionPolicy,
    ProjectMember,
    State,
    User,
    WorkspaceMember,
)
from plane.db.models.api import generate_token


@transaction.atomic
def seed(owner_email, policy_project_id, output, *, repository, base_branch, signature_tolerance):
    owner = User.objects.get(email__iexact=owner_email, is_bot=False, is_active=True)
    source = ProjectExecutionPolicy.objects.select_related("project__workspace").get(
        project_id=policy_project_id, enabled=True
    )
    workspace = source.project.workspace
    assert WorkspaceMember.objects.filter(workspace=workspace, member=owner, is_active=True).exists()
    project, created = Project.objects.get_or_create(
        workspace=workspace,
        identifier="AHT",
        defaults={"name": "Agent Handoff Test", "created_by": owner},
    )
    assert project.name == "Agent Handoff Test"
    ProjectMember.objects.get_or_create(project=project, member=owner, defaults={"role": 20})
    assert ProjectMember.objects.filter(project=project, member=owner, role=20, is_active=True).exists()
    for name, group, color in [
        ("Backlog", "backlog", "#777777"),
        ("Todo", "unstarted", "#5E6AD2"),
        ("In Progress", "started", "#F2C94C"),
        ("Done", "completed", "#4CB782"),
        ("Cancelled", "cancelled", "#EB5757"),
    ]:
        State.objects.get_or_create(
            project=project, name=name, defaults={"group": group, "color": color, "default": group == "unstarted"}
        )
    ProjectExecutionPolicy.objects.update_or_create(
        project=project,
        defaults={
            "enabled": True,
        },
    )
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        existing = json.loads(path.read_text())
        assert existing["project_id"] == str(project.pk)
        assert APIToken.objects.filter(
            pk=existing["dispatcher_credential_id"], is_active=True, project=project
        ).exists()
        return {"project_id": str(project.pk), "workspace": workspace.slug, "created": created}
    agents = {}
    for role, name in [("dispatcher", "Local Handoff Main"), ("worker", "Local Handoff Worker")]:
        identifier = uuid4().hex
        agent = User(email=f"{identifier}@handoff-demo.invalid", username=identifier, display_name=name, is_bot=True)
        agent.set_unusable_password()
        agent.save()
        WorkspaceMember.objects.create(workspace=workspace, member=agent, role=15)
        ProjectMember.objects.create(project=project, member=agent, role=15)
        access = AgentProjectAccess(project=project, agent=agent, role=role, created_by=owner)
        access.save(disable_auto_set_user=True)
        agents[role] = agent
    raw = generate_token()
    credential = APIToken(
        user=agents["dispatcher"],
        project=project,
        workspace=workspace,
        user_type=1,
        purpose="dispatcher",
        is_service=True,
        label="Local handoff demo",
        token=raw,
        created_by=owner,
    )
    credential.save(disable_auto_set_user=True)
    config = {
        "base_url": "http://proxy",
        "callback_url": "http://agent-callback-test.plane.test:8000",
        "prefix": f"/api/v1/workspaces/{workspace.slug}/projects/{project.pk}/coordination/",
        "project_id": str(project.pk),
        "worker_id": str(agents["worker"].pk),
        "dispatcher_token": raw,
        "dispatcher_credential_id": str(credential.pk),
        "repository": repository,
        "base_branch": base_branch,
        "http_timeout": settings.COORDINATION_WEBHOOK_TIMEOUT,
        "signature_tolerance": signature_tolerance,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(config, stream, indent=2)
    return {"project_id": str(project.pk), "workspace": workspace.slug, "created": created}
