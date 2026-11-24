"""Read-only month and week views for the schedule."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Count
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import CourseSession, School


DAY = timedelta(days=1)
WEEK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class _Period:
    view: str
    start: date
    stop: date
    previous: date

    @property
    def last_day(self):
        return self.stop - DAY


def _selected_date(value, default):
    if value:
        try:
            candidate = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if date.min.year < candidate.year < date.max.year:
                return candidate
    return default


def _week_start(day):
    return day - timedelta(days=day.weekday())


def _next_month(month):
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def _selected_period(params, today):
    if params.get("view") == "week":
        start = _week_start(_selected_date(params.get("day"), today))
        return _Period("week", start, start + WEEK, start - WEEK)

    value = params.get("month", "")
    start = _selected_date(f"{value}-01", today.replace(day=1))
    return _Period("month", start, _next_month(start), (start - DAY).replace(day=1))


def _selected_school(params):
    value = params.get("school", "")
    return value if value in School.values else ""


def _query_string(view, anchor, school):
    if view == "week":
        parameters = {"view": "week", "day": anchor.isoformat()}
    else:
        parameters = {"month": anchor.isoformat()[:7]}
    if school:
        parameters["school"] = school
    return urlencode(parameters)


def _ticks(period, today):
    ticks = []
    for offset in range((period.stop - period.start).days):
        day = period.start + timedelta(days=offset)
        ticks.append({
            "date": day,
            "number": day.day,
            "is_today": day == today,
            "is_weekend": day.weekday() >= 5,
        })
    return ticks


def _timeline_rows(sessions, period, starts, ends):
    by_school = defaultdict(list)
    for session in sessions:
        start = timezone.localtime(session.starts_at)
        end = timezone.localtime(session.ends_at)
        first_day = max(start.date(), period.start)
        last_day = end.date()
        if end.time() == time.min:
            last_day -= DAY
        last_day = min(last_day, period.last_day)
        by_school[session.school].append({
            "session": session,
            "start_column": (first_day - period.start).days + 1,
            "end_column": (last_day - period.start).days + 2,
            "starts_before": session.starts_at < starts,
            "ends_after": session.ends_at > ends,
        })

    return [
        {"school": school, "label": school.label, "rows": by_school[school]}
        for school in School
        if by_school[school]
    ]


@never_cache
@require_safe
def calendar(request):
    today = timezone.localdate()
    period = _selected_period(request.GET, today)
    school = _selected_school(request.GET)
    starts = timezone.make_aware(datetime.combine(period.start, time.min))
    ends = timezone.make_aware(datetime.combine(period.stop, time.min))
    sessions = CourseSession.objects.filter(
        starts_at__lt=ends, ends_at__gt=starts, school__in=School.values,
    )
    if school:
        sessions = sessions.filter(school=school)
    sessions = list(sessions.select_related("course").annotate(seat_count=Count("seats")).order_by(
        "starts_at", "course__name", "pk"
    ))

    # The Thursday of a cross-month week determines its month in the month zoom.
    month_anchor = period.start
    if period.view == "week":
        month_anchor += timedelta(days=3)
    week_anchor = period.start
    if period.view == "month":
        if period.start <= today < period.stop:
            week_anchor = today
        elif sessions:
            week_anchor = max(period.start, timezone.localtime(sessions[0].starts_at).date())
    ticks = _ticks(period, today)
    return render(request, "workspace/calendar.html", {
        "section": "calendar",
        "view": period.view,
        "month": month_anchor.replace(day=1),
        "month_id": month_anchor.isoformat()[:7],
        "day_id": period.start.isoformat(),
        "period_start": period.start,
        "period_end": period.last_day,
        "ticks": ticks,
        "day_count": len(ticks),
        "school_groups": _timeline_rows(sessions, period, starts, ends),
        "sessions": sessions,
        "selected_school": school,
        "school_choices": School.choices,
        "previous_query": _query_string(period.view, period.previous, school),
        "next_query": _query_string(period.view, period.stop, school),
        "current_query": _query_string(period.view, period.start, school),
        "month_query": _query_string("month", month_anchor, school),
        "week_query": _query_string("week", _week_start(week_anchor), school),
    })


@never_cache
@require_safe
def calendar_detail(request, session_id):
    session = get_object_or_404(CourseSession.objects.select_related("course"), pk=session_id)
    period = _selected_period(request.GET, timezone.localdate())
    back_query = _query_string(period.view, period.start, _selected_school(request.GET))
    return render(request, "workspace/calendar_detail.html", {
        "section": "calendar",
        "session": session,
        "seats": session.seats.select_related("participant").order_by("participant__name", "pk"),
        "back_href": f"{reverse('calendar')}?{back_query}",
    })
