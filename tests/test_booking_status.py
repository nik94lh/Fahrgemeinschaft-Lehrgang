from datetime import UTC, datetime, timedelta

from django.test import TestCase

from bookings.booking_status import booking_status_cards, booking_status_prefetches
from bookings.models import BookingHistory, BookingStatus, CourseSeat, EmailEvent, Participant
from bookings.services import ingest, set_participant_email
from mailimport.models import MailReceipt
from tests.test_participant_identity import PERSONAL, booking_mail
from tests.test_reconciliation import JUNE, eml


def cards(seat):
    return {card.label: card.value for card in booking_status_cards(seat)}


class BookingStatusTests(TestCase):
    def test_confirmation_proves_registration_and_later_approval_supplies_its_own_evidence(self):
        confirmed = ingest(eml("Teilnahmebestätigung"))
        self.assertEqual(cards(confirmed.seat), {
            "Angemeldet": "Ja", "Genehmigt": "Offen", "Storniert": "Nein",
        })

        approved = ingest(eml("Lehrgangsgenehmigung"))

        self.assertEqual(approved.processed_participant_id, confirmed.seat.participant_id)
        self.assertFalse(approved.booking_changes.exists())
        self.assertEqual(cards(approved.seat), {
            "Angemeldet": "Ja", "Genehmigt": "Ja", "Storniert": "Nein",
        })

    def test_ignored_older_cancellation_is_not_evidence_for_the_current_booking(self):
        now = datetime(2027, 4, 25, tzinfo=UTC)
        confirmed = ingest(eml("Teilnahmebestätigung"), occurred_at=now)
        receipt = MailReceipt(source_id="a" * 64, uid_validity=1, uid=1)
        cancelled = ingest(eml("Stornierung"), occurred_at=now - timedelta(days=1), receipt=receipt)

        self.assertEqual(cancelled.state, EmailEvent.State.APPLIED)
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.APPLIED)
        self.assertIsNone(cancelled.processed_participant_id)
        self.assertEqual(cards(confirmed.seat)["Storniert"], "Nein")
        self.assertEqual(booking_status_cards(confirmed.seat)[2].tone, "positive")

    def test_legacy_approval_is_preserved_after_cancellation(self):
        approved = ingest(eml("Lehrgangsgenehmigung"))
        cancelled = ingest(eml("Automatische Stornierung"))
        EmailEvent.objects.filter(pk__in=(approved.pk, cancelled.pk)).update(processed_participant=None)

        self.assertEqual(cards(cancelled.seat), {
            "Angemeldet": "Ja", "Genehmigt": "Ja", "Storniert": "Ja",
        })
        self.assertEqual(booking_status_cards(cancelled.seat)[2].tone, "negative")

    def test_effective_cancellation_remains_known_without_an_assigned_person(self):
        ingest(eml("Lehrgangsgenehmigung"))
        seat = ingest(eml("Stornierung")).seat
        seat.participant.delete()
        seat.refresh_from_db()

        self.assertEqual(cards(seat), {
            "Angemeldet": "Offen", "Genehmigt": "Offen", "Storniert": "Ja",
        })

    def test_reassigning_a_seat_back_does_not_reuse_the_previous_assignment_evidence(self):
        approved = ingest(eml("Lehrgangsgenehmigung"))
        seat = approved.seat
        original = seat.participant
        replacement = Participant.objects.create(name="Pat Musterfall")

        for person in (replacement, original):
            BookingHistory.objects.create(
                seat=seat, previous_participant=seat.participant, participant=person,
                previous_status=seat.status, status=BookingStatus.REASSIGNED,
            )
            seat.participant = person
            seat.status = BookingStatus.REASSIGNED
            seat.save(update_fields=["participant", "status"])
            self.assertEqual(cards(seat), {
                "Angemeldet": "Offen", "Genehmigt": "Offen", "Storniert": "Offen",
            })

    def test_linking_verified_profile_preserves_previous_approval_for_the_same_person(self):
        ingest(eml("Lehrgangsgenehmigung"))
        confirmed = ingest(eml("Teilnahmebestätigung"))
        profile = Participant.objects.create(name="Alex Beispiel", email="alex@example.invalid")

        set_participant_email(confirmed.seat_id, profile.email, expected_participant_id=confirmed.seat.participant_id)

        seat = CourseSeat.objects.get(pk=confirmed.seat_id)
        self.assertEqual(seat.participant_id, profile.pk)
        self.assertEqual(cards(seat), {
            "Angemeldet": "Ja", "Genehmigt": "Ja", "Storniert": "Nein",
        })

    def test_confirmation_import_keeps_approval_when_it_links_an_existing_profile(self):
        approved = ingest(eml("Lehrgangsgenehmigung"))
        profile = Participant.objects.create(name="Alex Beispiel", email=PERSONAL)

        confirmed = ingest(booking_mail(kind="confirmation", address=PERSONAL))

        self.assertEqual(confirmed.seat_id, approved.seat_id)
        self.assertEqual(confirmed.seat.participant_id, profile.pk)
        self.assertEqual(cards(confirmed.seat)["Genehmigt"], "Ja")

    def test_prefetches_retrieve_evidence_without_per_booking_queries(self):
        ingest(eml("Lehrgangsgenehmigung"))
        ingest(eml("Teilnahmebestätigung", period=JUNE))

        with self.assertNumQueries(3):
            rows = list(CourseSeat.objects.order_by("pk").prefetch_related(*booking_status_prefetches()))
            result = [cards(seat)["Genehmigt"] for seat in rows]

        self.assertEqual(result, ["Ja", "Offen"])
