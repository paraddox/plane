# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Explicit API-key adapters for the session UI's business workflows."""

from django.db.models import Q
from drf_spectacular.utils import OpenApiTypes, extend_schema, extend_schema_view
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.viewsets import ModelViewSet

from plane.api.idempotency import IdempotencyMixin
from plane.api.middleware.api_authentication import APIKeyAuthentication, filter_api_key_queryset
from plane.api.rate_limit import APIKeyRateLimitMixin
from plane.api.views.project import ProjectListCreateAPIEndpoint
from plane.app.permissions import ROLE, WorkspaceViewerPermission, allow_permission
from plane.app.permissions.page import ProjectPagePermission
from plane.app.serializers import (
    PageBinaryUpdateSerializer,
    PageDetailSerializer,
    PageVersionDetailSerializer,
    PageVersionSerializer,
    UserNotificationPreferenceSerializer,
    WebhookLogSerializer,
    WebhookSerializer,
)
from plane.app.views.notification.base import (
    MarkAllReadNotificationViewSet,
    NotificationViewSet,
    UnreadNotificationEndpoint,
    UserNotificationPreferenceEndpoint,
)
from plane.app.views.page.base import PageDuplicateEndpoint, PagesDescriptionViewSet, PageViewSet
from plane.app.views.page.version import PageVersionEndpoint
from plane.app.views.view.base import IssueViewViewSet, WorkspaceViewViewSet
from plane.app.views.webhook.base import WebhookEndpoint, WebhookLogsEndpoint, WebhookSecretRegenerateEndpoint
from plane.app.views.workspace.base import WorkSpaceViewSet
from plane.db.models import (
    IssueView,
    Label,
    Notification,
    Page,
    PageVersion,
    Project,
    ProjectMember,
    ProjectPage,
    Webhook,
    WebhookLog,
    WorkspaceMember,
)


def is_idempotent_destroy(request, view):
    return (
        request.method == "DELETE"
        and getattr(view, "action", None) == "destroy"
        and "Idempotency-Key" in request.headers
    )


class APIProjectPagePermission(ProjectPagePermission):
    def _get_page(self, request, view):
        if is_idempotent_destroy(request, view):
            return Page.all_objects.get(pk=view.kwargs["page_id"], workspace__slug=view.kwargs["slug"])
        return super()._get_page(request, view)


def require_handler_roles(view, request, roles, *, level="WORKSPACE", creator=False, model=None, **kwargs):
    check = allow_permission(roles, level=level, creator=creator, model=model)(lambda instance, request, **kwargs: None)
    if check(view, request, **kwargs) is not None:
        raise PermissionDenied("You don't have the required permissions.")


class APIKeyBusinessMixin(IdempotencyMixin, APIKeyRateLimitMixin):
    # Share machine-API policy without inheriting a second Django view base.
    authentication_classes = [APIKeyAuthentication]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            model = getattr(self, "model", None)
            return model.objects.none() if model else None
        return super().get_queryset()


@extend_schema(tags=["Workspaces"])
class WorkspaceAPIViewSet(APIKeyBusinessMixin, WorkSpaceViewSet):
    api_key_scope_discovery = "workspaces"
    idempotency_unsupported_actions = {"destroy"}

    def check_permissions(self, request):
        super().check_permissions(request)
        if self.action in self.idempotency_unsupported_actions and "Idempotency-Key" in request.headers:
            raise ValidationError(
                {"Idempotency-Key": "Workspace deletion does not support idempotency; its slug changes on deletion."}
            )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return filter_api_key_queryset(self.request, super().get_queryset(), "workspaces")

    def list(self, request, *args, **kwargs):
        # The app's list decorator requires a slug, absent on discovery routes.
        # Its queryset already requires active workspace membership.
        return ModelViewSet.list(self, request, *args, **kwargs)

    @allow_permission([ROLE.ADMIN], level="WORKSPACE")
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    def check_idempotency_permissions(self, request, *args, **kwargs):
        if self.action in ("update", "partial_update", "destroy"):
            require_handler_roles(self, request, [ROLE.ADMIN], **kwargs)


class ProjectDiscoveryAPIEndpoint(ProjectListCreateAPIEndpoint):
    api_key_scope_discovery = "projects"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return filter_api_key_queryset(self.request, super().get_queryset(), "projects")


