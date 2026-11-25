from datetime import timedelta
from ipaddress import IPv6Address, ip_address
from math import ceil

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import LoginAttemptBucket


ATTEMPT_LIMIT = 10
WINDOW = timedelta(minutes=1)
CLEANUP_BATCH = 64


def _canonical_address(value):
    try:
        address = ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.packed.hex()


def _client_address(request):
    if settings.HTTPS:
        # Caddy is the only ingress in this mode and supplies the last hop.
        forwarded = request.headers.get("X-Forwarded-For", "").rsplit(",", 1)[-1]
        if address := _canonical_address(forwarded):
            return address
    return _canonical_address(request.META.get("REMOTE_ADDR", "")) or "unknown"


def _clean_expired_buckets(window_start):
    # Keep the preceding window briefly for requests crossing a minute boundary.
    expired_keys = list(
        LoginAttemptBucket.objects.filter(window_start__lt=window_start - WINDOW)
        .order_by("window_start").values_list("pk", flat=True)[:CLEANUP_BATCH]
    )
    if expired_keys:
        LoginAttemptBucket.objects.filter(pk__in=expired_keys).delete()


def login_retry_after(request):
    """Reserve this login attempt, returning seconds to wait if the bucket is full."""
    now = timezone.now()
    window_start = now.replace(second=0, microsecond=0)
    key = salted_hmac(
        "accounts.login-throttle", f"{window_start.isoformat()}:{_client_address(request)}", algorithm="sha256",
    ).hexdigest()
    _, created = LoginAttemptBucket.objects.get_or_create(
        pk=key, defaults={"window_start": window_start, "attempts": 1},
    )
    # The database tests the cap and increments in the same write across workers.
    if not created and not LoginAttemptBucket.objects.filter(pk=key, attempts__lt=ATTEMPT_LIMIT).update(
        attempts=F("attempts") + 1,
    ):
        return ceil((window_start + WINDOW - now).total_seconds())

    _clean_expired_buckets(window_start)
    return None
