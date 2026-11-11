# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.


def seed():
    import json
    import os
    import secrets
    from pathlib import Path
    from tempfile import gettempdir

    from django.conf import settings
    from django.utils import timezone
    from plane.db.models import (
        User,
        Profile,
        Workspace,
        WorkspaceMember,
        WorkspaceUserProperties,
        Project,
        ProjectMember,
        State,
        Issue,
        Module,
        IssueType,
        AgentProjectAccess,
        APIToken,
    )
    from plane.db.models.issue_type import ProjectIssueType
    from plane.db.models.api import generate_token
    from plane.db.models.session import SessionStore
    from plane.license.models import Instance

    assert settings.DATABASES["default"]["NAME"] == "plane_coordination"
    assert settings.DATABASES["default"]["HOST"] == "127.0.0.1"
    Instance.objects.get_or_create(
        instance_id="coordination-browser-local",
        defaults={
            "instance_name": "Coordination Development",
            "current_version": "1.3.1",
            "last_checked_at": timezone.now(),
            "is_setup_done": True,
            "is_signup_screen_visited": True,
            "is_verified": True,
            "is_test": True,
            "is_telemetry_enabled": False,
            "is_support_required": False,
        },
    )
    owner, _ = User.objects.get_or_create(
        email="owner@coordination.test",
        defaults={
            "username": "coordination-owner",
            "display_name": "Product Owner",
            "is_email_verified": True,
            "is_email_valid": True,
        },
    )
    password = secrets.token_urlsafe()
    owner.set_password(password)
    owner.save()
    workspace, _ = Workspace.objects.get_or_create(
        slug="coordination-dev", defaults={"name": "Product Coordination", "owner": owner}
    )
    WorkspaceMember.objects.get_or_create(workspace=workspace, member=owner, defaults={"role": 20})
    WorkspaceUserProperties.objects.get_or_create(workspace=workspace, user=owner)
    Profile.objects.update_or_create(
        user=owner,
        defaults={
            "is_onboarded": True,
            "is_tour_completed": True,
            "last_workspace_id": workspace.pk,
            "onboarding_step": {
                key: True for key in ("profile_complete", "workspace_create", "workspace_invite", "workspace_join")
            },
        },
    )
    project, _ = Project.objects.get_or_create(
        workspace=workspace,
        identifier="AGENT",
        defaults={"name": "Agent Coordination", "created_by": owner, "project_lead": owner},
    )
    ProjectMember.objects.get_or_create(project=project, member=owner, defaults={"role": 20})
    for name, group in (("Todo", "unstarted"), ("In Progress", "started"), ("Done", "completed")):
        State.objects.get_or_create(
            project=project, name=name, defaults={"group": group, "color": "#777777", "default": group == "unstarted"}
        )
    kind, _ = IssueType.objects.get_or_create(workspace=workspace, name="Work Item", defaults={"is_default": True})
    ProjectIssueType.objects.get_or_create(project=project, issue_type=kind)
    issue = Issue(
        project=project,
        name="Coordinate agent delivery",
        description_html="<p>Product requirements and acceptance criteria for agent delivery.</p>",
        created_by=owner,
        type=kind,
    )
    issue.save(disable_auto_set_user=True)
    Module.objects.get_or_create(project=project, name="API", defaults={"created_by": owner})
    Module.objects.get_or_create(project=project, name="Web", defaults={"created_by": owner})
    reviewer, _ = User.objects.get_or_create(
        email="reviewer@coordination.test",
        defaults={
            "username": "coordination-reviewer",
            "display_name": "Designated Reviewer",
            "is_email_verified": True,
        },
    )
    WorkspaceMember.objects.get_or_create(workspace=workspace, member=reviewer, defaults={"role": 15})
    ProjectMember.objects.get_or_create(project=project, member=reviewer, defaults={"role": 15})
    Profile.objects.update_or_create(
        user=reviewer, defaults={"is_onboarded": True, "is_tour_completed": True, "last_workspace_id": workspace.pk}
    )
    agents = {}
    for role in ("dispatcher", "worker"):
        user, _ = User.objects.get_or_create(
            email=f"{role}@coordination.test",
            defaults={"username": f"coordination-{role}", "display_name": role.title(), "is_bot": True},
        )
        user.set_unusable_password()
        user.save()
        WorkspaceMember.objects.get_or_create(workspace=workspace, member=user, defaults={"role": 15})
        ProjectMember.objects.get_or_create(project=project, member=user, defaults={"role": 15})
        AgentProjectAccess.objects.update_or_create(
            project=project, agent=user, defaults={"role": role, "is_active": True}
        )
        agents[role] = user
    raw = generate_token()
    APIToken.objects.create(
        user=agents["dispatcher"],
        purpose="dispatcher",
        project=project,
        workspace=workspace,
        token=raw,
        is_service=True,
    )

    def session(user):
        store = SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.save()
        return store.session_key

    fixture = {
        "workspace": workspace.slug,
        "project": str(project.pk),
        "issue": str(issue.pk),
        "owner": str(owner.pk),
        "reviewer": str(reviewer.pk),
        "worker": str(agents["worker"].pk),
        "dispatcher_token": raw,
        "session_name": settings.SESSION_COOKIE_NAME,
        "owner_session": session(owner),
        "reviewer_session": session(reviewer),
    }
    path = os.environ.get("COORDINATION_FIXTURE", str(Path(gettempdir()) / "plane-coordination-fixture.json"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(fixture, output)
    login_path = str(Path(path).with_name("plane-coordination-login.txt"))
    fd = os.open(login_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(f"Local synthetic account only\nEmail: {owner.email}\nPassword: {password}\n")
    print(json.dumps({"fixture": path, "login": login_path, "project": str(project.pk), "issue": str(issue.pk)}))
