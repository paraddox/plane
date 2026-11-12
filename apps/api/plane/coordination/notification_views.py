import socket

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from plane.db.models import AgentEventDelivery, AgentSubscription, CoordinationEvent, FileAsset, RunTarget
from plane.settings.storage import S3Storage
from .notifications import callback_destination, create_subscription, enqueue
from .views import CoordinationEndpoint, MachineCoordinationMixin
from .workflow_views import EventPageSerializer, EventQuerySerializer, EventSerializer


class SubscriptionInputSerializer(serializers.Serializer):
    url = serializers.URLField(max_length=1024)
    event_types = serializers.ListField(
        child=serializers.RegexField(r"^[a-z][a-z0-9_.]*$", max_length=100), required=False
    )
    after = serializers.IntegerField(min_value=0, required=False)

    def validate(self, values):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {name: "This subscription field is not writable." for name in sorted(unknown)}
            )
        return values

    def validate_url(self, value):
        try:
            callback_destination(value)
        except (ValueError, OSError, socket.gaierror):
            raise serializers.ValidationError(
                "Use a publicly routable HTTPS callback, or an explicitly allowlisted test destination."
            ) from None
        return value


class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentSubscription
        fields = ["id", "run", "url", "event_types", "after_sequence", "is_active", "created_at"]
        read_only_fields = fields


class SubscriptionCreatedSerializer(SubscriptionSerializer):
    signing_secret = serializers.CharField(read_only=True)

    class Meta(SubscriptionSerializer.Meta):
        fields = [*SubscriptionSerializer.Meta.fields, "signing_secret"]


def owned_subscriptions(request, project):
    subscriptions = AgentSubscription.objects.filter(project=project)
    if request.auth.purpose == "assignment":
        return subscriptions.filter(run_id=request.auth.agent_run_id)
    return subscriptions.filter(dispatcher=request.auth, run__isnull=True)


class SubscriptionsEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = SubscriptionInputSerializer
    coordination_action = "subscriptions"
    idempotency_unsupported_methods = {"POST"}

    @extend_schema(tags=["Agent Notifications"], responses=SubscriptionSerializer(many=True))
    def get(self, request, slug, project_id):
        project = self.project(request)
        return Response(SubscriptionSerializer(owned_subscriptions(request, project), many=True).data)

    @extend_schema(
        tags=["Agent Notifications"],
        request=SubscriptionInputSerializer,
        responses={201: SubscriptionCreatedSerializer},
    )
    def post(self, request, slug, project_id):
        project = self.project(request, write=True)
        serializer = SubscriptionInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = create_subscription(request, project, serializer.validated_data)
        response = Response(
            {**SubscriptionSerializer(subscription).data, "signing_secret": subscription.signing_secret}, status=201
        )
        response["Cache-Control"] = "no-store"
        return response


class SubscriptionEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = SubscriptionSerializer
    coordination_action = "subscription"

    def get(self, request, slug, project_id, subscription_id):
        project = self.project(request)
        return Response(
            SubscriptionSerializer(get_object_or_404(owned_subscriptions(request, project), pk=subscription_id)).data
        )

    @transaction.atomic
    def delete(self, request, slug, project_id, subscription_id):
        project = self.project(request, write=True)
        subscription = get_object_or_404(owned_subscriptions(request, project).select_for_update(), pk=subscription_id)
        subscription.is_active = False
        subscription.save(update_fields=["is_active"])
        subscription.deliveries.filter(status__in=["pending", "sending"]).update(status="cancelled")
        return Response(status=204)


class DeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentEventDelivery
        fields = ["id", "event", "status", "attempts", "next_attempt_at", "last_status", "last_error", "delivered_at"]
        read_only_fields = fields


class DeliveriesEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = DeliverySerializer
    coordination_action = "deliveries"

    def get(self, request, slug, project_id, subscription_id):
        project = self.project(request)
        subscription = get_object_or_404(owned_subscriptions(request, project), pk=subscription_id)
        return self.paginate(
            request=request,
            queryset=subscription.deliveries.all(),
            on_results=lambda rows: DeliverySerializer(rows, many=True).data,
        )


class DeliveryReplayEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = DeliverySerializer
    coordination_action = "delivery_replay"

    @transaction.atomic
    def post(self, request, slug, project_id, subscription_id, delivery_id):
        project = self.project(request, write=True)
        subscription = get_object_or_404(owned_subscriptions(request, project), pk=subscription_id, is_active=True)
        delivery = get_object_or_404(
            AgentEventDelivery.objects.select_for_update(), pk=delivery_id, subscription=subscription
        )
        if delivery.status == "sending" and delivery.locked_until and delivery.locked_until > timezone.now():
            from .contracts import CoordinationConflict

            raise CoordinationConflict("This delivery is already in flight.")
        delivery.status, delivery.retry_count = "pending", 0
        delivery.next_attempt_at, delivery.locked_until = timezone.now(), None
        delivery.save(update_fields=["status", "retry_count", "next_attempt_at", "locked_until"])
        transaction.on_commit(lambda: enqueue(delivery.pk))
        return Response(DeliverySerializer(delivery).data, status=202)


class WorkerEventsEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = EventPageSerializer
    coordination_action = "worker_events"

    @extend_schema(tags=["Agent Notifications"], parameters=[EventQuerySerializer], responses=EventPageSerializer)
    def get(self, request, slug, project_id, run_id):
        project = self.project(request)
        query = EventQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        after = query.validated_data["after"]
        per_page = self.get_per_page(request)
        if per_page <= 0:
            raise ValidationError({"per_page": "A positive page size is required."})
        events = CoordinationEvent.objects.filter(
            project=project,
            sequence__gt=after,
            issue_id__in=RunTarget.objects.filter(run_id=run_id).values("issue_id"),
        )
        rows = list(events[: per_page + 1])
        page = rows[:per_page]
        return Response(
            {
                "events": EventSerializer(page, many=True).data,
                "next_cursor": page[-1].sequence if page else after,
                "has_more": len(rows) > per_page,
            }
        )


class AssignedAssetSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    attributes = serializers.JSONField()
    download_url = serializers.URLField()


class AssignedAssetEndpoint(MachineCoordinationMixin, CoordinationEndpoint):
    serializer_class = AssignedAssetSerializer
    coordination_action = "assigned_asset"

    @extend_schema(tags=["Agent Notifications"], responses=AssignedAssetSerializer)
    def get(self, request, slug, project_id, run_id, asset_id):
        project = self.project(request)
        targets = RunTarget.objects.filter(run_id=run_id).values("issue_id")
        asset = get_object_or_404(
            FileAsset.objects.filter(
                Q(issue_id__in=targets) | Q(comment__issue_id__in=targets),
                project=project,
                pk=asset_id,
                is_uploaded=True,
                is_deleted=False,
                is_archived=False,
            )
        )
        download = S3Storage(request=request).generate_presigned_url(
            object_name=asset.asset.name,
            disposition="attachment",
            filename=asset.attributes.get("name"),
        )
        if not download:
            return Response({"detail": "Attachment download signing is unavailable."}, status=503)
        response = Response({"id": str(asset.pk), "attributes": asset.attributes, "download_url": download})
        response["Cache-Control"] = "no-store"
        return response
