# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from redis.exceptions import ConnectionError
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from plane.api.middleware.api_authentication import APIKeyAuthentication, filter_api_key_queryset
from plane.api.rate_limit import APIKeyRateLimitUnavailable, ApiKeyRateThrottle, get_api_key_rate
from plane.api.views.base import BaseAPIView, BaseViewSet
from plane.app.views.api import ApiTokenEndpoint
from plane.app.serializers.api import APITokenSerializer
from plane.db.models import APIToken, Project, ProjectMember, Workspace, WorkspaceMember
from plane.db.models.api import hash_api_token
from plane.middleware.logger import APITokenLogMiddleware, RequestLoggerMiddleware
from plane.settings.redis import redis_instance


class ProbeAPIView(BaseAPIView):
    def get(self, request, **kwargs):
        return Response({"token_id": str(request.auth.pk)})

    def post(self, request, **kwargs):
        return Response({"ok": True})

    patch = post


class ProbeViewSet(BaseViewSet):
    def list(self, request, **kwargs):
        return Response({"token_id": str(request.auth.pk)})


class ProjectDetailProbe(ProbeAPIView):
    @property
    def project_id(self):
        return self.kwargs.get("pk")


class IdentityProbe(ProbeAPIView):
    api_key_scope_identity = True


class WorkspaceDiscoveryProbe(ProbeAPIView):
    api_key_scope_discovery = "workspaces"

    def get(self, request, **kwargs):
        queryset = Workspace.objects.filter(workspace_member__member=request.user, workspace_member__is_active=True)
        return Response(list(filter_api_key_queryset(request, queryset, "workspaces").values_list("id", flat=True)))


class ProjectDiscoveryProbe(ProbeAPIView):
    api_key_scope_discovery = "projects"

    def get(self, request, slug):
        queryset = Project.objects.filter(workspace__slug=slug, project_projectmember__member=request.user)
        return Response(list(filter_api_key_queryset(request, queryset, "projects").values_list("id", flat=True)))


@pytest.fixture
def keys(create_user):
    created = []

    def create(**kwargs):
        raw = "plane_api_" + uuid4().hex
        token = APIToken.objects.create(user=create_user, token=raw, **kwargs)
        created.append(token)
        return token, raw

    yield create
    client = redis_instance()
    for token in created:
        client.delete(f"api_key:{token.pk}")
    client.close()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Scoped project", identifier="SCOPED")
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    return project


def call(raw, method="get", view=ProbeAPIView, **kwargs):
    request = getattr(APIRequestFactory(), method)("/api/v1/probe/", {}, format="json", HTTP_X_API_KEY=raw)
    if issubclass(view, BaseViewSet):
        return view.as_view({"get": "list"})(request, **kwargs)
    return view.as_view()(request, **kwargs)


@pytest.mark.django_db
def test_keys_are_hashed_and_digest_cannot_authenticate(keys):
    token, raw = keys()
    token.refresh_from_db()
    assert token.token == hash_api_token(raw)
    assert call(raw).data["token_id"] == str(token.pk)
    with pytest.raises(AuthenticationFailed):
        APIKeyAuthentication().validate_api_token(token.token)
    assert call(token.token).status_code in (401, 403)


@pytest.mark.django_db
def test_legacy_key_after_hash_backfill(keys):
    token, raw = keys()
    APIToken.objects.filter(pk=token.pk).update(token=raw)
    APIToken.objects.filter(pk=token.pk).update(token=hash_api_token(raw))
    assert call(raw).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("invalid", ["user_disabled", "token_disabled", "expired"])
def test_invalid_principals_rejected(keys, create_user, invalid):
    token, raw = keys()
    if invalid == "user_disabled":
        create_user.is_active = False
        create_user.save()
    elif invalid == "token_disabled":
        APIToken.objects.filter(pk=token.pk).update(is_active=False)
    else:
        APIToken.objects.filter(pk=token.pk).update(expired_at=timezone.now() - timedelta(seconds=1))
    assert call(raw).status_code in (401, 403)