class ProjectPageBindingMixin:
    def check_permissions(self, request):
        super().check_permissions(request)
        page_id = self.kwargs.get("page_id")
        if page_id and is_idempotent_destroy(request, self):
            page = Page.all_objects.get(pk=page_id, workspace__slug=self.kwargs["slug"])
            if page.deleted_at:
                if not ProjectPage.all_objects.filter(
                    page=page, workspace__slug=self.kwargs["slug"], project_id=self.kwargs["project_id"]
                ).exists():
                    raise NotFound("Page not found in this project.")
                return
        if (
            page_id
            and not Page.objects.filter(
                pk=page_id,
                workspace__slug=self.kwargs["slug"],
                projects__id=self.kwargs["project_id"],
                project_pages__deleted_at__isnull=True,
            ).exists()
        ):
            raise NotFound("Page not found in this project.")


class PageReferenceSerializer(serializers.Serializer):
    parent = serializers.PrimaryKeyRelatedField(queryset=Page.objects.none(), required=False, allow_null=True)
    labels = serializers.ListField(
        child=serializers.PrimaryKeyRelatedField(queryset=Label.objects.none()), required=False
    )

    def __init__(self, *args, request, slug, project_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = Page.objects.filter(
            Q(owned_by=request.user) | Q(access=Page.PUBLIC_ACCESS),
            workspace__slug=slug,
            projects__id=project_id,
            project_pages__deleted_at__isnull=True,
        ).distinct()
        if (
            self.initial_data.get("parent")
            and ProjectMember.objects.filter(
                project_id=project_id,
                member=request.user,
                is_active=True,
                role=ROLE.GUEST.value,
                project__guest_view_all_features=False,
            ).exists()
        ):
            self.fields["parent"].queryset = self.fields["parent"].queryset.filter(owned_by=request.user)
        self.fields["labels"].child.queryset = Label.objects.filter(
            Q(project_id=project_id) | Q(project_id__isnull=True), workspace__slug=slug
        )


class PageWriteSerializer(PageDetailSerializer):
    description_html = serializers.CharField(required=False)

    class Meta(PageDetailSerializer.Meta):
        read_only_fields = PageDetailSerializer.Meta.read_only_fields + ["project_ids", "label_ids"]


@extend_schema_view(
    create=extend_schema(request=PageWriteSerializer, responses={201: PageDetailSerializer}),
    retrieve=extend_schema(responses=PageDetailSerializer),
    partial_update=extend_schema(request=PageWriteSerializer, responses=PageDetailSerializer),
)
@extend_schema(tags=["Pages"])
class PageAPIViewSet(ProjectPageBindingMixin, APIKeyBusinessMixin, PageViewSet):
    permission_classes = [APIProjectPagePermission]

    def check_permissions(self, request):
        super().check_permissions(request)
        if self.action not in ("create", "partial_update"):
            return
        for field in ("project_ids", "label_ids"):
            if field in request.data:
                raise ValidationError({field: "This field is read-only. Use labels to update page labels."})
        PageReferenceSerializer(
            data=request.data,
            request=request,
            slug=self.kwargs["slug"],
            project_id=self.kwargs["project_id"],
        ).is_valid(raise_exception=True)

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def archive(self, request, *args, **kwargs):
        return super().archive(request, *args, **kwargs)

    @extend_schema(responses={204: None})
    def unarchive(self, request, *args, **kwargs):
        return super().unarchive(request, *args, **kwargs)

    @extend_schema(responses={204: None})
    def lock(self, request, *args, **kwargs):
        return super().lock(request, *args, **kwargs)

    @extend_schema(responses={204: None})
    def unlock(self, request, *args, **kwargs):
        return super().unlock(request, *args, **kwargs)

    @extend_schema(responses={204: None})
    def access(self, request, *args, **kwargs):
        return super().access(request, *args, **kwargs)

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def summary(self, request, *args, **kwargs):
        return super().summary(request, *args, **kwargs)

    def check_idempotency_permissions(self, request, *args, **kwargs):
        if not kwargs.get("page_id"):
            return
        manager = Page.all_objects if is_idempotent_destroy(request, self) else Page.objects
        page = manager.get(pk=kwargs["page_id"], workspace__slug=kwargs["slug"])
        if self.action == "partial_update" and page.is_locked:
            raise PermissionDenied("Page is locked.")
        if self.action in ("partial_update", "access"):
            if request.data.get("access", page.access) != page.access and page.owned_by_id != request.user.id:
                raise PermissionDenied("Only the page owner can change access.")
        if self.action in ("archive", "unarchive", "destroy") and page.owned_by_id != request.user.id:
            if not ProjectMember.objects.filter(
                workspace__slug=kwargs["slug"],
                project_id=kwargs["project_id"],
                member=request.user,
                is_active=True,
                role=ROLE.ADMIN.value,
            ).exists():
                raise PermissionDenied("Only the page owner or a project admin can perform this action.")
        if self.action == "destroy" and page.archived_at is None:
            raise PermissionDenied("The page must be archived before deleting.")


@extend_schema_view(
    retrieve=extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY}),
    partial_update=extend_schema(request=PageBinaryUpdateSerializer, responses={200: OpenApiTypes.OBJECT}),
)
@extend_schema(tags=["Pages"])
class PageDescriptionAPIViewSet(ProjectPageBindingMixin, APIKeyBusinessMixin, PagesDescriptionViewSet):
    serializer_class = PageBinaryUpdateSerializer

    def check_idempotency_permissions(self, request, *args, **kwargs):
        page = Page.objects.get(pk=kwargs["page_id"])
        if page.is_locked or page.archived_at:
            raise PermissionDenied("Page is locked or archived.")


