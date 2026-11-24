import hashlib

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from bookings.models import BookingStatus, EmailEvent
from bookings.services import ingest
from tests.test_participant_identity import PERSONAL, booking_mail
from tests.test_reconciliation import JUNE, eml


class ReviewContextTests(TestCase):
    def test_reassignment_context_only_retains_parsed_booking_fields(self):
        raw = eml(
            "Ummeldung",
            participant="Toni Testperson",
            reference="FALL-37",
            extra=(
                "Bisheriger Teilnehmer: Alex Beispiel\n"
                "Absender: verborgen@example.invalid\n"
                "https://example.invalid/confirm/SYNTHETISCHER-TOKEN\n"
                "Ursprüngliche Nachricht mit synthetischen Zusatzdaten"
            ),
        )

        event = ingest(raw)
        event.refresh_from_db()

        self.assertEqual(event.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(event.review_context, {
            "course_name": "Gruppenführer",
            "school": "wuerzburg",
            "starts_at": "2027-05-10T09:45:00+02:00",
            "ends_at": "2027-05-14T15:00:00+02:00",
            "participant_name": "Toni Testperson",
            "booking_reference": "FALL-37",
            "previous_participant_name": "Alex Beispiel",
        })

    def test_previous_participant_is_omitted_when_event_is_not_reassignment(self):
        event = ingest(eml(
            "Terminänderung",
            participant="?",
            extra="Bisheriger Teilnehmer: Alex Beispiel",
        ))

        self.assertEqual(event.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertTrue(event.review_context)
        self.assertNotIn("previous_participant_name", event.review_context)
        self.assertNotIn("participant_name", event.review_context)
        self.assertNotIn("booking_reference", event.review_context)

    def test_context_reflects_incoming_message_even_if_ref_links_existing_seat(self):
        original = ingest(eml("Lehrgangsgenehmigung", reference="FALL-37"))
        mismatch = ingest(eml("Teilnahmebestätigung", reference="FALL-37", period=JUNE))

        self.assertEqual(original.review_context, {})
        self.assertEqual(mismatch.review_reason, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
        self.assertEqual(mismatch.seat_id, original.seat_id)
        self.assertEqual(mismatch.seat.status, BookingStatus.APPROVED)
        self.assertEqual(mismatch.review_context["starts_at"], "2027-06-07T09:45:00+02:00")

    def test_parsed_email_address_is_not_copied_into_review_context(self):
        ingest(booking_mail(address=PERSONAL))
        mismatch = ingest(booking_mail(address=PERSONAL, name="Pat Musterfall", period=JUNE))
        mismatch.refresh_from_db()

        self.assertEqual(mismatch.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        self.assertEqual(mismatch.review_context["participant_name"], "Pat Musterfall")
        self.assertNotIn(PERSONAL, str(mismatch.review_context))
        self.assertNotIn("participant_email", mismatch.review_context)

    def test_legacy_worker_can_insert_without_review_context_and_replay_leaves_it_empty(self):
        raw = eml("Terminänderung")
        digest = hashlib.sha256(raw).hexdigest()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO bookings_emailevent (sha256, kind, state, imported_at, review_reason, review_resolution) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [digest, EmailEvent.Kind.SCHEDULE_CHANGE, EmailEvent.State.NEEDS_REVIEW, timezone.now(), "", ""],
            )

        prior = EmailEvent.objects.get(sha256=digest)
        duplicate = ingest(raw)

        self.assertEqual(duplicate.pk, prior.pk)
        self.assertEqual(prior.review_context, {})
        self.assertEqual(duplicate.review_context, {})