@pytest.mark.django_db
def test_create_only_exposes_raw_key_and_policy_is_server_controlled(create_user):
    factory = APIRequestFactory()
    request = factory.post("/", {"is_service": True, "allowed_rate_limit": "999/day"}, format="json")
    force_authenticate(request, user=create_user)
    response = ApiTokenEndpoint.as_view()(request)
    assert response.status_code == 201
    token = APIToken.objects.get(pk=response.data["id"])
    assert token.token == hash_api_token(response.data["token"])
    assert token.is_service is False
    assert token.allowed_rate_limit == ""
    assert token.scopes == ["read", "write"]
    for method in ("get", "patch"):
        request = getattr(factory, method)("/", {"is_service": True, "token": "replacement"}, format="json")
        force_authenticate(request, user=create_user)
        response = ApiTokenEndpoint.as_view()(request, pk=token.pk)
        assert response.status_code == 200
        assert "token" not in response.data
    token.refresh_from_db()
    assert token.is_service is False
    assert token.token.startswith("sha256$")


@pytest.mark.django_db
def test_scope_creation_validates_membership_and_project_workspace(create_user, workspace, project):
    factory = APIRequestFactory()
    other = Workspace.objects.create(name="Other", slug="other", owner=create_user)
    payload = {"workspace": str(workspace.pk), "project": str(project.pk), "scopes": ["read"]}
    request = factory.post("/", payload, format="json")
    force_authenticate(request, user=create_user)
    response = ApiTokenEndpoint.as_view()(request)
    assert response.status_code == 201
    token = APIToken.objects.get(pk=response.data["id"])
    assert (token.workspace_id, token.project_id, token.scopes) == (workspace.pk, project.pk, ["read"])
    for invalid in (
        {"workspace": str(other.pk)},
        {"scopes": ["admin"]},
        {"scopes": []},
        {"workspace": str(other.pk), "project": None},
    ):
        request = factory.post("/", {**payload, **invalid}, format="json")
        force_authenticate(request, user=create_user)
        assert ApiTokenEndpoint.as_view()(request).status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scopes,read,write", [(["read", "write"], 200, 200), ([], 403, 403), (["read"], 200, 403), (["write"], 403, 200)]
)
def test_method_scopes(keys, scopes, read, write):
    _, raw = keys(scopes=scopes)
    assert call(raw).status_code == read
    assert call(raw, method="post").status_code == write


def test_scope_default_is_explicit_and_independent():
    first, second = APIToken(), APIToken()
    assert first.scopes == second.scopes == ["read", "write"]
    first.scopes.clear()
    assert second.scopes == ["read", "write"]


def test_scope_serializer_rejects_empty_list():
    serializer = APITokenSerializer(data={"scopes": []})
    assert not serializer.is_valid()
    assert serializer.errors["scopes"][0].code == "empty"


@pytest.mark.parametrize("scopes", [[], ["admin"], ["read", "admin"], "read", None, {}])
@pytest.mark.parametrize("method", ["get", "post"])
def test_empty_or_invalid_scopes_never_grant_access(scopes, method):
    token = APIToken(scopes=scopes)
    request = Request(getattr(APIRequestFactory(), method)("/"))
    with pytest.raises(PermissionDenied):
        APIKeyAuthentication().enforce_scopes(request, token)


@pytest.mark.django_db
def test_workspace_scope_denies_other_or_ambiguous_resources(keys, workspace, project, create_user):
    _, raw = keys(workspace=workspace)
    assert call(raw, slug=workspace.slug, project_id=project.pk).status_code == 200
    assert call(raw, slug="different").status_code == 403
    assert call(raw).status_code == 403
    other = Workspace.objects.create(name="Other", slug="other", owner=create_user)
    other_project = Project.objects.create(workspace=other, name="Other", identifier="OTHER")
    assert call(raw, slug=workspace.slug, project_id=other_project.pk).status_code == 403


