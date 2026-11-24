"""Reconcile a local EML message without fetching or storing its original contents."""

from datetime import datetime
from itertools import islice
from unicodedata import normalize

from django.db import transaction
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from mailimport.email_address import normalize_contact_email
from mailimport.parser import ParsedMail, parse_eml
from mailimport.participant_settings import get_functional_addresses

from .models import (
    BookingHistory,
    BookingStatus,
    Course,
    CourseSeat,
    CourseSession,
    EmailEvent,
    ImportLock,
    Participant,
)


ACTIVATION_EVENTS = {EmailEvent.Kind.APPROVAL, EmailEvent.Kind.CONFIRMATION}
CANCELLATION_EVENTS = {EmailEvent.Kind.CANCELLATION, EmailEvent.Kind.AUTOMATIC_CANCELLATION}
ACTIVE_STATUSES = {BookingStatus.APPROVED, BookingStatus.AWAITING_CONFIRMATION, BookingStatus.CONFIRMED}


def ingest(raw: bytes, *, occurred_at: datetime | None = None) -> EmailEvent:
    """Import one EML. `occurred_at` is optional, trusted event time from the caller."""
    if occurred_at is not None and timezone.is_naive(occurred_at):
        raise ValueError("occurred_at must include a time zone")
    mail = parse_eml(raw, functional_addresses=get_functional_addresses())
    return _reconcile(mail, occurred_at)


def _lock_import():
    # PostgreSQL locks this row even if there is no booking or event to lock yet.
    ImportLock.objects.select_for_update().get_or_create(pk=1)


@transaction.atomic
def set_participant_email(seat_id: int, email: str, *, expected_participant_id: int) -> Participant:
    """Record a verified address for the selected booking without overwriting a profile."""
    email = Participant.normalize_email(email)
    if not email:
        raise ValueError("Gib eine gültige E-Mail-Adresse ein.")
    if email in {normalize_contact_email(address) for address in get_functional_addresses()}:
        raise ValueError("Diese Adresse ist als Funktionspostfach hinterlegt.")
    _lock_import()
    seat = CourseSeat.objects.select_for_update().filter(pk=seat_id).first()
    if seat is None or seat.participant_id is None or seat.participant_id != expected_participant_id:
        raise ValueError("Die Teilnehmerzuordnung hat sich geändert. Lade die Buchung neu.")

    previous = seat.participant
    participant = _identity_for_seat(seat, previous.name, email, allow_enrichment=True)
    _fill_email(participant, email)
    if participant.pk != previous.pk:
        seat.participant = participant
        seat.save(update_fields=["participant"])
        BookingHistory.objects.create(
            seat=seat, previous_participant=previous, participant=participant,
            previous_status=seat.status, status=seat.status,
        )
    return participant


@transaction.atomic
def _reconcile(mail: ParsedMail, occurred_at: datetime | None) -> EmailEvent:
    _lock_import()
    event, new = EmailEvent.objects.get_or_create(
        sha256=mail.fingerprint,
        defaults={"kind": mail.event_type, "occurred_at": occurred_at},
    )
    if not new:
        return event

    referenced_seat = _referenced_seat(mail)
    reason = _parsed_review_reason(mail, event.kind)
    if reason:
        return _flag_review(event, mail, reason, referenced_seat)

    if referenced_seat is not None:
        if not _matches(referenced_seat, mail):
            email = mail.participant_email
            if email:
                try:
                    _identity_for_seat(referenced_seat, mail.participant_name, email, allow_enrichment=False)
                except _IdentityConflict as conflict:
                    return _flag_review(event, mail, conflict.reason, referenced_seat)
            return _flag_review(event, mail, EmailEvent.ReviewReason.REFERENCE_MISMATCH, referenced_seat)
        return _update_booking(event, referenced_seat, mail)
    return _reconcile_by_details(event, mail)


def _parsed_review_reason(mail: ParsedMail, event_kind: str) -> EmailEvent.ReviewReason:
    reason = EmailEvent.ReviewReason
    if mail.reference_conflict:
        return reason.INVALID_REFERENCE
    if event_kind not in ACTIVATION_EVENTS | CANCELLATION_EVENTS:
        return {
            EmailEvent.Kind.UNKNOWN: reason.UNRECOGNIZED_TYPE,
            EmailEvent.Kind.REASSIGNMENT: reason.REASSIGNMENT,
            EmailEvent.Kind.SCHEDULE_CHANGE: reason.SCHEDULE_CHANGE,
        }.get(event_kind, reason.UNSUPPORTED_TYPE)
    if not mail.participant_name:
        return reason.PARTICIPANT_UNCLEAR
    if not mail.course_name:
        return reason.COURSE_UNCLEAR
    if not mail.school:
        return reason.SCHOOL_UNCLEAR
    if not (mail.starts_at and mail.ends_at):
        return reason.PERIOD_UNCLEAR
    if mail.participant_email_ambiguous:
        return reason.PARTICIPANT_IDENTITY_UNCLEAR
    return reason.UNKNOWN


