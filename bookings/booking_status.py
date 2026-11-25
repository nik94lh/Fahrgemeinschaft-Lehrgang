from collections import Counter
from dataclasses import dataclass

from django.db.models import Prefetch, prefetch_related_objects

from .models import BookingHistory, BookingStatus, CourseSeat, EmailEvent
from .services import ACTIVATION_EVENTS, ACTIVE_STATUSES


APPROVAL_STATUSES = {BookingStatus.APPROVED, BookingStatus.AWAITING_CONFIRMATION}


@dataclass(frozen=True, slots=True)
class BookingStatusCard:
    label: str
    value: str
    tone: str


def booking_status_prefetches():
    return (
        Prefetch(
            "history",
            queryset=BookingHistory.objects.select_related("source_event").only(
                "seat_id", "participant_id", "previous_participant_id", "status", "recorded_at", "profile_linked",
                "source_event__kind", "source_event__state", "source_event__seat_id",
                "source_event__processed_participant_id",
            ).order_by("recorded_at", "pk"),
            to_attr="status_history",
        ),
        Prefetch(
            "email_events",
            queryset=EmailEvent.objects.filter(
                state=EmailEvent.State.APPLIED, kind__in=ACTIVATION_EVENTS, processed_participant__isnull=False,
            ).only("seat_id", "kind", "processed_participant_id", "imported_at"),
            to_attr="status_events",
        ),
    )


def _activation_evidence(seat: CourseSeat) -> set[str]:
    prefetch_related_objects([seat], *booking_status_prefetches())
    history = seat.status_history
    participant_id = seat.participant_id
    if history and history[-1].participant_id != participant_id:
        return set()

    # Only a verified link between profiles can continue the same assignment under another ID.
    participant_ids = {participant_id}
    assignment = []
    for change in reversed(history):
        if change.participant_id != participant_id:
            break
        assignment.append(change)
        if change.previous_participant_id != participant_id:
            if not change.profile_linked or change.previous_participant_id is None:
                break
            participant_id = change.previous_participant_id
            participant_ids.add(participant_id)

    start = assignment[-1] if assignment else None
    if start and len(assignment) == len(history) and start.previous_participant_id == participant_id:
        start = None

    kinds = {
        event.kind for event in seat.status_events
        if event.processed_participant_id in participant_ids
        and (start is None or event.pk == start.source_event_id or event.imported_at >= start.recorded_at)
    }
    source_counts = Counter(change.source_event_id for change in history if change.source_event_id)
    for change in assignment:
        event = change.source_event
        if (
            event is not None and source_counts[event.pk] == 1 and event.seat_id == seat.pk
            and event.state == EmailEvent.State.APPLIED and event.kind in ACTIVATION_EVENTS
            and event.processed_participant_id in (None, change.participant_id)
            and change.status in ACTIVE_STATUSES
        ):
            kinds.add(event.kind)
    return kinds


def _card(label: str, value: bool | None, *, positive_value: bool = True) -> BookingStatusCard:
    if value is None:
        return BookingStatusCard(label, "Offen", "open")
    tone = "positive" if value == positive_value else "negative"
    return BookingStatusCard(label, "Ja" if value else "Nein", tone)


def booking_status_cards(seat: CourseSeat) -> tuple[BookingStatusCard, ...]:
    """Describe the booking's current participant assignment and effective cancellation."""
    active = seat.participant_id is not None and seat.status in ACTIVE_STATUSES
    kinds = _activation_evidence(seat) if seat.participant_id is not None else set()
    registered = True if active or kinds else None
    approved = True if (active and seat.status in APPROVAL_STATUSES) or EmailEvent.Kind.APPROVAL in kinds else None
    cancelled = True if seat.status == BookingStatus.CANCELLED else None
    if active:
        cancelled = False
    return (
        _card("Angemeldet", registered),
        _card("Genehmigt", approved),
        _card("Storniert", cancelled, positive_value=False),
    )
