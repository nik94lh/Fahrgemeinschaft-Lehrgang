"""Contact details for the participant assigned to one booking."""
from django import forms
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .models import CourseSeat
from .services import set_participant_email


class ParticipantEmailForm(forms.Form):
    participant_id = forms.IntegerField(widget=forms.HiddenInput)
    email = forms.EmailField(
        label="E-Mail-Adresse",
        max_length=254,
        widget=forms.TextInput(attrs={"inputmode": "email", "autocomplete": "email"}),
    )


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def booking_participant(request, seat_id):
    seat = get_object_or_404(
        CourseSeat.objects.select_related("participant", "session__course"),
        pk=seat_id,
        participant__isnull=False,
    )
    status = 200
    form = None
    if request.method == "POST":
        form = ParticipantEmailForm(request.POST)
        status = 400
        if form.is_valid():
            try:
                set_participant_email(
                    seat.pk,
                    form.cleaned_data["email"],
                    expected_participant_id=form.cleaned_data["participant_id"],
                )
            except ValueError as error:
                form.add_error("email", str(error))
            else:
                return redirect("booking_participant", seat_id=seat.pk)
    elif not seat.participant.email:
        form = ParticipantEmailForm(initial={"participant_id": seat.participant_id})

    return render(request, "workspace/booking_participant.html", {
        "section": "bookings",
        "seat": seat,
        "booking_count": seat.participant.seats.count(),
        "form": form,
    }, status=status)