@extend_schema_view(get=extend_schema(operation_id="list_page_versions", responses=PageVersionSerializer(many=True)))
@extend_schema(tags=["Pages"])
class PageVersionAPIEndpoint(ProjectPageBindingMixin, APIKeyBusinessMixin, PageVersionEndpoint):
    model = PageVersion

    def get_serializer_class(self):
        return PageVersionDetailSerializer if self.kwargs.get("pk") else PageVersionSerializer


@extend_schema_view(get=extend_schema(operation_id="retrieve_page_version", responses=PageVersionDetailSerializer))
class PageVersionDetailAPIEndpoint(PageVersionAPIEndpoint):
    pass


@extend_schema_view(post=extend_schema(request=None, responses={201: PageDetailSerializer}))
@extend_schema(tags=["Pages"])
class PageDuplicateAPIEndpoint(ProjectPageBindingMixin, APIKeyBusinessMixin, PageDuplicateEndpoint):
    model = Page
    serializer_class = PageDetailSerializer


class SavedViewUpdateMixin:
    def update(self, request, *args, **kwargs):
        # Reuse the owner/lock checks instead of the unguarded generic PUT.
        return self.partial_update(request, *args, **kwargs)

    def check_idempotency_permissions(self, request, *args, **kwargs):
        if self.action not in ("update", "partial_update", "destroy"):
            return
        roles = [ROLE.ADMIN] if self.action == "destroy" else []
        level = "PROJECT" if kwargs.get("project_id") else "WORKSPACE"
        if is_idempotent_destroy(request, self):
            saved_view = IssueView.all_objects.get(
                pk=kwargs["pk"], workspace__slug=kwargs["slug"], project_id=kwargs.get("project_id")
            )
            if (
                kwargs.get("project_id")
                and not Project.objects.filter(pk=kwargs["project_id"], archived_at__isnull=True).exists()
            ):
                raise NotFound("Project not found.")
            if not WorkspaceMember.objects.filter(
                workspace__slug=kwargs["slug"], member=request.user, is_active=True
            ).exists():
                raise PermissionDenied("An active workspace membership is required.")
            if saved_view.created_by_id != request.user.id:
                require_handler_roles(self, request, roles, level=level, **kwargs)
        else:
            require_handler_roles(self, request, roles, level=level, creator=True, model=IssueView, **kwargs)
            saved_view = self.get_queryset().get(pk=kwargs["pk"])
        if self.action != "destroy" and (saved_view.is_locked or saved_view.owned_by_id != request.user.id):
            raise PermissionDenied("Only the owner can update an unlocked view.")
        if self.action == "destroy" and saved_view.owned_by_id != request.user.id:
            if kwargs.get("project_id"):
                if not ProjectMember.objects.filter(
                    workspace__slug=kwargs["slug"],
                    project_id=kwargs["project_id"],
                    member=request.user,
                    is_active=True,
                    role=ROLE.ADMIN.value,
                ).exists():
                    raise PermissionDenied("Only the owner or a project admin can delete this view.")
            else:
                require_handler_roles(self, request, [ROLE.ADMIN], level=level, **kwargs)


