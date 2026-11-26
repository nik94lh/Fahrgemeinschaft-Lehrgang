from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bookings.models import (
    BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, FireBrigade, Participant, RideCase, RideNotice, School,
)
from bookings.ride_participation import active_participation, record_ride_reply, start_ride_inquiry
from bookings.rides_matching import sync_ride_cases
from bookings.services import ingest
from tests.test_participant_identity import booking_mail
from tests.workspace_client import WorkspaceTestCase


class BookingEditTests(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.original = ingest(booking_mail(address="alex@example.invalid"))
        self.seat = self.original.seat
        self.url = f"/buchungen/{self.seat.pk}/bearbeiten/"
        self.detail = reverse("booking_detail", args=[self.seat.pk])

    def data(self, **changes):
        form = self.client.get(self.url).context["form"]
        data = {field.name: field.value() or "" for field in form}
        data["starts_at"] = "2027-05-10T09:45"
        data["ends_at"] = "2027-05-14T15:00"
        return data | changes

    def test_editor_changes_one_booking_and_records_the_edit(self):
        session = self.seat.session
        participant = self.seat.participant
        other_term = CourseSession.objects.create(
            course=session.course, school=School.REGENSBURG, starts_at=session.starts_at, ends_at=session.ends_at,
        )
        shared_profile = CourseSeat.objects.create(session=other_term, participant=participant)
        shared_term = CourseSeat.objects.create(
            session=session, participant=Participant.objects.create(name="Bela Testperson"),
        )

        self.assertContains(self.client.get(self.detail), "Buchung bearbeiten")
        response = self.client.post(self.url, self.data(
            course_name="Synthetischer Ersatzlehrgang", school=School.GERETSRIED,
            starts_at="2027-05-11T09:45", status=BookingStatus.CONFIRMED,
            participant_email="alex.neu@example.invalid", confirmation_deadline="2027-05-01",
        ))

        self.assertRedirects(response, self.detail)
        self.seat.refresh_from_db()
        self.assertEqual(self.seat.session.course.name, "Synthetischer Ersatzlehrgang")
        self.assertEqual(self.seat.session.school, School.GERETSRIED)
        self.assertEqual(self.seat.session.starts_at, datetime(2027, 5, 11, 7, 45, tzinfo=UTC))
        self.assertEqual(self.seat.participant.email, "alex.neu@example.invalid")
        self.assertEqual(self.seat.confirmation_deadline.isoformat(), "2027-05-01")
        self.original.refresh_from_db()
        self.assertEqual(self.original.seat_id, self.seat.pk)
        for other in (shared_profile, shared_term):
            other.refresh_from_db()
        self.assertEqual(shared_profile.participant.email, "alex@example.invalid")
        self.assertEqual(shared_term.session.school, School.WUERZBURG)
        self.assertContains(self.client.get(self.detail), "Buchung manuell bearbeitet")
        self.assertEqual(self.seat.history.last().status, BookingStatus.CONFIRMED)

    def test_old_form_does_not_overwrite_a_concurrent_change(self):
        stale = self.data(status=BookingStatus.CANCELLED)
        self.assertRedirects(self.client.post(self.url, self.data(status=BookingStatus.CONFIRMED)), self.detail)

        response = self.client.post(self.url, stale)

        self.assertContains(response, "seit dem Öffnen geändert", status_code=409)
        self.seat.refresh_from_db()
        self.assertEqual(self.seat.status, BookingStatus.CONFIRMED)

    def test_adding_missing_email_keeps_an_isolated_profile(self):
        participant = self.seat.participant
        participant.email = ""
        participant.home_brigade = FireBrigade.objects.create(name="Testfeuerwehr", municipality="Testort")
        participant.save()

        response = self.client.post(self.url, self.data(participant_email="alex.neu@example.invalid"))

        self.assertRedirects(response, self.detail)
        self.seat.refresh_from_db()
        self.assertEqual(self.seat.participant_id, participant.pk)
        self.assertEqual(self.seat.participant.email, "alex.neu@example.invalid")
        self.assertEqual(self.seat.participant.home_brigade_id, participant.home_brigade_id)

    def test_existing_imported_reference_is_preserved(self):
        self.seat.external_reference = "TEST-REF-01"
        self.seat.save(update_fields=["external_reference"])
        form = self.client.get(self.url).context["form"]
        self.assertTrue(form.fields["external_reference"].disabled)

        response = self.client.post(self.url, self.data(external_reference="TEST-REF-02", status=BookingStatus.CANCELLED))

        self.assertRedirects(response, self.detail)
        self.seat.refresh_from_db()
        self.assertEqual(self.seat.external_reference, "TEST-REF-01")
        self.assertEqual(self.seat.status, BookingStatus.CANCELLED)

    def test_unreferenced_mail_for_old_tuple_goes_to_review(self):
        self.assertRedirects(self.client.post(self.url, self.data(participant_name="Alex Testperson")), self.detail)
        self.assertRedirects(self.client.post(self.url, self.data(starts_at="2027-05-11T09:45")), self.detail)

        follow_up = ingest(booking_mail(kind="confirmation"))

        self.assertEqual(follow_up.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(follow_up.review_reason, EmailEvent.ReviewReason.PREVIOUS_BOOKING_DATA)
        self.assertEqual(follow_up.review_context["candidate_seat_id"], self.seat.pk)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_unverified_message_for_moved_booking_does_not_change_same_named_booking(self):
        remaining = CourseSeat.objects.create(
            session=self.seat.session, status=BookingStatus.APPROVED,
            participant=Participant.objects.create(name="Alex Beispiel", email="andere-person@example.invalid"),
        )
        self.assertRedirects(self.client.post(self.url, self.data(starts_at="2027-05-11T09:45")), self.detail)

        follow_up = ingest(booking_mail(kind="confirmation"))

        self.assertEqual(follow_up.review_reason, EmailEvent.ReviewReason.PREVIOUS_BOOKING_DATA)
        remaining.refresh_from_db()
        self.assertEqual(remaining.status, BookingStatus.APPROVED)
        verified = ingest(booking_mail(kind="confirmation", address="andere-person@example.invalid"))
        self.assertEqual(verified.state, EmailEvent.State.APPLIED)
        self.assertEqual(verified.seat_id, remaining.pk)

    def test_reader_cannot_edit(self):
        reader = get_user_model().objects.create_user("buchungs-leser")
        reader.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_bookings"))
        self.client.force_login(reader)

        self.assertNotContains(self.client.get(self.detail), "Buchung bearbeiten")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {"status": BookingStatus.CANCELLED}).status_code, 403)

    def test_duplicate_person_and_term_are_rejected_without_partial_changes(self):
        target = CourseSession.objects.create(
            course=Course.objects.create(name="Synthetischer Ersatzlehrgang"), school=self.seat.session.school,
            starts_at=self.seat.session.starts_at, ends_at=self.seat.session.ends_at,
        )
        CourseSeat.objects.create(session=target, participant=self.seat.participant)

        response = self.client.post(self.url, self.data(course_name=target.course.name, status=BookingStatus.CANCELLED))

        self.assertContains(response, "bereits eine Buchung zu diesem Termin", status_code=400)
        self.seat.refresh_from_db()
        self.assertEqual(self.seat.session_id, self.original.seat.session_id)
        self.assertEqual(self.seat.status, BookingStatus.APPROVED)

    @override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.test")
    def test_cancelling_a_test_booking_permanently_retires_its_participation_access(self):
        start = timezone.localtime().replace(second=0, microsecond=0) + timedelta(days=6)
        end = start + timedelta(days=3)
        session = CourseSession.objects.create(
            course=Course.objects.create(name="Synthetischer Testlehrgang"), school=School.WUERZBURG,
            starts_at=start, ends_at=end, is_test=True,
        )
        for index in range(2):
            participant = Participant.objects.create(name=f"Testperson {index}", email=f"person{index}@example.invalid")
            CourseSeat.objects.create(
                session=session, participant=participant, status=BookingStatus.CONFIRMED, is_test=True,
            )
        sync_ride_cases()
        case = RideCase.objects.get(is_test=True)
        self.assertEqual(start_ride_inquiry(case.pk), 2)
        changed, remaining = case.participation_requests.order_by("pk")
        for entry in (changed, remaining):
            record_ride_reply(entry.pk, entry.token_digest, answer="yes", channels=["email"], consent=True)
        self.url = reverse("booking_edit", args=[changed.seat_id])
        period = {"starts_at": start.strftime("%Y-%m-%dT%H:%M"), "ends_at": end.strftime("%Y-%m-%dT%H:%M")}
        detail = reverse("booking_detail", args=[changed.seat_id])

        self.assertRedirects(self.client.post(self.url, self.data(**period, status=BookingStatus.CANCELLED)), detail)
        self.assertRedirects(self.client.post(self.url, self.data(**period, status=BookingStatus.CONFIRMED)), detail)

        self.assertIsNone(active_participation(changed.pk, changed.token_digest))
        self.assertIsNotNone(active_participation(remaining.pk, remaining.token_digest))
        self.assertTrue(remaining.notices.filter(kind=RideNotice.Kind.WITHDRAWAL).exists())
