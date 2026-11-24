"""Fill a missing participant address from a previously applied message."""

from enum import StrEnum

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.views.decorators.debug import sensitive_variables

from mailimport.email_address import normalize_contact_email
from mailimport.parser import MailParseError, ParsedMail, parse_eml
from mailimport.participant_settings import get_functional_addresses

from .models import CourseSeat, EmailEvent, Participant
from .services import (
    ACTIVATION_EVENTS,
    ACTIVE_STATUSES,
    _IdentityConflict,
    _lock_import,
    _matches,
    _parsed_review_reason,
    _participant_with_email,
)


class ContactReplayResult(StrEnum):
    ADDED = "Die fehlende Adresse wurde ergänzt. Der Buchungsstatus blieb gleich."
    INVALID = "Die Nachricht enthält keine eindeutigen Angaben für einen Adressnachtrag."
    NO_APPLIED_EVENT = "Zur früheren Mail gibt es keine anwendbare Buchungszuordnung."
    NO_ACTIVE_BOOKING = "Die frühere Mail ist keiner aktiven Buchung mit Teilnehmer zugeordnet."
    BOOKING_CHANGED = "Die erkannten Angaben passen nicht mehr zur aktuell zugeordneten Buchung."
    SAME_ADDRESS = "Die erkannte Adresse ist bei diesem Teilnehmer bereits hinterlegt."
    OTHER_ADDRESS = "Beim Teilnehmer ist bereits eine andere Adresse hinterlegt."
    OPEN_REVIEW = "Zu dieser Buchung gibt es einen offenen Klärungsfall."
    PARTICIPANT_CHANGED = "Aus der Buchungshistorie lässt sich die ursprünglich zugeordnete Person nicht mehr eindeutig bestätigen."
    ADDRESS_IN_USE = "Die erkannte Adresse ist bereits in einem anderen Teilnehmerprofil hinterlegt."
    PROFILE_CHANGED = "Das Teilnehmerprofil wurde während des Abgleichs geändert."


@sensitive_variables()
def enrich_contact_from_imported_mail(raw: bytes) -> bool:
    """Read the EML in memory and update only a provably unchanged blank contact."""
    try:
        mail = parse_eml(raw, functional_addresses=get_functional_addresses())
    except MailParseError:
        return False
    return fill_existing_contact(mail) == ContactReplayResult.ADDED


def _history_matches(event, seat) -> bool:
    history = seat.history
    sourced = list(history.filter(source_event=event)[:2])
    current_id = seat.participant_id
    if sourced:
        if len(sourced) != 1 or sourced[0].participant_id != current_id:
            return False
        original = sourced[0]
        later = history.filter(
            Q(recorded_at__gt=original.recorded_at)
            | Q(recorded_at=original.recorded_at, pk__gt=original.pk)
        )
        return not later.exclude(participant_id=current_id, previous_participant_id=current_id).exists()

    # A same-status import has no own history row. Use its earlier recorded occupant
    # only if the seat has no evidence of any other occupant.
    original = history.filter(recorded_at__lte=event.imported_at).order_by("-recorded_at", "-pk").first()
    if original is None or original.participant_id != current_id:
        return False
    unchanged = Q(participant_id=current_id) & (
        Q(previous_participant_id=current_id) | Q(previous_participant_id__isnull=True)
    )
    return not history.exclude(unchanged).exists()


@sensitive_variables()
@transaction.atomic
def fill_existing_contact(mail: ParsedMail) -> ContactReplayResult:
    if mail.event_type not in ACTIVATION_EVENTS or not mail.participant_email:
        return ContactReplayResult.INVALID
    if _parsed_review_reason(mail, mail.event_type):
        return ContactReplayResult.INVALID
    _lock_import()
    event = EmailEvent.objects.select_for_update().filter(
        sha256=mail.fingerprint, state=EmailEvent.State.APPLIED,
        kind=mail.event_type, seat__isnull=False,
    ).first()
    if event is None:
        return ContactReplayResult.NO_APPLIED_EVENT
    seat = CourseSeat.objects.select_for_update().filter(
        pk=event.seat_id, status__in=ACTIVE_STATUSES, participant__isnull=False,
    ).first()
    if seat is None:
        return ContactReplayResult.NO_ACTIVE_BOOKING
    if not _matches(seat, mail) or (mail.booking_reference and seat.external_reference != mail.booking_reference):
        return ContactReplayResult.BOOKING_CHANGED

    email = mail.participant_email
    if seat.participant.email:
        if normalize_contact_email(seat.participant.email) == email:
            return ContactReplayResult.SAME_ADDRESS
        return ContactReplayResult.OTHER_ADDRESS
    if seat.email_events.filter(state=EmailEvent.State.NEEDS_REVIEW).exists():
        return ContactReplayResult.OPEN_REVIEW
    if not _history_matches(event, seat):
        return ContactReplayResult.PARTICIPANT_CHANGED
    try:
        if _participant_with_email(mail.participant_name, email) is not None:
            return ContactReplayResult.ADDRESS_IN_USE
    except _IdentityConflict:
        return ContactReplayResult.ADDRESS_IN_USE

    try:
        with transaction.atomic():
            updated = Participant.objects.filter(
                pk=seat.participant_id, email="", name=seat.participant.name,
            ).update(email=email)
    except IntegrityError:
        # The database unique index also protects writers outside the import lock.
        return ContactReplayResult.ADDRESS_IN_USE
    return ContactReplayResult.ADDED if updated else ContactReplayResult.PROFILE_CHANGED
