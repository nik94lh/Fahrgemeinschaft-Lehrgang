from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse

from bookings.models import BookingStatus, EmailEvent, Participant
from bookings.services import ingest
from mailimport.models import MailReceipt
from tests.test_participant_identity import booking_mail
from tests.workspace_client import WorkspaceTestCase


class ReviewAddressActionTests(WorkspaceTestCase):
    saved_address = "bestand@example.invalid"
    incoming_address = "neuer-eingang@example.invalid"
    keep_title = "Gespeicherte Adresse behalten"

    @classmethod
    def setUpTestData(cls):
        cls.booking = ingest(booking_mail(address=cls.saved_address, reference="TEST-ADRESSE")).seat
        receipt = MailReceipt(source_id="1" * 64, uid_validity=1, uid=1)
        cls.case = ingest(
            booking_mail(kind="confirmation", address=cls.incoming_address, reference="TEST-ADRESSE"),
            receipt=receipt,
        )

    def detail_url(self):
        return reverse("review_detail", args=[self.case.pk])

    def keep_payload(self):
        response = self.client.get(self.detail_url(), {"aktion": "keep_address"})
        self.assertContains(response, self.keep_title)
        return {
            "action": "keep_address",
            "address_snapshot": response.context["form"]["address_snapshot"].value(),
            "confirm": "on",
        }

    def login_with(self, *codenames):
        user = get_user_model().objects.create_user("review-" + "-".join(codenames))
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames))
        self.client.force_login(user)

    def assert_case_still_open(self):
        self.case.refresh_from_db()
        self.assertEqual(self.case.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(self.case.review_resolution, "")
        self.assertIsNone(self.case.reviewed_at)

    def test_keep_address_closes_case_without_applying_the_incoming_confirmation(self):
        self.login_with("edit_review", "edit_bookings")
        self.assertEqual(self.case.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        original_history = list(self.booking.history.values())
        original_participant_id = self.booking.participant_id
        data = self.keep_payload()
        self.assert_case_still_open()

        response = self.client.post(self.detail_url(), data)

        self.assertRedirects(response, reverse("review"))
        self.case.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(self.case.state, EmailEvent.State.REVIEWED)
        self.assertEqual(self.case.review_resolution, "address_kept")
        self.assertEqual(self.case.seat_id, self.booking.pk)
        self.assertIsNotNone(self.case.reviewed_at)
        self.assertIsNone(self.case.processed_participant_id)
        self.assertEqual(self.booking.participant_id, original_participant_id)
        self.assertEqual(self.booking.participant.email, self.saved_address)
        self.assertEqual(self.booking.status, BookingStatus.APPROVED)
        self.assertEqual(list(self.booking.history.values()), original_history)
        self.assertEqual(self.client.post(self.detail_url(), data).status_code, 409)

    def test_keep_address_requires_csrf_and_explicit_confirmation(self):
        data = self.keep_payload()
        csrf_client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(self.detail_url(), data).status_code, 403)

        data.pop("confirm")
        response = self.client.post(self.detail_url(), data)

        self.assertEqual(response.status_code, 200)
        self.assertIn("confirm", response.context["form"].errors)
        self.assert_case_still_open()

    def test_keep_address_needs_both_edit_rights(self):
        data = self.keep_payload()

        for rights in (("edit_review", "view_bookings"), ("view_review", "edit_bookings")):
            with self.subTest(rights=rights):
                self.login_with(*rights)
                self.assertNotContains(self.client.get(self.detail_url()), "aktion=keep_address")
                self.assertEqual(self.client.post(self.detail_url(), data).status_code, 403)
                self.assert_case_still_open()

    def test_keep_address_refuses_a_snapshot_of_a_previous_email(self):
        data = self.keep_payload()
        person = self.booking.participant
        person.email = "aktualisierter-bestand@example.invalid"
        person.save(update_fields=["email"])

        response = self.client.post(self.detail_url(), data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assert_case_still_open()
        person.refresh_from_db()
        self.assertEqual(person.email, "aktualisierter-bestand@example.invalid")

    def test_keep_address_refuses_a_snapshot_of_a_previous_occupant(self):
        data = self.keep_payload()
        previous = self.booking.participant
        previous.email = ""
        previous.save(update_fields=["email"])
        replacement = Participant.objects.create(name=previous.name, email=self.saved_address)
        self.booking.participant = replacement
        self.booking.save(update_fields=["participant"])

        response = self.client.post(self.detail_url(), data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assert_case_still_open()
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.participant_id, replacement.pk)
        self.assertEqual(self.booking.participant.email, self.saved_address)

    def test_keep_address_is_unavailable_when_the_detected_booking_facts_disagree(self):
        data = self.keep_payload()
        initial_context = self.case.review_context.copy()

        for field, value in (
            ("participant_name", "Andere Testperson"),
            ("booking_reference", "TEST-ANDERE"),
            ("school", "regensburg"),
        ):
            with self.subTest(field=field):
                self.case.review_context = {**initial_context, field: value}
                self.case.save(update_fields=["review_context"])

                self.assertNotContains(self.client.get(self.detail_url()), "aktion=keep_address")
                self.assertEqual(self.client.post(self.detail_url(), data).status_code, 200)
                self.assert_case_still_open()

    def test_keep_address_is_unavailable_without_a_linked_booking(self):
        data = self.keep_payload()
        self.case.seat = None
        self.case.review_context = {**self.case.review_context, "candidate_seat_id": self.booking.pk}
        self.case.save(update_fields=["seat", "review_context"])

        self.assertNotContains(self.client.get(self.detail_url()), "aktion=keep_address")
        self.assertEqual(self.client.post(self.detail_url(), data | {"seat": self.booking.pk}).status_code, 200)
        self.assert_case_still_open()
        self.assertIsNone(self.case.seat_id)

    @override_settings(HTTPS=True)
    def test_address_visibility_follows_booking_and_archive_read_rights(self):
        self.assertNotIn("participant_email", self.case.review_context)
        self.assertNotIn(self.incoming_address, str(self.case.review_context))
        for rights, sees_saved, sees_incoming in (
            (("view_review",), False, False),
            (("view_review", "view_bookings"), True, False),
            (("view_review", "view_mail_receipts"), False, False),
            (("view_review", "view_bookings", "view_mail_receipts"), True, True),
        ):
            with self.subTest(rights=rights):
                self.login_with(*rights)
                response = self.client.get(self.detail_url(), secure=True)
                self.assertContains(response, self.case.get_review_reason_display())
                (self.assertContains if sees_saved else self.assertNotContains)(response, self.saved_address)
                (self.assertContains if sees_incoming else self.assertNotContains)(response, self.incoming_address)
                (self.assertContains if sees_incoming else self.assertNotContains)(response, "E-Mail-Adresse weicht ab")
                self.assertNotContains(response, "aktion=keep_address")

        with override_settings(HTTPS=False):
            self.assertNotContains(self.client.get(self.detail_url(), secure=True), self.incoming_address)
