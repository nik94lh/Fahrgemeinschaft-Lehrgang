from django.test import Client, TestCase
from django.urls import reverse

from bookings.models import BookingStatus, CourseSeat, EmailEvent, Participant
from bookings.services import ingest
from tests.test_reconciliation import eml


class ReviewActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.booking = ingest(eml("Teilnahmebestätigung", reference="TEST-17")).seat
        cls.reassignment = ingest(eml(
            "Ummeldung", participant="Toni Testperson", reference="TEST-17",
        ))
        cls.replacement = Participant.objects.create(name="Toni Testperson")

    def detail_url(self):
        return reverse("review_detail", kwargs={"event_id": self.reassignment.pk})

    def test_resolution_requires_post_csrf_and_explicit_confirmation(self):
        payload = {"action": "link", "seat": self.booking.pk, "confirm": "on"}
        self.assertEqual(Client(enforce_csrf_checks=True).post(self.detail_url(), payload).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url(), payload).status_code, 200)
        payload.pop("confirm")
        self.assertEqual(self.client.post(self.detail_url(), payload).status_code, 200)
        self.reassignment.refresh_from_db()
        self.assertEqual(self.reassignment.state, EmailEvent.State.NEEDS_REVIEW)

    def test_link_records_a_deliberate_review_without_changing_booking(self):
        response = self.client.post(self.detail_url(), {
            "action": "link", "seat": self.booking.pk, "confirm": "on",
        })

        self.assertRedirects(response, reverse("review"))
        self.reassignment.refresh_from_db()
        self.assertEqual(self.reassignment.state, EmailEvent.State.REVIEWED)
        self.assertEqual(self.reassignment.review_resolution, EmailEvent.ReviewResolution.LINKED)
        self.assertIsNotNone(self.reassignment.reviewed_at)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_dismiss_uses_structured_reason_and_cannot_be_repeated(self):
        first = self.client.post(self.detail_url(), {
            "action": "dismiss", "reason": "duplicate", "confirm": "on",
        })
        second = self.client.post(self.detail_url(), {
            "action": "dismiss", "reason": "not_relevant", "confirm": "on",
        })

        self.assertRedirects(first, reverse("review"))
        self.assertEqual(second.status_code, 409)
        self.reassignment.refresh_from_db()
        self.assertEqual(self.reassignment.state, EmailEvent.State.DISMISSED)
        self.assertEqual(self.reassignment.review_resolution, EmailEvent.ReviewResolution.DUPLICATE)

    def test_reassign_records_current_and_new_participant_on_existing_seat(self):
        previous = self.booking.participant
        response = self.client.post(self.detail_url(), {
            "action": "reassign", "seat": self.booking.pk, "previous_participant": previous.pk,
            "replacement": self.replacement.pk, "confirm": "on",
        })

        self.assertRedirects(response, reverse("review"))
        self.booking.refresh_from_db()
        history = self.booking.history.last()
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(self.booking.participant, self.replacement)
        self.assertEqual(self.booking.status, BookingStatus.REASSIGNED)
        self.assertEqual(history.previous_participant, previous)
        self.assertEqual(history.participant, self.replacement)
        self.assertEqual(history.source_event, self.reassignment)

    def test_reassign_stale_or_unverified_previous_participant_has_no_effect(self):
        different_person = Participant.objects.create(name="Pat Muster")
        response = self.client.post(self.detail_url(), {
            "action": "reassign", "seat": self.booking.pk, "previous_participant": different_person.pk,
            "replacement": self.replacement.pk, "confirm": "on",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bisherige Person stimmt nicht überein")
        self.booking.refresh_from_db()
        self.reassignment.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.reassignment.state, EmailEvent.State.NEEDS_REVIEW)
