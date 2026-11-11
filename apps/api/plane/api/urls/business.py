# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.api.views.business import (
    MarkAllReadNotificationAPIViewSet,
    NotificationAPIViewSet,
    PageAPIViewSet,
    PageDescriptionAPIViewSet,
    PageDuplicateAPIEndpoint,
    PageVersionAPIEndpoint,
    PageVersionDetailAPIEndpoint,
    ProjectDiscoveryAPIEndpoint,
    ProjectSavedViewAPIViewSet,
    UnreadNotificationAPIEndpoint,
    UserNotificationPreferenceAPIEndpoint,
    WebhookAPIEndpoint,
    WebhookDetailAPIEndpoint,
    WebhookLogsAPIEndpoint,
    WebhookSecretRegenerateAPIEndpoint,
    WorkspaceAPIViewSet,
    WorkspaceSavedViewAPIViewSet,
)


urlpatterns = [
    path(
        "workspaces/<str:slug>/projects/",
        ProjectDiscoveryAPIEndpoint.as_view(http_method_names=["get", "post"]),
        name="api-project-discovery",
    ),
    path(
        "workspaces/",
        WorkspaceAPIViewSet.as_view({"get": "list", "post": "create"}),
        name="api-workspace-list",
    ),
    path(
        "workspaces/<str:slug>/",
        WorkspaceAPIViewSet.as_view(
            {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
        ),
        name="api-workspace-detail",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/",
        PageAPIViewSet.as_view({"get": "list", "post": "create"}),
        name="api-page-list",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages-summary/",
        PageAPIViewSet.as_view({"get": "summary"}),
        name="api-page-summary",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/",
        PageAPIViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="api-page-detail",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/description/",
        PageDescriptionAPIViewSet.as_view({"get": "retrieve", "patch": "partial_update"}),
        name="api-page-description",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/versions/",
        PageVersionAPIEndpoint.as_view(http_method_names=["get"]),
        name="api-page-version-list",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/versions/<uuid:pk>/",
        PageVersionDetailAPIEndpoint.as_view(http_method_names=["get"]),
        name="api-page-version-detail",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/duplicate/",
        PageDuplicateAPIEndpoint.as_view(http_method_names=["post"]),
        name="api-page-duplicate",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/archive/",
        PageAPIViewSet.as_view({"post": "archive", "delete": "unarchive"}),
        name="api-page-archive",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/lock/",
        PageAPIViewSet.as_view({"post": "lock", "delete": "unlock"}),
        name="api-page-lock",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/pages/<uuid:page_id>/access/",
        PageAPIViewSet.as_view({"post": "access"}),
        name="api-page-access",
    ),
    path(
        "workspaces/<str:slug>/views/",
        WorkspaceSavedViewAPIViewSet.as_view({"get": "list", "post": "create"}),
        name="api-workspace-view-list",
    ),
    path(
        "workspaces/<str:slug>/views/<uuid:pk>/",
        WorkspaceSavedViewAPIViewSet.as_view(
            {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
        ),
        name="api-workspace-view-detail",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/",
        ProjectSavedViewAPIViewSet.as_view({"get": "list", "post": "create"}),
        name="api-project-view-list",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/<uuid:pk>/",
        ProjectSavedViewAPIViewSet.as_view(
            {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
        ),
        name="api-project-view-detail",
    ),
    path(
        "workspaces/<str:slug>/users/notifications/",
        NotificationAPIViewSet.as_view({"get": "list"}),
        name="api-notification-list",
    ),
    path(
        "workspaces/<str:slug>/users/notifications/<uuid:pk>/",
        NotificationAPIViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        name="api-notification-detail",
    ),
    path(
        "workspaces/<str:slug>/users/notifications/<uuid:pk>/read/",
        NotificationAPIViewSet.as_view({"post": "mark_read", "delete": "mark_unread"}),
        name="api-notification-read",
    ),
    path(
        "workspaces/<str:slug>/users/notifications/<uuid:pk>/archive/",
        NotificationAPIViewSet.as_view({"post": "archive", "delete": "unarchive"}),
        name="api-notification-archive",
    ),
    path(
        "workspaces/<str:slug>/users/notifications/unread/",
        UnreadNotificationAPIEndpoint.as_view(http_method_names=["get"]),
        name="api-notification-unread",
    ),
    path(
        "workspaces/<str:slug>/users/notifications/mark-all-read/",
        MarkAllReadNotificationAPIViewSet.as_view({"post": "create"}),
        name="api-notification-mark-all-read",
    ),
    path(
        "users/me/notification-preferences/",
        UserNotificationPreferenceAPIEndpoint.as_view(http_method_names=["get", "patch"]),
        name="api-notification-preferences",
    ),
    path(
        "workspaces/<str:slug>/webhooks/",
        WebhookAPIEndpoint.as_view(http_method_names=["get", "post"]),
        name="api-webhook-list",
    ),
    path(
        "workspaces/<str:slug>/webhooks/<uuid:pk>/",
        WebhookDetailAPIEndpoint.as_view(http_method_names=["get", "patch", "delete"]),
        name="api-webhook-detail",
    ),
    path(
        "workspaces/<str:slug>/webhooks/<uuid:pk>/regenerate/",
        WebhookSecretRegenerateAPIEndpoint.as_view(http_method_names=["post"]),
        name="api-webhook-regenerate",
    ),
    path(
        "workspaces/<str:slug>/webhook-logs/<uuid:webhook_id>/",
        WebhookLogsAPIEndpoint.as_view(http_method_names=["get"]),
        name="api-webhook-logs",
    ),
]
