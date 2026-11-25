"""Contact details for the participant assigned to one booking."""
from django import forms
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Case, Exists, OuterRef, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.access import can_edit, can_view
from mailimport.email_address import normalize_contact_email
from mailimport.models import MailReceipt
from mailimport.participant_settings import get_functional_addresses

from .contact_replay import _history_matches
from .models import BookingStatus, CourseSeat, EmailEvent
from .services import (
    ACTIVATION_EVENTS,
    ACTIVE_STATUSES,
    _IdentityConflict,
    _normalized_name,
    _participant_with_email,
    set_participant_email,
)


def _profile_bookings(participant_id):
    others = CourseSeat.objects.filter(participant_id=participant_id).exclude(
        pk=OuterRef("pk"),
    ).exclude(status=BookingStatus.CANCELLED)
    same_term = Q(
        session__course__name__iexact=OuterRef("session__course__name"),
        session__school=OuterRef("session__school"),
        session__is_online=OuterRef("session__is_online"),
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


def _eligible_receipt_address(seat, receipt, functional_addresses, checked):
    if not isinstance(receipt.fields, dict) or receipt.fields.get("participant_email"):
        return None

    candidate = receipt.fields.get("participant_email_candidate")
    name = receipt.fields.get("participant_name")
    if not isinstance(candidate, str) or not isinstance(name, str):
        return None
    email = normalize_contact_email(candidate)
    if not email or email in functional_addresses:
        return None
    if _normalized_name(name) != _normalized_name(seat.participant.name):
        return None

    key = receipt.event_id, email
    if key not in checked:
        checked[key] = _receipt_address_matches(seat, receipt.event, email)
    return email if checked[key] else None


def _receipt_address_matches(seat, event, email):
    if not _history_matches(event, seat):
        return False
    try:
        owner = _participant_with_email(seat.participant.name, email)
    except _IdentityConflict:
        return False
    return owner is None or owner.pk == seat.participant_id


def _receipt_address(seat, value):
    if seat.status not in ACTIVE_STATUSES:
        return None, ""
    receipt_id = None
    if value is not None:
        if not (len(value) <= 19 and value.isascii() and value.isdecimal()):
            return None, ""
        receipt_id = int(value)
        if not 0 < receipt_id <= (1 << 63) - 1:
            return None, ""

    receipts = MailReceipt.objects.select_related("event").filter(
        event__seat=seat, event__state=EmailEvent.State.APPLIED, event__kind__in=ACTIVATION_EVENTS,
        fields__has_key="participant_email_candidate",
    ).order_by("-observed_at", "-pk")
    functional_addresses = set(get_functional_addresses())
    checked = {}
    suggestion = None
    selected = None
    for receipt in receipts:
        email = _eligible_receipt_address(seat, receipt, functional_addresses, checked)
        if email is None:
            continue
        if suggestion is None:
            if seat.email_events.filter(state=EmailEvent.State.NEEDS_REVIEW).exists():
                return None, "open_review"
            suggestion = email, receipt.pk
        elif suggestion[0] != email:
            return None, "conflict"
        if receipt.pk == receipt_id:
            selected = email, receipt.pk
    return (selected if receipt_id is not None else suggestion), ""


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
    address_receipt_id = None
    address_suggestion_issue = ""
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
    elif not seat.participant.email and can_edit(request.user, "bookings"):
        initial = {"participant_id": seat.participant_id}
        if settings.HTTPS and request.is_secure() and can_view(request.user, "mail_receipts"):
            suggestion, address_suggestion_issue = _receipt_address(seat, request.GET.get("mail"))
            if suggestion:
                initial["email"], address_receipt_id = suggestion
        form = ParticipantEmailForm(initial=initial)

    page = Paginator(_profile_bookings(seat.participant_id), 25).get_page(request.GET.get("page"))
    return render(request, "workspace/booking_participant.html", {
        "section": "bookings",
        "seat": seat,
        "booking_count": page.paginator.count,
        "profile_bookings": page,
        "form": form,
        "address_receipt_id": address_receipt_id,
        "address_suggestion_issue": address_suggestion_issue,
    }, status=status)
