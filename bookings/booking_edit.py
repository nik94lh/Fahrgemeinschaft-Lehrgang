"""Correct the local data on one booking without rewriting other bookings."""

from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from mailimport.participant_settings import get_functional_addresses

from .booking_create import BERLIN, BookingDetailsForm, _existing_session, _new_session
from .booking_deletion import booking_snapshot
from .booking_search import booking_list_navigation
from .models import BookingHistory, BookingStatus, CourseSeat, EmailEvent, Participant, School
from .ride_participation import expire_booking_participation
from .rides_matching import sync_ride_cases
from .services import _IdentityConflict, _lock_import, _normalized_name, _participant_with_email


BOOKING_FIELDS = ("status", "external_reference", "confirmation_deadline")
SESSION_FIELDS = ("course_name", "school", "is_online", "starts_at", "ends_at")
PARTICIPANT_FIELDS = ("participant_name", "participant_email")
BOOKING_SAVED_SESSION_KEY = "booking_saved_id"


class BookingEditForm(BookingDetailsForm):
    snapshot = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def __init__(self, *args, seat, **kwargs):
        super().__init__(*args, **kwargs)
        self.seat = seat
        self.fields["participant_email"].required = False
        self.fields["participant_name"].required = seat.participant_id is not None
        if seat.external_reference and seat.email_events.exists():
            self.fields["external_reference"].disabled = True
            self.fields["external_reference"].help_text = "Diese Referenz gehört zu einer importierten Buchung."

    def clean(self):
        data = super().clean()
        self.validate_test_address(self.seat.is_test)
        if data.get("participant_email") and not data.get("participant_name") and "participant_name" not in self.errors:
            self.add_error("participant_name", "Gib den Namen zu dieser E-Mail-Adresse ein.")
        return data

    def _preserve_existing_time(self, field):
        value = self.cleaned_data[field]
        original = getattr(self.seat.session, field)
        return original if value == original.replace(second=0, microsecond=0) else value

    def clean_starts_at(self):
        return self._preserve_existing_time("starts_at")

    def clean_ends_at(self):
        return self._preserve_existing_time("ends_at")


def _booking_values(seat):
    return {
        **{field: getattr(seat, field) for field in BOOKING_FIELDS},
        "course_name": seat.session.course.name,
        "school": seat.session.school,
        "is_online": seat.session.is_online or seat.session.school == School.ONLINE,
        "starts_at": seat.session.starts_at,
        "ends_at": seat.session.ends_at,
        "participant_name": seat.participant.name if seat.participant else "",
        "participant_email": seat.participant.email if seat.participant else "",
    }


def _snapshot(seat):
    return booking_snapshot(seat, purpose="bookings.edit_booking")


def _profile_used_by_other_bookings(seat):
    participant = seat.participant
    return participant.seats.exclude(pk=seat.pk).exists() or BookingHistory.objects.filter(
        Q(participant=participant) | Q(previous_participant=participant),
    ).exclude(seat_id=seat.pk).exists()


@sensitive_variables()
def _edited_participant(seat, data):
    previous = seat.participant
    name, email = data["participant_name"], data["participant_email"]
    if email and email in get_functional_addresses():
        raise ValidationError({"participant_email": "Diese Adresse ist als Funktionspostfach hinterlegt."})
    same_name = previous is not None and _normalized_name(name) == _normalized_name(previous.name)
    if previous and email == previous.email and (email or same_name):
        if _profile_used_by_other_bookings(seat):
            raise ValidationError({"participant_name": "Dieses Profil gehört zu weiteren Buchungen. Gib für eine andere Person ihre eigene Adresse ein."})
        previous.name = name
        previous.save(update_fields=["name"])
        return previous
    try:
        found = _participant_with_email(name, email, is_test=seat.is_test) if email else None
    except _IdentityConflict as error:
        message = (
            "Für Testbuchungen und reguläre Buchungen sind getrennte Adressen erforderlich."
            if error.reason == EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR
            else "Diese Adresse ist bereits einer anderen Person zugeordnet."
        )
        raise ValidationError({"participant_email": message}) from error
    if found is not None:
        return found
    if same_name and not previous.email and not _profile_used_by_other_bookings(seat):
        previous.name, previous.email = name, email
        previous.save(update_fields=["name", "email"])
        return previous
    return Participant.objects.create(name=name, email=email)


