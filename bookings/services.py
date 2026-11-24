"""Reconcile a local EML message without fetching or storing its original contents."""

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from mailimport.parser import ParsedMail, parse_eml

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
    mail = parse_eml(raw)
    return _reconcile(mail, occurred_at)


@transaction.atomic
def _reconcile(mail: ParsedMail, occurred_at: datetime | None) -> EmailEvent:
    # PostgreSQL locks this row even if there is no booking or event to lock yet.
    ImportLock.objects.select_for_update().get_or_create(pk=1)
    event, new = EmailEvent.objects.get_or_create(
        sha256=mail.fingerprint,
        defaults={"kind": mail.event_type, "occurred_at": occurred_at},
    )
    if not new:
        return event

    referenced_seat = _referenced_seat(mail)
    supported = event.kind in ACTIVATION_EVENTS | CANCELLATION_EVENTS
    complete = all((mail.course_name, mail.participant_name, mail.school, mail.starts_at, mail.ends_at))
    if not supported or not complete or mail.reference_conflict:
        return _record(event, EmailEvent.State.NEEDS_REVIEW, referenced_seat)

    if referenced_seat is not None:
        if not _matches(referenced_seat, mail):
            return _record(event, EmailEvent.State.NEEDS_REVIEW, referenced_seat)
        seat = referenced_seat
    else:
        seat = _resolve_by_details(mail, allow_create=event.kind in ACTIVATION_EVENTS)
    if seat is None:
        return _record(event, EmailEvent.State.NEEDS_REVIEW)
    return _update_booking(event, seat, mail)


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
        and seat.participant.name.casefold() == mail.participant_name.casefold()
        and session.course.name.casefold() == mail.course_name.casefold()
        and session.school == mail.school
        and session.starts_at == mail.starts_at
        and session.ends_at == mail.ends_at
    )


def _resolve_by_details(mail: ParsedMail, *, allow_create: bool) -> CourseSeat | None:
    sessions = list(CourseSession.objects.filter(
        course__name__iexact=mail.course_name,
        school=mail.school,
        starts_at=mail.starts_at,
        ends_at=mail.ends_at,
    )[:2])
    if len(sessions) > 1:
        return None
    if sessions:
        session = sessions[0]
        seats = list(session.seats.select_related("participant").filter(
            participant__name__iexact=mail.participant_name,
        )[:2])
        if seats:
            seat = seats[0]
            if len(seats) > 1 or (mail.booking_reference and seat.external_reference):
                return None
            return seat
        if BookingHistory.objects.filter(seat__session=session).filter(
            previous_participant__name__iexact=mail.participant_name,
        ).exists():
            return None
    elif allow_create:
        courses = list(Course.objects.filter(name__iexact=mail.course_name)[:2])
        if len(courses) > 1:
            return None
        course = courses[0] if courses else Course.objects.create(name=mail.course_name)
        session = CourseSession.objects.create(
            course=course, school=mail.school, starts_at=mail.starts_at, ends_at=mail.ends_at,
        )
    if not allow_create:
        return None

    # A name on another date alone does not identify the same person.
    participant = Participant.objects.create(name=mail.participant_name)
    return CourseSeat(session=session, participant=participant)


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
                return _record(event, EmailEvent.State.NEEDS_REVIEW, seat)
        if seat.status not in ACTIVE_STATUSES and status != seat.status:
            return _record(event, EmailEvent.State.NEEDS_REVIEW, seat)

    previous_status = "" if new else seat.status
    previous_participant = None if new else seat.participant
    changed = []
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

    if new or status != previous_status:
        BookingHistory.objects.create(
            seat=seat,
            previous_participant=previous_participant,
            participant=seat.participant,
            previous_status=previous_status,
            status=status,
            source_event=event,
        )
    return _record(event, EmailEvent.State.APPLIED, seat)


def _record(event: EmailEvent, state: str, seat: CourseSeat | None = None) -> EmailEvent:
    event.state = state
    event.seat = seat
    event.save(update_fields=["state", "seat"])
    return event
