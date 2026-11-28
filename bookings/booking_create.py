"""Create a booking directly in BMS and record its starting state."""

from zoneinfo import ZoneInfo

from django import forms
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from mailimport.participant_settings import get_functional_addresses

from .models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, School
from .rides_matching import sync_ride_cases
from .services import _IdentityConflict, _lock_import, _normalized_name, _participant_with_email


BERLIN = ZoneInfo("Europe/Berlin")
DATETIME_LOCAL = {"type": "datetime-local"}
DATETIME_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


class BookingDetailsForm(forms.Form):
    participant_name = forms.CharField(label="Teilnehmername", max_length=200)
    participant_email = forms.EmailField(label="Persönliche E-Mail-Adresse", max_length=254)
    course_name = forms.CharField(label="Lehrgang", max_length=200)
    school = forms.ChoiceField(label="Schulungsort", choices=[("", "Bitte auswählen"), *School.choices])
    is_online = forms.BooleanField(label="Online-Lehrgang", required=False)
    starts_at = forms.DateTimeField(
        label="Beginn", widget=forms.DateTimeInput(attrs=DATETIME_LOCAL, format=DATETIME_LOCAL_FORMAT),
    )
    ends_at = forms.DateTimeField(
        label="Ende", widget=forms.DateTimeInput(attrs=DATETIME_LOCAL, format=DATETIME_LOCAL_FORMAT),
    )
    status = forms.ChoiceField(label="Buchungsstatus", choices=BookingStatus.choices, initial=BookingStatus.CONFIRMED)
    external_reference = forms.CharField(label="Externe Buchungsreferenz", max_length=120, required=False)
    confirmation_deadline = forms.DateField(
        label="Bestätigungsfrist", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), required=False,
    )

    def clean_participant_email(self):
        try:
            return Participant.normalize_email(self.cleaned_data["participant_email"])
        except ValueError as error:
            raise ValidationError(str(error)) from error

    def clean(self):
        data = super().clean()
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "Das Ende muss nach dem Beginn liegen.")
        return data

    def validate_test_address(self, is_test):
        email = self.cleaned_data.get("participant_email")
        if is_test and email and not email.rpartition("@")[2].endswith(".invalid"):
            self.add_error("participant_email", "Gib für eine Testbuchung eine Adresse mit der Endung .invalid ein.")


class BookingCreateForm(BookingDetailsForm):
    is_test = forms.BooleanField(
        label="Testbuchung", required=False, initial=True,
        help_text="Verwende für eine Testbuchung eine Beispieladresse mit der Endung .invalid.",
    )

    def clean(self):
        data = super().clean()
        self.validate_test_address(data.get("is_test"))
        return data


def _existing_session(data, online):
    found = CourseSession.objects.filter(
        course__name__iexact=data["course_name"], school=data["school"],
        starts_at=data["starts_at"], ends_at=data["ends_at"], is_test=data["is_test"],
    ).select_related("course")
    if data["school"] != School.ONLINE:
        found = found.filter(is_online=online)
    sessions = list(found[:2])
    if len(sessions) > 1:
        raise ValidationError({"course_name": "Zu diesen Angaben gibt es mehrere Lehrgangstermine."})
    return sessions[0] if sessions else None


def _new_session(data, online):
    courses = list(Course.objects.filter(name__iexact=data["course_name"])[:2])
    if len(courses) > 1:
        raise ValidationError({"course_name": "Unter dieser Bezeichnung gibt es mehrere Lehrgänge."})
    course = courses[0] if courses else Course.objects.create(name=data["course_name"])
    return CourseSession.objects.create(
        course=course, school=data["school"], is_online=online, is_test=data["is_test"],
        starts_at=data["starts_at"], ends_at=data["ends_at"],
    )


@transaction.atomic
def _create_booking(data):
    _lock_import()
    email = data["participant_email"]
    if email in get_functional_addresses():
        raise ValidationError({"participant_email": "Diese Adresse ist als Funktionspostfach hinterlegt."})
    reference = data["external_reference"]
    if reference and CourseSeat.objects.filter(external_reference=reference, is_test=data["is_test"]).exists():
        raise ValidationError({"external_reference": "Diese Buchungsreferenz wird bereits verwendet."})

    try:
        participant = _participant_with_email(data["participant_name"], email, is_test=data["is_test"])
    except _IdentityConflict as error:
        message = (
            "Für Testbuchungen und reguläre Buchungen sind getrennte Adressen erforderlich."
            if error.reason == EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR
            else "Diese Adresse ist bereits einer anderen Person zugeordnet."
        )
        raise ValidationError({"participant_email": message}) from error

    online = data["is_online"] or data["school"] == School.ONLINE
    session = _existing_session(data, online)
    if session is not None:
        if participant and session.seats.filter(participant=participant).exists():
            raise ValidationError({"participant_email": "Für diese Person gibt es bereits eine Buchung zu diesem Termin."})
        unresolved_names = session.seats.filter(is_test=data["is_test"], participant__email="").values_list(
            "participant__name", flat=True,
        )
        name = _normalized_name(data["participant_name"])
        if any(_normalized_name(existing) == name for existing in unresolved_names):
            raise ValidationError({"participant_name": "Prüfe die vorhandene Buchung mit diesem Namen und ergänze dort die Adresse."})
    else:
        session = _new_session(data, online)

    if participant is None:
        participant = Participant.objects.create(name=data["participant_name"], email=email)
    seat = CourseSeat.objects.create(
        participant=participant, session=session, is_test=data["is_test"], status=data["status"],
        external_reference=reference, confirmation_deadline=data["confirmation_deadline"],
    )
    BookingHistory.objects.create(seat=seat, participant=participant, status=seat.status)
    if seat.status == BookingStatus.CONFIRMED and not online and session.ends_at > timezone.now():
        sync_ride_cases()
    return seat


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def booking_create(request):
    with timezone.override(BERLIN):
        form = BookingCreateForm(request.POST if request.method == "POST" else None)
        status = 200
        if request.method == "POST":
            status = 400
            if form.is_valid():
                try:
                    seat = _create_booking(form.cleaned_data)
                except ValidationError as error:
                    form.add_error(None, error)
                except IntegrityError:
                    form.add_error(None, "Die Buchung überschneidet sich mit einem bereits gespeicherten Eintrag.")
                else:
                    return redirect("booking_detail", seat_id=seat.pk)
        return render(request, "workspace/booking_form.html", {"section": "bookings", "form": form}, status=status)