@sensitive_variables()
@transaction.atomic
def _edit_booking(seat_id, data):
    _lock_import()
    seat = get_object_or_404(CourseSeat.objects.select_for_update(), pk=seat_id)
    if not constant_time_compare(data["snapshot"], _snapshot(seat)):
        return False
    if seat.external_reference and seat.email_events.exists():
        data["external_reference"] = seat.external_reference

    data["is_online"] = data["is_online"] or data["school"] == School.ONLINE
    data["is_test"] = seat.is_test
    previous = _booking_values(seat)
    changed = {field for field, value in previous.items() if value != data[field]}
    if previous["course_name"].casefold() == data["course_name"].casefold():
        changed.discard("course_name")
    if not changed:
        return True
    reference = data["external_reference"]
    if "external_reference" in changed and reference and CourseSeat.objects.filter(
        is_test=seat.is_test, external_reference=reference,
    ).exclude(pk=seat.pk).exists():
        raise ValidationError({"external_reference": "Diese Buchungsreferenz wird bereits verwendet."})

    old_session, old_participant = seat.session, seat.participant
    if changed.intersection(SESSION_FIELDS):
        seat.session = _existing_session(data, data["is_online"]) or _new_session(data, data["is_online"])
    if changed.intersection(PARTICIPANT_FIELDS):
        seat.participant = _edited_participant(seat, data)
    if seat.participant and changed.intersection((*SESSION_FIELDS, *PARTICIPANT_FIELDS)):
        other_seats = seat.session.seats.exclude(pk=seat.pk)
        if other_seats.filter(participant=seat.participant).exists():
            raise ValidationError({"participant_email": "Für diese Person gibt es bereits eine Buchung zu diesem Termin."})
        name = _normalized_name(data["participant_name"])
        unresolved = other_seats.filter(participant__email="").values_list("participant__name", flat=True)
        if any(_normalized_name(existing) == name for existing in unresolved):
            raise ValidationError({"participant_name": "Prüfe die vorhandene Buchung mit diesem Namen und ergänze dort die Adresse."})

    changes_assignment = changed.intersection((*SESSION_FIELDS, *PARTICIPANT_FIELDS))
    remember_previous = changed.intersection((*SESSION_FIELDS, "participant_name"))
    if changes_assignment or ("status" in changed and data["status"] != BookingStatus.CONFIRMED):
        expire_booking_participation(seat.pk)
    for field in BOOKING_FIELDS:
        setattr(seat, field, data[field])
    seat.save(update_fields=["session", "participant", *BOOKING_FIELDS])
    BookingHistory.objects.create(
        seat=seat, previous_participant=old_participant, participant=seat.participant,
        previous_status=previous["status"], status=seat.status,
        previous_session=old_session if remember_previous else None,
        previous_name=previous["participant_name"] if remember_previous else "",
        edited_fields=[str(BookingDetailsForm.base_fields[field].label) for field in previous if field in changed],
    )
    sync_ride_cases()
    return True


@sensitive_post_parameters("participant_name", "participant_email", "external_reference")
@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def booking_edit(request, seat_id):
    seats = CourseSeat.objects.select_related("participant", "session__course")
    seat = get_object_or_404(seats, pk=seat_id)
    _, detail_params = booking_list_navigation(request.GET)
    detail_url = reverse("booking_detail", args=[seat_id], query=detail_params)
    status = 200
    with timezone.override(BERLIN):
        form = BookingEditForm(
            request.POST if request.method == "POST" else None,
            seat=seat, initial={**_booking_values(seat), "snapshot": _snapshot(seat)},
        )
        if request.method == "POST":
            status = 400
            if form.is_valid():
                try:
                    saved = _edit_booking(seat.pk, form.cleaned_data)
                except ValidationError as error:
                    form.add_error(None, error)
                except IntegrityError:
                    form.add_error(None, "Die Buchung überschneidet sich mit einem bereits gespeicherten Eintrag.")
                else:
                    if saved:
                        request.session[BOOKING_SAVED_SESSION_KEY] = seat.pk
                        return redirect(detail_url)
                    status = 409
                    seat = get_object_or_404(seats, pk=seat_id)
                    form = BookingEditForm(seat=seat, initial={**_booking_values(seat), "snapshot": _snapshot(seat)})
        return render(request, "workspace/booking_form.html", {
            "section": "bookings", "form": form, "edit_seat": seat, "detail_url": detail_url, "stale": status == 409,
        }, status=status)
