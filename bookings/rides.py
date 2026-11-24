"""Operator views for persisted ride matches."""
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from .models import RideCase
from .rides_matching import set_ride_case_status, sync_ride_cases


class RideCaseStatusForm(forms.Form):
    status = forms.ChoiceField(label="Bearbeitungsstand", choices=RideCase.Status.choices)
    expected_status = forms.ChoiceField(choices=RideCase.Status.choices, widget=forms.HiddenInput)


def _selected_status(params):
    value = params.get("status", "")
    return value if value in RideCase.Status.values else ""


@never_cache
@require_safe
def rides(request):
    selected_status = _selected_status(request.GET)
    archived = request.GET.get("archiv") == "1"
    combined = RideCase.objects.filter(direction=RideCase.ROUND_TRIP)
    legacy = RideCase.objects.exclude(direction=RideCase.ROUND_TRIP)
    counts = combined.aggregate(
        active=Count("pk", filter=Q(currently_matches=True)),
        pending=Count("pk", filter=Q(currently_matches=True) & ~Q(status=RideCase.Status.CLOSED)),
    )
    cases = legacy if archived else combined
    params = {"archiv": "1"} if archived else {}
    if selected_status:
        cases = cases.filter(status=selected_status)
        params["status"] = selected_status
    cases = cases.annotate(participant_count=Count("seats__participant_id", distinct=True)).order_by(
        "-currently_matches", "travel_date", "earliest", "pk",
    )
    return render(request, "workspace/rides.html", {
        "section": "rides",
        "page_obj": Paginator(cases, 25).get_page(request.GET.get("page")),
        "active_count": counts["active"],
        "pending_count": counts["pending"],
        "status_choices": RideCase.Status.choices,
        "selected_status": selected_status,
        "archived": archived,
        "has_legacy": not archived and legacy.exists(),
        "querystring": urlencode(params),
        "synced": request.GET.get("abgleich") == "1",
    })


@never_cache
@require_POST
@csrf_protect
def rides_sync(request):
    sync_ride_cases()
    params = {"abgleich": "1"}
    status = _selected_status(request.POST)
    if status:
        params["status"] = status
    return redirect(f"{reverse('rides')}?{urlencode(params)}")


def _status_form(case):
    return RideCaseStatusForm(initial={"status": case.status, "expected_status": case.status})


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def ride_detail(request, case_id):
    case = get_object_or_404(RideCase, pk=case_id)
    status = 200
    conflict = ""
    if request.method == "POST":
        form = RideCaseStatusForm(request.POST)
        status = 400
        if form.is_valid():
            try:
                set_ride_case_status(
                    case.pk, form.cleaned_data["status"], expected_status=form.cleaned_data["expected_status"],
                )
            except ValueError as error:
                case.refresh_from_db()
                form = _status_form(case)
                conflict = str(error)
                status = 409
            else:
                return redirect(f"{reverse('ride_detail', args=[case.pk])}?gespeichert=1")
    else:
        form = _status_form(case)

    seats = case.seats.select_related("participant", "session__course").order_by("participant__name", "pk")
    return render(request, "workspace/ride_detail.html", {
        "section": "rides",
        "case": case,
        "round_trip": case.direction == RideCase.ROUND_TRIP,
        "seats": seats,
        "form": form,
        "conflict": conflict,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
    }, status=status)
