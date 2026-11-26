from datetime import UTC, datetime
from html import escape

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from bookings.models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, School
from mailimport.models import MailReceipt, ParticipantRecognitionConfiguration
from tests.workspace_client import WorkspaceTestCase


OPAQUE_ADDRESS = "x7q19@example.invalid"


class ParticipantContactTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = Participant.objects.create(name="Alex Beispiel")
        session = CourseSession.objects.create(
            course=Course.objects.create(name="Synthetischer Lehrgang"),
            school=School.WUERZBURG,
            starts_at=datetime(2031, 5, 2, 7, tzinfo=UTC),
            ends_at=datetime(2031, 5, 3, 12, tzinfo=UTC),
        )
        cls.seat = CourseSeat.objects.create(session=session, participant=cls.person)

    def setUp(self):
        super().setUp()
        self.url = f"/buchungen/{self.seat.pk}/teilnehmer/"

    def make_address_receipt(self):
        self.seat.status = BookingStatus.CONFIRMED
        self.seat.save(update_fields=["status"])
        event = EmailEvent.objects.create(
            sha256="a" * 64, kind=EmailEvent.Kind.CONFIRMATION,
            state=EmailEvent.State.APPLIED, seat=self.seat,
        )
        BookingHistory.objects.create(
            seat=self.seat, participant=self.person, status=self.seat.status, source_event=event,
        )
        return MailReceipt.objects.create(
            source_id="b" * 64, uid_validity=1, uid=2, event=event, outcome=MailReceipt.Outcome.APPLIED,
            fields={"participant_name": self.person.name, "participant_email_candidate": OPAQUE_ADDRESS},
        )

    def test_booking_detail_links_to_the_current_profile(self):
        booking_url = reverse("booking_detail", args=[self.seat.pk])
        self.assertContains(self.client.get(reverse("bookings")), booking_url)
        self.assertContains(self.client.get(booking_url), self.url)
        response = self.client.get(self.url)

        self.assertContains(response, "Alex Beispiel")
        self.assertContains(response, "Adresse speichern")
        self.assertEqual(response.context["booking_count"], 1)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_manual_entry_fills_blank_and_can_be_viewed(self):
        response = self.client.post(self.url, {
            "email": "Alex.Beispiel@example.invalid",
            "participant_id": self.person.pk,
        })
        self.assertRedirects(response, self.url)
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "alex.beispiel@example.invalid")
        self.assertContains(self.client.get(self.url), "alex.beispiel@example.invalid")
        self.assertNotContains(self.client.get(self.url), "Adresse speichern")
        bookings = self.client.get(reverse("bookings"))
        self.assertContains(bookings, "Buchungsdaten vollständig")
        self.assertNotContains(bookings, "alex.beispiel@example.invalid")

    def test_address_save_preserves_list_filters_when_the_booking_gets_a_new_profile(self):
        CourseSeat.objects.bulk_create([
            CourseSeat(session=self.seat.session, participant=self.person) for _ in range(25)
        ])
        navigation = {"q": self.person.name, "school": "wuerzburg", "status": "pending", "list_page": 3}
        profile_url = reverse("booking_participant", args=[self.seat.pk], query=navigation)

        response = self.client.post(profile_url + "&page=2&next=https://example.invalid/", {
            "email": OPAQUE_ADDRESS, "participant_id": self.person.pk,
        })

        self.assertRedirects(response, profile_url)
        profile = self.client.get(profile_url)
        self.assertEqual(profile.context["profile_bookings"].number, 1)
        self.assertEqual(profile.context["booking_count"], 1)
        self.assertNotEqual(profile.context["seat"].participant_id, self.person.pk)
        list_url = reverse("bookings", query={"q": self.person.name, "school": "wuerzburg", "status": "pending", "page": 3})
        self.assertContains(profile, f'href="{escape(list_url)}"')

    @override_settings(HTTPS=True)
    def test_profile_pagination_keeps_the_explicit_authorized_receipt(self):
        receipt = self.make_address_receipt()
        newer = MailReceipt.objects.create(
            source_id=receipt.source_id, uid_validity=receipt.uid_validity, uid=3,
            event=receipt.event, outcome=MailReceipt.Outcome.DUPLICATE, fields=receipt.fields,
        )
        CourseSeat.objects.bulk_create([
            CourseSeat(session=self.seat.session, participant=self.person) for _ in range(25)
        ])
        navigation = {"q": "Alex Beispiel", "list_page": 3, "mail": receipt.pk}
        profile = self.client.get(self.url, navigation, secure=True)
        next_url = reverse("booking_participant", args=[self.seat.pk], query={**navigation, "page": 2})

        self.assertEqual(profile.context["address_receipt_id"], receipt.pk)
        self.assertContains(profile, f'href="?{escape(next_url.split("?", 1)[1])}"')
        second = self.client.get(next_url, secure=True)
        self.assertEqual(second.context["profile_bookings"].number, 2)
        self.assertEqual(second.context["address_receipt_id"], receipt.pk)
        self.assertNotContains(second, reverse("mail_receipt", args=[newer.pk]))

    def test_stale_assignment_and_unverified_post_cannot_store_email(self):
        old_participant = self.person
        replacement = Participant.objects.create(name="Lina Beispiel")
        self.seat.participant = replacement
        self.seat.save(update_fields=["participant"])

        body = {"email": "alex.beispiel@example.invalid", "participant_id": old_participant.pk}
        invalid = self.client.post(self.url, body)
        self.assertContains(invalid, "Lade die Buchung neu.", status_code=400)
        client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(client.post(self.url, body).status_code, 403)
        replacement.refresh_from_db()
        self.assertEqual(replacement.email, "")

    @override_settings(HTTPS=True)
    def test_linked_mail_candidate_prefills_only_until_operator_saves(self):
        receipt = self.make_address_receipt()
        review_url = f"{self.url}?mail={receipt.pk}"
        mail_detail = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(mail_detail, f'href="{review_url}"')
        self.assertContains(mail_detail, OPAQUE_ADDRESS)
        self.assertContains(mail_detail, "Die geprüfte Adresse speicherst du im Teilnehmerprofil.")
        content = mail_detail.content.decode()
        self.assertLess(content.index("Adressvorschlag bei der Buchung prüfen"), content.index("Erkannte Angaben beim Abruf"))

        profile = self.client.get(self.url, secure=True)
        self.assertEqual(profile.context["form"]["email"].value(), OPAQUE_ADDRESS)
        self.assertContains(profile, "Adressvorschlag · noch nicht gespeichert")

        form = self.client.get(review_url, secure=True)
        self.assertEqual(form.context["form"]["email"].value(), OPAQUE_ADDRESS)
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "")

        response = self.client.post(review_url, {"email": OPAQUE_ADDRESS, "participant_id": self.person.pk}, secure=True)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.person.refresh_from_db()
        self.seat.refresh_from_db()
        self.assertEqual(self.person.email, OPAQUE_ADDRESS)
        self.assertEqual(self.seat.status, BookingStatus.CONFIRMED)
        saved_mail = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(saved_mail, "Adresse an der Buchung aktuell</strong> · Hinterlegt")
        self.assertContains(saved_mail, "Adressvorschlag aus dem ursprünglichen An-Feld")
        self.assertNotContains(saved_mail, "Adressvorschlag bei der Buchung prüfen")

    @override_settings(HTTPS=True)
    def test_receipt_separates_an_original_suggestion_from_a_different_saved_address(self):
        receipt = self.make_address_receipt()
        current = "geprueft@example.invalid"
        self.person.email = current
        self.person.save(update_fields=["email"])

        response = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)

        self.assertContains(response, current)
        self.assertContains(response, OPAQUE_ADDRESS)
        self.assertContains(response, "Adressvorschlag aus dem ursprünglichen An-Feld")
        self.assertNotContains(response, "Adressvorschlag bei der Buchung prüfen")

    @override_settings(HTTPS=True)
    def test_conflicting_eligible_mail_candidates_leave_the_profile_form_empty(self):
        receipt = self.make_address_receipt()
        duplicate = MailReceipt.objects.create(
            source_id=receipt.source_id, uid_validity=receipt.uid_validity, uid=3,
            event=receipt.event, outcome=MailReceipt.Outcome.DUPLICATE,
            fields={**receipt.fields, "participant_email_candidate": OPAQUE_ADDRESS.upper()},
        )
        self.assertEqual(self.client.get(self.url, secure=True).context["form"]["email"].value(), OPAQUE_ADDRESS)

        duplicate.fields["participant_email_candidate"] = "zweiter-vorschlag@example.invalid"
        duplicate.save(update_fields=["fields"])

        for parameters in ({}, {"mail": receipt.pk}):
            with self.subTest(parameters=parameters):
                response = self.client.get(self.url, parameters, secure=True)
                self.assertFalse(response.context["form"]["email"].value())
                self.assertContains(response, "Mehrere Adressvorschläge")
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "")

        response = self.client.post(self.url, {"email": OPAQUE_ADDRESS, "participant_id": self.person.pk}, secure=True)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, OPAQUE_ADDRESS)

    @override_settings(HTTPS=True)
    def test_open_booking_review_pauses_suggestion_and_keeps_independent_manual_save(self):
        receipt = self.make_address_receipt()
        review = EmailEvent.objects.create(
            sha256="d" * 64, seat=self.seat, state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR,
        )

        detail = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(detail, "Zu dieser Buchung ist ein Klärungsfall offen.")
        self.assertNotContains(detail, "Adressvorschlag bei der Buchung prüfen")

        for parameters in ({}, {"mail": receipt.pk}):
            with self.subTest(parameters=parameters):
                response = self.client.get(self.url, parameters, secure=True)
                self.assertFalse(response.context["form"]["email"].value())
                self.assertContains(response, "Zu dieser Buchung ist ein Klärungsfall offen.")
                self.assertNotContains(response, OPAQUE_ADDRESS)
        self.person.refresh_from_db()
        self.assertEqual(self.person.email, "")

        response = self.client.post(self.url, {"email": OPAQUE_ADDRESS, "participant_id": self.person.pk}, secure=True)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.person.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(self.person.email, OPAQUE_ADDRESS)
        self.assertEqual(review.state, EmailEvent.State.NEEDS_REVIEW)

    @override_settings(HTTPS=True)
    def test_duplicate_candidate_receipts_reuse_checks_and_keep_the_selected_source(self):
        receipt = self.make_address_receipt()
        with CaptureQueriesContext(connection) as initial_queries:
            self.client.get(self.url, {"mail": receipt.pk}, secure=True)
        MailReceipt.objects.bulk_create([
            MailReceipt(
                source_id=receipt.source_id, uid_validity=receipt.uid_validity, uid=uid,
                event=receipt.event, outcome=MailReceipt.Outcome.DUPLICATE,
                fields={**receipt.fields, "participant_email_candidate": OPAQUE_ADDRESS.upper()},
            )
            for uid in range(3, 8)
        ])

        with self.assertNumQueries(len(initial_queries)):
            response = self.client.get(self.url, {"mail": receipt.pk}, secure=True)
        self.assertEqual(response.context["address_receipt_id"], receipt.pk)
        self.assertEqual(response.context["form"]["email"].value(), OPAQUE_ADDRESS)

    @override_settings(HTTPS=True)
    def test_receipt_query_and_status_must_identify_an_applied_active_booking(self):
        receipt = self.make_address_receipt()
        for source in ("ungültig", "0", "9223372036854775808", str(receipt.pk + 1)):
            response = self.client.get(self.url, {"mail": source}, secure=True)
            self.assertFalse(response.context["form"]["email"].value())

        self.seat.status = BookingStatus.CANCELLED
        self.seat.save(update_fields=["status"])
        self.assertFalse(self.client.get(self.url, {"mail": receipt.pk}, secure=True).context["form"]["email"].value())
        self.seat.status = BookingStatus.CONFIRMED
        self.seat.save(update_fields=["status"])
        receipt.event.state = EmailEvent.State.NEEDS_REVIEW
        receipt.event.save(update_fields=["state"])
        self.assertFalse(self.client.get(self.url, {"mail": receipt.pk}, secure=True).context["form"]["email"].value())

    @override_settings(HTTPS=True)
    def test_candidate_is_unavailable_after_participant_history_changed(self):
        receipt = self.make_address_receipt()
        replacement = Participant.objects.create(name=self.person.name)
        BookingHistory.objects.create(
            seat=self.seat, previous_participant=self.person, participant=replacement, status=self.seat.status,
        )
        BookingHistory.objects.create(
            seat=self.seat, previous_participant=replacement, participant=self.person, status=self.seat.status,
        )

        response = self.client.get(self.url, {"mail": receipt.pk}, secure=True)
        self.assertFalse(response.context["form"]["email"].value())

    @override_settings(HTTPS=True)
    def test_candidate_is_unavailable_for_functional_or_other_profile_address(self):
        receipt = self.make_address_receipt()
        other_owner = Participant.objects.create(name=self.person.name, email=OPAQUE_ADDRESS)
        response = self.client.get(self.url, {"mail": receipt.pk}, secure=True)
        self.assertFalse(response.context["form"]["email"].value())

        other_owner.delete()
        ParticipantRecognitionConfiguration.objects.create(functional_addresses=[OPAQUE_ADDRESS])
        response = self.client.get(self.url, {"mail": receipt.pk}, secure=True)
        self.assertFalse(response.context["form"]["email"].value())

    def test_linked_bookings_show_exact_duplicates_and_time_overlaps_separately(self):
        original = self.seat.session
        self.seat.status = BookingStatus.CONFIRMED
        self.seat.save(update_fields=["status"])
        duplicate = CourseSeat.objects.create(
            session=CourseSession.objects.create(
                course=Course.objects.create(name=original.course.name.upper()),
                school=original.school,
                starts_at=original.starts_at,
                ends_at=original.ends_at,
            ),
            participant=self.person,
            status=BookingStatus.CONFIRMED,
            external_reference="TEST-DUPLIKAT",
        )
        overlap = CourseSeat.objects.create(
            session=CourseSession.objects.create(
                course=Course.objects.create(name="Zeitlich überlappender Testlehrgang"),
                school=School.REGENSBURG,
                starts_at=original.starts_at,
                ends_at=datetime(2031, 5, 3, 16, tzinfo=UTC),
            ),
            participant=self.person,
            status=BookingStatus.CONFIRMED,
        )
        next_seat = CourseSeat.objects.create(
            session=CourseSession.objects.create(
                course=Course.objects.create(name="Anschließender Testlehrgang"),
                school=School.WUERZBURG,
                starts_at=overlap.session.ends_at,
                ends_at=datetime(2031, 5, 4, 16, tzinfo=UTC),
            ),
            participant=self.person,
            status=BookingStatus.CONFIRMED,
        )
        CourseSeat.objects.create(session=next_seat.session, participant=self.person, status=BookingStatus.CANCELLED)
        other = Participant.objects.create(name=self.person.name)
        CourseSeat.objects.create(session=original, participant=other, status=BookingStatus.CONFIRMED)

        response = self.client.get(self.url)
        self.assertContains(response, "Lehrgangsbuchungen dieses Profils")
        bookings = {booking.pk: booking for booking in response.context["profile_bookings"]}
        self.assertNotIn(other.seats.get().pk, bookings)
        self.assertTrue(bookings[duplicate.pk].possible_duplicate)
        self.assertFalse(bookings[overlap.pk].possible_duplicate)
        self.assertTrue(bookings[overlap.pk].time_overlap)
        self.assertFalse(bookings[next_seat.pk].time_overlap)
        self.assertFalse(bookings[next_seat.pk].possible_duplicate)
        self.assertEqual(self.person.seats.count(), 5)
