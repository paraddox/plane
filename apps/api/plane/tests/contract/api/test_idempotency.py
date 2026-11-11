# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import close_old_connections, connection, connections
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from plane.api.idempotency import IdempotencyMixin
from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.db.models import Workspace, WorkspaceMember
from plane.db.models.api_idempotency import APIIdempotency


class MembershipPermission(BasePermission):
    def has_permission(self, request, view):
        return WorkspaceMember.objects.filter(member=request.user, workspace__slug="test-workspace").exists()


class MutationView(IdempotencyMixin, APIView):
    permission_classes = [IsAuthenticated, MembershipPermission]

    def post(self, request):
        item = Workspace.objects.create(name=request.data["name"], slug=request.data["slug"], owner=request.user)
        return Response({"id": str(item.pk), "name": item.name}, status=201, headers={"ETag": '"revision"'})


def invoke(view, user, token, data=None, key="request-key", **headers):
    if key is not None:
        headers["HTTP_IDEMPOTENCY_KEY"] = key
    request = APIRequestFactory().post(
        "/api/v1/test-mutation/", data or {"name": "New", "slug": "new"}, format="json", **headers
    )
    force_authenticate(request, user=user, token=token)
    return view.as_view()(request)


@pytest.mark.django_db
def test_success_replays_body_status_and_revision(api_token, create_user, workspace):
    first = invoke(MutationView, create_user, api_token)
    replay = invoke(MutationView, create_user, api_token)
    assert first.status_code == replay.status_code == 201
    assert first.data == replay.data
    assert replay["ETag"] == '"revision"'
    assert replay["Idempotency-Replayed"] == "true"
    assert Workspace.objects.filter(slug="new").count() == 1
    assert APIIdempotency.objects.count() == 1


@pytest.mark.django_db
def test_key_cannot_be_reused_for_another_payload(api_token, create_user, workspace):
    invoke(MutationView, create_user, api_token)
    response = invoke(MutationView, create_user, api_token, {"name": "Other", "slug": "other"})
    assert response.status_code == 409
    assert not Workspace.objects.filter(slug="other").exists()


@pytest.mark.django_db
def test_equivalent_json_object_order_replays(api_token, create_user, workspace):
    first = invoke(MutationView, create_user, api_token)
    replay = invoke(MutationView, create_user, api_token, {"slug": "new", "name": "New"})
    assert replay.status_code == 201
    assert replay.data == first.data


@pytest.mark.django_db
def test_authentication_and_current_permissions_run_before_replay(api_token, create_user, workspace):
    invoke(MutationView, create_user, api_token)
    unauthenticated = invoke(MutationView, None, None)
    assert unauthenticated.status_code in (401, 403)
    WorkspaceMember.objects.filter(member=create_user, workspace=workspace).delete()
    denied = invoke(MutationView, create_user, api_token)
    assert denied.status_code == 403
    assert "Idempotency-Replayed" not in denied


@pytest.mark.django_db
def test_handler_level_permission_hook_precedes_replay(api_token, create_user, workspace):
    class ProtectedView(MutationView):
        allowed = True

        def check_idempotency_permissions(self, request):
            if not self.allowed:
                raise PermissionDenied()

    invoke(ProtectedView, create_user, api_token)
    ProtectedView.allowed = False
    assert invoke(ProtectedView, create_user, api_token).status_code == 403


@pytest.mark.django_db
def test_permission_hooks_can_return_decorator_style_denials(api_token, create_user, workspace):
    class ProtectedView(MutationView):
        allowed = True

        def check_idempotency_permissions(self, request):
            if not self.allowed:
                return Response({"error": "Denied"}, status=403)

    invoke(ProtectedView, create_user, api_token)
    ProtectedView.allowed = False
    response = invoke(ProtectedView, create_user, api_token)
    assert response.status_code == 403
    assert response.data == {"error": "Denied"}


