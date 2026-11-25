from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from bookings.models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, School
from mailimport.models import MailReceipt


class AccessDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = Participant.objects.create(name="Alex Beispiel")
        cls.session = CourseSession.objects.create(
            course=Course.objects.create(name="Synthetischer Lehrgang"), school=School.WUERZBURG,
            starts_at=datetime(2032, 5, 4, 8, tzinfo=UTC), ends_at=datetime(2032, 5, 5, 15, tzinfo=UTC),
        )
        cls.seat = CourseSeat.objects.create(
            session=cls.session, participant=cls.person, external_reference="TEST-PRIVAT-17",
            status=BookingStatus.CONFIRMED,
        )
        cls.event = EmailEvent.objects.create(
            sha256="7" * 64, kind=EmailEvent.Kind.CONFIRMATION, state=EmailEvent.State.APPLIED, seat=cls.seat,
        )
        BookingHistory.objects.create(
            seat=cls.seat, participant=cls.person, status=cls.seat.status, source_event=cls.event,
        )
        cls.receipt = MailReceipt.objects.create(
            source_id="8" * 64, uid_validity=1, uid=2, event=cls.event, outcome=MailReceipt.Outcome.APPLIED,
            fields={"participant_name": cls.person.name, "participant_email_candidate": "vorschlag@example.invalid"},
        )

    def login_with(self, *codenames):
        user = get_user_model().objects.create_user(username="test-mitarbeit")
        permissions = Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames)
        self.assertEqual(permissions.count(), len(codenames))
        user.user_permissions.add(*permissions)
        self.client.force_login(user)

    def test_calendar_reader_sees_event_but_neither_private_booking_details_nor_links(self):
        self.login_with("view_calendar")
        path = reverse("calendar_detail", args=[self.session.pk])
        response = self.client.get(path)

        self.assertContains(response, self.session.course.name)
        self.assertNotContains(response, self.person.name)
        self.assertNotContains(response, self.seat.external_reference)
        self.assertNotContains(response, f'href="{reverse("bookings")}"')
        self.assertNotContains(response, f'href="{reverse("account_list")}"')

        fragment = self.client.get(path, headers={"X-BMS-Live": "1"})
        self.assertNotContains(fragment, self.person.name)

    def test_overview_keeps_counts_but_does_not_show_a_mail_event_as_person_data(self):
        self.login_with("view_overview")

        response = self.client.get(reverse("overview"))

        self.assertContains(response, "Bestätigt")
        self.assertNotContains(response, self.person.name)
        self.assertNotContains(response, "Letzte E-Mail-Ereignisse")

    @override_settings(HTTPS=True)
    def test_booking_right_alone_does_not_expose_receipt_candidates(self):
        self.login_with("edit_bookings")
        path = reverse("booking_participant", args=[self.seat.pk])

        for parameters in ({}, {"mail": self.receipt.pk}):
            with self.subTest(parameters=parameters):
                response = self.client.get(path, parameters, secure=True)
                self.assertContains(response, "Adresse speichern")
                self.assertNotContains(response, "vorschlag@example.invalid")

    @override_settings(HTTPS=True)
    def test_receipt_under_review_keeps_candidate_as_an_original_mail_detail(self):
        self.event.state = EmailEvent.State.NEEDS_REVIEW
        self.event.save(update_fields=["state"])
        self.login_with("view_bookings", "view_mail_receipts")

        response = self.client.get(reverse("mail_receipt", args=[self.receipt.pk]), secure=True)

        self.assertContains(response, "Adressvorschlag aus dem ursprünglichen An-Feld")
        self.assertNotContains(response, "Diese Mail enthält einen Adressvorschlag für die Buchung.")

    def test_preview_stays_usable_for_readers_and_hides_the_import_action(self):
        self.login_with("view_preview")

        response = self.client.get(reverse("preview"))

        self.assertContains(response, "Prüfen")
        self.assertNotContains(response, "Testmail importieren")

    def test_review_reader_sees_parsed_mail_but_not_current_booking_data(self):
        self.event.state = EmailEvent.State.NEEDS_REVIEW
        self.event.review_context = {"participant_name": "Eingangsname Beispiel"}
        self.event.save(update_fields=["state", "review_context"])
        self.login_with("view_review")

        response = self.client.get(reverse("review_detail", args=[self.event.pk]))

        self.assertContains(response, "Eingangsname Beispiel")
        self.assertNotContains(response, self.person.name)
        self.assertNotContains(response, self.seat.external_reference)

    @override_settings(HTTPS=True)
    def test_editing_mailbox_without_booking_right_cannot_change_the_import_source(self):
        self.login_with("edit_mailbox")
        path = reverse("mailbox")

        response = self.client.post(path, {}, secure=True)

        self.assertEqual(response.status_code, 403)
        self.assertNotContains(self.client.get(path, secure=True), "Postfach speichern")
        self.assertContains(self.client.get(path, secure=True), "Funktionsadressen speichern")

    @override_settings(HTTPS=True)
    def test_archive_reader_does_not_see_current_participant_from_a_linked_booking(self):
        self.receipt.delete()
        self.login_with("view_mail_receipts")

        response = self.client.get(reverse("mail_receipts"), {"ansicht": "frueher"}, secure=True)

        self.assertContains(response, self.event.get_kind_display())
        self.assertNotContains(response, self.person.name)
        self.assertNotContains(response, self.session.course.name)
