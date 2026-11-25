"""Reconcile a local EML message without fetching or storing its original contents."""

from dataclasses import replace
from datetime import datetime
from itertools import islice
from unicodedata import normalize

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from mailimport.email_address import normalize_contact_email
from mailimport.models import MailReceipt
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
    School,
)


ACTIVATION_EVENTS = {EmailEvent.Kind.APPROVAL, EmailEvent.Kind.CONFIRMATION}
CANCELLATION_EVENTS = {EmailEvent.Kind.CANCELLATION, EmailEvent.Kind.AUTOMATIC_CANCELLATION}
ACTIVE_STATUSES = {BookingStatus.APPROVED, BookingStatus.AWAITING_CONFIRMATION, BookingStatus.CONFIRMED}


@sensitive_variables()
def ingest(
    raw: bytes, *, occurred_at: datetime | None = None, receipt: MailReceipt | None = None,
) -> EmailEvent:
    """Import one EML. `occurred_at` is optional, trusted event time from the caller."""
    if occurred_at is not None and timezone.is_naive(occurred_at):
        raise ValueError("occurred_at must include a time zone")
    mail = parse_eml(raw, functional_addresses=get_functional_addresses())
    return _reconcile(mail, occurred_at, receipt=receipt)


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
def _reconcile(
    mail: ParsedMail, occurred_at: datetime | None, *, receipt: MailReceipt | None = None,
) -> EmailEvent:
    _lock_import()
    mail = _use_known_participant_address(mail)
    event, created = EmailEvent.objects.get_or_create(
        sha256=mail.fingerprint,
        defaults={"kind": mail.event_type, "occurred_at": occurred_at},
    )
    reclassified = (
        not created
        and mail.event_type == EmailEvent.Kind.CONFIRMATION
        and event.kind == EmailEvent.Kind.UNKNOWN
        and event.state == EmailEvent.State.NEEDS_REVIEW
        and event.review_reason == EmailEvent.ReviewReason.UNRECOGNIZED_TYPE
    )
    if reclassified:
        event.kind = mail.event_type
        event.review_reason = EmailEvent.ReviewReason.UNKNOWN
        event.review_context = {}
        event.save(update_fields=["kind", "review_reason", "review_context"])

    evaluated = created or reclassified
    address_added = False
    address_note = mail.address_note
    if evaluated:
        event = _apply_event(event, mail, receipt=receipt)
    elif event.state == EmailEvent.State.APPLIED and mail.participant_email:
        # Share the contact replay checks without reapplying the booking event.
        from .contact_replay import ContactReplayResult, fill_existing_contact

        contact_result = fill_existing_contact(mail)
        address_added = contact_result == ContactReplayResult.ADDED
        if not address_added:
            address_note = contact_result.value
    elif mail.participant_email:
        address_note = "Zur früheren Mail gibt es keine anwendbare Buchungszuordnung für einen Adressnachtrag."
    if receipt is not None:
        _record_receipt(receipt, event, mail, evaluated=evaluated, address_added=address_added, address_note=address_note)
    return event


def _apply_event(event: EmailEvent, mail: ParsedMail, *, receipt: MailReceipt | None) -> EmailEvent:
    referenced_seat = _referenced_seat(mail)
    allow_partial = referenced_seat is not None and event.kind in ACTIVATION_EVENTS
    reason = _parsed_review_reason(mail, event.kind, allow_partial=allow_partial)
    if reason:
        return _flag_review(event, mail, reason, referenced_seat)

    if referenced_seat is not None:
        if not _matches(referenced_seat, mail, allow_partial=allow_partial):
            email = mail.participant_email
            if email:
                try:
                    _identity_for_seat(referenced_seat, mail.participant_name, email, allow_enrichment=False)
                except _IdentityConflict as conflict:
                    return _flag_review(event, mail, conflict.reason, referenced_seat)
            return _flag_review(event, mail, EmailEvent.ReviewReason.REFERENCE_MISMATCH, referenced_seat)
        return _update_booking(event, referenced_seat, mail, receipt=receipt)
    return _reconcile_by_details(event, mail, receipt=receipt)


