# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from . import views, workflow_views, review_views, notification_views


def build_patterns(machine=False):
    base = "workspaces/<str:slug>/projects/<uuid:project_id>/coordination/"
    selected = "Machine" if machine else ""
    routes = [
        ("releases/", "ReleaseEndpoint"),
        ("releases/<uuid:release_id>/", "ReleaseDetailEndpoint"),
        ("policy/", "ExecutionPolicyEndpoint"),
        ("agents/", "AgentsEndpoint"),
        ("work-items/<uuid:issue_id>/contract/", "ContractEndpoint"),
        ("work-items/<uuid:issue_id>/auto-merge/", "AdvanceMergeEndpoint"),
        ("work-items/<uuid:issue_id>/modules/", "ModuleAssignmentsEndpoint"),
        ("runs/<uuid:run_id>/", "RunEndpoint"),
    ]
    patterns = [path(base + route, getattr(views, selected + name).as_view()) for route, name in routes]
    patterns += [
        path(base + route, getattr(workflow_views, selected + name).as_view())
        for route, name in [
            ("events/", "EventsEndpoint"),
            ("work-items/<uuid:issue_id>/overview/", "OverviewEndpoint"),
            ("work-items/<uuid:issue_id>/questions/<uuid:question_id>/answer/", "AnswerEndpoint"),
        ]
    ]
    if machine:
        patterns += [
            path(base + route, getattr(notification_views, name).as_view())
            for route, name in [
                ("subscriptions/", "SubscriptionsEndpoint"),
                ("subscriptions/<uuid:subscription_id>/", "SubscriptionEndpoint"),
                ("subscriptions/<uuid:subscription_id>/deliveries/", "DeliveriesEndpoint"),
                (
                    "subscriptions/<uuid:subscription_id>/deliveries/<uuid:delivery_id>/replay/",
                    "DeliveryReplayEndpoint",
                ),
                ("runs/<uuid:run_id>/events/", "WorkerEventsEndpoint"),
                ("runs/<uuid:run_id>/assets/<uuid:asset_id>/", "AssignedAssetEndpoint"),
            ]
        ]
        patterns += [
            path(base + "candidates/", views.CandidatesEndpoint.as_view()),
            path(base + "work-items/<uuid:issue_id>/claim/", views.ClaimEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/credentials/", views.CredentialEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/handoff/", views.HandoffEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/refresh/", views.RefreshEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/heartbeat/", views.HeartbeatEndpoint.as_view()),
            path(base + "work-items/<uuid:issue_id>/questions/", workflow_views.QuestionsEndpoint.as_view()),
            path(
                base + "work-items/<uuid:issue_id>/questions/<uuid:question_id>/resolve/",
                workflow_views.ResolveEndpoint.as_view(),
            ),
            path(base + "work-items/<uuid:issue_id>/children/", workflow_views.ChildrenEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/head/", workflow_views.HeadEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/evidence/", workflow_views.EvidenceEndpoint.as_view()),
        ]
    else:
        from .codex.views import ConversationEndpoint, ConversationStreamEndpoint

        patterns += [
            path(base + "work-items/<uuid:issue_id>/conversation/", ConversationEndpoint.as_view()),
            path(base + "work-items/<uuid:issue_id>/conversation/stream/", ConversationStreamEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/cancel/", views.CancelRunEndpoint.as_view()),
            path(base + "agents/<uuid:access_id>/credentials/", views.AgentCredentialsEndpoint.as_view()),
            path(
                base + "agents/<uuid:access_id>/credentials/<uuid:credential_id>/",
                views.AgentCredentialDetailEndpoint.as_view(),
            ),
        ]
    patterns += [
        path(base + route, getattr(review_views, selected + name).as_view())
        for route, name in [
            ("work-items/<uuid:issue_id>/reviewers/", "ReviewersEndpoint"),
            ("work-items/<uuid:issue_id>/decisions/", "DecisionEndpoint"),
            ("runs/<uuid:run_id>/reconcile/", "ReconcileEndpoint"),
        ]
    ]
    if machine:
        patterns += [
            path(base + "runs/<uuid:run_id>/review-ready/", review_views.ReviewReadyEndpoint.as_view()),
            path(base + "runs/<uuid:run_id>/merge/", review_views.MergeEndpoint.as_view()),
        ]
    return patterns


urlpatterns = build_patterns()
machine_urlpatterns = build_patterns(machine=True)
