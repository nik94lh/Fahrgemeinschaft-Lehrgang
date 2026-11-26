"""Keep round-trip suggestions in sync with the confirmed bookings."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailimport.participant_settings import get_functional_addresses

from .booking_state import booking_missing_fields
from .models import IN_PERSON_SCHOOL_CHOICES, BookingStatus, CourseSeat, EmailEvent, RideCase
from .services import _lock_import, _normalized_name


BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True, slots=True)
class _Match:
    school: str
    travel_date: date
    return_date: date
    earliest: datetime
    latest: datetime
    return_earliest: datetime
    return_latest: datetime
    seat_ids: frozenset[int]
    is_test: bool

    @property
    def key(self) -> str:
        prefix = "test:" if self.is_test else ""
        return f"{prefix}{self.school}:{RideCase.ROUND_TRIP}:{self.travel_date}:{self.return_date}"


def _review_booking(kind, context):
    name_field = "previous_participant_name" if kind == EmailEvent.Kind.REASSIGNMENT else "participant_name"
    course, school, start, end, name = (
        context.get(field) for field in ("course_name", "school", "starts_at", "ends_at", name_field)
    )
    if context.get("is_online") is True or not all(isinstance(value, str) and value for value in (
        course, school, start, end, name,
    )):
        return None
    try:
        starts_at, ends_at = parse_datetime(start), parse_datetime(end)
    except ValueError:
        return None
    if not starts_at or not ends_at or timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        return None
    return (course.casefold(), school, starts_at, ends_at), _normalized_name(name)


def _open_review_exclusions():
    seat_ids, references, bookings = set(), set(), set()
    referenced_bookings = defaultdict(set)
    events = EmailEvent.objects.filter(state=EmailEvent.State.NEEDS_REVIEW).values_list(
        "seat_id", "kind", "review_context",
    )
    for seat_id, kind, context in events.iterator():
        if seat_id is not None:
            seat_ids.add(seat_id)
            continue
        if not isinstance(context, dict):
            continue
        candidate_id = context.get("candidate_seat_id")
        reference = context.get("booking_reference")
        has_candidate = type(candidate_id) is int and candidate_id > 0
        has_reference = isinstance(reference, str) and bool(reference.strip())
        if has_candidate:
            seat_ids.add(candidate_id)
        if has_reference:
            references.add(reference)
        if not has_candidate and (booking := _review_booking(kind, context)):
            if has_reference:
                referenced_bookings[reference].add(booking)
            else:
                bookings.add(booking)

    if referenced_bookings:
        known_references = set(CourseSeat.objects.filter(is_test=False, external_reference__in=referenced_bookings).values_list(
            "external_reference", flat=True,
        ))
        # An exact booking reference takes precedence over the recognized details.
        for reference in referenced_bookings.keys() - known_references:
            bookings.update(referenced_bookings[reference])
    return seat_ids, references, bookings


def _round_trip_matches(now: datetime):
    seats = CourseSeat.objects.filter(
        session__starts_at__gt=now,
        participant__isnull=False,
        session__school__in=[code for code, _ in IN_PERSON_SCHOOL_CHOICES],
        session__is_online=False,
    ).exclude(status=BookingStatus.CANCELLED).select_related("participant", "session__course")
    excluded_seats, excluded_references, excluded_bookings = _open_review_exclusions()
    functional_addresses = set(get_functional_addresses())
    booking_counts = Counter()
    eligible_bookings = {}
    for seat in seats.iterator():
        session = seat.session
        term = (session.course.name.casefold(), session.school, session.starts_at, session.ends_at)
        identity = (seat.is_test, term, seat.participant_id)
        booking_counts[identity] += 1
        has_open_review = not seat.is_test and (
            seat.pk in excluded_seats
            or seat.external_reference in excluded_references
            or (term, _normalized_name(seat.participant.name)) in excluded_bookings
        )
        if (
            seat.status != BookingStatus.CONFIRMED
            or has_open_review
            or booking_missing_fields(seat, functional_addresses=functional_addresses)
        ):
            continue
        eligible_bookings[identity] = seat.pk

    groups = defaultdict(list)
    for identity, seat_id in eligible_bookings.items():
        if booking_counts[identity] != 1:
            continue
        is_test, (_, school, start, end), participant = identity
        key = (is_test, school, start.astimezone(BERLIN).date(), end.astimezone(BERLIN).date())
        groups[key].append((start, end, participant, seat_id))

    for (is_test, school, travel_date, return_date), trips in groups.items():
        starts, ends, participants, seat_ids = zip(*trips)
        if len(set(participants)) >= 2:
            yield _Match(
                school, travel_date, return_date,
                min(starts), max(starts), min(ends), max(ends), frozenset(seat_ids), is_test,
            )


def _seat_links(cases):
    links = defaultdict(set)
    associations = RideCase.seats.through.objects.filter(ridecase_id__in=[case.pk for case in cases])
    for case_id, seat_id in associations.values_list("ridecase_id", "courseseat_id").iterator():
        links[case_id].add(seat_id)
    return links


@transaction.atomic
def sync_ride_cases(*, now: datetime | None = None) -> int:
    """Refresh round-trip suggestions while preserving their workflow state."""
    _lock_import()
    matches = {match.key: match for match in _round_trip_matches(now or timezone.now())}
    cases = RideCase.objects.in_bulk(matches, field_name="match_key")
    new = []
    changed = []
    for key, match in matches.items():
        case = cases.get(key)
        times = (match.earliest, match.latest, match.return_earliest, match.return_latest)
        if case is None:
            new.append(RideCase(
                match_key=key, school=match.school, direction=RideCase.ROUND_TRIP,
                travel_date=match.travel_date, return_date=match.return_date,
                earliest=match.earliest, latest=match.latest,
                return_earliest=match.return_earliest, return_latest=match.return_latest,
                is_test=match.is_test,
            ))
        elif not case.currently_matches or (case.earliest, case.latest, case.return_earliest, case.return_latest) != times:
            case.currently_matches = True
            case.earliest, case.latest, case.return_earliest, case.return_latest = times
            changed.append(case)
    if new:
        RideCase.objects.bulk_create(new)
        cases.update((case.match_key, case) for case in new)
    if changed:
        RideCase.objects.bulk_update(changed, ["currently_matches", "earliest", "latest", "return_earliest", "return_latest"])

    links = _seat_links(cases.values())
    for key, match in matches.items():
        case = cases[key]
        if links[case.pk] != match.seat_ids:
            case.seats.set(match.seat_ids)

    RideCase.objects.filter(currently_matches=True).exclude(match_key__in=matches).update(currently_matches=False)
    return len(matches)


@transaction.atomic
def set_ride_case_status(case_id: int, status: str, *, expected_status: str) -> RideCase:
    """Apply an operator's decision only if the workflow state is still current."""
    if status not in RideCase.Status.values:
        raise ValueError("Wähle einen gültigen Bearbeitungsstand.")
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    if case is None or case.status != expected_status:
        raise ValueError("Der Bearbeitungsstand hat sich geändert. Lade die Seite neu.")
    if case.status != status:
        case.status = status
        case.save(update_fields=["status"])
    return case
