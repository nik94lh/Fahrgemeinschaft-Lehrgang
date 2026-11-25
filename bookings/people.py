"""Read-only list of participant profiles in the workspace."""

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Case, Count, F, Prefetch, Q, When
from django.db.models.functions import Lower
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from accounts.access import can_view

from .models import BookingStatus, CourseSeat, Participant


@never_cache
@require_safe
def people(request):
    query = request.GET.get("q", "").strip()[:100]
    show_bookings = can_view(request.user, "bookings")
    participants = Participant.objects.order_by(Lower("name"), "pk")
    if query:
        participants = participants.filter(Q(name__icontains=query) | Q(email__icontains=query))
    if show_bookings:
        upcoming = Case(When(
            Q(session__starts_at__gte=timezone.now()) & ~Q(status=BookingStatus.CANCELLED),
            then=F("session__starts_at"),
        ))
        featured = CourseSeat.objects.select_related("session__course").order_by(
            upcoming.asc(nulls_last=True), "-session__starts_at", "pk",
        )[:1]
        participants = participants.annotate(booking_count=Count("seats")).prefetch_related(
            Prefetch("seats", queryset=featured, to_attr="featured_bookings"),
        )

    return render(request, "workspace/people.html", {
        "section": "people",
        "page_obj": Paginator(participants, 25).get_page(request.GET.get("page")),
        "show_bookings": show_bookings,
        "query": query,
        "querystring": urlencode({"q": query}) if query else "",
    })
