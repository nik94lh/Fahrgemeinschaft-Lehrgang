"""Audit timeline for a single booked course seat."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from accounts.access import can_edit, can_view
from mailimport.models import MailReceipt

from .booking_edit import BOOKING_SAVED_SESSION_KEY
from .booking_search import booking_list_navigation
from .models import BookingHistory, CourseSeat, EmailEvent


@dataclass(frozen=True)
class _TimelineEntry:
    recorded_at: datetime
    event: EmailEvent | None = None
    changes: tuple[BookingHistory, ...] = ()


def _booking_timeline(seat, *, show_receipts):
    events = seat.email_events.only(
        "kind", "state", "imported_at", "reviewed_at", "review_reason", "review_resolution",
    ).order_by("imported_at", "pk")
    if show_receipts:
        original_receipt = MailReceipt.objects.filter(event_id=OuterRef("pk")).order_by("observed_at", "pk")
        events = events.annotate(receipt_id=Subquery(original_receipt.values("pk")[:1]))
    events = list(events)
    linked_event_ids = {event.pk for event in events}
    changes_by_event = defaultdict(list)
    timeline = []
    for change in seat.history.only("source_event", "recorded_at", "status", "previous_status", "profile_linked", "edited_fields"):
        if change.source_event_id in linked_event_ids:
            changes_by_event[change.source_event_id].append(change)
        else:
            # A history row can refer to a mail currently attached to another seat.
            timeline.append(_TimelineEntry(change.recorded_at, changes=(change,)))
    timeline.extend(
        _TimelineEntry(event.imported_at, event=event, changes=tuple(changes_by_event[event.pk]))
        for event in events
    )
    return sorted(timeline, key=lambda entry: entry.recorded_at)


@never_cache
@require_safe
def booking_detail(request, seat_id):
    seat = get_object_or_404(
        CourseSeat.objects.select_related("participant", "session__course").only(
            "status", "external_reference", "participant__name", "session__course__name",
            "session__school", "session__is_online", "session__starts_at", "session__ends_at",
        ),
        pk=seat_id,
    )
    show_receipts = settings.HTTPS and request.is_secure() and can_view(request.user, "mail_receipts")
    bookings_url, detail_params = booking_list_navigation(request.GET)
    can_edit_booking = can_edit(request.user, "bookings")
    saved = request.session.get(BOOKING_SAVED_SESSION_KEY) == seat.pk
    if saved and request.method == "GET":
        request.session.pop(BOOKING_SAVED_SESSION_KEY)
    return render(request, "workspace/booking_detail.html", {
        "section": "bookings",
        "seat": seat,
        "bookings_url": bookings_url,
        "participant_url": reverse("booking_participant", args=[seat.pk], query=detail_params),
        "edit_url": reverse("booking_edit", args=[seat.pk], query=detail_params) if can_edit_booking else "",
        "delete_url": reverse("booking_delete", args=[seat.pk], query=detail_params) if can_edit_booking else "",
        "saved": saved,
        "timeline": _booking_timeline(seat, show_receipts=show_receipts),
    })
