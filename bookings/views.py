"""Server-rendered views for the BMS operations workspace."""
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from accounts.access import can_view
from mailimport.participant_settings import get_functional_addresses

from .booking_search import booking_filters, booking_id_query
from .booking_state import booking_missing_fields
from .booking_status import booking_status_cards, booking_status_prefetches
from .models import BookingStatus, CourseSeat, CourseSession, EmailEvent, RideCase, School
from .review_actions import _recognized
from .rides import ride_phase_filters
from .ride_settings import get_ride_lead_days


PENDING_BOOKING_STATUSES = (BookingStatus.APPROVED, BookingStatus.AWAITING_CONFIRMATION)


def _seats():
    return CourseSeat.objects.select_related("session__course", "participant").order_by(
        "session__starts_at", "session__course__name", "pk"
    )


@never_cache
@require_safe
def overview(request):
    now = timezone.now()
    seats = CourseSeat.objects.aggregate(
        total=Count("pk"),
        confirmed=Count("pk", filter=Q(status=BookingStatus.CONFIRMED)),
        pending=Count("pk", filter=Q(status__in=PENDING_BOOKING_STATUSES)),
    )
    upcoming_sessions = CourseSession.objects.filter(starts_at__gte=now).select_related(
        "course",
    ).order_by("starts_at", "pk")[:6]
    ride_overview = None
    if can_view(request.user, "rides"):
        eligible, phases = ride_phase_filters(now=now, lead_days=get_ride_lead_days())
        ride_overview = RideCase.objects.filter(eligible).aggregate(
            ready=Count("pk", filter=phases["ready"]),
            standby=Count("pk", filter=phases["standby"]),
        )
    review_count = EmailEvent.objects.filter(state=EmailEvent.State.NEEDS_REVIEW).count()
    booking_url = reverse("bookings") if can_view(request.user, "bookings") else ""
    review_url = reverse("review") if can_view(request.user, "review") else ""
    dashboard_metrics = (
        {"label": "Buchungen", "value": seats["total"], "url": booking_url},
        {
            "label": "Bestätigt", "value": seats["confirmed"],
            "url": f"{booking_url}?status=confirmed" if booking_url else "",
        },
        {
            "label": "Bestätigung offen", "value": seats["pending"],
            "url": f"{booking_url}?status=pending" if booking_url else "",
        },
        {"label": "Klärungsbedarf", "value": review_count, "url": review_url},
    )
    return render(request, "workspace/overview.html", {
        "section": "overview",
        "seats": seats,
        "review_count": review_count,
        "dashboard_metrics": dashboard_metrics,
        "ride_overview": ride_overview,
        "upcoming_sessions": upcoming_sessions,
        "recent_events": EmailEvent.objects.select_related("seat__session__course", "seat__participant").order_by("-imported_at", "-pk")[:8],
    })


@never_cache
@require_safe
def bookings(request):
    filters = booking_filters(request.GET)
    selected_status = filters.get("status", "")
    selected_school = filters.get("school", "")
    query = filters.get("q", "")
    seats = _seats()
    if selected_status == "pending":
        seats = seats.filter(status__in=PENDING_BOOKING_STATUSES)
    elif selected_status:
        seats = seats.filter(status=selected_status)
    if selected_school == School.ONLINE:
        seats = seats.filter(Q(session__school=School.ONLINE) | Q(session__is_online=True))
    elif selected_school:
        seats = seats.filter(session__school=selected_school)
    if query:
        explicit_id, booking_id = booking_id_query(query)
        if explicit_id:
            seats = seats.filter(pk=booking_id)
        else:
            matches = (
                Q(session__course__name__icontains=query)
                | Q(participant__name__icontains=query)
                | Q(external_reference__icontains=query)
            )
            if booking_id is not None:
                matches |= Q(pk=booking_id)
            seats = seats.filter(matches)
    seats = seats.prefetch_related(*booking_status_prefetches())
    page_obj = Paginator(seats, 25).get_page(request.GET.get("page"))
    detail_params = filters.copy()
    if page_obj.number > 1:
        detail_params["list_page"] = page_obj.number
    imported = request.GET.get("importiert") == "1"
    pagination_params = filters | {"importiert": "1"} if imported else filters
    functional_addresses = set(get_functional_addresses()) if page_obj else set()
    booking_rows = [
        {
            "seat": seat,
            "status_cards": booking_status_cards(seat),
            "missing_fields": booking_missing_fields(seat, functional_addresses=functional_addresses),
        }
        for seat in page_obj
    ]
    return render(request, "workspace/bookings.html", {
        "section": "bookings",
        "imported": imported,
        "page_obj": page_obj,
        "booking_rows": booking_rows,
        "status_choices": [("pending", "Bestätigung offen"), *BookingStatus.choices],
        "school_choices": School.choices,
        "selected_status": selected_status,
        "selected_school": selected_school,
        "query": query,
        "querystring": urlencode(pagination_params),
        "detail_querystring": urlencode(detail_params),
    })


@never_cache
@require_safe
def review(request):
    events = EmailEvent.objects.filter(state=EmailEvent.State.NEEDS_REVIEW).order_by("-imported_at", "-pk")
    reason = request.GET.get("grund", "")
    if reason not in EmailEvent.ReviewReason.values:
        reason = ""
    if reason:
        events = events.filter(review_reason=reason)
    page_obj = Paginator(events, 25).get_page(request.GET.get("page"))
    queue_params = {"grund": reason} if reason else {}
    detail_params = dict(queue_params)
    if page_obj.number > 1:
        detail_params["page"] = page_obj.number
    cases = []
    for event in page_obj:
        recognized = _recognized(event)
        school = recognized.get("school")
        cases.append({
            "event": event,
            "participant": recognized.get("participant_name"),
            "course": recognized.get("course_name"),
            "school": School(school).label if school else "",
        })
    return render(request, "workspace/review.html", {
        "section": "review",
        "page_obj": page_obj,
        "cases": cases,
        "reason": reason,
        "reason_choices": [(key, label) for key, label in EmailEvent.ReviewReason.choices if key],
        "querystring": urlencode(queue_params),
        "detail_querystring": urlencode(detail_params),
    })