@extend_schema(tags=["Views"])
class WorkspaceSavedViewAPIViewSet(SavedViewUpdateMixin, APIKeyBusinessMixin, WorkspaceViewViewSet):
    permission_classes = [WorkspaceViewerPermission]


@extend_schema(tags=["Views"])
class ProjectSavedViewAPIViewSet(SavedViewUpdateMixin, APIKeyBusinessMixin, IssueViewViewSet):
    def check_permissions(self, request):
        super().check_permissions(request)
        require_handler_roles(self, request, [ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="PROJECT", **self.kwargs)


@extend_schema(tags=["Notifications"])
class NotificationAPIViewSet(APIKeyBusinessMixin, NotificationViewSet):
    permission_classes = [WorkspaceViewerPermission]

    @extend_schema(responses={200: NotificationViewSet.serializer_class})
    def mark_read(self, request, *args, **kwargs):
        return super().mark_read(request, *args, **kwargs)

    @extend_schema(responses={200: NotificationViewSet.serializer_class})
    def mark_unread(self, request, *args, **kwargs):
        return super().mark_unread(request, *args, **kwargs)

    @extend_schema(responses={200: NotificationViewSet.serializer_class})
    def archive(self, request, *args, **kwargs):
        return super().archive(request, *args, **kwargs)

    @extend_schema(responses={200: NotificationViewSet.serializer_class})
    def unarchive(self, request, *args, **kwargs):
        return super().unarchive(request, *args, **kwargs)

    def check_idempotency_permissions(self, request, *args, **kwargs):
        if kwargs.get("pk"):
            if is_idempotent_destroy(request, self):
                Notification.all_objects.get(pk=kwargs["pk"], workspace__slug=kwargs["slug"], receiver=request.user)
            else:
                self.get_queryset().get(pk=kwargs["pk"])


@extend_schema_view(get=extend_schema(responses=OpenApiTypes.OBJECT))
@extend_schema(tags=["Notifications"])
class UnreadNotificationAPIEndpoint(APIKeyBusinessMixin, UnreadNotificationEndpoint):
    permission_classes = [WorkspaceViewerPermission]
    serializer_class = NotificationAPIViewSet.serializer_class


@extend_schema_view(create=extend_schema(responses={200: OpenApiTypes.OBJECT}))
@extend_schema(tags=["Notifications"])
class MarkAllReadNotificationAPIViewSet(APIKeyBusinessMixin, MarkAllReadNotificationViewSet):
    permission_classes = [WorkspaceViewerPermission]
    serializer_class = NotificationAPIViewSet.serializer_class


@extend_schema(tags=["Notifications"])
class UserNotificationPreferenceAPIEndpoint(APIKeyBusinessMixin, UserNotificationPreferenceEndpoint):
    serializer_class = UserNotificationPreferenceSerializer


class WebhookPermissionMixin:
    def check_idempotency_permissions(self, request, *args, **kwargs):
        require_handler_roles(self, request, [ROLE.ADMIN], **kwargs)
        if request.method == "DELETE":
            Webhook.all_objects.get(pk=kwargs["pk"], workspace__slug=kwargs["slug"])


@extend_schema_view(get=extend_schema(operation_id="list_webhooks", responses=WebhookSerializer(many=True)))
@extend_schema(tags=["Webhooks"])
class WebhookAPIEndpoint(WebhookPermissionMixin, APIKeyBusinessMixin, WebhookEndpoint):
    model = Webhook
    serializer_class = WebhookSerializer


@extend_schema_view(get=extend_schema(operation_id="retrieve_webhook", responses=WebhookSerializer))
class WebhookDetailAPIEndpoint(WebhookAPIEndpoint):
    pass


@extend_schema_view(post=extend_schema(responses={200: WebhookSerializer}))
@extend_schema(tags=["Webhooks"])
class WebhookSecretRegenerateAPIEndpoint(WebhookPermissionMixin, APIKeyBusinessMixin, WebhookSecretRegenerateEndpoint):
    serializer_class = WebhookSerializer


@extend_schema_view(get=extend_schema(responses=WebhookLogSerializer(many=True)))
@extend_schema(tags=["Webhooks"])
class WebhookLogsAPIEndpoint(APIKeyBusinessMixin, WebhookLogsEndpoint):
    model = WebhookLog
    serializer_class = WebhookLogSerializer
