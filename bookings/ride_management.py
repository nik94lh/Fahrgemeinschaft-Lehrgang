"""Workspace actions for managing a round-trip proposal and its outcome."""

from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST

from .booking_search import positive_integer
from .models import RideCase
from .ride_history import HISTORY_MODES
from .rides_matching import remove_ride_case, restore_ride_case, set_ride_confirmed


def _round_trip(case_id):
    return get_object_or_404(RideCase, pk=case_id, direction=RideCase.ROUND_TRIP)


def _origin(params):
    mode = params.get("verlauf")
    if mode not in HISTORY_MODES:
        return {}
    origin = {"verlauf": mode}
    page = positive_integer(params.get("page"))
    if page and page > 1:
        origin["page"] = str(page)
    return origin


def _detail_url(case_id, origin, **notice):
    return reverse("ride_detail", args=[case_id], query=origin | notice)


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_remove(request, case_id):
    case = _round_trip(case_id)
    origin = _origin(request.POST if request.method == "POST" else request.GET)
    error, status = "", 200
    if request.method == "POST":
        if request.POST.get("confirm") != "1":
            error, status = "Bestätige, dass du diesen Vorschlag entfernen möchtest.", 400
        else:
            try:
                remove_ride_case(case_id)
            except ValueError as conflict:
                error, status = str(conflict), 409
            else:
                route = "ride_history" if origin else "rides"
                return redirect(reverse(route, query=origin | {"entfernt": "1"}))
    return render(request, "workspace/ride_remove.html", {
        "section": "rides",
        "case": case,
        "origin": origin,
        "back_href": _detail_url(case_id, origin),
        "error": error,
    }, status=status)


@never_cache
@require_POST
@csrf_protect
def ride_restore(request, case_id):
    _round_trip(case_id)
    origin = _origin(request.POST)
    try:
        case = restore_ride_case(case_id)
    except ValueError:
        return redirect(_detail_url(case_id, origin, aktion="nicht-moeglich"))

    now = timezone.now()
    notice = {"wiederhergestellt": "1"}
    if case.status == RideCase.Status.CLOSED or case.earliest <= now:
        return redirect(reverse("ride_history", query={"verlauf": "test" if case.is_test else "regulaer"} | notice))
    if case.currently_matches:
        return redirect(reverse("rides", query=notice))
    return redirect(_detail_url(case_id, {}, **notice))


@never_cache
@require_POST
@csrf_protect
def ride_confirm(request, case_id):
    _round_trip(case_id)
    state = request.POST.get("confirmed")
    if state not in {"0", "1"}:
        return HttpResponseBadRequest("Wähle aus, ob die Fahrt stattgefunden hat.")
    origin = _origin(request.POST)
    try:
        set_ride_confirmed(case_id, state == "1")
    except ValueError:
        return redirect(_detail_url(case_id, origin, aktion="nicht-moeglich"))
    return redirect(_detail_url(case_id, origin, fahrt=state))
