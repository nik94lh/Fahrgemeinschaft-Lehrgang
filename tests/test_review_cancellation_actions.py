from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from bookings.models import BookingHistory, BookingStatus, EmailEvent, Participant
from bookings.services import ingest
from tests.test_participant_identity import PERSONAL, booking_mail
from tests.test_reconciliation import JUNE, eml
from tests.workspace_client import WorkspaceTestCase


class ReviewCancellationActionTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        known_time = datetime(2027, 4, 25, tzinfo=UTC)
        cls.booking = ingest(booking_mail(kind="confirmation", address=PERSONAL, reference="TEST-31"), occurred_at=known_time).seat
        cls.other = ingest(booking_mail(period=JUNE, address=PERSONAL, reference="TEST-32")).seat
        cls.case = ingest(eml("Stornierung", reference="TEST-31"))

    def detail_url(self):
        return reverse("review_detail", args=[self.case.pk])

    def cancellation_payload(self):
        detail = self.client.get(self.detail_url(), {"aktion": "apply_cancellation"})
        self.assertContains(detail, "Stornierung in BMS übernehmen")
        return {
            "action": "apply_cancellation",
            "cancellation_snapshot": detail.context["form"]["cancellation_snapshot"].value(),
            "confirm": "on",
        }

    def test_review_applies_the_cancellation_to_only_the_known_booking(self):
        self.assertEqual(self.case.review_reason, EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR)
        self.assertEqual(self.booking.participant_id, self.other.participant_id)
        original_id = self.booking.participant_id
        data = self.cancellation_payload()

        response = self.client.post(self.detail_url(), {**data, "seat": self.other.pk})

        self.assertRedirects(response, reverse("review"))
        self.case.refresh_from_db()
        self.booking.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.case.state, EmailEvent.State.REVIEWED)
        self.assertEqual(self.case.review_resolution, "cancelled")
        self.assertEqual(self.case.seat_id, self.booking.pk)
        self.assertIsNotNone(self.case.reviewed_at)
        self.assertEqual(self.booking.status, BookingStatus.CANCELLED)
        self.assertEqual(self.other.status, BookingStatus.APPROVED)
        self.assertEqual(self.booking.participant_id, original_id)
        self.assertEqual(self.booking.participant.email, PERSONAL)
        history = self.booking.history.get(source_event=self.case)
        self.assertEqual(history.previous_status, BookingStatus.CONFIRMED)
        self.assertEqual(history.status, BookingStatus.CANCELLED)
        self.assertEqual((history.previous_participant_id, history.participant_id), (original_id, original_id))
        self.assertEqual(self.client.post(self.detail_url(), data).status_code, 409)

    def test_cancellation_requires_current_snapshot_and_explicit_confirmation(self):
        data = self.cancellation_payload()
        self.assertEqual(self.authenticated_client(enforce_csrf_checks=True).post(self.detail_url(), data).status_code, 403)
        response = self.client.post(self.detail_url(), {**data, "confirm": ""})
        self.assertIn("confirm", response.context["form"].errors)
        response = self.client.post(self.detail_url(), {**data, "cancellation_snapshot": "0" * 64})
        self.assertTrue(response.context["form"].errors)

        BookingHistory.objects.create(
            seat=self.booking, previous_participant=self.booking.participant, participant=self.booking.participant,
            previous_status=self.booking.status, status=self.booking.status,
        )
        response = self.client.post(self.detail_url(), data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.case.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(self.case.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_new_confirmation_invalidates_the_cancellation_even_when_status_stays_the_same(self):
        data = self.cancellation_payload()
        reconfirmed = ingest(eml("Teilnahmebestätigung", reference="TEST-31"))
        self.assertEqual(reconfirmed.state, EmailEvent.State.APPLIED)
        self.assertFalse(reconfirmed.booking_changes.exists())

        response = self.client.post(self.detail_url(), data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertContains(response, "Die verknüpfte Buchung hat sich seit dem Eingang der Stornierung geändert.")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_cancellation_is_unavailable_for_a_replacement_with_the_same_name(self):
        replacement = Participant.objects.create(name=self.booking.participant.name)
        self.booking.participant = replacement
        self.booking.save(update_fields=["participant"])

        response = self.client.get(self.detail_url())

        self.assertNotContains(response, "aktion=apply_cancellation")

    def test_cancellation_requires_review_and_booking_edit_rights(self):
        data = self.cancellation_payload()
        for codenames in (("edit_review", "view_bookings"), ("view_review", "edit_bookings")):
            with self.subTest(codenames=codenames):
                user = get_user_model().objects.create_user("cancel-" + "-".join(codenames))
                user.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames))
                self.client.force_login(user)
                self.assertNotContains(self.client.get(self.detail_url()), "aktion=apply_cancellation")
                self.assertEqual(self.client.post(self.detail_url(), data).status_code, 403)
