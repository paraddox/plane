# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from datetime import timedelta

from django.utils import timezone
from django.db.models import Q

# Third party imports
from rest_framework import authentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import SAFE_METHODS

# Module imports
from plane.db.models import APIToken, Project
from plane.db.models.api import hash_api_token
from plane.api.rate_limit import get_api_key_rate
from plane.utils.core import set_use_read_replica, should_use_read_replica


def filter_api_key_queryset(request, queryset, resource):
    """Apply resource scopes to an already owner-authorized discovery queryset."""
    token = request.auth
    if not isinstance(token, APIToken):
        raise PermissionDenied("API-key authentication is required.")
    workspace_id = token.workspace_id or (token.project.workspace_id if token.project_id else None)
    if resource == "workspaces":
        return queryset.filter(pk=workspace_id) if workspace_id else queryset
    if resource == "projects":
        if workspace_id:
            queryset = queryset.filter(workspace_id=workspace_id)
        return queryset.filter(pk=token.project_id) if token.project_id else queryset
    raise PermissionDenied("Unknown API-key discovery resource.")


class APIKeyAuthentication(authentication.BaseAuthentication):
    """
    Authentication with an API Key
    """

    www_authenticate_realm = "api"
    media_type = "application/json"
    auth_header_name = "X-Api-Key"

    def get_api_token(self, request):
        return request.headers.get(self.auth_header_name)

    def validate_api_token(self, token):
        # A database digest is not a bearer credential.
        if token.startswith("sha256$"):
            raise AuthenticationFailed("Given API token is not valid")
        try:
            api_token = APIToken.objects.select_related("user", "workspace", "project").get(
                Q(Q(expired_at__gt=timezone.now()) | Q(expired_at__isnull=True)),
                token=hash_api_token(token),
                is_active=True,
                user__is_active=True,
            )
        except APIToken.DoesNotExist:
            raise AuthenticationFailed("Given API token is not valid")

        return api_token.user, api_token

    def enforce_scopes(self, request, token):
        scopes = token.scopes
        if not isinstance(scopes, list) or not scopes or any(scope not in ("read", "write") for scope in scopes):
            raise PermissionDenied("Invalid API-key scopes.")
        required_scope = "read" if request.method in SAFE_METHODS else "write"
        if required_scope not in scopes:
            raise PermissionDenied("API key does not permit this operation.")

        if not token.workspace_id and not token.project_id:
            return
        workspace = token.workspace or token.project.workspace
        if token.project_id and token.project.workspace_id != workspace.pk:
            raise PermissionDenied("API key has inconsistent resource restrictions.")

        context = request.parser_context or {}
        view = context.get("view")
        kwargs = context.get("kwargs", {})
        slug = kwargs.get("slug")
        project_id = kwargs.get("project_id")
        if project_id is None and kwargs.get("pk"):
            project_id = getattr(view, "project_id", None)

        if request.method in ("GET", "HEAD") and getattr(view, "api_key_scope_identity", False) and not kwargs:
            return

        discovery = getattr(view, "api_key_scope_discovery", None)
        if request.method in SAFE_METHODS and project_id is None:
            if discovery == "workspaces" and not slug and not kwargs:
                return
            if discovery == "projects" and slug == workspace.slug:
                return

        if slug != workspace.slug:
            raise PermissionDenied("API key does not permit access to this workspace.")
        if token.project_id:
            if str(project_id) != str(token.project_id):
                raise PermissionDenied("API key does not permit access to this project.")
        elif project_id and not Project.objects.filter(pk=project_id, workspace=workspace).exists():
            raise PermissionDenied("Project does not belong to this key's workspace.")

    def record_usage(self, token):
        # Precision follows the configured key rate window; no separate sampling policy.
        _, duration = get_api_key_rate(token)
        now = timezone.now()
        cutoff = now - timedelta(seconds=duration)
        if token.last_used is not None and token.last_used > cutoff:
            return
        updated = (
            APIToken.objects.filter(pk=token.pk)
            .filter(Q(last_used__isnull=True) | Q(last_used__lte=cutoff))
            .update(last_used=now)
        )
        if updated:
            token.last_used = now

    def authenticate(self, request):
        token = self.get_api_token(request=request)
        if not token:
            return None

        # Revocation and grant checks must not read a lagging replica.
        replica = should_use_read_replica()
        set_use_read_replica(False)
        try:
            user, api_token = self.validate_api_token(token)
            self.enforce_scopes(request, api_token)
            from plane.coordination.access import enforce_coordination_access

            enforce_coordination_access(request, api_token)
            self.record_usage(api_token)
        finally:
            set_use_read_replica(replica)
        return user, api_token
