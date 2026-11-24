"""Read-only monthly course schedule."""
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Count
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import CourseSession, School


def _selected_month(value):
    if value:
        try:
            candidate = date.fromisoformat(f"{value}-01")
        except ValueError:
            pass
        else:
            if date.min.year < candidate.year < date.max.year:
                return candidate
    return timezone.localdate().replace(day=1)


def _next_month(month):
    last_week = month.replace(day=28)
    return (last_week + timedelta(days=4)).replace(day=1)


def _query_string(month, school):
    parameters = {"month": month.isoformat()[:7]}
    if school:
        parameters["school"] = school
    return urlencode(parameters)


@never_cache
@require_safe
def calendar(request):
    month = _selected_month(request.GET.get("month"))
    previous_month = (month - timedelta(days=1)).replace(day=1)
    next_month = _next_month(month)
    school = request.GET.get("school", "")
    if school not in School.values:
        school = ""

    starts = timezone.make_aware(datetime.combine(month, time.min))
    ends = timezone.make_aware(datetime.combine(next_month, time.min))
    sessions = CourseSession.objects.filter(starts_at__lt=ends, ends_at__gt=starts)
    if school:
        sessions = sessions.filter(school=school)
    sessions = sessions.select_related("course").annotate(seat_count=Count("seats")).order_by(
        "starts_at", "course__name", "pk"
    )
    return render(request, "workspace/calendar.html", {
        "section": "calendar",
        "month": month,
        "month_id": month.isoformat()[:7],
        "previous_query": _query_string(previous_month, school),
        "next_query": _query_string(next_month, school),
        "selected_school": school,
        "school_choices": School.choices,
        "sessions": sessions,
    })