@pytest.mark.django_db
def test_revoked_api_key_cannot_replay_cached_response(api_token, create_user, workspace):
    class KeyAuthenticatedView(MutationView):
        authentication_classes = [APIKeyAuthentication]

    def call():
        request = APIRequestFactory().post(
            "/api/v1/test-mutation/",
            {"name": "New", "slug": "new"},
            format="json",
            HTTP_X_API_KEY="test-api-token-12345",
            HTTP_IDEMPOTENCY_KEY="authenticated-request",
        )
        return KeyAuthenticatedView.as_view()(request)

    assert call().status_code == 201
    api_token.is_active = False
    api_token.save(update_fields=["is_active"])
    response = call()
    assert response.status_code in (401, 403)
    assert "Idempotency-Replayed" not in response


@pytest.mark.django_db
@pytest.mark.parametrize("failure", ["response", "exception"])
def test_failure_rolls_back_mutation_claim_and_commit_callbacks(api_token, create_user, workspace, failure):
    published = []

    class FailingView(MutationView):
        def post(self, request):
            from django.db import transaction

            super().post(request)
            transaction.on_commit(lambda: published.append("event"))
            if failure == "exception":
                raise PermissionDenied()
            return Response({"error": "invalid"}, status=400)

    response = invoke(FailingView, create_user, api_token)
    assert response.status_code in (400, 403)
    assert not Workspace.objects.filter(slug="new").exists()
    assert not APIIdempotency.objects.exists()
    assert not published
    assert invoke(MutationView, create_user, api_token).status_code == 201


@pytest.mark.django_db
def test_header_free_writes_are_not_cached(api_token, create_user, workspace):
    assert invoke(MutationView, create_user, api_token, key=None).status_code == 201
    assert invoke(MutationView, create_user, api_token, {"name": "Other", "slug": "other"}, key=None).status_code == 201
    assert not APIIdempotency.objects.exists()


@pytest.mark.django_db
def test_viewset_actions_use_the_same_retry_contract(api_token, create_user, workspace):
    class MutationViewSet(IdempotencyMixin, ViewSet):
        permission_classes = [IsAuthenticated, MembershipPermission]
        create = MutationView.post

    view = MutationViewSet.as_view({"post": "create"})
    responses = []
    for _ in range(2):
        request = APIRequestFactory().post(
            "/api/v1/test-mutation/", {"name": "New", "slug": "new"}, format="json", HTTP_IDEMPOTENCY_KEY="viewset-key"
        )
        force_authenticate(request, user=create_user, token=api_token)
        responses.append(view(request))
    assert responses[0].status_code == responses[1].status_code == 201
    assert responses[0].data == responses[1].data
    assert Workspace.objects.filter(slug="new").count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_result", ["success", "error_response", "exception"])
def test_concurrent_postgres_claims_execute_mutation_once(api_token, create_user, workspace, first_result):
    assert connection.vendor == "postgresql", "This test proves the PostgreSQL unique-insert serialization."
    handler_entered = Event()
    retry_insert_started = Event()

    class ConcurrentView(MutationView):
        def post(self, request):
            handler_entered.set()
            retry_insert_started.wait()
            response = super().post(request)
            if request.headers.get("X-Fail-First"):
                if first_result == "exception":
                    raise PermissionDenied()
                return Response({"error": "invalid"}, status=400)
            return response

    def call(first):
        close_old_connections()

        def observe_retry_insert(execute, sql, params, many, context):
            if 'INSERT INTO "api_idempotency"' in sql:
                retry_insert_started.set()
            return execute(sql, params, many, context)

        try:
            if first:
                headers = {"HTTP_X_FAIL_FIRST": "true"} if first_result != "success" else {}
                return invoke(ConcurrentView, create_user, api_token, **headers)
            handler_entered.wait()
            with connection.execute_wrapper(observe_retry_insert):
                return invoke(ConcurrentView, create_user, api_token)
        finally:
            # A failing setup/permission check must not strand the other thread.
            if first:
                handler_entered.set()
            else:
                retry_insert_started.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(call, True)
        second = executor.submit(call, False)
        responses = [first.result(), second.result()]

    if first_result == "success":
        assert [response.status_code for response in responses] == [201, 201]
        assert responses[0].data == responses[1].data
        assert responses[1]["Idempotency-Replayed"] == "true"
    else:
        assert responses[0].status_code in (400, 403)
        assert responses[1].status_code == 201
        assert "Idempotency-Replayed" not in responses[1]
    assert Workspace.objects.filter(slug="new").count() == 1
    assert APIIdempotency.objects.count() == 1
