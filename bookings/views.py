"""Server-rendered views for the BMS operations workspace."""
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import BookingStatus, CourseSeat, CourseSession, EmailEvent, School


def _seats():
    return CourseSeat.objects.select_related("session__course", "participant").order_by(
        "session__starts_at", "session__course__name", "pk"
    )


@never_cache
@require_safe
def overview(request):
    seats = CourseSeat.objects.aggregate(
        total=Count("pk"),
        confirmed=Count("pk", filter=Q(status=BookingStatus.CONFIRMED)),
        pending=Count("pk", filter=Q(status__in=[
            BookingStatus.APPROVED, BookingStatus.AWAITING_CONFIRMATION,
        ])),
    )
    upcoming_sessions = CourseSession.objects.filter(starts_at__gte=timezone.now()).select_related(
        "course",
    ).order_by("starts_at", "pk")[:6]
    return render(request, "workspace/overview.html", {
        "section": "overview",
        "seats": seats,
        "review_count": EmailEvent.objects.filter(state=EmailEvent.State.NEEDS_REVIEW).count(),
        "upcoming_sessions": upcoming_sessions,
        "recent_events": EmailEvent.objects.select_related("seat__session__course", "seat__participant").order_by("-imported_at", "-pk")[:8],
    })


@never_cache
@require_safe
def bookings(request):
    selected_status = request.GET.get("status", "")
    selected_school = request.GET.get("school", "")
    query = request.GET.get("q", "").strip()[:100]
    seats = _seats()
    if selected_status in BookingStatus.values:
        seats = seats.filter(status=selected_status)
    if selected_school in School.values:
        seats = seats.filter(session__school=selected_school)
    if query:
        seats = seats.filter(
            Q(session__course__name__icontains=query)
            | Q(participant__name__icontains=query)
            | Q(external_reference__icontains=query)
        )
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "workspace/bookings.html", {
        "section": "bookings",
        "imported": request.GET.get("importiert") == "1",
        "page_obj": Paginator(seats, 25).get_page(request.GET.get("page")),
        "status_choices": BookingStatus.choices,
        "school_choices": School.choices,
        "selected_status": selected_status,
        "selected_school": selected_school,
        "query": query,
        "querystring": params.urlencode(),
    })


@never_cache
@require_safe
def review(request):
    events = EmailEvent.objects.filter(state=EmailEvent.State.NEEDS_REVIEW).select_related(
        "seat__session__course", "seat__participant",
    ).order_by("-imported_at", "-pk")
    return render(request, "workspace/review.html", {
        "section": "review",
        "page_obj": Paginator(events, 25).get_page(request.GET.get("page")),
    })