def _record_receipt(
    receipt: MailReceipt, event: EmailEvent, mail: ParsedMail, *, evaluated: bool, address_added: bool,
    address_note: str,
) -> None:
    outcome = MailReceipt.Outcome
    if address_added:
        receipt.outcome = outcome.ADDRESS_ADDED
        receipt.detail = "Die fehlende Adresse wurde ergänzt. Der Buchungsstatus blieb gleich."
    elif not evaluated:
        receipt.outcome = outcome.DUPLICATE
        receipt.detail = "Die Nachricht ist bytegleich mit einem früheren Eingang. Buchung und Adresse wurden bei diesem Abruf nicht geändert."
    elif event.state == EmailEvent.State.NEEDS_REVIEW:
        receipt.outcome = outcome.REVIEW
        receipt.detail = event.get_review_reason_display()
        if mail.participant_email:
            address_note = "BMS hat die Teilnehmeradresse erkannt. Die Übernahme erfordert eine Klärung."
    elif receipt.outcome != outcome.ADDRESS_ADDED:
        receipt.outcome = outcome.APPLIED
        receipt.detail = "Die Nachricht wurde der Buchung zugeordnet."
        if mail.participant_email and event.seat and event.seat.participant:
            if normalize_contact_email(event.seat.participant.email) == mail.participant_email:
                address_note = "Die erkannte Adresse ist im verknüpften Teilnehmerprofil hinterlegt."

    receipt.event = event
    receipt.sent_at = mail.sent_at
    fields = {
        **_recognized_booking_fields(mail, include_previous=True),
        "mail_type": EmailEvent.Kind(mail.event_type).label,
        "participant_email": mail.participant_email,
        "participant_email_candidate": mail.participant_email_candidate,
        "address_note": address_note,
        "confirmation_deadline": mail.confirmation_deadline.isoformat() if mail.confirmation_deadline else None,
        "cancellation_date": mail.cancellation_date.isoformat() if mail.cancellation_date else None,
        "warnings": mail.warnings,
    }
    receipt.fields = {name: value for name, value in fields.items() if value}
    receipt.save()


def _parsed_review_reason(
    mail: ParsedMail, event_kind: str, *, allow_partial: bool = False,
) -> EmailEvent.ReviewReason:
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
    session_fields = (
        ("course_name", mail.course_name, reason.COURSE_UNCLEAR),
        ("school", mail.school, reason.SCHOOL_UNCLEAR),
        ("period", mail.starts_at and mail.ends_at, reason.PERIOD_UNCLEAR),
    )
    for field, value, review_reason in session_fields:
        if field in mail.unusable_fields or (not value and not allow_partial):
            return review_reason
    if mail.participant_email_ambiguous:
        return reason.PARTICIPANT_IDENTITY_UNCLEAR
    return reason.UNKNOWN


def _referenced_seat(mail: ParsedMail) -> CourseSeat | None:
    if not mail.booking_reference:
        return None
    return CourseSeat.objects.select_related("session__course", "participant").filter(
        external_reference=mail.booking_reference,
    ).first()


def _matches(seat: CourseSeat, mail: ParsedMail, *, allow_partial: bool = False) -> bool:
    session = seat.session
    if seat.participant is None or not mail.participant_name:
        return False
    if _normalized_name(seat.participant.name) != _normalized_name(mail.participant_name):
        return False
    # An online-only location records the format, with the organizing school still open.
    same_online_format = session.is_online and mail.is_online
    school = mail.school
    if (allow_partial or same_online_format) and School.ONLINE in (session.school, school):
        school = session.school
    fields = (
        (session.course.name.casefold(), mail.course_name.casefold() if mail.course_name else None),
        (session.school, school),
        (session.starts_at, mail.starts_at),
        (session.ends_at, mail.ends_at),
    )
    return all(stored == received or (allow_partial and received is None) for stored, received in fields)


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


def _use_known_participant_address(mail: ParsedMail) -> ParsedMail:
    candidate = mail.participant_email_candidate
    if not candidate or mail.participant_email or not mail.participant_name:
        return mail
    try:
        participant = _participant_with_email(mail.participant_name, candidate)
    except _IdentityConflict:
        return mail
    if participant is None:
        return mail
    return replace(
        mail, participant_email=candidate, participant_email_candidate=None,
        address_note="Die Adresse aus dem ursprünglichen An-Feld passt zu einem bestehenden Teilnehmerprofil.",
    )


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


def _reconcile_by_details(event: EmailEvent, mail: ParsedMail, *, receipt: MailReceipt | None) -> EmailEvent:
    schedule = CourseSession.objects.filter(
        course__name__iexact=mail.course_name,
        starts_at=mail.starts_at,
        ends_at=mail.ends_at,
    )
    eligible = schedule.filter(school=mail.school)
    if mail.is_online and event.kind in ACTIVATION_EVENTS:
        matched = _reconcile_online_follow_up(event, mail, schedule, receipt=receipt)
        if matched is not None:
            return matched
        if mail.school != School.ONLINE:
            eligible = eligible.filter(is_online=True)

    sessions = list(eligible[:2])
    name = _normalized_name(mail.participant_name)
    if len(sessions) > 1:
        formats = set(eligible.order_by().values_list("is_online", flat=True).distinct())
        if formats == {False, True}:
            seats = _seats_with_name(eligible, name)
            if len(seats) == 1:
                return _apply_matching_seat(event, seats[0], mail, receipt=receipt)
        return _flag_review(event, mail, EmailEvent.ReviewReason.MULTIPLE_SESSIONS)
    session = sessions[0] if sessions else None
    if session is not None:
        seats = _seats_with_name(eligible, name)
        if len(seats) > 1:
            return _flag_review(event, mail, EmailEvent.ReviewReason.MULTIPLE_SEATS)
        if seats:
            return _apply_matching_seat(event, seats[0], mail, receipt=receipt)
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
            course=course, school=mail.school, is_online=mail.is_online,
            starts_at=mail.starts_at, ends_at=mail.ends_at,
        )
    # A name on another date alone does not identify the same person.
    if participant is None:
        participant = Participant.objects.create(name=mail.participant_name, email=email)
    return _update_booking(event, CourseSeat(session=session, participant=participant), mail, receipt=receipt)


