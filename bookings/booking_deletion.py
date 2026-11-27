"""Delete one explicitly confirmed local booking."""

from datetime import UTC
from enum import StrEnum

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.views.decorators.debug import sensitive_variables

from .models import CourseSeat, EmailEvent
from .rides_matching import sync_ride_cases
from .services import _lock_import


UNVERIFIED_BOOKING_REVIEW_REASONS = (
    EmailEvent.ReviewReason.INVALID_REFERENCE,
    EmailEvent.ReviewReason.REFERENCE_MISMATCH,
    EmailEvent.ReviewReason.MULTIPLE_SEATS,
    EmailEvent.ReviewReason.MULTIPLE_SESSIONS,
    EmailEvent.ReviewReason.MULTIPLE_COURSES,
)


class BookingDeletionResult(StrEnum):
    DELETED = "deleted"
    STALE = "stale"
    MISSING = "missing"


@sensitive_variables()
def booking_snapshot(seat: CourseSeat, *, purpose: str) -> str:
    """Sign the current booking and its most recent events for a write form."""
    session = seat.session
    participant = seat.participant
    latest_history_id = seat.history.order_by("-pk").values_list("pk", flat=True).first()
    latest_event = seat.email_events.order_by("-pk").values_list("pk", "state", "review_resolution").first()
    open_review_ids = tuple(seat.email_events.filter(state=EmailEvent.State.NEEDS_REVIEW).order_by("pk").values_list(
        "pk", flat=True,
    ))
    facts = (
        seat.pk, str(seat.status), seat.external_reference, seat.confirmation_deadline,
        (participant.pk, participant.name, participant.email) if participant else None,
        session.pk, session.course_id, session.course.name, str(session.school), session.is_online,
        session.starts_at.astimezone(UTC), session.ends_at.astimezone(UTC),
        latest_history_id, latest_event, open_review_ids,
    )
    return salted_hmac(purpose, repr(facts), algorithm="sha256").hexdigest()


def deletion_token(seat: CourseSeat) -> str:
    return booking_snapshot(seat, purpose="bookings.delete_booking")


@transaction.atomic
def delete_booking(seat_id: int, snapshot: str) -> BookingDeletionResult:
    """Delete the selected booking after its signed confirmation is revalidated."""
    _lock_import()
    seat = CourseSeat.objects.select_for_update().filter(pk=seat_id).first()
    if seat is None:
        return BookingDeletionResult.MISSING
    if not isinstance(snapshot, str) or not constant_time_compare(snapshot, deletion_token(seat)):
        return BookingDeletionResult.STALE

    seat.email_events.filter(state=EmailEvent.State.NEEDS_REVIEW).exclude(
        review_reason__in=UNVERIFIED_BOOKING_REVIEW_REASONS,
    ).update(
        state=EmailEvent.State.REVIEWED,
        review_resolution=EmailEvent.ReviewResolution.BOOKING_DELETED,
        reviewed_at=timezone.now(),
    )
    match_keys = set(seat.ride_cases.values_list("match_key", flat=True))
    seat.delete()
    sync_ride_cases(match_keys=match_keys)
    return BookingDeletionResult.DELETED
