"""Read-only year, month and week views for the schedule."""
from calendar import Calendar
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import BookingStatus, CourseSession, School


DAY = timedelta(days=1)
WEEK = timedelta(days=7)
BERLIN = ZoneInfo("Europe/Berlin")
TIMELINE_SIDE_MONTHS = 2
FIRST_TIMELINE_DAY = date(2, 1, 1)
TIMELINE_STOP = date(9999, 1, 1)
MONTH_CHOICES = tuple(date(2000, month, 1) for month in range(1, 13))
MONTH_OCCUPANCY_RANGES = (
    (0, "0"), (1, "1"), (5, "2–5"), (10, "6–10"), (20, "11–20"), (31, "21–31"),
)


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


def _shift_month(month, months):
    year, month_index = divmod(month.year * 12 + month.month - 1 + months, 12)
    return date(year, month_index + 1, 1)


def _selected_month(value, default):
    return _selected_date(f"{value}-01", default) if len(value) == 7 else default


def _selected_period(params, today):
    view = params.get("view")
    if view == "year":
        value = params.get("year", "")
        start = _selected_date(f"{value}-01-01", today.replace(month=1, day=1))
        return _Period("year", start, start.replace(year=start.year + 1), start.replace(year=start.year - 1))

    if view == "week":
        start = _week_start(_selected_date(params.get("day"), today))
        return _Period("week", start, start + WEEK, start - WEEK)

    value = params.get("month", "")
    month_number = params.get("month_number", "")
    if not value and len(month_number) in (1, 2) and month_number.isascii() and month_number.isdigit():
        value = f"{params.get('year', '')}-{month_number.zfill(2)}"
    start = _selected_month(value, today.replace(day=1))
    return _Period("month", start, _next_month(start), (start - DAY).replace(day=1))


def _selected_school(params):
    value = params.get("school", "")
    return value if value in School.values else ""


def _query_string(view, anchor, school):
    if view == "week":
        parameters = {"view": "week", "day": anchor.isoformat()}
    elif view == "year":
        parameters = {"view": "year", "year": anchor.isoformat()[:4]}
    else:
        parameters = {"month": anchor.isoformat()[:7]}
    if school:
        parameters["school"] = school
    return urlencode(parameters)


def _ticks(start, stop, today):
    ticks = []
    for offset in range((stop - start).days):
        day = start + timedelta(days=offset)
        ticks.append({
            "date": day,
            "number": day.day,
            "is_today": day == today,
            "is_weekend": day.weekday() >= 5,
            "is_month_start": day.day == 1,
        })
    return ticks


def _session_dates(session, *, zone=None):
    starts_on = timezone.localtime(session.starts_at, timezone=zone).date()
    ends_at = timezone.localtime(session.ends_at, timezone=zone)
    last_day = ends_at.date() - DAY if ends_at.time() == time.min else ends_at.date()
    return starts_on, last_day


def _occupied_dates(session, period):
    starts_on, last_day = _session_dates(session)
    return max(starts_on, period.start), min(last_day, period.last_day)


def _month_overview(sessions, period, today, school):
    day_deltas = Counter()
    month_counts = Counter()
    for session in sessions:
        start, end = _occupied_dates(session, period)
        day_deltas[start] += 1
        day_deltas[end + DAY] -= 1
        for month in range(start.month, end.month + 1):
            month_counts[month] += 1

    months = []
    running_count = 0
    calendar = Calendar()
    for month_number in range(1, 13):
        start = period.start.replace(month=month_number)
        days = []
        occupied_days = []
        for number in calendar.itermonthdays(start.year, month_number):
            if number == 0:
                days.append(None)
                continue
            day = start.replace(day=number)
            running_count += day_deltas[day]
            days.append({"date": day, "count": running_count, "is_today": day == today})
            if running_count:
                occupied_days.append(number)
        occupied_count = len(occupied_days)
        occupancy_level = next(
            level for level, (upper, _) in enumerate(MONTH_OCCUPANCY_RANGES)
            if occupied_count <= upper
        )
        months.append({
            "start": start,
            "query": _query_string("month", start, school),
            "days": days,
            "occupied_days": occupied_days,
            "occupied_count": occupied_count,
            "occupancy_level": occupancy_level,
            "session_count": month_counts[month_number],
        })
    return months


def _month_groups(sessions, start, stop, selected, school_filter):
    bars = defaultdict(list)
    lane_ends = defaultdict(list)
    for session in sessions:
        starts_on, last_day = _session_dates(session, zone=BERLIN)
        visible_start = max(starts_on, start)
        visible_end = min(last_day, stop - DAY)
        if visible_start > visible_end:
            continue

        start_column = (visible_start - start).days + 1
        end_column = (visible_end - start).days + 2
        school_lanes = lane_ends[session.school]
        lane = next(
            (index for index, free_at in enumerate(school_lanes) if free_at <= start_column),
            len(school_lanes),
        )
        if lane == len(school_lanes):
            school_lanes.append(end_column)
        else:
            school_lanes[lane] = end_column
        detail_month = selected.start if starts_on < selected.stop and last_day >= selected.start else visible_start
        bars[session.school].append({
            "session": session,
            "start_column": start_column,
            "end_column": end_column,
            "lane": lane + 1,
            "starts_before": starts_on < visible_start,
            "ends_after": last_day > visible_end,
            "detail_query": _query_string("month", detail_month, school_filter),
        })
    return [
        {"school": school, "bars": bars[school], "lane_count": len(lane_ends[school])}
        for school in School if bars[school]
    ]


