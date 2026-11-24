"""Persist potential rides separately from the operator's workflow decisions."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from .models import BookingStatus, Carpool, CourseSeat, EmailEvent, RideCase, School
from .services import _lock_import


BERLIN = ZoneInfo("Europe/Berlin")
_LEGACY_STATUS_PRIORITY = {RideCase.Status.IN_REVIEW: 2, RideCase.Status.CLOSED: 1}


@dataclass(frozen=True, slots=True)
class _Match:
    school: School
    direction: Carpool.Direction
    travel_date: date
    earliest: datetime
    latest: datetime
    seat_ids: frozenset[int]

    @property
    def key(self) -> str:
        return f"{self.school}:{self.direction}:{self.travel_date.isoformat()}"


def _trip_groups(now: datetime):
    seats = CourseSeat.objects.filter(
        session__ends_at__gte=now,
        status=BookingStatus.CONFIRMED,
        participant__isnull=False,
        session__school__in=School.values,
    ).exclude(email_events__state=EmailEvent.State.NEEDS_REVIEW).values_list(
        "session__school", "session__starts_at", "session__ends_at", "participant_id", "id",
    )
    groups = defaultdict(list)
    for school, start, end, participant, seat_id in seats.iterator():
        for direction, instant in ((Carpool.Direction.OUTBOUND, start), (Carpool.Direction.RETURN, end)):
            if instant >= now:
                travel_date = instant.astimezone(BERLIN).date()
                groups[(School(school), direction, travel_date)].append((instant, participant, seat_id))
    return groups


def _seat_links(cases):
    links = defaultdict(set)
    associations = RideCase.seats.through.objects.filter(ridecase_id__in=[case.pk for case in cases])
    for case_id, seat_id in associations.values_list("ridecase_id", "courseseat_id").iterator():
        links[case_id].add(seat_id)
    return links


def _adopt_legacy_return_cases(matches, cases):
    pending = {
        (match.school, match.travel_date): match
        for key, match in matches.items()
        if key not in cases and match.direction == Carpool.Direction.RETURN
    }
    if not pending:
        return

    legacy = RideCase.objects.select_for_update().filter(
        school__in={school for school, _ in pending},
        direction=Carpool.Direction.RETURN,
        travel_date__in={day for _, day in pending},
    )
    options = []
    for case in legacy:
        match = pending.get((case.school, case.travel_date))
        if match and case.match_key.startswith(f"{match.key}:"):
            options.append((match, case))

    links = _seat_links(case for _, case in options)
    candidates = defaultdict(list)
    for match, case in options:
        shared = len(links[case.pk] & match.seat_ids)
        if shared:
            priority = _LEGACY_STATUS_PRIORITY.get(case.status, 0)
            candidates[match.key].append((priority, shared, -case.pk, case))

    adopted = []
    for key, choices in candidates.items():
        # Preserve work in progress and closed decisions before adopting an open case.
        case = max(choices, key=lambda choice: choice[:3])[3]
        case.match_key = key
        cases[key] = case
        adopted.append(case)
    if adopted:
        RideCase.objects.bulk_update(adopted, ["match_key"])


@transaction.atomic
def sync_ride_cases(*, now: datetime | None = None) -> int:
    """Refresh qualifying seats while preserving each case's workflow status."""
    _lock_import()
    groups = _trip_groups(now or timezone.now())
    matches = {}
    for (school, direction, travel_date), trips in groups.items():
        if len({participant for _, participant, _ in trips}) < 2:
            continue
        times = [instant for instant, _, _ in trips]
        match = _Match(
            school, direction, travel_date, min(times), max(times),
            frozenset(seat_id for _, _, seat_id in trips),
        )
        matches[match.key] = match
    cases = RideCase.objects.in_bulk(matches, field_name="match_key")
    _adopt_legacy_return_cases(matches, cases)
    new = []
    changed = []
    for key, match in matches.items():
        case = cases.get(key)
        if case is None:
            new.append(RideCase(
                match_key=key, school=match.school, direction=match.direction, travel_date=match.travel_date,
                earliest=match.earliest, latest=match.latest,
            ))
        elif (case.currently_matches, case.earliest, case.latest) != (True, match.earliest, match.latest):
            case.currently_matches = True
            case.earliest = match.earliest
            case.latest = match.latest
            changed.append(case)
    if new:
        RideCase.objects.bulk_create(new)
        cases.update((case.match_key, case) for case in new)
    if changed:
        RideCase.objects.bulk_update(changed, ["currently_matches", "earliest", "latest"])

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
        raise ValueError("Wähle einen gültigen Fallstatus.")
    case = RideCase.objects.select_for_update().filter(pk=case_id).first()
    if case is None or case.status != expected_status:
        raise ValueError("Der Fallstatus hat sich geändert. Lade die Seite neu.")
    if case.status != status:
        case.status = status
        case.save(update_fields=["status"])
    return case
