from celery import shared_task
from django.conf import settings


# Bound a stalled DNS/TLS/header exchange by the same two-phase transport lease.
@shared_task(time_limit=2 * settings.COORDINATION_WEBHOOK_TIMEOUT)
def deliver_agent_event(delivery_id):
    from plane.coordination.notifications import deliver

    deliver(delivery_id)


@shared_task
def dispatch_agent_events():
    from plane.coordination.notifications import dispatch_due

    dispatch_due()