def _month_timeline(selected, center, today, school):
    start = max(FIRST_TIMELINE_DAY, _shift_month(center, -TIMELINE_SIDE_MONTHS))
    stop = min(TIMELINE_STOP, _shift_month(center, TIMELINE_SIDE_MONTHS + 1))
    sessions = _calendar_sessions(_overlapping_sessions(
        datetime.combine(start, time.min, tzinfo=BERLIN), datetime.combine(stop, time.min, tzinfo=BERLIN), school,
    ))
    days = _ticks(start, stop, today)
    months = [
        {"start": day["date"], "start_column": column, "end_column": (_next_month(day["date"]) - start).days + 1}
        for column, day in enumerate(days, 1) if day["is_month_start"]
    ]
    return {
        "timeline_days": days,
        "timeline_months": months,
        "timeline_groups": _month_groups(sessions, start, stop, selected, school),
        "timeline_anchor": selected.start if start <= selected.start < stop else center,
        "timeline_can_extend_before": start > FIRST_TIMELINE_DAY,
        "timeline_can_extend_after": stop < TIMELINE_STOP,
        "timeline_center": center.isoformat()[:7],
        "timeline_day_count": len(days),
    }


def _timeline_rows(sessions, period, starts, ends):
    by_school = defaultdict(list)
    for session in sessions:
        first_day, last_day = _occupied_dates(session, period)
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


def _overlapping_sessions(starts, ends, school):
    sessions = CourseSession.objects.filter(
        starts_at__lt=ends, ends_at__gt=starts, school__in=School.values,
    )
    if school == School.ONLINE:
        return sessions.filter(Q(school=School.ONLINE) | Q(is_online=True))
    return sessions.filter(school=school) if school else sessions


def _calendar_sessions(sessions):
    return sessions.select_related("course").annotate(
        participant_count=Count(
            "seats__participant_id", filter=~Q(seats__status=BookingStatus.CANCELLED), distinct=True,
        ),
    ).order_by("starts_at", "course__name", "pk")


@never_cache
@require_safe
def calendar(request):
    zone = None if request.GET.get("view") in ("week", "year") else BERLIN
    today = timezone.localdate(timezone=zone)
    period = _selected_period(request.GET, today)
    school = _selected_school(request.GET)
    starts = timezone.make_aware(datetime.combine(period.start, time.min), timezone=zone)
    ends = timezone.make_aware(datetime.combine(period.stop, time.min), timezone=zone)
    sessions = _overlapping_sessions(starts, ends, school)
    if period.view == "year":
        sessions = list(sessions.only("starts_at", "ends_at").order_by("starts_at", "pk"))
    else:
        sessions = list(_calendar_sessions(sessions).annotate(seat_count=Count("seats")))

    month_anchor = period.start
    week_anchor = period.start
    if period.view == "week":
        # The Thursday determines the month and year of an ISO week.
        month_anchor += timedelta(days=3)
    else:
        if period.start <= today < period.stop:
            week_anchor = today
        elif sessions:
            week_anchor = max(period.start, timezone.localtime(sessions[0].starts_at, timezone=zone).date())
        if period.view == "year":
            month_anchor = week_anchor
    context = {
        "section": "calendar",
        "view": period.view,
        "month": month_anchor.replace(day=1),
        "month_id": month_anchor.isoformat()[:7],
        "month_choices": MONTH_CHOICES,
        "year_id": month_anchor.isoformat()[:4],
        "day_id": period.start.isoformat(),
        "period_start": period.start,
        "period_end": period.last_day,
        "today_query": _query_string(
            period.view, _week_start(today) if period.view == "week" else today, school,
        ),
        "selected_school": school,
        "school_choices": School.choices,
        "previous_query": _query_string(period.view, period.previous, school),
        "next_query": _query_string(period.view, period.stop, school),
        "current_query": _query_string(period.view, period.start, school),
        "month_query": _query_string("month", month_anchor, school),
        "week_query": _query_string("week", _week_start(week_anchor), school),
        "year_query": _query_string("year", month_anchor, school),
    }
    if period.view == "year":
        context["months"] = _month_overview(sessions, period, today, school)
        context["occupancy_legend"] = tuple(enumerate(label for _, label in MONTH_OCCUPANCY_RANGES))
    elif period.view == "month":
        center = _selected_month(request.GET.get("range", ""), period.start)
        context.update(_month_timeline(period, center, today, school))
        context["sessions"] = sessions
    else:
        ticks = _ticks(period.start, period.stop, today)
        context.update({
            "ticks": ticks,
            "day_count": len(ticks),
            "school_groups": _timeline_rows(sessions, period, starts, ends),
            "sessions": sessions,
        })
    return render(request, "workspace/calendar.html", context)


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
