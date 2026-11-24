"""Fill a missing participant address from a previously applied message."""

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.views.decorators.debug import sensitive_variables

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


@sensitive_variables()
def enrich_contact_from_imported_mail(raw: bytes) -> bool:
    """Read the EML in memory and update only a provably unchanged blank contact."""
    try:
        mail = parse_eml(raw, functional_addresses=get_functional_addresses())
    except MailParseError:
        return False
    return _fill_existing_contact(mail)


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
def _fill_existing_contact(mail: ParsedMail) -> bool:
    if mail.event_type not in ACTIVATION_EVENTS or not mail.participant_email:
        return False
    if _parsed_review_reason(mail, mail.event_type):
        return False
    _lock_import()
    event = EmailEvent.objects.select_for_update().filter(
        sha256=mail.fingerprint, state=EmailEvent.State.APPLIED,
        kind=mail.event_type, seat__isnull=False,
    ).first()
    if event is None:
        return False
    seat = CourseSeat.objects.select_for_update().filter(
        pk=event.seat_id, status__in=ACTIVE_STATUSES, participant__isnull=False,
    ).first()
    if seat is None or seat.participant.email or not _matches(seat, mail):
        return False
    if mail.booking_reference and seat.external_reference != mail.booking_reference:
        return False
    if seat.email_events.filter(state=EmailEvent.State.NEEDS_REVIEW).exists() or not _history_matches(event, seat):
        return False

    email = mail.participant_email
    try:
        if _participant_with_email(mail.participant_name, email) is not None:
            return False
    except _IdentityConflict:
        return False

    try:
        with transaction.atomic():
            return Participant.objects.filter(
                pk=seat.participant_id, email="", name=seat.participant.name,
            ).update(email=email) == 1
    except IntegrityError:
        # The database unique index also protects writers outside the import lock.
        return False
