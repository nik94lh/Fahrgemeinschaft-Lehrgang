"""Aggregate workspace figures for a selected Berlin calendar period."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Q
from django.db.models.functions import ExtractMonth
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from accounts.access import can_view
from mailimport.models import MailReceipt

from .models import BookingStatus, CourseSession, EmailEvent, RideCase, School


BERLIN = ZoneInfo("Europe/Berlin")
MIN_YEAR = 1900
MAX_YEAR = 9998
MONTH_CHOICES = tuple(date(2000, month, 1) for month in range(1, 13))
ONLINE = Q(school=School.ONLINE) | Q(is_online=True)


def _bounded_number(value, minimum, maximum, default=None):
    value = value.strip()
    if not (value.isascii() and value.isdecimal() and len(value) <= len(str(maximum))):
        return default
    number = int(value)
    return number if minimum <= number <= maximum else default


def _midnight(day):
    return datetime.combine(day, time.min, tzinfo=BERLIN)


def _session_counts():
    return {
        "sessions": Count("pk", distinct=True),
        "bookings": Count("seats"),
        "online_sessions": Count("pk", filter=ONLINE, distinct=True),
    }


def _booking_metrics(sessions):
    assigned = Q(seats__participant__isnull=False) & ~Q(seats__status=BookingStatus.CANCELLED)
    metrics = sessions.aggregate(
        courses=Count("course_id", distinct=True),
        people=Count("seats__participant_id", filter=assigned, distinct=True),
        people_with_email=Count("seats__participant_id", filter=assigned & ~Q(seats__participant__email=""), distinct=True),
        brigades=Count("seats__participant__home_brigade_id", filter=assigned, distinct=True),
        **_session_counts(),
        **{status: Count("seats", filter=Q(seats__status=status)) for status in BookingStatus.values},
    )
    statuses = [
        {"status": status, "label": label, "count": metrics.pop(status)}
        for status, label in BookingStatus.choices
    ]
    return metrics, statuses


def _school_breakdown(sessions):
    rows = {
        row["school"]: row
        for row in sessions.values("school").annotate(courses=Count("course_id", distinct=True), **_session_counts())
    }
    return [{**rows[school], "label": label} for school, label in School.choices if school in rows]


def _course_breakdown(sessions):
    rows = sessions.values("course_id", "course__name").annotate(**_session_counts()).order_by("course__name", "course_id")
    return [
        {
            "label": row["course__name"], "sessions": row["sessions"], "bookings": row["bookings"],
            "online_sessions": row["online_sessions"],
        }
        for row in rows
    ]


def _year_trend(sessions, year):
    grouped = sessions.values(month_number=ExtractMonth("starts_at", tzinfo=BERLIN)).annotate(
        sessions=Count("pk", distinct=True), bookings=Count("seats"),
    ).order_by("month_number")
    by_month = {row["month_number"]: (row["sessions"], row["bookings"]) for row in grouped}
    trend = []
    for month in range(1, 13):
        terms, bookings = by_month.get(month, (0, 0))
        trend.append({"month": date(year, month, 1), "sessions": terms, "bookings": bookings})
    return trend


def _ride_counts(start, stop, school, now):
    if school == School.ONLINE:
        return {"total": 0, "active": 0, "closed": 0}
    cases = RideCase.objects.filter(
        direction=RideCase.ROUND_TRIP, removed_at__isnull=True, travel_date__gte=start, travel_date__lt=stop,
    )
    if school:
        cases = cases.filter(school=school)
    closed = Q(status=RideCase.Status.CLOSED)
    return cases.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(currently_matches=True, earliest__gt=now) & ~closed),
        closed=Count("pk", filter=closed),
    )


def _mail_counts(start, stop):
    counts = MailReceipt.objects.filter(observed_at__gte=start, observed_at__lt=stop).aggregate(
        total=Count("pk"),
        **{outcome: Count("pk", filter=Q(outcome=outcome)) for outcome in MailReceipt.Outcome.values},
    )
    return {
        "total": counts["total"],
        "outcomes": [
            {"outcome": outcome, "label": label, "count": counts[outcome]}
            for outcome, label in MailReceipt.Outcome.choices
        ],
    }


def _review_reasons(start, stop):
    rows = EmailEvent.objects.filter(
        imported_at__gte=start, imported_at__lt=stop, state=EmailEvent.State.NEEDS_REVIEW,
    ).values("review_reason").annotate(count=Count("pk")).order_by("-count", "review_reason")
    labels = dict(EmailEvent.ReviewReason.choices)
    return [
        {"label": labels.get(row["review_reason"], EmailEvent.ReviewReason.UNKNOWN.label), "count": row["count"]}
        for row in rows
    ]


@never_cache
@require_safe
def statistics(request):
    now = timezone.now()
    current_year = timezone.localdate(now, timezone=BERLIN).year
    year = _bounded_number(request.GET.get("year", ""), MIN_YEAR, MAX_YEAR, current_year)
    month = _bounded_number(request.GET.get("month", ""), 1, 12)
    school = request.GET.get("school", "")
    if school not in School.values:
        school = ""

    year_start, year_stop = date(year, 1, 1), date(year + 1, 1, 1)
    start, stop = year_start, year_stop
    if month:
        start = date(year, month, 1)
        stop = date(year, month + 1, 1) if month < 12 else year_stop
    starts_at, stops_at = _midnight(start), _midnight(stop)

    year_sessions = CourseSession.objects.filter(
        starts_at__gte=_midnight(year_start), starts_at__lt=_midnight(year_stop), school__in=School.values,
    )
    if school == School.ONLINE:
        year_sessions = year_sessions.filter(ONLINE)
    elif school:
        year_sessions = year_sessions.filter(school=school)
    sessions = year_sessions.filter(starts_at__gte=starts_at, starts_at__lt=stops_at) if month else year_sessions
    metrics, status_counts = _booking_metrics(sessions)
    trend = _year_trend(year_sessions, year)

    mail_counts = None
    if settings.HTTPS and request.is_secure() and can_view(request.user, "mail_receipts"):
        mail_counts = _mail_counts(starts_at, stops_at)
    review_count = None
    review_reasons = []
    if can_view(request.user, "review"):
        review_reasons = _review_reasons(starts_at, stops_at)
        review_count = sum(row["count"] for row in review_reasons)

    return render(request, "workspace/statistics.html", {
        "section": "statistics",
        "year": year,
        "month": month,
        "school": school,
        "min_year": MIN_YEAR,
        "max_year": MAX_YEAR,
        "month_choices": MONTH_CHOICES,
        "school_choices": School.choices,
        "period_start": start,
        "period_end": stop - timedelta(days=1),
        "metrics": metrics,
        "status_counts": status_counts,
        "school_rows": _school_breakdown(sessions),
        "course_rows": _course_breakdown(sessions),
        "trend": trend,
        "trend_max": max(1, max(row["bookings"] for row in trend)),
        "ride_counts": _ride_counts(start, stop, school, now),
        "mail_counts": mail_counts,
        "review_count": review_count,
        "review_reasons": review_reasons,
    })
