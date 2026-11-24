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
    reason = _parsed_review_reason(mail, event.kind)
    if reason:
        return _flag_review(event, reason, referenced_seat)

    if referenced_seat is not None:
        if not _matches(referenced_seat, mail):
            return _flag_review(event, EmailEvent.ReviewReason.REFERENCE_MISMATCH, referenced_seat)
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
        and seat.participant.name.casefold() == mail.participant_name.casefold()
        and session.course.name.casefold() == mail.course_name.casefold()
        and session.school == mail.school
        and session.starts_at == mail.starts_at
        and session.ends_at == mail.ends_at
    )


def _reconcile_by_details(event: EmailEvent, mail: ParsedMail) -> EmailEvent:
    sessions = list(CourseSession.objects.filter(
        course__name__iexact=mail.course_name,
        school=mail.school,
        starts_at=mail.starts_at,
        ends_at=mail.ends_at,
    )[:2])
    if len(sessions) > 1:
        return _flag_review(event, EmailEvent.ReviewReason.MULTIPLE_SESSIONS)
    session = sessions[0] if sessions else None
    if session is not None:
        seats = list(session.seats.select_related("participant").filter(
            participant__name__iexact=mail.participant_name,
        )[:2])
        if len(seats) > 1:
            return _flag_review(event, EmailEvent.ReviewReason.MULTIPLE_SEATS)
        if seats:
            seat = seats[0]
            if mail.booking_reference and seat.external_reference:
                return _flag_review(event, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
            return _update_booking(event, seat, mail)
        if BookingHistory.objects.filter(seat__session=session).filter(
            previous_participant__name__iexact=mail.participant_name,
        ).exists():
            return _flag_review(event, EmailEvent.ReviewReason.PREVIOUS_PARTICIPANT)
    if event.kind not in ACTIVATION_EVENTS:
        return _flag_review(event, EmailEvent.ReviewReason.NO_MATCHING_BOOKING)
    if session is None:
        courses = list(Course.objects.filter(name__iexact=mail.course_name)[:2])
        if len(courses) > 1:
            return _flag_review(event, EmailEvent.ReviewReason.MULTIPLE_COURSES)
        course = courses[0] if courses else Course.objects.create(name=mail.course_name)
        session = CourseSession.objects.create(
            course=course, school=mail.school, starts_at=mail.starts_at, ends_at=mail.ends_at,
        )
    # A name on another date alone does not identify the same person.
    participant = Participant.objects.create(name=mail.participant_name)
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
                return _flag_review(event, EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR, seat)
        if seat.status not in ACTIVE_STATUSES and status != seat.status:
            return _flag_review(event, EmailEvent.ReviewReason.PROTECTED_STATUS, seat)

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


def _flag_review(
    event: EmailEvent,
    reason: EmailEvent.ReviewReason,
    seat: CourseSeat | None = None,
) -> EmailEvent:
    event.review_reason = reason
    return _record(event, EmailEvent.State.NEEDS_REVIEW, seat)


def _record(event: EmailEvent, state: str, seat: CourseSeat | None = None) -> EmailEvent:
    event.state = state
    event.seat = seat
    event.save(update_fields=["state", "seat", "review_reason"])
    return event
