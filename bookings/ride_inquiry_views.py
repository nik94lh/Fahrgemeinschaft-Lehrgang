"""Workspace entry points for the synthetic participant questionnaire."""

from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from accounts.access import is_workspace_admin

from .models import RideCase, RideConfiguration
from .ride_participation import portal_enabled, set_ride_automation, start_ride_inquiry


def get_ride_automation() -> bool:
    return bool(RideConfiguration.objects.filter(pk=1).values_list("automatic_inquiries", flat=True).first())


def inquiry_controls_available(request) -> bool:
    return request.is_secure() and portal_enabled()


def ride_automation_header(request):
    if not is_workspace_admin(request.user) or not inquiry_controls_available(request):
        return {}
    return {"workspace_ride_automation": {"enabled": get_ride_automation()}}


@never_cache
@require_POST
@csrf_protect
def ride_automation(request):
    if not inquiry_controls_available(request):
        raise Http404
    enabled = request.POST.get("enabled")
    if enabled not in {"0", "1"}:
        return HttpResponseBadRequest("Wähle einen gültigen Wert für die Automatik.")
    set_ride_automation(enabled == "1")
    destination = request.POST.get("next", "")
    if (
        destination.startswith("/") and not destination.startswith("//")
        and url_has_allowed_host_and_scheme(destination, allowed_hosts=set())
    ):
        return redirect(destination)
    return redirect("rides")


@never_cache
@require_POST
@csrf_protect
def ride_inquiry_start(request, case_id):
    if not inquiry_controls_available(request):
        raise Http404
    get_object_or_404(RideCase, pk=case_id, is_test=True)
    try:
        prepared = start_ride_inquiry(case_id, automatic=False)
    except ValueError:
        result = "nicht-moeglich"
    else:
        result = "vorbereitet" if prepared else "unveraendert"
    return redirect(reverse("ride_detail", args=[case_id], query={"testabfrage": result}))
