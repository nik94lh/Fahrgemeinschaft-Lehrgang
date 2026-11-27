"""Show past ride proposals and their recorded outcomes."""

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import RideCase, RideNotice


HISTORY_MODES = {"regulaer": "Regulär", "test": "Test", "entfernt": "Entfernte"}


@never_cache
@require_safe
def ride_history(request):
    mode = request.GET.get("verlauf", "regulaer")
    if mode not in HISTORY_MODES:
        mode = "regulaer"

    cases = RideCase.objects.filter(direction=RideCase.ROUND_TRIP)
    if mode == "entfernt":
        cases = cases.filter(removed_at__isnull=False).order_by("-removed_at", "-pk")
    else:
        cases = cases.filter(
            Q(earliest__lte=timezone.now()) | Q(status=RideCase.Status.CLOSED),
            removed_at__isnull=True, is_test=(mode == "test"),
        ).order_by("-travel_date", "-earliest", "-pk")

    page_obj = Paginator(cases, 25).get_page(request.GET.get("page"))
    totals = {
        "proposals": page_obj.paginator.count,
        "people": RideNotice.objects.filter(
            participation__case__in=cases, kind=RideNotice.Kind.CONTACTS,
        ).aggregate(people=Count("participation__participant_id", distinct=True))["people"],
        "confirmed": cases.filter(ride_confirmed_at__isnull=False).count(),
    }
    params = {"verlauf": mode}
    detail_params = params | ({"page": page_obj.number} if page_obj.number > 1 else {})
    return render(request, "workspace/ride_history.html", {
        "section": "rides",
        "page_obj": page_obj,
        "selected_mode": mode,
        "modes": HISTORY_MODES.items(),
        "totals": totals,
        "removed": request.GET.get("entfernt") == "1",
        "restored": request.GET.get("wiederhergestellt") == "1",
        "querystring": urlencode(params),
        "detail_query": urlencode(detail_params),
    })
