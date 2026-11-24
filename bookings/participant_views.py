"""Contact details for the participant assigned to one booking."""
from django import forms
from django.core.paginator import Paginator
from django.db.models import Case, Exists, OuterRef, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .models import BookingStatus, CourseSeat
from .services import set_participant_email


def _profile_bookings(participant_id):
    others = CourseSeat.objects.filter(participant_id=participant_id).exclude(
        pk=OuterRef("pk"),
    ).exclude(status=BookingStatus.CANCELLED)
    same_term = Q(
        session__course__name__iexact=OuterRef("session__course__name"),
        session__school=OuterRef("session__school"),
        session__starts_at=OuterRef("session__starts_at"),
        session__ends_at=OuterRef("session__ends_at"),
    )
    overlaps = others.exclude(same_term).filter(
        session__starts_at__lt=OuterRef("session__ends_at"),
        session__ends_at__gt=OuterRef("session__starts_at"),
    )
    cancelled = When(status=BookingStatus.CANCELLED, then=Value(False))
    return CourseSeat.objects.filter(participant_id=participant_id).select_related(
        "session__course",
    ).annotate(
        possible_duplicate=Case(cancelled, default=Exists(others.filter(same_term))),
        time_overlap=Case(cancelled, default=Exists(overlaps)),
    ).order_by("-session__starts_at", "pk")


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

    page = Paginator(_profile_bookings(seat.participant_id), 25).get_page(request.GET.get("page"))
    return render(request, "workspace/booking_participant.html", {
        "section": "bookings",
        "seat": seat,
        "booking_count": page.paginator.count,
        "profile_bookings": page,
        "form": form,
    }, status=status)
