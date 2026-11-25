from django import forms
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .models import DEFAULT_RIDE_LEAD_DAYS, MAX_RIDE_LEAD_DAYS, RideConfiguration


class RideSettingsForm(forms.Form):
    lead_days = forms.IntegerField(
        label="Vorlauf in Kalendertagen",
        min_value=0,
        max_value=MAX_RIDE_LEAD_DAYS,
        help_text="Der Vorlauf zählt ab dem Tag der Hinfahrt zurück. Bei 0 Tagen ist der Vorschlag am Tag der Hinfahrt bereit.",
    )


def get_ride_lead_days() -> int:
    lead_days = RideConfiguration.objects.filter(pk=1).values_list("lead_days", flat=True).first()
    return DEFAULT_RIDE_LEAD_DAYS if lead_days is None else lead_days


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def rides_settings(request):
    if request.method == "POST":
        form = RideSettingsForm(request.POST)
        if form.is_valid():
            RideConfiguration.objects.update_or_create(pk=1, defaults={"lead_days": form.cleaned_data["lead_days"]})
            return redirect(f"{reverse('rides_settings')}?gespeichert=1")
    else:
        form = RideSettingsForm(initial={"lead_days": get_ride_lead_days()})

    return render(request, "workspace/ride_settings.html", {
        "section": "rides",
        "form": form,
        "saved": request.method == "GET" and request.GET.get("gespeichert") == "1",
    }, status=400 if form.is_bound else 200)
