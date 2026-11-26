"""Explicit confirmation for the permanent deletion of one booking."""

from django import forms
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .booking_deletion import BookingDeletionResult, delete_booking, deletion_token
from .booking_search import booking_list_navigation
from .models import CourseSeat


BOOKING_DELETED_SESSION_KEY = "booking_deleted_id"


class BookingDeleteForm(forms.Form):
    confirm_id = forms.CharField(
        label="Buchungs-ID eingeben", strip=False, max_length=19,
        help_text="Tippe die oben angezeigte Buchungs-ID genau ab.",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off", "spellcheck": "false"}),
    )
    snapshot = forms.CharField(strip=False, max_length=256, widget=forms.HiddenInput)

    def __init__(self, *args, seat_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.seat_id = seat_id

    def clean_confirm_id(self):
        if self.cleaned_data["confirm_id"] != str(self.seat_id):
            raise forms.ValidationError("Gib die Buchungs-ID genau wie angezeigt ein.")
        return self.cleaned_data["confirm_id"]


def _fresh_form(seat):
    return BookingDeleteForm(seat_id=seat.pk, initial={"snapshot": deletion_token(seat)})


@sensitive_post_parameters("snapshot")
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def booking_delete(request, seat_id):
    seats = CourseSeat.objects.select_related("participant", "session__course")
    seat = get_object_or_404(seats, pk=seat_id)
    bookings_url, detail_params = booking_list_navigation(request.GET)
    status, error = 200, ""
    if request.method == "POST":
        status = 400
        form = BookingDeleteForm(request.POST, seat_id=seat.pk)
        if form.is_valid():
            outcome = delete_booking(seat.pk, form.cleaned_data["snapshot"])
            if outcome is BookingDeletionResult.DELETED:
                request.session[BOOKING_DELETED_SESSION_KEY] = seat_id
                return redirect(bookings_url)
            if outcome is BookingDeletionResult.MISSING:
                raise Http404("Die Buchung wurde bereits gelöscht.")
            status = 409
            error = "Die Buchung wurde seit dem Öffnen geändert. Prüfe die aktuellen Angaben und bestätige erneut."
            seat = get_object_or_404(seats, pk=seat_id)
            form = _fresh_form(seat)
        elif "snapshot" in form.errors:
            error = "Die Bestätigung ist unvollständig. Prüfe die Angaben und bestätige erneut."
            form = _fresh_form(seat)
    else:
        form = _fresh_form(seat)
    return render(request, "workspace/booking_delete.html", {
        "section": "bookings",
        "seat": seat,
        "form": form,
        "error": error,
        "form_url": reverse("booking_delete", args=[seat_id], query=detail_params),
        "detail_url": reverse("booking_detail", args=[seat_id], query=detail_params),
        "bookings_url": bookings_url,
    }, status=status)
