from celery import shared_task

from . import holds, webhooks


@shared_task
def deliver_webhook(delivery_id: str) -> bool:
    return webhooks.deliver(delivery_id)


@shared_task
def retry_webhooks() -> int:
    return webhooks.retry_due()


@shared_task
def expire_holds() -> int:
    return holds.expire_overdue()


@shared_task
def purge_idempotency_keys(hours: int = 48) -> int:
    """Retries come within minutes; a stored answer older than two days is never needed."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import IdempotencyRecord
    return IdempotencyRecord.objects.filter(created_at__lt=timezone.now() - timedelta(hours=hours)).delete()[0]
