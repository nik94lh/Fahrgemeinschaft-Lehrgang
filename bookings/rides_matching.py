"""Keep round-trip suggestions in sync with the confirmed bookings."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .models import IN_PERSON_SCHOOL_CHOICES, BookingStatus, CourseSeat, EmailEvent, RideCase
from .services import _lock_import


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

    @property
    def key(self) -> str:
        return f"{self.school}:{RideCase.ROUND_TRIP}:{self.travel_date}:{self.return_date}"


def _round_trip_matches(now: datetime):
    seats = CourseSeat.objects.filter(
        session__starts_at__gte=now,
        status=BookingStatus.CONFIRMED,
        participant__isnull=False,
        session__school__in=[code for code, _ in IN_PERSON_SCHOOL_CHOICES],
        session__is_online=False,
    ).exclude(email_events__state=EmailEvent.State.NEEDS_REVIEW).values_list(
        "session__school", "session__starts_at", "session__ends_at", "participant_id", "id",
    )
    groups = defaultdict(list)
    for school, start, end, participant, seat_id in seats.iterator():
        key = (school, start.astimezone(BERLIN).date(), end.astimezone(BERLIN).date())
        groups[key].append((start, end, participant, seat_id))

    for (school, travel_date, return_date), trips in groups.items():
        starts, ends, participants, seat_ids = zip(*trips)
        if len(set(participants)) >= 2:
            yield _Match(
                school, travel_date, return_date,
                min(starts), max(starts), min(ends), max(ends), frozenset(seat_ids),
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
