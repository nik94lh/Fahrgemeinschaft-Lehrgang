"""Potential ride windows based only on confirmed course appointments."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import BookingStatus, Carpool, CourseSeat, EmailEvent, School


LOOKAHEAD = timedelta(days=28)
TOLERANCE = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class Suggestion:
    school: School
    direction: Carpool.Direction
    earliest: datetime
    latest: datetime
    participant_count: int


def _trip_groups(now, horizon):
    possible = CourseSeat.objects.filter(
        Q(session__starts_at__gte=now, session__starts_at__lt=horizon)
        | Q(session__ends_at__gte=now, session__ends_at__lt=horizon),
        status=BookingStatus.CONFIRMED,
        participant__isnull=False,
        session__school__in=School.values,
    ).exclude(email_events__state=EmailEvent.State.NEEDS_REVIEW).values_list(
        "session__school", "session__starts_at", "session__ends_at", "participant_id",
    )
    groups = defaultdict(set)
    for school, start, end, participant in possible.iterator():
        for direction, instant in ((Carpool.Direction.OUTBOUND, start), (Carpool.Direction.RETURN, end)):
            if now <= instant < horizon:
                day = timezone.localtime(instant).date()
                groups[(School(school), direction, day)].add((instant, participant))
    return groups


def _candidate_windows(trips, school, direction):
    ordered = sorted(trips)
    counts = Counter()
    right = 0
    for earliest, participant in ordered:
        previous_right = right
        while right < len(ordered) and ordered[right][0] - earliest <= TOLERANCE:
            counts[ordered[right][1]] += 1
            right += 1
        # A window with the same end as its predecessor would only repeat a subset.
        if right > previous_right and len(counts) >= 2:
            yield Suggestion(school, direction, earliest, ordered[right - 1][0], len(counts))
        counts[participant] -= 1
        if not counts[participant]:
            del counts[participant]


@never_cache
@require_safe
def rides(request):
    now = timezone.now()
    suggestions = [
        suggestion
        for (school, direction, _day), trips in _trip_groups(now, now + LOOKAHEAD).items()
        for suggestion in _candidate_windows(trips, school, direction)
    ]
    suggestions.sort(key=lambda suggestion: (suggestion.earliest, suggestion.direction, suggestion.school))
    return render(request, "workspace/rides.html", {
        "section": "rides",
        "page_obj": Paginator(suggestions, 25).get_page(request.GET.get("page")),
        "lookahead_days": LOOKAHEAD.days,
        "tolerance_minutes": int(TOLERANCE.total_seconds() / 60),
    })
