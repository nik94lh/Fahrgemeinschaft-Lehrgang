from datetime import UTC, datetime

from django.test import Client, TestCase
from django.urls import reverse

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, School


class ParticipantContactTests(TestCase):
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
        self.url = f"/buchungen/{self.seat.pk}/teilnehmer/"

    def test_booking_lists_the_link_and_shows_current_profile(self):
        self.assertContains(self.client.get(reverse("bookings")), self.url)
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

    def test_stale_assignment_and_unverified_post_cannot_store_email(self):
        old_participant = self.person
        replacement = Participant.objects.create(name="Lina Beispiel")
        self.seat.participant = replacement
        self.seat.save(update_fields=["participant"])

        body = {"email": "alex.beispiel@example.invalid", "participant_id": old_participant.pk}
        invalid = self.client.post(self.url, body)
        self.assertContains(invalid, "Lade die Buchung neu.", status_code=400)
        self.assertEqual(Client(enforce_csrf_checks=True).post(self.url, body).status_code, 403)
        replacement.refresh_from_db()
        self.assertEqual(replacement.email, "")

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
