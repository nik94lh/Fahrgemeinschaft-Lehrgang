import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from django.db import connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from bookings.models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant
from bookings.services import ingest
from mailimport.parser import MailParseError
from tests.test_mail_parser import message


SCHOOL = "SFS Würzburg"
MAY = "10.05.2027 09:45 Uhr – 14.05.2027 15:00 Uhr"
JUNE = "07.06.2027 09:45 Uhr – 11.06.2027 15:00 Uhr"


def eml(
    subject,
    *,
    participant="Alex Beispiel",
    course="Gruppenführer",
    period=MAY,
    school=SCHOOL,
    reference=None,
    extra="",
):
    fields = [
        f"Teilnehmer: {participant}",
        f"Lehrgangsart: {course}",
        f"Lehrgangstermin: {period}",
        f"Schulungsort: {school}",
    ]
    if reference:
        fields.append(f"Buchungsreferenz: {reference}")
    return message(subject, "\n".join(fields) + "\n" + extra).as_bytes()


class ReconciliationTests(TestCase):
    def test_same_bytes_are_imported_once(self):
        raw = eml("Lehrgangsgenehmigung")
        result = ingest(raw)

        self.assertEqual(result, ingest(raw))
        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.UNKNOWN)

    def test_approval_and_confirmation_use_same_seat(self):
        approval = ingest(eml(
            "Lehrgangsgenehmigung",
            extra="Die Anmeldung muss spätestens am Sonntag, 18. April 2027 bestätigt werden.",
        ))
        confirmation = ingest(eml("Teilnahmebestätigung"))

        self.assertEqual(approval.seat, confirmation.seat)
        self.assertEqual(confirmation.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(confirmation.seat.confirmation_deadline.isoformat(), "2027-04-18")
        self.assertEqual(confirmation.seat.history.count(), 2)

    def test_different_people_and_dates_are_independent(self):
        first = ingest(eml("Lehrgangsgenehmigung"))
        second = ingest(eml("Lehrgangsgenehmigung", participant="Toni Testperson"))
        later = ingest(eml("Lehrgangsgenehmigung", period=JUNE))

        self.assertEqual(first.seat.session, second.seat.session)
        self.assertNotEqual(first.seat.session, later.seat.session)
        self.assertEqual(CourseSeat.objects.count(), 3)

    def test_known_reference_resolves_ambiguous_full_signature(self):
        existing = ingest(eml("Lehrgangsgenehmigung", reference="TEST-17"))
        duplicate_name = Participant.objects.create(name="Alex Beispiel")
        CourseSeat.objects.create(session=existing.seat.session, participant=duplicate_name)
        result = ingest(eml("Teilnahmebestätigung", reference="TEST-17"))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat, existing.seat)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)

    def test_ambiguous_fallback_does_not_change_either_seat(self):
        existing = ingest(eml("Lehrgangsgenehmigung"))
        duplicate_name = Participant.objects.create(name="Alex Beispiel")
        other = CourseSeat.objects.create(session=existing.seat.session, participant=duplicate_name)
        result = ingest(eml("Teilnahmebestätigung"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.MULTIPLE_SEATS)
        self.assertIsNone(result.seat)
        self.assertEqual(CourseSeat.objects.get(pk=existing.seat_id).status, BookingStatus.APPROVED)
        self.assertEqual(CourseSeat.objects.get(pk=other.pk).status, BookingStatus.NEEDS_REVIEW)

    def test_booking_reference_cannot_override_conflicting_details(self):
        existing = ingest(eml("Lehrgangsgenehmigung", reference="TEST-17"))
        result = ingest(eml("Teilnahmebestätigung", reference="TEST-17", period=JUNE))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(CourseSession.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.get(pk=existing.seat_id).status, BookingStatus.APPROVED)

    def test_new_reference_enriches_an_unreferenced_booking(self):
        approval = ingest(eml("Lehrgangsgenehmigung"))
        confirmation = ingest(eml("Teilnahmebestätigung", reference="TEST-17"))

        self.assertEqual(confirmation.seat, approval.seat)
        self.assertEqual(confirmation.seat.external_reference, "TEST-17")

    def test_conflicting_new_reference_cannot_create_duplicate(self):
        ingest(eml("Lehrgangsgenehmigung", reference="TEST-17"))
        result = ingest(eml("Teilnahmebestätigung", reference="TEST-18"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_multiple_explicit_references_cannot_fall_back_to_other_fields(self):
        existing = ingest(eml("Lehrgangsgenehmigung", reference="TEST-17"))
        result = ingest(eml(
            "Teilnahmebestätigung", reference="TEST-17", extra="Buchungsreferenz: TEST-18",
        ))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.INVALID_REFERENCE)
        self.assertIsNone(result.seat_id)
        self.assertEqual(CourseSeat.objects.get(pk=existing.seat_id).status, BookingStatus.APPROVED)

    def test_confirmation_arriving_first_is_not_downgraded(self):
        confirmation = ingest(eml("Teilnahmebestätigung"))
        approval = ingest(eml("Lehrgangsgenehmigung"))

        self.assertEqual(approval.seat, confirmation.seat)
        self.assertEqual(approval.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(approval.seat.history.count(), 1)

    def test_old_cancellation_does_not_override_newer_confirmation(self):
        now = datetime(2027, 4, 25, tzinfo=UTC)
        ingest(eml("Teilnahmebestätigung"), occurred_at=now)
        result = ingest(eml("Stornierung"), occurred_at=now - timedelta(days=1))

        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(result.seat.history.count(), 1)

    def test_cancellation_is_recorded_and_late_confirmation_does_not_reopen(self):
        ingest(eml("Lehrgangsgenehmigung"))
        cancellation = ingest(eml("Automatische Stornierung"))
        confirmation = ingest(eml("Teilnahmebestätigung"))

        self.assertEqual(cancellation.seat.status, BookingStatus.CANCELLED)
        self.assertEqual(confirmation.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(confirmation.review_reason, EmailEvent.ReviewReason.PROTECTED_STATUS)
        self.assertEqual(CourseSeat.objects.get(pk=cancellation.seat_id).history.count(), 2)

    def test_followup_events_without_known_booking_go_to_review(self):
        cases = {
            "Stornierung": EmailEvent.ReviewReason.NO_MATCHING_BOOKING,
            "Ummeldung": EmailEvent.ReviewReason.REASSIGNMENT,
            "Terminänderung": EmailEvent.ReviewReason.SCHEDULE_CHANGE,
            "Teilnehmerinformation": EmailEvent.ReviewReason.UNRECOGNIZED_TYPE,
            "Einladung": EmailEvent.ReviewReason.UNSUPPORTED_TYPE,
        }
        for subject, reason in cases.items():
            with self.subTest(subject=subject):
                result = ingest(eml(subject))
                self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
                self.assertEqual(result.review_reason, reason)
                self.assertIsNone(result.seat)

        self.assertEqual(CourseSeat.objects.count(), 0)
        self.assertEqual(CourseSession.objects.count(), 0)

    def test_reassignment_with_known_reference_still_requires_review(self):
        existing = ingest(eml("Lehrgangsgenehmigung", reference="TEST-17"))
        result = ingest(eml("Ummeldung", reference="TEST-17", participant="Toni Testperson"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.REASSIGNMENT)
        self.assertEqual(result.seat, existing.seat)
        self.assertEqual(result.seat.participant.name, "Alex Beispiel")

    def test_invalid_or_incomplete_mail_cannot_generate_booking(self):
        with self.assertRaises(MailParseError):
            ingest(b"")
        result = ingest(eml("Teilnahmebestätigung", participant="?"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.PARTICIPANT_UNCLEAR)
        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.count(), 0)

    def test_undated_cancellation_against_dated_confirmation_records_unclear_order(self):
        instant = datetime(2027, 4, 25, tzinfo=UTC)
        ingest(eml("Teilnahmebestätigung"), occurred_at=instant)
        result = ingest(eml("Stornierung"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)

    def test_undated_cancellation_does_not_override_undated_confirmation(self):
        confirmation = ingest(eml("Teilnahmebestätigung", reference="TEST-41"))

        cancellation = ingest(eml("Stornierung", reference="TEST-41"))

        self.assertEqual(cancellation.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(cancellation.review_reason, EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR)
        self.assertEqual(cancellation.seat_id, confirmation.seat_id)
        confirmation.seat.refresh_from_db()
        self.assertEqual(confirmation.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(confirmation.seat.history.count(), 1)

    def test_newer_dated_cancellation_still_applies_to_a_confirmation(self):
        instant = datetime(2027, 4, 25, tzinfo=UTC)
        confirmation = ingest(eml("Teilnahmebestätigung"), occurred_at=instant)

        cancellation = ingest(eml("Stornierung"), occurred_at=instant + timedelta(days=1))

        self.assertEqual(cancellation.state, EmailEvent.State.APPLIED)
        self.assertEqual(cancellation.seat_id, confirmation.seat_id)
        self.assertEqual(cancellation.seat.status, BookingStatus.CANCELLED)

    def test_incomplete_fields_have_distinct_safe_reasons(self):
        reasons = {
            "course": EmailEvent.ReviewReason.COURSE_UNCLEAR,
            "school": EmailEvent.ReviewReason.SCHOOL_UNCLEAR,
            "period": EmailEvent.ReviewReason.PERIOD_UNCLEAR,
        }
        for field, reason in reasons.items():
            with self.subTest(field=field):
                raw = eml("Teilnahmebestätigung", **{field: ""})
                result = ingest(raw)
                result.refresh_from_db()
                self.assertEqual(result.review_reason, reason)
        self.assertFalse(CourseSeat.objects.exists())

    def test_duplicate_sessions_require_review_without_inserting_a_seat(self):
        current = ingest(eml("Lehrgangsgenehmigung")).seat.session
        CourseSession.objects.create(
            course=current.course,
            school=current.school,
            starts_at=current.starts_at,
            ends_at=current.ends_at,
        )

        result = ingest(eml("Teilnahmebestätigung"))

        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.MULTIPLE_SESSIONS)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_duplicate_course_labels_require_review_without_creating_session(self):
        Course.objects.create(name="Gruppenführer")
        Course.objects.create(name="Gruppenführer")

        result = ingest(eml("Teilnahmebestätigung"))

        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.MULTIPLE_COURSES)
        self.assertFalse(CourseSession.objects.exists())

    def test_previous_participant_is_not_automatically_rebooked(self):
        event = ingest(eml("Lehrgangsgenehmigung"))
        seat = event.seat
        previous = seat.participant
        seat.participant = Participant.objects.create(name="Toni Testperson")
        seat.save(update_fields=["participant"])
        BookingHistory.objects.create(
            seat=seat,
            previous_participant=previous,
            participant=seat.participant,
            previous_status=BookingStatus.APPROVED,
            status=BookingStatus.REASSIGNED,
        )

        result = ingest(eml("Teilnahmebestätigung"))

        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.PREVIOUS_PARTICIPANT)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_legacy_reason_is_never_reconstructed_from_a_duplicate(self):
        raw = eml("Teilnahmebestätigung", participant="?")
        legacy = EmailEvent.objects.create(
            sha256=hashlib.sha256(raw).hexdigest(),
            kind=EmailEvent.Kind.CONFIRMATION,
            state=EmailEvent.State.NEEDS_REVIEW,
        )

        result = ingest(raw)

        self.assertEqual(result.pk, legacy.pk)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.UNKNOWN)
        self.assertEqual(result.get_review_reason_display(), "Grund nicht erfasst")
        legacy.refresh_from_db()
        self.assertEqual(legacy.review_reason, EmailEvent.ReviewReason.UNKNOWN)
        self.assertFalse(CourseSeat.objects.exists())


class ConcurrentImportTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_first_imports_serialize_matching_and_exact_deduplication(self):
        ready = Barrier(3)
        approval = eml("Lehrgangsgenehmigung")
        confirmation = eml("Teilnahmebestätigung")

        def import_in_thread(raw):
            try:
                ready.wait(timeout=5)
                return ingest(raw).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(import_in_thread, [approval, confirmation, confirmation]))

        self.assertEqual(results[1], results[2])
        self.assertEqual(EmailEvent.objects.count(), 2)
        self.assertEqual(CourseSeat.objects.get().status, BookingStatus.CONFIRMED)
