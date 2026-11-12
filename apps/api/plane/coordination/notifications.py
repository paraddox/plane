"""Durable, runtime-independent project and assigned-task notifications."""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import time
from datetime import timedelta
from urllib.parse import urlsplit

import urllib3
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from plane.db.models import (
    AgentEventDelivery,
    AgentSubscription,
    CoordinationEvent,
    CoordinationSequence,
    RunTarget,
)

logger = logging.getLogger(__name__)

WORKER_EVENT_TYPES = [
    "question.answered",
    "comment.created",
    "comment.updated",
    "comment.deleted",
    "attachment.uploaded",
    "attachment.deleted",
    "task.updated",
    "task.specification_changed",
    "task.modules_changed",
    "task.auto_merge_authorization_changed",
    "task.reviewers_changed",
    "review.decided",
    "review.requested",
    "run.cancelled",
    "task.merged",
    "child.created",
    "child.updated",
]


def callback_destination(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Use an HTTPS callback URL without credentials or a fragment.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = list(
        dict.fromkeys(row[4][0] for row in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM))
    )
    if not addresses:
        raise ValueError("The callback host has no addresses.")
    allowlisted = True
    for address in addresses:
        ip = ipaddress.ip_address(address)
        explicitly_allowed = any(
            ip.version == network.version and ip in network for network in settings.WEBHOOK_ALLOWED_IPS
        )
        allowlisted = allowlisted and explicitly_allowed
        if not ip.is_global and not explicitly_allowed:
            raise ValueError("The callback destination is not publicly routable.")
    if parsed.scheme != "https" and not allowlisted:
        raise ValueError("Public callbacks require HTTPS.")
    return parsed, addresses, port


