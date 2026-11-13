from uuid import uuid4

from django.db import transaction
from django.shortcuts import get_object_or_404

from plane.db.models import AgentProjectAccess, APIToken, ProjectMember, User, WorkspaceMember
from plane.db.models.api import generate_token
from .execution import require_agent_access


def main_agent_access(project, access_id, *, lock=False):
    query = AgentProjectAccess.objects.filter(project=project, role=AgentProjectAccess.Role.DISPATCHER)
    if lock:
        query = query.select_for_update()
    return get_object_or_404(query, pk=access_id)


def create_dispatcher_key(access, actor):
    require_agent_access(access.agent, access.project, AgentProjectAccess.Role.DISPATCHER)
    raw = generate_token()
    credential = APIToken(
        user=access.agent,
        user_type=1,
        workspace=access.project.workspace,
        project=access.project,
        label=access.agent.display_name,
        purpose="dispatcher",
        is_service=True,
        token=raw,
        created_by=actor,
    )
    credential.save(disable_auto_set_user=True)
    return credential, raw


@transaction.atomic
def create_main_agent(project, actor, name):
    identifier = uuid4().hex
    agent = User(
        username=identifier,
        email=f"{identifier}@agents.plane.invalid",
        display_name=name,
        is_bot=True,
    )
    agent.set_unusable_password()
    agent.save()
    WorkspaceMember.objects.create(workspace=project.workspace, member=agent, role=15, created_by_id=actor.pk)
    ProjectMember.objects.create(project=project, member=agent, role=15, created_by_id=actor.pk)
    access = AgentProjectAccess(project=project, agent=agent, role=AgentProjectAccess.Role.DISPATCHER, created_by=actor)
    access.save(disable_auto_set_user=True)
    credential, raw = create_dispatcher_key(access, actor)
    return access, credential, raw
