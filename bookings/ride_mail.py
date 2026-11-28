"""Queue and deliver live ride notices without replaying interrupted SMTP attempts."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from mailimport import outbound

from .models import RideMailDelivery, RideNotice
from .ride_participation import ride_mail_payload
from .services import _lock_import


Status = RideMailDelivery.Status
ACTIVE_DELIVERIES = {Status.PENDING, Status.SENDING}
CLAIM_TIMEOUT = timedelta(minutes=15)


@transaction.atomic
def queue_ride_notices(notices):
    """Prepare first attempts for newly created live notices while outbound mail is enabled."""
    notice_ids = {notice.pk for notice in notices}
    if not notice_ids or not outbound.outbound_enabled():
        return 0
    _lock_import()
    candidates = RideNotice.objects.filter(
        pk__in=notice_ids, participation__case__is_test=False, deliveries__isnull=True,
    ).values_list("pk", flat=True)
    return len(RideMailDelivery.objects.bulk_create([RideMailDelivery(notice_id=pk) for pk in candidates]))


def can_resend_ride_notice(notice, *, now=None):
    if not outbound.outbound_enabled():
        return False
    latest = next(iter(notice.deliveries.all()), None)
    return (
        (latest is None or latest.status not in ACTIVE_DELIVERIES)
        and ride_mail_payload(notice, now=now or timezone.now()) is not None
    )


@transaction.atomic
def queue_ride_resend(notice_id):
    _lock_import()
    notice = RideNotice.objects.filter(pk=notice_id).first()
    if notice is None or not can_resend_ride_notice(notice):
        raise ValueError("Für diese Nachricht kann derzeit kein Versand eingeplant werden.")
    return RideMailDelivery.objects.create(notice=notice)


@transaction.atomic(durable=True)
def _claim_delivery(delivery_id):
    return RideMailDelivery.objects.filter(pk=delivery_id, status=Status.PENDING).update(
        status=Status.SENDING, claimed_at=timezone.now(),
    )


@sensitive_variables("payload")
@transaction.atomic
def _send_claimed_delivery(delivery_id):
    delivery = RideMailDelivery.objects.select_for_update().filter(pk=delivery_id, status=Status.SENDING).first()
    if delivery is None:
        return False
    if not outbound.outbound_enabled():
        delivery.status = Status.PENDING
        delivery.claimed_at = None
        delivery.save(update_fields=["status", "claimed_at"])
        return False

    payload = ride_mail_payload(delivery.notice, now=timezone.now())
    if payload is None:
        delivery.status = Status.CANCELLED
    else:
        try:
            outbound.send_ride_mail(
                recipient=payload["recipient"], subject=payload["subject"], body=payload["body"],
                message_id=f"<bms-ride-{delivery.pk}@{settings.RIDE_PORTAL_HOST}>",
            )
        except outbound.SMTPDeliveryUncertain:
            delivery.status = Status.UNCERTAIN
        except outbound.SMTPDeliveryError:
            delivery.status = Status.FAILED
        else:
            delivery.status = Status.SENT
    delivery.finished_at = timezone.now()
    delivery.save(update_fields=["status", "finished_at"])
    return delivery.status == Status.SENT


def deliver_ride_mail(*, limit=25):
    """Process a batch of pending attempts and return the number accepted by SMTP."""
    if not outbound.outbound_enabled():
        return 0
    now = timezone.now()
    RideMailDelivery.objects.filter(status=Status.SENDING, claimed_at__lte=now - CLAIM_TIMEOUT).update(
        status=Status.UNCERTAIN, finished_at=now,
    )
    pending_ids = list(RideMailDelivery.objects.filter(status=Status.PENDING).order_by("pk").values_list(
        "pk", flat=True,
    )[:limit])
    return sum(_send_claimed_delivery(pk) for pk in pending_ids if _claim_delivery(pk))
