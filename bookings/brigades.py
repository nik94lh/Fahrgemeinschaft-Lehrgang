"""Local fire brigade register."""
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .models import FireBrigade


BRIGADE_STATUSES = {"active": "Aktiv", "inactive": "Inaktiv"}


class BrigadeForm(forms.ModelForm):
    class Meta:
        model = FireBrigade
        fields = ("name", "municipality", "district", "address", "latitude", "longitude", "is_active")
        localized_fields = ("latitude", "longitude")
        widgets = {
            "latitude": forms.TextInput(attrs={"inputmode": "decimal"}),
            "longitude": forms.TextInput(attrs={"inputmode": "decimal"}),
        }
        labels = {
            "name": "Name",
            "municipality": "Gemeinde",
            "district": "Ortsteil",
            "address": "Adresse",
            "latitude": "Breitengrad",
            "longitude": "Längengrad",
            "is_active": "Aktiv",
        }
        help_texts = {
            "latitude": "Optional, −90 bis 90. Punkt oder Komma verwenden. Koordinaten paarweise angeben.",
            "longitude": "Optional, −180 bis 180.",
        }

    def clean(self):
        cleaned_data = super().clean()
        if not {"latitude", "longitude"} <= cleaned_data.keys():
            return cleaned_data

        latitude = cleaned_data["latitude"]
        longitude = cleaned_data["longitude"]
        if latitude is None and longitude is not None:
            self.add_error("latitude", "Gib den Breitengrad zusammen mit dem Längengrad an.")
        if longitude is None and latitude is not None:
            self.add_error("longitude", "Gib den Längengrad zusammen mit dem Breitengrad an.")
        if latitude is not None and not -90 <= latitude <= 90:
            self.add_error("latitude", "Der Breitengrad muss zwischen −90 und 90 liegen.")
        if longitude is not None and not -180 <= longitude <= 180:
            self.add_error("longitude", "Der Längengrad muss zwischen −180 und 180 liegen.")
        return cleaned_data


def _filter_params(parameters):
    filters = {}
    if query := parameters.get("q", "").strip()[:100]:
        filters["q"] = query
    if parameters.get("status") in BRIGADE_STATUSES:
        filters["status"] = parameters["status"]
    return filters


@never_cache
@require_safe
def brigades(request):
    filters = _filter_params(request.GET)
    query = filters.get("q", "")
    selected_status = filters.get("status", "")
    results = FireBrigade.objects.order_by("name", "municipality", "pk")
    if query:
        results = results.filter(
            Q(name__icontains=query) | Q(municipality__icontains=query)
            | Q(district__icontains=query) | Q(address__icontains=query)
        )
    if selected_status:
        results = results.filter(is_active=selected_status == "active")
    page_obj = Paginator(results, 25).get_page(request.GET.get("page"))
    edit_params = dict(filters)
    if page_obj.number > 1:
        edit_params["page"] = page_obj.number
    return render(request, "workspace/brigades.html", {
        "section": "brigades",
        "query": query,
        "selected_status": selected_status,
        "status_choices": BRIGADE_STATUSES.items(),
        "querystring": urlencode(filters),
        "edit_query": urlencode(edit_params),
        "page_obj": page_obj,
        "saved": request.GET.get("gespeichert") == "1",
    })


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def brigade_edit(request, brigade_id=None):
    brigade = get_object_or_404(FireBrigade, pk=brigade_id) if brigade_id is not None else None
    list_path = reverse("brigades")
    list_params = _filter_params(request.GET)
    if page := request.GET.get("page", "")[:20]:
        list_params["page"] = page
    if request.method == "POST":
        form = BrigadeForm(request.POST, instance=brigade)
        if form.is_valid():
            form.save()
            return redirect(f"{list_path}?{urlencode({**list_params, 'gespeichert': '1'})}")
        status = 400
    else:
        form = BrigadeForm(instance=brigade)
        status = 200

    return render(request, "workspace/brigade_form.html", {
        "section": "brigades",
        "title": "Feuerwehr bearbeiten" if brigade else "Feuerwehr anlegen",
        "form": form,
        "back_href": f"{list_path}?{urlencode(list_params)}" if list_params else list_path,
    }, status=status)