@pytest.mark.django_db
def test_project_scope_allows_detail_pk_and_only_marked_identity(keys, workspace, project):
    _, raw = keys(workspace=workspace, project=project, scopes=["read", "write"])
    for method in ("get", "patch"):
        assert call(raw, method=method, view=ProjectDetailProbe, slug=workspace.slug, pk=project.pk).status_code == 200
        assert call(raw, method=method, view=ProjectDetailProbe, slug=workspace.slug, pk=uuid4()).status_code == 403
    assert call(raw, slug=workspace.slug).status_code == 403
    assert call(raw, view=IdentityProbe).status_code == 200
    assert call(raw, view=IdentityProbe, method="post").status_code == 403
    assert call(raw).status_code == 403
    _, write_only = keys(workspace=workspace, scopes=["write"])
    assert call(write_only, view=IdentityProbe).status_code == 403


@pytest.mark.django_db
def test_project_scoped_key_can_get_and_patch_actual_project_detail(keys, workspace, project, api_client, settings):
    settings.APP_BASE_URL = "http://testserver"
    _, raw = keys(workspace=workspace, project=project, scopes=["read", "write"])
    api_client.credentials(HTTP_X_API_KEY=raw)
    url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.pk}/"
    assert api_client.get(url).status_code == 200
    response = api_client.patch(url, {"name": "Updated scoped project"}, format="json")
    assert response.status_code == 200
    project.refresh_from_db()
    assert project.name == "Updated scoped project"


@pytest.mark.django_db
def test_discovery_is_explicit_and_filters_resources(keys, workspace, project, create_user):
    other = Workspace.objects.create(name="Other", slug="other", owner=create_user)
    WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
    Project.objects.create(workspace=workspace, name="Sibling", identifier="SIBLING")
    _, raw = keys(workspace=workspace, project=project)
    assert call(raw, view=WorkspaceDiscoveryProbe).data == [workspace.pk]
    assert call(raw, view=ProjectDiscoveryProbe, slug=workspace.slug).data == [project.pk]
    assert call(raw, view=ProjectDiscoveryProbe, slug=other.slug).status_code == 403
    assert call(raw, view=WorkspaceDiscoveryProbe, method="post").status_code == 403


@pytest.mark.django_db
def test_key_scopes_do_not_raise_owner_role(keys, workspace, project, create_user, api_client):
    WorkspaceMember.objects.filter(workspace=workspace, member=create_user).update(role=5)
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    _, raw = keys(workspace=workspace, project=project, scopes=["read", "write"])
    api_client.credentials(HTTP_X_API_KEY=raw)
    response = api_client.post(f"/api/v1/workspaces/{workspace.slug}/projects/{project.pk}/labels/", {"name": "Denied"})
    assert response.status_code == 403


@pytest.mark.django_db
def test_api_and_viewset_share_budget_headers_and_retry_after(keys):
    token, raw = keys(allowed_rate_limit="2/min")
    first = call(raw)
    second = call(raw, view=ProbeViewSet)
    denied = call(raw)
    assert (first.status_code, second.status_code, denied.status_code) == (200, 200, 429)
    assert [int(response["X-RateLimit-Remaining"]) for response in (first, second, denied)] == [1, 0, 0]
    assert int(denied["X-RateLimit-Reset"]) > 0
    assert 0 < int(denied["Retry-After"]) <= get_api_key_rate(token)[1]


def test_inherited_and_explicit_rate_policies(monkeypatch):
    monkeypatch.setenv("API_KEY_RATE_LIMIT", "90/minute")
    assert get_api_key_rate(APIToken()) == (90, 60)
    assert get_api_key_rate(APIToken(is_service=True)) == (300, 60)
    assert get_api_key_rate(APIToken(is_service=True, allowed_rate_limit="60/min")) == (60, 60)
    assert get_api_key_rate(APIToken(allowed_rate_limit="2/hour")) == (2, 3600)