def _referenced_seat(mail: ParsedMail) -> CourseSeat | None:
    if not mail.booking_reference:
        return None
    return CourseSeat.objects.select_related("session__course", "participant").filter(
        external_reference=mail.booking_reference,
    ).first()


def _matches(seat: CourseSeat, mail: ParsedMail) -> bool:
    session = seat.session
    return (
        seat.participant is not None
        and _normalized_name(seat.participant.name) == _normalized_name(mail.participant_name)
        and session.course.name.casefold() == mail.course_name.casefold()
        and session.school == mail.school
        and session.starts_at == mail.starts_at
        and session.ends_at == mail.ends_at
    )


class _IdentityConflict(ValueError):
    def __init__(self, reason: EmailEvent.ReviewReason):
        super().__init__(reason.label)
        self.reason = reason


def _normalized_name(name: str) -> str:
    return normalize("NFC", " ".join(name.split())).casefold()


def _participant_with_email(name: str, email: str) -> Participant | None:
    participant = Participant.objects.alias(email_key=Lower(Trim("email"))).filter(
        email_key=email,
    ).exclude(email="").first()
    if participant and _normalized_name(participant.name) != _normalized_name(name):
        raise _IdentityConflict(EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
    return participant


def _identity_for_seat(
    seat: CourseSeat, name: str, email: str, *, allow_enrichment: bool,
) -> Participant | None:
    current = seat.participant
    if current and current.email and normalize_contact_email(current.email) != email:
        raise _IdentityConflict(EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
    canonical = _participant_with_email(name, email)
    if not allow_enrichment or current is None or canonical is None or canonical == current:
        return current
    if (
        current.home_brigade_id
        or current.consents.exists()
        or current.carpool_memberships.exists()
        or current.seats.exclude(pk=seat.pk).exists()
        or canonical.seats.filter(session_id=seat.session_id).exclude(pk=seat.pk).exists()
    ):
        raise _IdentityConflict(EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR)
    return canonical


def _fill_email(participant: Participant, email: str):
    if email and not participant.email:
        participant.email = email
        participant.save(update_fields=["email"])


def _reconcile_by_details(event: EmailEvent, mail: ParsedMail) -> EmailEvent:
    sessions = list(CourseSession.objects.filter(
        course__name__iexact=mail.course_name,
        school=mail.school,
        starts_at=mail.starts_at,
        ends_at=mail.ends_at,
    )[:2])
    if len(sessions) > 1:
        return _flag_review(event, mail, EmailEvent.ReviewReason.MULTIPLE_SESSIONS)
    session = sessions[0] if sessions else None
    if session is not None:
        name = _normalized_name(mail.participant_name)
        candidates = session.seats.select_related("participant").filter(participant__isnull=False).iterator()
        seats = list(islice((seat for seat in candidates if _normalized_name(seat.participant.name) == name), 2))
        if len(seats) > 1:
            return _flag_review(event, mail, EmailEvent.ReviewReason.MULTIPLE_SEATS)
        if seats:
            seat = seats[0]
            if mail.booking_reference and seat.external_reference:
                return _flag_review(event, mail, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
            return _update_booking(event, seat, mail)
        previous_names = BookingHistory.objects.filter(
            seat__session=session, previous_participant__isnull=False,
        ).order_by().values_list("previous_participant__name", flat=True).distinct()
        if any(_normalized_name(previous) == name for previous in previous_names.iterator()):
            return _flag_review(event, mail, EmailEvent.ReviewReason.PREVIOUS_PARTICIPANT)
    if event.kind not in ACTIVATION_EVENTS:
        return _flag_review(event, mail, EmailEvent.ReviewReason.NO_MATCHING_BOOKING)

    email = mail.participant_email or ""
    try:
        participant = _participant_with_email(mail.participant_name, email) if email else None
    except _IdentityConflict as conflict:
        return _flag_review(event, mail, conflict.reason)
    if session is None:
        courses = list(Course.objects.filter(name__iexact=mail.course_name)[:2])
        if len(courses) > 1:
            return _flag_review(event, mail, EmailEvent.ReviewReason.MULTIPLE_COURSES)
        course = courses[0] if courses else Course.objects.create(name=mail.course_name)
        session = CourseSession.objects.create(
            course=course, school=mail.school, starts_at=mail.starts_at, ends_at=mail.ends_at,
        )
    # A name on another date alone does not identify the same person.
    if participant is None:
        participant = Participant.objects.create(name=mail.participant_name, email=email)
    return _update_booking(event, CourseSeat(session=session, participant=participant), mail)


def _new_status(event: EmailEvent, seat: CourseSeat, mail: ParsedMail) -> str:
    if event.kind == EmailEvent.Kind.CONFIRMATION:
        return BookingStatus.CONFIRMED
    if event.kind in CANCELLATION_EVENTS:
        return BookingStatus.CANCELLED
    if seat.status == BookingStatus.CONFIRMED:
        return BookingStatus.CONFIRMED
    if mail.confirmation_deadline or seat.status == BookingStatus.AWAITING_CONFIRMATION:
        return BookingStatus.AWAITING_CONFIRMATION
    return BookingStatus.APPROVED


def _update_booking(event: EmailEvent, seat: CourseSeat, mail: ParsedMail) -> EmailEvent:
    new = seat.pk is None
    status = _new_status(event, seat, mail)
    participant = seat.participant
    email = mail.participant_email or ""
    allow_enrichment = event.kind in ACTIVATION_EVENTS
    if email and not new:
        try:
            participant = _identity_for_seat(seat, mail.participant_name, email, allow_enrichment=allow_enrichment)
        except _IdentityConflict as conflict:
            return _flag_review(event, mail, conflict.reason, seat)
    if not new:
        latest_timestamp = seat.history.filter(source_event__occurred_at__isnull=False).order_by(
            "-source_event__occurred_at",
        ).values_list("source_event__occurred_at", flat=True).first()
        if latest_timestamp is not None:
            if event.occurred_at is not None and event.occurred_at < latest_timestamp:
                return _record(event, EmailEvent.State.APPLIED, seat)
            if status != seat.status and (
                event.occurred_at == latest_timestamp
                or (event.occurred_at is None and event.kind in CANCELLATION_EVENTS)
            ):
                return _flag_review(event, mail, EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR, seat)
        if seat.status not in ACTIVE_STATUSES and status != seat.status:
            return _flag_review(event, mail, EmailEvent.ReviewReason.PROTECTED_STATUS, seat)

    previous_status = "" if new else seat.status
    previous_participant = None if new else seat.participant
    changed = []
    if allow_enrichment:
        _fill_email(participant, email)
        if participant != seat.participant:
            seat.participant = participant
            changed.append("participant")
    if status != seat.status:
        seat.status = status
        changed.append("status")
    if mail.booking_reference and not seat.external_reference:
        seat.external_reference = mail.booking_reference
        changed.append("external_reference")
    if mail.confirmation_deadline and not seat.confirmation_deadline:
        seat.confirmation_deadline = mail.confirmation_deadline
        changed.append("confirmation_deadline")
    if new:
        seat.save()
    elif changed:
        seat.save(update_fields=changed)

    if new or status != previous_status or "participant" in changed:
        BookingHistory.objects.create(
            seat=seat,
            previous_participant=previous_participant,
            participant=seat.participant,
            previous_status=previous_status,
            status=status,
            source_event=event,
        )
    return _record(event, EmailEvent.State.APPLIED, seat)


def _flag_review(
    event: EmailEvent,
    mail: ParsedMail,
    reason: EmailEvent.ReviewReason,
    seat: CourseSeat | None = None,
) -> EmailEvent:
    context = {
        "course_name": mail.course_name,
        "school": mail.school,
        "starts_at": mail.starts_at.isoformat() if mail.starts_at else None,
        "ends_at": mail.ends_at.isoformat() if mail.ends_at else None,
        "participant_name": mail.participant_name,
        "booking_reference": mail.booking_reference,
    }
    if event.kind == EmailEvent.Kind.REASSIGNMENT:
        context["previous_participant_name"] = mail.previous_participant_name
    event.review_reason = reason
    event.review_context = {key: value for key, value in context.items() if value is not None}
    return _record(event, EmailEvent.State.NEEDS_REVIEW, seat)


def _record(event: EmailEvent, state: str, seat: CourseSeat | None = None) -> EmailEvent:
    event.state = state
    event.seat = seat
    update_fields = ["state", "seat", "review_reason"]
    if state == EmailEvent.State.NEEDS_REVIEW:
        update_fields.append("review_context")
    event.save(update_fields=update_fields)
    return event