def _seats_with_name(sessions, name: str) -> list[CourseSeat]:
    candidates = CourseSeat.objects.filter(session__in=sessions, participant__isnull=False).select_related(
        "participant", "session__course",
    ).iterator()
    return list(islice((seat for seat in candidates if _normalized_name(seat.participant.name) == name), 2))


def _apply_matching_seat(
    event: EmailEvent, seat: CourseSeat, mail: ParsedMail, *, receipt: MailReceipt | None,
) -> EmailEvent:
    if mail.booking_reference and seat.external_reference:
        return _flag_review(event, mail, EmailEvent.ReviewReason.REFERENCE_MISMATCH, candidate_seat_id=seat.pk)
    return _update_booking(event, seat, mail, receipt=receipt)


def _reconcile_online_follow_up(
    event: EmailEvent, mail: ParsedMail, schedule, *, receipt: MailReceipt | None,
) -> EmailEvent | None:
    name = _normalized_name(mail.participant_name)
    if mail.school == School.ONLINE:
        seats = _seats_with_name(schedule, name)
    else:
        # A course can run online and in person at the same time under the same organizer.
        seats = _seats_with_name(schedule.filter(school=mail.school, is_online=True), name)
        if not seats:
            possible = schedule.filter(Q(school=mail.school) | Q(school=School.ONLINE))
            seats = _seats_with_name(possible, name)

    if len(seats) > 1:
        return _flag_review(event, mail, EmailEvent.ReviewReason.MULTIPLE_SEATS)
    if not seats:
        return None

    seat = seats[0]
    if mail.school == School.ONLINE and seat.session.school != School.ONLINE and not seat.session.is_online:
        if not mail.participant_email or normalize_contact_email(seat.participant.email) != mail.participant_email:
            return _flag_review(
                event, mail, EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR, candidate_seat_id=seat.pk,
            )
    return _apply_matching_seat(event, seat, mail, receipt=receipt)


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


def _update_booking(
    event: EmailEvent, seat: CourseSeat, mail: ParsedMail, *, receipt: MailReceipt | None,
) -> EmailEvent:
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
    missing_address = previous_participant is not None and not previous_participant.email
    changed = []
    if allow_enrichment:
        _update_online_session(seat.session, mail)
        _fill_email(participant, email)
        if receipt is not None and missing_address and email:
            receipt.outcome = MailReceipt.Outcome.ADDRESS_ADDED
            receipt.detail = "BMS hat die fehlende Teilnehmeradresse bei der bestehenden Buchung ergänzt."
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


def _update_online_session(session: CourseSession, mail: ParsedMail) -> None:
    changed = []
    if not session.is_online and (mail.is_online or session.school == School.ONLINE):
        session.is_online = True
        changed.append("is_online")
    if session.school == School.ONLINE and mail.school and mail.school != School.ONLINE:
        session.school = mail.school
        changed.append("school")
    if changed:
        session.save(update_fields=changed)


def _recognized_booking_fields(mail: ParsedMail, *, include_previous: bool = False) -> dict:
    context = {
        "course_name": mail.course_name,
        "school": mail.school,
        "starts_at": mail.starts_at.isoformat() if mail.starts_at else None,
        "ends_at": mail.ends_at.isoformat() if mail.ends_at else None,
        "participant_name": mail.participant_name,
        "booking_reference": mail.booking_reference,
    }
    if include_previous:
        context["previous_participant_name"] = mail.previous_participant_name
    if mail.is_online:
        context["is_online"] = True
    return {key: value for key, value in context.items() if value is not None}


def _flag_review(
    event: EmailEvent,
    mail: ParsedMail,
    reason: EmailEvent.ReviewReason,
    seat: CourseSeat | None = None,
    *,
    candidate_seat_id: int | None = None,
) -> EmailEvent:
    event.review_reason = reason
    event.review_context = _recognized_booking_fields(
        mail, include_previous=event.kind == EmailEvent.Kind.REASSIGNMENT,
    )
    if candidate_seat_id is not None:
        event.review_context["candidate_seat_id"] = candidate_seat_id
    return _record(event, EmailEvent.State.NEEDS_REVIEW, seat)


def _record(event: EmailEvent, state: str, seat: CourseSeat | None = None) -> EmailEvent:
    event.state = state
    event.seat = seat
    update_fields = ["state", "seat", "review_reason"]
    if state == EmailEvent.State.NEEDS_REVIEW:
        update_fields.append("review_context")
    event.save(update_fields=update_fields)
    return event