@pytest.mark.parametrize("inherited", [False, True])
def test_rate_without_duration_fails_closed(monkeypatch, inherited):
    monkeypatch.setenv("API_KEY_RATE_LIMIT", "1/")
    token = APIToken(allowed_rate_limit="" if inherited else "1/")
    with pytest.raises(APIKeyRateLimitUnavailable) as exc:
        get_api_key_rate(token)
    assert exc.value.status_code == 503


def test_concurrent_requests_cannot_exceed_key_budget():
    token = APIToken(allowed_rate_limit="2/min")
    client = redis_instance()

    def admit(_):
        request = Request(APIRequestFactory().get("/"))
        request.auth = token
        return ApiKeyRateThrottle().allow_request(request, None)

    try:
        with ThreadPoolExecutor() as pool:
            assert sum(pool.map(admit, range(8))) == 2
        assert client.zcard(f"api_key:{token.pk}") == 2
    finally:
        client.delete(f"api_key:{token.pk}")
        client.close()


@pytest.mark.django_db
def test_redis_failure_does_not_allow_unthrottled_access(keys):
    _, raw = keys()
    with patch("plane.api.rate_limit.redis_instance", side_effect=ConnectionError):
        assert call(raw).status_code == 503


@pytest.mark.django_db
def test_last_used_precision_tracks_rate_window(keys):
    token, raw = keys(allowed_rate_limit="2/hour")
    assert call(raw).status_code == 200
    token.refresh_from_db()
    first_used = token.last_used
    assert call(raw).status_code == 200
    token.refresh_from_db()
    assert token.last_used == first_used
    APIToken.objects.filter(pk=token.pk).update(last_used=first_used - timedelta(hours=1))
    APIKeyAuthentication().authenticate(Request(APIRequestFactory().get("/", HTTP_X_API_KEY=raw)))
    token.refresh_from_db()
    assert token.last_used > first_used


@pytest.mark.parametrize("streaming", [False, True])
def test_api_audit_logs_metadata_without_secrets(streaming, caplog):
    secret = "plane_api_example_secret"
    request = APIRequestFactory().post(
        f"/api/v1/{secret}/?password=secret&unexpected=private-business-data",
        {"arbitrary": "private-business-data", "password": "secret"},
        format="json",
        HTTP_X_API_KEY=secret,
        HTTP_AUTHORIZATION="Bearer private",
        HTTP_COOKIE="session=private",
    )
    request.auth = SimpleNamespace(pk=uuid4())
    request.user = SimpleNamespace(is_authenticated=False)
    response = StreamingHttpResponse(iter([b"private"])) if streaming else JsonResponse({"private": secret})
    with patch("plane.middleware.logger.process_logs.delay") as enqueue:
        assert APITokenLogMiddleware(lambda _: response)(request) is response
    log = enqueue.call_args.kwargs["log_data"]
    assert log["token_identifier"] == str(request.auth.pk)
    assert log["response_code"] == 200
    assert log["body"] is None and log["response_body"] is None
    serialized = json.dumps(enqueue.call_args.kwargs, default=str)
    for sensitive in (secret, "private", "secret", "session="):
        assert sensitive not in serialized
    with caplog.at_level("INFO", logger="plane.api.request"):
        RequestLoggerMiddleware(lambda _: response)(request)
    assert secret not in caplog.text
    assert "private-business-data" not in caplog.text


@pytest.mark.django_db
def test_real_api_authentication_records_key_identity_without_secret(keys, api_client):
    token, raw = keys()
    api_client.credentials(HTTP_X_API_KEY=raw)
    with patch("plane.middleware.logger.process_logs.delay") as enqueue:
        response = api_client.get("/api/v1/users/me/")
    assert response.status_code == 200
    assert enqueue.call_args.kwargs["log_data"]["token_identifier"] == str(token.pk)
    assert raw not in json.dumps(enqueue.call_args.kwargs, default=str)