def send_callback(url, body, headers):
    parsed, addresses, port = callback_destination(url)
    host = parsed.hostname.encode("idna").decode("ascii")
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    headers = {**headers, "Host": authority}
    # Try only validated IPs in resolver order; TLS verifies the original hostname.
    for index, address in enumerate(addresses):
        pool = (
            urllib3.HTTPSConnectionPool(
                address, port=port, server_hostname=host, assert_hostname=host, ssl_context=ssl.create_default_context()
            )
            if parsed.scheme == "https"
            else urllib3.HTTPConnectionPool(address, port=port)
        )
        try:
            response = pool.urlopen(
                "POST",
                (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                body=body,
                headers=headers,
                redirect=False,
                retries=False,
                preload_content=False,
                timeout=urllib3.Timeout(total=settings.COORDINATION_WEBHOOK_TIMEOUT),
            )
            try:
                return response.status
            finally:
                response.close()
        except (OSError, urllib3.exceptions.HTTPError):
            if index == len(addresses) - 1:
                raise
        finally:
            pool.close()


def event_envelope(event):
    return {
        "specversion": "1.0",
        "id": str(event.pk),
        "type": event.kind,
        "source": f"/projects/{event.project_id}/coordination",
        "subject": f"work-items/{event.issue_id}" if event.issue_id else f"projects/{event.project_id}",
        "time": event.created_at.isoformat(),
        "datacontenttype": "application/json",
        "data": {
            "project_id": str(event.project_id),
            "issue_id": str(event.issue_id) if event.issue_id else None,
            "actor_id": str(event.actor_id) if event.actor_id else None,
            "spec_revision": event.spec_revision,
            "sequence": event.sequence,
            **{key: value for key, value in event.payload.items() if key != "previous_snapshot"},
        },
    }


def subscription_access(subscription, event=None):
    from .execution import require_agent_access, validate_live_run
    from plane.db.models import AgentProjectAccess
    from .contracts import task_owners

    parent = subscription.dispatcher
    project = subscription.project
    if (
        not subscription.is_active
        or project.deleted_at
        or project.archived_at
        or project.workspace.deleted_at
        or parent.deleted_at
        or not parent.is_active
        or not parent.user.is_active
        or parent.purpose != "dispatcher"
        or parent.project_id != project.pk
        or parent.workspace_id != project.workspace_id
        or not isinstance(parent.scopes, list)
        or set(parent.scopes) != {"read", "write"}
        or (parent.expired_at and parent.expired_at <= timezone.now())
    ):
        raise PermissionDenied("This notification subscription is no longer authorized.")
    require_agent_access(parent.user, project, AgentProjectAccess.Role.DISPATCHER)
    if subscription.run_id:
        run = subscription.run
        terminal = bool(
            event and event.kind in ("run.cancelled", "task.merged") and event.payload.get("run_id") == str(run.pk)
        )
        if terminal:
            # The final lifecycle event is deliverable after action keys close.
            require_agent_access(run.agent, project, AgentProjectAccess.Role.WORKER)
            if run.dispatcher_id != parent.pk or run.contract.deleted_at or not task_owners(run.contract).exists():
                raise PermissionDenied("The assignment is no longer accessible.")
        else:
            validate_live_run(run, assignment=True)


def enqueue(delivery_id):
    from plane.bgtasks.coordination_notification_task import deliver_agent_event

    try:
        deliver_agent_event.delay(str(delivery_id))
    except Exception:
        # The database outbox, not broker publication, is the delivery authority.
        logger.exception("Agent notification remains in the outbox after broker publication failed")


def queue_event(event):
    subscriptions = AgentSubscription.objects.filter(
        Q(run__isnull=True) | Q(run__targets__issue_id=event.issue_id),
        Q(event_types=[]) | Q(event_types__contains=[event.kind]),
        project_id=event.project_id,
        is_active=True,
        after_sequence__lt=event.sequence,
    ).distinct()
    for subscription in subscriptions:
        delivery, created = AgentEventDelivery.objects.get_or_create(subscription=subscription, event=event)
        if created:
            transaction.on_commit(lambda identifier=delivery.pk: enqueue(identifier))


@transaction.atomic
def create_subscription(request, project, values):
    token = request.auth
    run = getattr(request, "coordination_run", None) if token.purpose == "assignment" else None
    if token.purpose not in ("assignment", "dispatcher"):
        raise PermissionDenied("Use a dispatcher or assigned-worker key to subscribe.")
    values.setdefault("event_types", WORKER_EVENT_TYPES.copy() if run else ["task.ready"])
    # Serialize registration/catch-up with event commits so no event falls in a gap.
    CoordinationSequence.objects.get_or_create(project=project)
    sequence = CoordinationSequence.objects.select_for_update().get(project=project)
    secret = "whsec_" + base64.b64encode(secrets.token_bytes(hashlib.sha256().digest_size)).decode()
    after = values.pop("after", sequence.last_value)
    if after > sequence.last_value:
        from rest_framework.exceptions import ValidationError

        raise ValidationError({"after": "The cursor is ahead of this project's event log."})
    subscription = AgentSubscription.objects.create(
        project=project,
        dispatcher=run.dispatcher if run else token,
        run=run,
        signing_secret=secret,
        after_sequence=after,
        **values,
    )
    events = CoordinationEvent.objects.filter(project=project, sequence__gt=after)
    if run:
        events = events.filter(issue_id__in=RunTarget.objects.filter(run=run).values("issue_id"))
    if subscription.event_types:
        events = events.filter(kind__in=subscription.event_types)
    for event in events.iterator():
        delivery = AgentEventDelivery.objects.create(subscription=subscription, event=event)
        transaction.on_commit(lambda identifier=delivery.pk: enqueue(identifier))
    return subscription


def deliver(delivery_id):
    now = timezone.now()
    with transaction.atomic():
        delivery = (
            AgentEventDelivery.objects.select_for_update(of=("self",))
            .select_related(
                "subscription__project__workspace",
                "subscription__dispatcher__user",
                "subscription__run__contract__issue",
                "event",
            )
            .get(pk=delivery_id)
        )
        if delivery.status in ("delivered", "failed", "cancelled"):
            return
        if delivery.status == "sending" and delivery.locked_until and delivery.locked_until > now:
            return
        if delivery.next_attempt_at > now:
            return
        try:
            subscription_access(delivery.subscription, delivery.event)
        except PermissionDenied:
            delivery.status, delivery.last_error = "cancelled", "subscription_revoked"
            delivery.save(update_fields=["status", "last_error"])
            return
        if delivery.retry_count > settings.COORDINATION_WEBHOOK_MAX_RETRIES:
            delivery.status = "failed"
            delivery.save(update_fields=["status"])
            return
        delivery.status = "sending"
        delivery.attempts += 1
        # Covers the connection and response-header phases of the transport.
        delivery.locked_until = now + timedelta(seconds=2 * settings.COORDINATION_WEBHOOK_TIMEOUT)
        delivery.save(update_fields=["status", "attempts", "locked_until"])
        attempt = delivery.attempts

    subscription = delivery.subscription
    body = json.dumps(event_envelope(delivery.event), separators=(",", ":"), ensure_ascii=True).encode()
    timestamp = str(int(time.time()))
    signed = f"{delivery.pk}.{timestamp}.".encode() + body
    secret = base64.b64decode(subscription.signing_secret.removeprefix("whsec_"))
    signature = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
    status, error = None, ""
    try:
        status = send_callback(
            subscription.url,
            body,
            {
                "Content-Type": "application/cloudevents+json",
                "webhook-id": str(delivery.pk),
                "webhook-timestamp": timestamp,
                "webhook-signature": "v1," + signature,
            },
        )
        if not 200 <= status < 300:
            error = "http_error"
    except (ValueError, OSError, urllib3.exceptions.HTTPError):
        error = "transport_error"

    with transaction.atomic():
        current = AgentEventDelivery.objects.select_for_update().get(pk=delivery.pk)
        if current.status != "sending" or current.attempts != attempt:
            return
        current.last_status, current.last_error, current.locked_until = status, error, None
        if not error:
            current.status, current.delivered_at = "delivered", timezone.now()
        else:
            current.retry_count += 1
            current.status = "failed" if current.retry_count > settings.COORDINATION_WEBHOOK_MAX_RETRIES else "pending"
            current.next_attempt_at = timezone.now() + timedelta(
                seconds=settings.COORDINATION_WEBHOOK_RETRY_DELAY * 2 ** (current.retry_count - 1)
            )
        current.save()


def dispatch_due():
    now = timezone.now()
    due = AgentEventDelivery.objects.filter(
        Q(status="pending", next_attempt_at__lte=now) | Q(status="sending", locked_until__lte=now)
    )
    for identifier in due.values_list("id", flat=True).iterator():
        enqueue(identifier)
