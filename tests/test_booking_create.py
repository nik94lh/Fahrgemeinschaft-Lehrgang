from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bookings.booking_state import booking_missing_fields
from bookings.models import (
    BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, RideCase, School,
)
from bookings.services import ingest
from mailimport.models import MailReceipt, ParticipantRecognitionConfiguration
from tests.test_participant_identity import booking_mail
from tests.workspace_client import WorkspaceTestCase


class BookingCreateTests(WorkspaceTestCase):
    url = "/buchungen/neu/"

    def data(self, **values):
        return {
            "participant_name": "Anna Testperson",
            "participant_email": "anna.testperson@example.invalid",
            "course_name": "Synthetischer Lehrgang",
            "school": School.WUERZBURG,
            "starts_at": "2031-05-10T09:00",
            "ends_at": "2031-05-12T15:00",
            "status": BookingStatus.CONFIRMED,
            "is_test": "on",
        } | values

    def test_create_requires_booking_edit_access_and_csrf(self):
        self.assertRedirects(Client().get(self.url), "/anmelden/?next=/buchungen/neu/", fetch_redirect_response=False)
        reader = get_user_model().objects.create_user("buchungen-leser")
        reader.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_bookings"))
        self.client.force_login(reader)

        self.assertNotContains(self.client.get(reverse("bookings")), "Buchung anlegen")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 403)
        csrf_client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(csrf_client.post(self.url, self.data()).status_code, 403)
        self.assertFalse(CourseSeat.objects.exists())

    def test_create_form_defaults_to_test_mode_and_confirmed_status(self):
        self.assertContains(self.client.get(reverse("bookings")), "Buchung anlegen")
        response = self.client.get(self.url)

        self.assertContains(response, "Testbuchung")
        self.assertNotContains(response, 'data-live-region="main"')
        self.assertTrue(response.context["form"]["is_test"].value())
        self.assertEqual(response.context["form"]["status"].value(), BookingStatus.CONFIRMED)
        self.assertFalse(CourseSeat.objects.exists())

    def test_saves_a_complete_local_test_booking_and_audits_the_initial_status(self):
        with timezone.override("UTC"):
            response = self.client.post(self.url, self.data(
                participant_email="Anna.Testperson@EXAMPLE.INVALID", external_reference="TEST-PRUEFUNG-1",
                confirmation_deadline="2031-05-06",
            ))

        seat = CourseSeat.objects.get()
        self.assertRedirects(response, reverse("booking_detail", args=[seat.pk]))
        self.assertTrue(seat.is_test)
        self.assertEqual(seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(seat.participant.email, "anna.testperson@example.invalid")
        self.assertEqual(seat.session.starts_at, datetime(2031, 5, 10, 7, tzinfo=UTC))
        self.assertEqual(seat.session.ends_at, datetime(2031, 5, 12, 13, tzinfo=UTC))
        self.assertEqual(seat.external_reference, "TEST-PRUEFUNG-1")
        self.assertEqual(seat.confirmation_deadline.isoformat(), "2031-05-06")
        self.assertFalse(booking_missing_fields(seat, functional_addresses=()))
        history = BookingHistory.objects.get(seat=seat)
        self.assertEqual(history.participant_id, seat.participant_id)
        self.assertEqual(history.status, BookingStatus.CONFIRMED)
        self.assertIsNone(history.source_event_id)
        self.assertFalse(EmailEvent.objects.exists())
        self.assertFalse(MailReceipt.objects.exists())
        self.assertContains(self.client.get(reverse("bookings")), "Testbuchung")

    def test_duplicate_post_does_not_create_another_booking_or_history_row(self):
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 302)
        response = self.client.post(self.url, self.data())

        self.assertEqual(response.status_code, 400)
        self.assertIn("participant_email", response.context["form"].errors)
        self.assertEqual(Course.objects.count(), 1)
        self.assertEqual(CourseSession.objects.count(), 1)
        self.assertEqual(Participant.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(BookingHistory.objects.count(), 1)

    def test_invalid_or_functional_test_address_and_invalid_period_create_no_records(self):
        ParticipantRecognitionConfiguration.objects.create(functional_addresses=["funktion@example.invalid"])
        for values, field in (
            ({"participant_email": "adresse@example.com"}, "participant_email"),
            ({"participant_email": "funktion@example.invalid"}, "participant_email"),
            ({"ends_at": "2031-05-10T08:00"}, "ends_at"),
            ({"starts_at": "2031-03-30T02:30"}, "starts_at"),
        ):
            with self.subTest(field=field):
                response = self.client.post(self.url, self.data(**values))
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context["form"].errors)
        self.assertFalse(Participant.objects.exists())
        self.assertFalse(CourseSession.objects.exists())

    def test_an_address_cannot_join_a_profile_in_the_other_booking_mode(self):
        self.assertEqual(self.client.post(self.url, self.data(is_test="")).status_code, 302)
        response = self.client.post(self.url, self.data(starts_at="2031-05-11T09:00"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("participant_email", response.context["form"].errors)
        seat = CourseSeat.objects.get()
        self.assertFalse(seat.is_test)
        self.assertEqual(Participant.objects.count(), 1)
        self.assertEqual(CourseSession.objects.count(), 1)

    def test_reference_conflict_does_not_create_or_reassign_other_records(self):
        self.assertEqual(self.client.post(self.url, self.data(external_reference="TEST-90")).status_code, 302)
        response = self.client.post(self.url, self.data(
            participant_name="Bela Testperson", participant_email="bela.testperson@example.invalid",
            course_name="Anderer synthetischer Lehrgang", external_reference="TEST-90",
        ))

        self.assertEqual(response.status_code, 400)
        self.assertIn("external_reference", response.context["form"].errors)
        self.assertEqual(CourseSeat.objects.get().participant.name, "Anna Testperson")
        self.assertEqual(Participant.objects.count(), 1)
        self.assertEqual(Course.objects.count(), 1)

    def test_test_booking_reference_does_not_block_a_regular_mail_import(self):
        reference = "TEST-90"
        self.assertEqual(self.client.post(self.url, self.data(external_reference=reference)).status_code, 302)
        test_seat = CourseSeat.objects.get(is_test=True)
        test_participant_id = test_seat.participant_id

        event = ingest(booking_mail(reference=reference, address="alex@example.invalid"))

        self.assertEqual(event.state, EmailEvent.State.APPLIED)
        regular_seat = CourseSeat.objects.get(is_test=False)
        self.assertEqual(event.seat, regular_seat)
        self.assertEqual(regular_seat.external_reference, reference)
        self.assertEqual(regular_seat.status, BookingStatus.APPROVED)
        test_seat.refresh_from_db()
        self.assertTrue(test_seat.is_test)
        self.assertEqual(test_seat.external_reference, reference)
        self.assertEqual(test_seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(test_seat.participant_id, test_participant_id)
        self.assertEqual(test_seat.history.count(), 1)

    def test_test_only_online_session_does_not_make_regular_mail_import_ambiguous(self):
        original = ingest(booking_mail(address="alex@example.invalid"))
        regular_session = original.seat.session
        response = self.client.post(self.url, self.data(
            course_name="Gruppenführer", starts_at="2027-05-10T09:45", ends_at="2027-05-14T15:00", is_online="on",
        ))
        self.assertEqual(response.status_code, 302)
        test_session = CourseSeat.objects.get(is_test=True).session
        self.assertNotEqual(test_session.pk, regular_session.pk)
        self.assertEqual(test_session.course.name, regular_session.course.name)
        self.assertEqual(test_session.school, regular_session.school)
        self.assertEqual(test_session.starts_at, regular_session.starts_at)
        self.assertEqual(test_session.ends_at, regular_session.ends_at)
        self.assertTrue(test_session.is_online)
        self.assertFalse(regular_session.is_online)

        result = ingest(booking_mail(name="Toni Testperson", address="toni@example.invalid"))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat.session_id, regular_session.pk)
        self.assertFalse(result.seat.is_test)
        self.assertFalse(regular_session.is_test)
        self.assertTrue(test_session.is_test)
        self.assertEqual(CourseSession.objects.count(), 2)

    def test_two_confirmed_test_bookings_create_a_test_only_match(self):
        start = timezone.localdate() + timedelta(days=7)
        end = start + timedelta(days=2)
        period = {"starts_at": f"{start.isoformat()}T09:00", "ends_at": f"{end.isoformat()}T15:00"}
        real = self.data(
            **period, is_test="", participant_name="Elia Testperson", participant_email="elia@example.invalid",
        )
        self.assertEqual(self.client.post(self.url, real).status_code, 302)
        self.assertEqual(self.client.post(self.url, self.data(**period)).status_code, 302)
        self.assertFalse(RideCase.objects.exists())

        second_test = self.data(**period, participant_name="Bela Testperson", participant_email="bela@example.invalid")
        self.assertEqual(self.client.post(self.url, second_test).status_code, 302)

        match = RideCase.objects.get()
        self.assertTrue(match.is_test)
        self.assertCountEqual(match.seats.all(), CourseSeat.objects.filter(is_test=True))
        regular_session = CourseSeat.objects.get(is_test=False).session
        test_session = CourseSession.objects.get(is_test=True)
        self.assertFalse(regular_session.is_test)
        self.assertNotEqual(regular_session.pk, test_session.pk)
        self.assertEqual(match.seats.filter(session=test_session).count(), 2)
        self.assertEqual(CourseSession.objects.count(), 2)
