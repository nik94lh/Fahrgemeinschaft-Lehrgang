"""Explicit, local operator decisions for a mail import review."""

from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.dateparse import parse_datetime
from django.utils.formats import date_format
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.access import can_edit, can_view
from mailimport.email_address import normalize_contact_email

from .booking_search import booking_id_query, positive_integer
from .models import BookingHistory, BookingStatus, CourseSeat, EmailEvent, ImportLock, Participant, School
from .services import ACTIVE_STATUSES, CANCELLATION_EVENTS, _cancellation_snapshot, _normalized_name


RESULT_LIMIT = 20
SEAT_ACTIONS = ("link", "reassign")
ACTIONS = {
    "apply_cancellation": (
        "Stornierung in BMS übernehmen",
        "Setzt die zugeordnete Buchung auf „Storniert“ und dokumentiert deine Entscheidung.",
    ),
    "keep_address": (
        "Gespeicherte Adresse behalten",
        "Ordnet die Nachricht der Buchung zu und verwendet weiter die gespeicherte E-Mail-Adresse.",
    ),
    "link": (
        "E-Mail zuordnen",
        "Verknüpft die Nachricht mit einer Lehrgangsbuchung und schließt den Fall. Die Aktion ändert ausschließlich den Klärungsfall.",
    ),
    "reassign": (
        "Ummeldung in BMS übernehmen",
        "Wechselt die Person auf der Buchung, setzt den Status auf „Umgemeldet“ und dokumentiert den Wechsel.",
    ),
    "dismiss": (
        "Fall verwerfen",
        "Schließt den Fall mit dem Status „Verworfen“ und dokumentiert den ausgewählten Grund.",
    ),
}
REASON_GUIDANCE = {
    EmailEvent.ReviewReason.UNRECOGNIZED_TYPE: "Prüfe, ob die Nachricht eine Lehrgangsmeldung ist. Bei einer Antwort leite die Originalmeldung mit ihren Kopfzeilen weiter.",
    EmailEvent.ReviewReason.UNSUPPORTED_TYPE: "BMS erkennt den Meldungstyp, kann daraus aber keine Buchungsänderung ableiten. Prüfe, zu welcher Buchung die Nachricht gehört.",
    EmailEvent.ReviewReason.REASSIGNMENT: "Prüfe, wer den Lehrgangsplatz bisher belegt hat und wer ihn übernehmen soll.",
    EmailEvent.ReviewReason.SCHEDULE_CHANGE: "Vergleiche Beginn und Ende aus der Nachricht mit dem aktuellen Lehrgangstermin.",
    EmailEvent.ReviewReason.INVALID_REFERENCE: "Vergleiche die Buchungsreferenz in der Originalnachricht mit der Lehrgangsbuchung.",
    EmailEvent.ReviewReason.PARTICIPANT_UNCLEAR: "Suche den vollständigen Namen der angemeldeten Person in der Nachricht.",
    EmailEvent.ReviewReason.COURSE_UNCLEAR: "Prüfe die Bezeichnung in der Zeile „Lehrgangsart“ und wähle den passenden Kurs.",
    EmailEvent.ReviewReason.SCHOOL_UNCLEAR: "Prüfe den Schulungsort oder die Durchführungsform. BMS erkennt Würzburg, Regensburg, Geretsried und eindeutig ausgewiesene Online-Lehrgänge.",
    EmailEvent.ReviewReason.PERIOD_UNCLEAR: "Prüfe, ob die Nachricht für Beginn und Ende jeweils ein vollständiges Datum mit Uhrzeit nennt.",
    EmailEvent.ReviewReason.REFERENCE_MISMATCH: "Vergleiche Person, Lehrgang und Zeitraum der Nachricht mit der verknüpften Buchung.",
    EmailEvent.ReviewReason.MULTIPLE_SEATS: "Gleiche die infrage kommenden Buchungen mit Name und Buchungsreferenz in der Nachricht ab.",
    EmailEvent.ReviewReason.MULTIPLE_SESSIONS: "Suche die richtige Buchung anhand der Referenz. Mehrere Termine haben denselben Lehrgang, dieselbe Schule und denselben Zeitraum.",
    EmailEvent.ReviewReason.MULTIPLE_COURSES: "In BMS gibt es mehrere Lehrgänge mit dieser Bezeichnung. Prüfe, welcher zur Meldung gehört.",
    EmailEvent.ReviewReason.PREVIOUS_PARTICIPANT: "Prüfe, ob die Nachricht die aktuell zugeordnete Person oder eine frühere Belegung betrifft.",
    EmailEvent.ReviewReason.NO_MATCHING_BOOKING: "Suche die Buchung nach Referenz, Person oder Lehrgang.",
    EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR: "Vergleiche das Datum und den gemeldeten Status mit den bisherigen Buchungsänderungen.",
    EmailEvent.ReviewReason.PROTECTED_STATUS: "Vergleiche den in der Nachricht gemeldeten Status mit dem aktuellen Buchungsstatus.",
    EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT: (
        "Vergleiche die E-Mail-Adresse aus der Nachricht mit dem Teilnehmerprofil und prüfe, zu welcher Buchung die Nachricht gehört."
    ),
    EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR: "Prüfe anhand der Originalnachricht, zu welcher Person die Buchung gehört.",
}


class SeatChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, seat):
        name = seat.participant.name if seat.participant else "Ohne Zuordnung"
        starts = date_format(timezone.localtime(seat.session.starts_at), "SHORT_DATETIME_FORMAT")
        return (
            f"Buchungs-ID {seat.pk} · {name} · {seat.session.course.name} · "
            f"{seat.session.get_school_display()} · {starts} · {seat.get_status_display()}"
        )


class ParticipantChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, participant):
        return f"{participant.name} · Nr. {participant.pk}"


class ReviewForm(forms.Form):
    APPLY_CANCELLATION = "apply_cancellation"
    KEEP_ADDRESS = "keep_address"
    LINK = "link"
    REASSIGN = "reassign"
    DISMISS = "dismiss"
    DISMISS_REASONS = (
        EmailEvent.ReviewResolution.DUPLICATE,
        EmailEvent.ReviewResolution.INCOMPLETE,
        EmailEvent.ReviewResolution.NOT_RELEVANT,
    )

    action = forms.ChoiceField(label="Entscheidung", widget=forms.HiddenInput)
    seat = SeatChoiceField(
        label="Lehrgangsbuchung", queryset=CourseSeat.objects.none(),
        required=False, empty_label="Bitte auswählen",
    )
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
    address_snapshot = forms.CharField(max_length=64, required=False, widget=forms.HiddenInput)
    cancellation_snapshot = forms.CharField(max_length=64, required=False, widget=forms.HiddenInput)
    confirm = forms.BooleanField(label="Ich habe die Angaben geprüft und bestätige die Entscheidung.")

    def __init__(self, *args, event, seat_ids, actions, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action"].choices = [(key, details[0]) for key, details in actions.items()]
        self.fields["seat"].queryset = _seats().filter(pk__in=seat_ids)
        if event.kind == EmailEvent.Kind.REASSIGNMENT:
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
            self.add_error("seat", "Wähle eine Lehrgangsbuchung aus.")
        if action == self.REASSIGN:
            if not fields.get("previous_participant"):
                self.add_error("previous_participant", "Wähle die bisherige Person aus.")
            if not fields.get("replacement") or fields.get("replacement") == fields.get("previous_participant"):
                self.add_error("replacement", "Wähle eine andere, bereits erfasste Person aus.")
        if action == self.DISMISS and not fields.get("reason"):
            self.add_error("reason", "Wähle einen Grund aus.")
        return fields


def _actions(event, can_keep_address, can_apply_cancellation):
    available = {ReviewForm.LINK, ReviewForm.DISMISS}
    if can_apply_cancellation:
        available.add(ReviewForm.APPLY_CANCELLATION)
    if can_keep_address:
        available.add(ReviewForm.KEEP_ADDRESS)
    if event.kind == EmailEvent.Kind.REASSIGNMENT:
        available.add(ReviewForm.REASSIGN)
    return {key: details for key, details in ACTIONS.items() if key in available}


def _seats():
    return CourseSeat.objects.select_related("participant", "session__course").order_by("-session__starts_at", "pk")


def _review_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return None
    return parsed if parsed and timezone.is_aware(parsed) else None


def _recognized(event):
    data = event.review_context
    if not isinstance(data, dict):
        return {}
    fields = {
        key: data[key] for key in ("course_name", "school", "participant_name", "previous_participant_name", "booking_reference")
        if isinstance(data.get(key), str) and data[key]
    }
    fields["starts_at"] = _review_time(data.get("starts_at"))
    fields["ends_at"] = _review_time(data.get("ends_at"))
    if data.get("is_online") is True:
        fields["is_online"] = True
    if fields.get("school") not in School.values:
        fields.pop("school", None)
    return fields


def _candidate_seats(event, recognized, action, search):
    seats = _seats()
    if action == ReviewForm.REASSIGN and event.seat_id:
        return seats.filter(pk=event.seat_id)
    if search:
        explicit_id, booking_id = booking_id_query(search)
        if explicit_id:
            return seats.filter(pk=booking_id)
        if booking_id is not None:
            return seats.filter(Q(pk=booking_id) | Q(external_reference__iexact=search))
        if len(search) < 2:
            return seats.filter(external_reference__iexact=search)
        return seats.filter(
            Q(participant__name__icontains=search)
            | Q(session__course__name__icontains=search)
            | Q(external_reference__iexact=search),
        )

    criteria = Q(pk=event.seat_id) if event.seat_id else Q(pk__in=[])
    if reference := recognized.get("booking_reference"):
        criteria |= Q(external_reference=reference)
    if all(recognized.get(key) for key in ("course_name", "school", "starts_at", "ends_at")):
        matching_session = Q(
            session__course__name__iexact=recognized["course_name"],
            session__school=recognized["school"],
            session__starts_at=recognized["starts_at"],
            session__ends_at=recognized["ends_at"],
        )
        name_key = "previous_participant_name" if event.kind == EmailEvent.Kind.REASSIGNMENT else "participant_name"
        if person := recognized.get(name_key):
            matching_session &= Q(participant__name__iexact=person)
        criteria |= matching_session
    return seats.filter(criteria)


def _queue_params(request):
    reason = request.POST.get("grund", request.GET.get("grund", ""))
    page = positive_integer(request.POST.get("page", request.GET.get("page")))
    params = {}
    if reason and reason in EmailEvent.ReviewReason.values:
        params["grund"] = reason
    if page is not None:
        params["page"] = page
    return params


def _context(request, event):
    recognized = _recognized(event)
    comparison_seat = _comparison_seat(event)
    address_conflict = event.review_reason == EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT
    queue_params = _queue_params(request)
    context = {
        "section": "review",
        "event": event,
        "queue_params": queue_params,
        "queue_query": urlencode(queue_params),
        "recognized": recognized,
        "has_recognized_data": any(recognized.values()),
        "school_name": dict(School.choices).get(recognized.get("school")),
        "comparison_seat": comparison_seat,
        "differences": _current_booking_differences(event, recognized, comparison_seat),
        "address_conflict": address_conflict,
        "guidance": REASON_GUIDANCE.get(
            event.review_reason, "Vergleiche die erkannten Angaben mit der Originalnachricht und der Lehrgangsbuchung.",
        ),
    }
    if settings.HTTPS and request.is_secure() and can_view(request.user, "mail_receipts"):
        _receipt_context(request, event, context)
        if context.get("incoming_address") and comparison_seat and comparison_seat.participant_id:
            current_address = normalize_contact_email(comparison_seat.participant.email)
            if current_address and current_address != context["incoming_address"]:
                context["differences"].append("E-Mail-Adresse weicht ab")
    if event.state != EmailEvent.State.NEEDS_REVIEW:
        return context

    can_decide = can_edit(request.user, "review") and can_edit(request.user, "bookings")
    can_keep_address = can_decide and _can_keep_address(event, recognized, comparison_seat)
    cancellation_snapshot = _current_cancellation_snapshot(event, comparison_seat) if can_decide else None
    actions = _actions(event, can_keep_address, bool(cancellation_snapshot))
    context["cancellation_changed"] = bool(
        can_decide and not cancellation_snapshot and _recorded_cancellation_snapshot(event)
    )
    data = request.POST if request.method == "POST" else None
    requested = data.get("action") if data is not None else request.GET.get("aktion")
    action = requested if requested in actions else None
    context.update(actions=actions, action=action, action_details=actions.get(action))
    if action is None and data is None:
        return context

    seat_search_available = action in SEAT_ACTIONS and not (
        action == ReviewForm.REASSIGN and event.seat_id
    )
    search = " ".join(request.GET.get("platzsuche", "")[:80].split()) if seat_search_available else ""
    candidate_ids = []
    if action in SEAT_ACTIONS:
        candidate_ids = list(
            _candidate_seats(event, recognized, action, search).values_list("pk", flat=True)[:RESULT_LIMIT + 1],
        )
    seat_ids = set(candidate_ids[:RESULT_LIMIT])
    if data is not None and (submitted := positive_integer(data.get("seat"))):
        # An intentional selection remains valid even when the operator changes the search.
        seat_ids.add(submitted)

    initial = {"action": action}
    if action == ReviewForm.KEEP_ADDRESS:
        initial["address_snapshot"] = _address_snapshot(event, comparison_seat)
    elif action == ReviewForm.APPLY_CANCELLATION:
        initial["cancellation_snapshot"] = cancellation_snapshot
    if event.seat_id in seat_ids and not search:
        initial["seat"] = event.seat_id
    context.update(
        search=search,
        results_capped=len(candidate_ids) > RESULT_LIMIT,
        has_candidate_seats=bool(candidate_ids),
        seat_search_available=seat_search_available,
        form=ReviewForm(data, event=event, seat_ids=seat_ids, actions=actions, initial=initial),
    )
    return context


def _comparison_seat(event):
    if event.seat_id:
        return event.seat
    if isinstance(event.review_context, dict):
        candidate_id = event.review_context.get("candidate_seat_id")
        if isinstance(candidate_id, int):
            return _seats().filter(pk=candidate_id).first()
    return None


def _receipt_context(request, event, context):
    receipts = event.mail_receipts.order_by("-observed_at", "-pk")
    if context["address_conflict"] and can_view(request.user, "bookings"):
        receipt = receipts.filter(fields__has_key="participant_email").first()
        address = receipt.fields.get("participant_email") if receipt and isinstance(receipt.fields, dict) else None
        if isinstance(address, str) and (normalized := normalize_contact_email(address)):
            context.update(receipt_id=receipt.pk, incoming_address=normalized)
            return
    context["receipt_id"] = receipts.values_list("pk", flat=True).first()


def _current_booking_differences(event, recognized, seat):
    if seat is None or event.review_reason not in (
        EmailEvent.ReviewReason.REFERENCE_MISMATCH, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT,
    ):
        return []
    comparisons = (
        ("Teilnehmer", _normalized_name(recognized.get("participant_name", "")),
         _normalized_name(seat.participant.name) if seat.participant else ""),
        ("Lehrgang", recognized.get("course_name", "").casefold(), seat.session.course.name.casefold()),
        ("Schule", recognized.get("school"), seat.session.school),
        ("Beginn", recognized.get("starts_at"), seat.session.starts_at),
        ("Ende", recognized.get("ends_at"), seat.session.ends_at),
        ("Buchungsreferenz", recognized.get("booking_reference"), seat.external_reference),
    )
    return [f"{label} weicht ab" for label, incoming, current in comparisons if incoming and incoming != current]


def _can_keep_address(event, recognized, seat):
    return bool(
        event.review_reason == EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT
        and seat and event.seat_id == seat.pk and seat.participant_id
        and normalize_contact_email(seat.participant.email)
        and _normalized_name(recognized.get("participant_name", ""))
        and not _current_booking_differences(event, recognized, seat)
    )


def _address_snapshot(event, seat):
    return salted_hmac(
        "bookings.review.keep_address",
        f"{event.pk}:{seat.pk}:{seat.participant_id}:{seat.participant.email}",
        algorithm="sha256",
    ).hexdigest()


def _recorded_cancellation_snapshot(event):
    if (
        event.kind in CANCELLATION_EVENTS and event.review_reason == EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR
        and isinstance(event.review_context, dict)
    ):
        snapshot = event.review_context.get("cancellation_snapshot")
        if isinstance(snapshot, str):
            return snapshot
    return None


def _current_cancellation_snapshot(event, seat):
    recorded = _recorded_cancellation_snapshot(event)
    if not recorded or seat is None or event.seat_id != seat.pk or not seat.participant_id or seat.status not in ACTIVE_STATUSES:
        return None
    current = _cancellation_snapshot(event, seat)
    return current if constant_time_compare(recorded, current) else None


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
            context = _context(request, event)
            if event.state != EmailEvent.State.NEEDS_REVIEW:
                return render(request, "workspace/review_detail.html", context, status=409)
            form = context["form"]
            if form.is_valid() and _apply_decision(form, event):
                return redirect(reverse("review", query=context["queue_params"]))
    else:
        event = get_object_or_404(cases.select_related("seat__participant", "seat__session__course"), pk=event_id)
        context = _context(request, event)
    return render(request, "workspace/review_detail.html", context)


def _apply_decision(form, event):
    data = form.cleaned_data
    action = data["action"]
    if action == ReviewForm.DISMISS:
        event.state = EmailEvent.State.DISMISSED
        event.review_resolution = data["reason"]
    else:
        fixed_seat = action in (ReviewForm.KEEP_ADDRESS, ReviewForm.APPLY_CANCELLATION)
        seat_id = event.seat_id if fixed_seat else data["seat"].pk
        seat = CourseSeat.objects.select_for_update().filter(pk=seat_id).first()
        if seat is None:
            field = None if fixed_seat else "seat"
            form.add_error(field, "Der Lehrgangsplatz ist nicht mehr vorhanden.")
            return False
        event.review_resolution = EmailEvent.ReviewResolution.LINKED
        if action == ReviewForm.KEEP_ADDRESS:
            participant = Participant.objects.select_for_update().filter(pk=seat.participant_id).first()
            if participant:
                seat.participant = participant
            if (
                not _can_keep_address(event, _recognized(event), seat)
                or not constant_time_compare(data["address_snapshot"], _address_snapshot(event, seat))
            ):
                form.add_error(None, "Teilnehmer oder Adresse haben sich geändert. Öffne den Klärungsfall erneut.")
                return False
            event.review_resolution = EmailEvent.ReviewResolution.ADDRESS_KEPT
        elif action == ReviewForm.APPLY_CANCELLATION:
            if not _apply_cancellation(form, event, seat):
                return False
            event.review_resolution = EmailEvent.ReviewResolution.CANCELLED
        elif action == ReviewForm.REASSIGN:
            if not _reassign(form, event, seat):
                return False
            event.review_resolution = EmailEvent.ReviewResolution.REASSIGNED
        event.seat = seat
        event.state = EmailEvent.State.REVIEWED
    event.reviewed_at = timezone.now()
    event.save(update_fields=["state", "review_resolution", "reviewed_at", "seat"])
    return True


def _apply_cancellation(form, event, seat):
    current = _current_cancellation_snapshot(event, seat)
    if not current or not constant_time_compare(form.cleaned_data["cancellation_snapshot"], current):
        form.add_error(None, "Die Buchung oder die geprüfte Zuordnung hat sich geändert. Öffne den Klärungsfall erneut.")
        return False

    BookingHistory.objects.create(
        seat=seat, previous_participant_id=seat.participant_id, participant_id=seat.participant_id,
        previous_status=seat.status, status=BookingStatus.CANCELLED, source_event=event,
    )
    seat.status = BookingStatus.CANCELLED
    seat.save(update_fields=["status"])
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
