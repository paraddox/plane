# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from plane.db.models import AgentProjectAccess, APIToken, Project, ProjectMember, User, WorkspaceMember
from plane.db.models.api import generate_token


class Command(BaseCommand):
    help = "Register a non-interactive agent for a project; only dispatchers receive a bootstrap credential."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=UUID)
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--role", required=True, choices=AgentProjectAccess.Role.values)
        parser.add_argument("--owner", type=UUID, required=True, help="Responsible human project administrator UUID.")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            project = Project.objects.get(pk=options["project_id"], archived_at__isnull=True)
            owner = User.objects.get(pk=options["owner"], is_active=True, is_bot=False)
        except (Project.DoesNotExist, User.DoesNotExist):
            raise CommandError("The active project or responsible human was not found.") from None
        if (
            not ProjectMember.objects.filter(project=project, member=owner, role=20, is_active=True).exists()
            or not WorkspaceMember.objects.filter(workspace=project.workspace, member=owner, is_active=True).exists()
        ):
            raise CommandError("The responsible human must be an active project administrator.")
        email = options["email"].strip().lower()
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError

        try:
            validate_email(email)
        except ValidationError:
            raise CommandError("A valid agent email identifier is required.") from None
        agent = User.objects.filter(email=email).first()
        if agent and not agent.is_bot:
            raise CommandError("An existing human account cannot be converted into an agent.")
        if agent and not agent.is_active:
            raise CommandError("Reactivate the agent explicitly before registering it.")
        if agent is None:
            agent = User(email=email, username=generate_token(), display_name=options["name"], is_bot=True)
            agent.set_unusable_password()
            agent.save()
        WorkspaceMember.objects.update_or_create(
            workspace=project.workspace, member=agent, defaults={"role": 15, "is_active": True}
        )
        ProjectMember.objects.update_or_create(project=project, member=agent, defaults={"role": 15, "is_active": True})
        access, _ = AgentProjectAccess.objects.get_or_create(
            project=project, agent=agent, defaults={"role": options["role"]}
        )
        access.role, access.is_active = options["role"], True
        access.created_by = owner
        access.save(disable_auto_set_user=True)
        result = {"agent_id": str(agent.pk), "project_id": str(project.pk), "role": access.role}
        if access.role == AgentProjectAccess.Role.DISPATCHER:
            raw = generate_token()
            token = APIToken(
                user=agent,
                user_type=1,
                workspace=project.workspace,
                project=project,
                purpose="dispatcher",
                is_service=True,
                label=options["name"],
                token=raw,
                created_by=owner,
            )
            token.save(disable_auto_set_user=True)
            result.update({"credential_id": str(token.pk), "token": raw})
        self.stdout.write(json.dumps(result))
