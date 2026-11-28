"""Keep round-trip suggestions in sync with the confirmed bookings."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailimport.participant_settings import get_functional_addresses

from .booking_state import booking_missing_fields
from .models import IN_PERSON_SCHOOL_CHOICES, BookingStatus, CourseSeat, EmailEvent, RideCase
from .ride_segments import BERLIN, ride_segments
from .services import _lock_import, _normalized_name


_MATCH_FIELDS = [
    "currently_matches", "earliest", "latest", "return_earliest", "return_latest",
    "departure_time_unknown", "return_time_unknown",
]


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
    departure_time_unknown: bool
    return_time_unknown: bool

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
        session__ends_at__gt=now,
        participant__isnull=False,
        session__school__in=[code for code, _ in IN_PERSON_SCHOOL_CHOICES],
        session__is_online=False,
    ).exclude(status=BookingStatus.CANCELLED).select_related("participant", "session__course")
    excluded_seats, excluded_references, excluded_bookings = _open_review_exclusions()
    functional_addresses = set(get_functional_addresses())
    booking_counts = Counter()
    eligible_bookings = {}
    segments_by_session = {}
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
        if session.pk not in segments_by_session:
            segments_by_session[session.pk] = ride_segments(session)
        eligible_bookings[identity] = (seat.pk, segments_by_session[session.pk])

    groups = defaultdict(list)
    for identity, (seat_id, segments) in eligible_bookings.items():
        if booking_counts[identity] != 1:
            continue
        is_test, (_, school, _, _), participant = identity
        for segment in segments:
            if segment.starts_at > now:
                key = (is_test, school, segment.departure_day, segment.return_day)
                groups[key].append((segment, participant, seat_id))

    for (is_test, school, travel_date, return_date), trips in groups.items():
        segments, participants, seat_ids = zip(*trips)
        if len(set(participants)) < 2:
            continue
        departure_unknown = any(not segment.start_known for segment in segments)
        return_unknown = any(not segment.end_known for segment in segments)
        departures = _local_day_bounds(travel_date) if departure_unknown else [segment.starts_at for segment in segments]
        returns = _local_day_bounds(return_date) if return_unknown else [segment.ends_at for segment in segments]
        yield _Match(
            school, travel_date, return_date,
            min(departures), max(departures), min(returns), max(returns), frozenset(seat_ids), is_test,
            departure_time_unknown=departure_unknown, return_time_unknown=return_unknown,
        )


def _local_day_bounds(day: date):
    return datetime.combine(day, time.min, tzinfo=BERLIN), datetime.combine(day, time.max, tzinfo=BERLIN)


def _seat_links(cases):
    links = defaultdict(set)
    associations = RideCase.seats.through.objects.filter(ridecase_id__in=[case.pk for case in cases])
    for case_id, seat_id in associations.values_list("ridecase_id", "courseseat_id").iterator():
        links[case_id].add(seat_id)
    return links


def _refresh_match(case: RideCase, match: _Match) -> bool:
    times = (match.earliest, match.latest, match.return_earliest, match.return_latest)
    if (
        case.currently_matches and (case.earliest, case.latest, case.return_earliest, case.return_latest) == times
        and case.departure_time_unknown == match.departure_time_unknown
        and case.return_time_unknown == match.return_time_unknown
    ):
        return False
    case.currently_matches = True
    case.earliest, case.latest, case.return_earliest, case.return_latest = times
    case.departure_time_unknown = match.departure_time_unknown
    case.return_time_unknown = match.return_time_unknown
    return True


def _inferred_departure_started(case: RideCase, now: datetime) -> bool:
    return case.departure_time_unknown and case.earliest <= now


@transaction.atomic
def sync_ride_cases(*, now: datetime | None = None, match_keys: set[str] | None = None) -> int:
    """Refresh round-trip suggestions while preserving their workflow state."""
    if match_keys is not None and not match_keys:
        return 0
    _lock_import()
    now = now or timezone.now()
    matches = {
        match.key: match for match in _round_trip_matches(now)
        if match_keys is None or match.key in match_keys
    }
    cases = RideCase.objects.in_bulk(matches, field_name="match_key")
    retained_keys = {
        key for key, case in cases.items() if case.removed_at is not None or _inferred_departure_started(case, now)
    }
    matches = {key: match for key, match in matches.items() if key not in retained_keys}
    new = []
    changed = []
    for key, match in matches.items():
        case = cases.get(key)
        if case is None:
            new.append(RideCase(
                match_key=key, school=match.school, direction=RideCase.ROUND_TRIP,
                travel_date=match.travel_date, return_date=match.return_date,
                earliest=match.earliest, latest=match.latest,
                return_earliest=match.return_earliest, return_latest=match.return_latest,
                departure_time_unknown=match.departure_time_unknown, return_time_unknown=match.return_time_unknown,
                is_test=match.is_test,
            ))
        elif _refresh_match(case, match):
            changed.append(case)
    if new:
        RideCase.objects.bulk_create(new)
        cases.update((case.match_key, case) for case in new)
    if changed:
        RideCase.objects.bulk_update(changed, _MATCH_FIELDS)

    links = _seat_links(cases[key] for key in matches)
    for key, match in matches.items():
        case = cases[key]
        if links[case.pk] != match.seat_ids:
            case.seats.set(match.seat_ids)

    obsolete = RideCase.objects.filter(currently_matches=True).exclude(match_key__in=matches)
    if match_keys is not None:
        obsolete = obsolete.filter(match_key__in=match_keys)
    obsolete.update(currently_matches=False)
    return len(matches)


@transaction.atomic
def remove_ride_case(case_id: int, *, now: datetime | None = None) -> RideCase:
    """Pause the proposal and invitations while keeping its previous state."""
    _lock_import()
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    if case is None:
        raise ValueError("Dieser Fahrgemeinschaftsvorschlag ist nicht mehr vorhanden.")
    if case.removed_at is None:
        case.removed_at = now or timezone.now()
        case.currently_matches = False
        case.save(update_fields=["removed_at", "currently_matches"])
    return case


@transaction.atomic
def restore_ride_case(case_id: int, *, now: datetime | None = None) -> RideCase:
    """Restore normal matching for a proposal, preserving its identity and history."""
    _lock_import()
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    if case is None:
        raise ValueError("Dieser Fahrgemeinschaftsvorschlag ist nicht mehr vorhanden.")
    if case.removed_at is None:
        return case

    now = now or timezone.now()
    match = None
    if not _inferred_departure_started(case, now):
        match = next((match for match in _round_trip_matches(now) if match.key == case.match_key), None)
    case.removed_at = None
    case.currently_matches = False
    if match is not None:
        _refresh_match(case, match)
    case.save(update_fields=["removed_at", *_MATCH_FIELDS])
    if match is not None:
        case.seats.set(match.seat_ids)
    return case


@transaction.atomic
def set_ride_confirmed(case_id: int, confirmed: bool, *, now: datetime | None = None) -> RideCase:
    """Record whether the proposed round trip actually took place."""
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    now = now or timezone.now()
    if (
        case is None or case.removed_at is not None or case.direction != RideCase.ROUND_TRIP
        or case.return_latest > now
    ):
        raise ValueError("Erst nach der Rückfahrt kann die Durchführung bestätigt werden.")
    if confirmed != (case.ride_confirmed_at is not None):
        case.ride_confirmed_at = now if confirmed else None
        case.save(update_fields=["ride_confirmed_at"])
    return case


@transaction.atomic
def set_ride_case_status(case_id: int, status: str, *, expected_status: str) -> RideCase:
    """Apply an operator's decision only if the workflow state is still current."""
    if status not in RideCase.Status.values:
        raise ValueError("Wähle einen gültigen Bearbeitungsstand.")
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    if case is None or case.removed_at is not None or case.status != expected_status:
        raise ValueError("Der Bearbeitungsstand hat sich geändert. Lade die Seite neu.")
    if case.status != status:
        case.status = status
        case.save(update_fields=["status"])
    return case
