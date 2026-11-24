"""Persist potential rides separately from the operator's workflow decisions."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import BookingStatus, Carpool, CourseSeat, EmailEvent, RideCase, School
from .services import _lock_import


LOOKAHEAD = timedelta(days=28)
RETURN_TOLERANCE = timedelta(minutes=60)
BERLIN = ZoneInfo("Europe/Berlin")


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
        day_key = f"{self.school}:{self.direction}:{self.travel_date.isoformat()}"
        if self.direction == Carpool.Direction.OUTBOUND:
            return day_key
        return f"{day_key}:{self.earliest.astimezone(UTC).isoformat()}"


def _trip_groups(now: datetime):
    horizon = now + LOOKAHEAD
    seats = CourseSeat.objects.filter(
        Q(session__starts_at__gte=now, session__starts_at__lt=horizon)
        | Q(session__ends_at__gte=now, session__ends_at__lt=horizon),
        status=BookingStatus.CONFIRMED,
        participant__isnull=False,
        session__school__in=School.values,
    ).exclude(email_events__state=EmailEvent.State.NEEDS_REVIEW).values_list(
        "session__school", "session__starts_at", "session__ends_at", "participant_id", "id",
    )
    groups = defaultdict(list)
    for school, start, end, participant, seat_id in seats.iterator():
        for direction, instant in ((Carpool.Direction.OUTBOUND, start), (Carpool.Direction.RETURN, end)):
            if now <= instant < horizon:
                travel_date = instant.astimezone(BERLIN).date()
                groups[(School(school), direction, travel_date)].append((instant, participant, seat_id))
    return groups


def _matches_for_group(school, direction, travel_date, trips):
    ordered = sorted(trips)
    if direction == Carpool.Direction.OUTBOUND:
        if len({participant for _, participant, _ in ordered}) >= 2:
            yield _Match(
                school, direction, travel_date, ordered[0][0], ordered[-1][0],
                frozenset(seat_id for _, _, seat_id in ordered),
            )
        return

    counts = Counter()
    right = 0
    for left, (earliest, participant, _) in enumerate(ordered):
        previous_right = right
        while right < len(ordered) and ordered[right][0] - earliest <= RETURN_TOLERANCE:
            counts[ordered[right][1]] += 1
            right += 1
        # Later left bounds with the same right bound only repeat subsets.
        if right > previous_right and len(counts) >= 2:
            yield _Match(
                school, direction, travel_date, earliest, ordered[right - 1][0],
                frozenset(seat_id for _, _, seat_id in ordered[left:right]),
            )
        counts[participant] -= 1
        if not counts[participant]:
            del counts[participant]


@transaction.atomic
def sync_ride_cases(*, now: datetime | None = None) -> int:
    """Refresh qualifying seats while preserving each case's workflow status."""
    _lock_import()
    groups = _trip_groups(now or timezone.now())
    matches = {
        match.key: match
        for (school, direction, travel_date), trips in groups.items()
        for match in _matches_for_group(school, direction, travel_date, trips)
    }
    cases = RideCase.objects.in_bulk(matches, field_name="match_key")
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

    links = defaultdict(set)
    associations = RideCase.seats.through.objects.filter(ridecase_id__in=[case.pk for case in cases.values()])
    for case_id, seat_id in associations.values_list("ridecase_id", "courseseat_id").iterator():
        links[case_id].add(seat_id)
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
