"""Explicit, local operator decisions for a mail import review."""

from django import forms
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .models import BookingHistory, BookingStatus, CourseSeat, EmailEvent, ImportLock, Participant


class SeatChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, seat):
        name = seat.participant.name if seat.participant else "Ohne Zuordnung"
        starts = date_format(timezone.localtime(seat.session.starts_at), "SHORT_DATETIME_FORMAT")
        return f"Platz {seat.pk} · {name} · {seat.session.course.name} · {starts}"


class ParticipantChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, participant):
        return f"{participant.name} · Nr. {participant.pk}"


class ReviewForm(forms.Form):
    LINK = "link"
    REASSIGN = "reassign"
    DISMISS = "dismiss"
    DISMISS_REASONS = (
        EmailEvent.ReviewResolution.DUPLICATE,
        EmailEvent.ReviewResolution.INCOMPLETE,
        EmailEvent.ReviewResolution.NOT_RELEVANT,
    )

    action = forms.ChoiceField(label="Entscheidung")
    seat = SeatChoiceField(label="Lehrgangsplatz", queryset=CourseSeat.objects.none(), required=False)
    previous_participant = ParticipantChoiceField(
        label="Bisheriger Teilnehmer", queryset=Participant.objects.none(), required=False,
    )
    replacement = ParticipantChoiceField(
        label="Neuer Teilnehmer", queryset=Participant.objects.none(), required=False,
    )
    reason = forms.ChoiceField(
        label="Grund für das Verwerfen",
        choices=[("", "Bitte auswählen"), *((reason.value, reason.label) for reason in DISMISS_REASONS)],
        required=False,
    )
    confirm = forms.BooleanField(label="Ich habe die Angaben geprüft und bestätige die Entscheidung.")

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action"].choices = [
            ("", "Bitte auswählen"),
            (self.LINK, "Einem Lehrgangsplatz zuordnen"),
            (self.DISMISS, "Klärungsfall verwerfen"),
        ]
        self.fields["seat"].queryset = CourseSeat.objects.select_related(
            "participant", "session__course",
        ).order_by("-session__starts_at", "pk")
        if event.kind == EmailEvent.Kind.REASSIGNMENT:
            self.fields["action"].choices += [(self.REASSIGN, "Ummeldung in BMS übernehmen")]
            participants = Participant.objects.order_by("name", "pk")
            self.fields["previous_participant"].queryset = participants
            self.fields["replacement"].queryset = participants
        else:
            del self.fields["previous_participant"]
            del self.fields["replacement"]

    def clean(self):
        fields = super().clean()
        action = fields.get("action")
        if action in (self.LINK, self.REASSIGN) and not fields.get("seat"):
            self.add_error("seat", "Wähle einen Lehrgangsplatz aus.")
        if action == self.REASSIGN:
            if not fields.get("previous_participant"):
                self.add_error("previous_participant", "Wähle die bisherige Person aus.")
            if not fields.get("replacement") or fields.get("replacement") == fields.get("previous_participant"):
                self.add_error("replacement", "Wähle eine andere, bereits erfasste Person aus.")
        if action == self.DISMISS and not fields.get("reason"):
            self.add_error("reason", "Wähle einen Grund aus.")
        return fields


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
@csrf_protect
def review_detail(request, event_id):
    cases = EmailEvent.objects.filter(state__in=[
        EmailEvent.State.NEEDS_REVIEW, EmailEvent.State.REVIEWED, EmailEvent.State.DISMISSED,
    ])
    if request.method == "POST":
        with transaction.atomic():
            ImportLock.objects.select_for_update().get_or_create(pk=1)
            event = get_object_or_404(cases.select_for_update(), pk=event_id)
            if event.state != EmailEvent.State.NEEDS_REVIEW:
                return _render(request, event, status=409)
            form = ReviewForm(request.POST, event=event)
            if form.is_valid() and _apply_decision(form, event):
                return redirect("review")
        return _render(request, event, form=form)

    event = get_object_or_404(cases, pk=event_id)
    form = None
    if event.state == EmailEvent.State.NEEDS_REVIEW:
        form = ReviewForm(event=event, initial={"seat": event.seat_id})
    return _render(request, event, form=form)


def _render(request, event, *, form=None, status=200):
    return render(request, "workspace/review_detail.html", {
        "section": "review", "event": event, "form": form,
    }, status=status)


def _apply_decision(form, event):
    data = form.cleaned_data
    action = data["action"]
    if action == ReviewForm.DISMISS:
        event.state = EmailEvent.State.DISMISSED
        event.review_resolution = data["reason"]
    else:
        seat = CourseSeat.objects.select_for_update().filter(pk=data["seat"].pk).first()
        if seat is None:
            form.add_error("seat", "Der Lehrgangsplatz ist nicht mehr vorhanden.")
            return False
        event.review_resolution = EmailEvent.ReviewResolution.LINKED
        if action == ReviewForm.REASSIGN:
            if not _reassign(form, event, seat):
                return False
            event.review_resolution = EmailEvent.ReviewResolution.REASSIGNED
        event.seat = seat
        event.state = EmailEvent.State.REVIEWED
    event.reviewed_at = timezone.now()
    event.save(update_fields=["state", "review_resolution", "reviewed_at", "seat"])
    return True


def _reassign(form, event, seat):
    previous = form.cleaned_data["previous_participant"]
    replacement = form.cleaned_data["replacement"]
    if event.seat_id is not None and event.seat_id != seat.pk:
        form.add_error("seat", "Wähle den durch die Buchungsreferenz identifizierten Lehrgangsplatz.")
        return False
    if seat.participant_id != previous.pk:
        form.add_error("previous_participant", "Die bisherige Person stimmt nicht überein.")
        return False
    if seat.status in (BookingStatus.CANCELLED, BookingStatus.COMPLETED):
        form.add_error("seat", "Der Lehrgangsplatz ist storniert oder abgeschlossen.")
        return False
    if event.occurred_at and seat.history.filter(source_event__occurred_at__gt=event.occurred_at).exists():
        form.add_error("seat", "Zu diesem Platz liegt bereits eine neuere Buchungsänderung vor.")
        return False
    if not Participant.objects.select_for_update().filter(pk=replacement.pk).exists():
        form.add_error("replacement", "Die ausgewählte Person ist nicht mehr vorhanden.")
        return False

    BookingHistory.objects.create(
        seat=seat,
        previous_participant=previous,
        participant=replacement,
        previous_status=seat.status,
        status=BookingStatus.REASSIGNED,
        source_event=event,
    )
    seat.participant = replacement
    seat.status = BookingStatus.REASSIGNED
    seat.save(update_fields=["participant", "status"])
    return True
