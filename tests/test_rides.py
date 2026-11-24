from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from bookings.models import (
    BookingStatus,
    Carpool,
    Course,
    CourseSeat,
    CourseSession,
    EmailEvent,
    Participant,
    School,
)


class RideSuggestionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name="Übungslehrgang")
        cls.date = (timezone.now() + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
        cls.first = Participant.objects.create(name="Alex Person Eins")
        cls.second = Participant.objects.create(name="Sam Person Zwei")
        cls.third = Participant.objects.create(name="Jules Person Drei")

    def seat(self, participant, start, end, school=School.WUERZBURG, status=BookingStatus.CONFIRMED):
        session = CourseSession.objects.create(course=self.course, school=school, starts_at=start, ends_at=end)
        return CourseSeat.objects.create(session=session, participant=participant, status=status)

    def test_suggestions_separate_trip_directions_and_disclose_no_names(self):
        self.seat(self.first, self.date, self.date + timedelta(hours=4))
        self.seat(self.second, self.date + timedelta(hours=1), self.date + timedelta(hours=6))
        self.seat(self.third, self.date - timedelta(days=1), self.date + timedelta(hours=7))

        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Fahrgemeinschaften")
        self.assertContains(response, "Hinfahrt")
        self.assertContains(response, "Rückfahrt")
        self.assertNotContains(response, self.first.name)
        self.assertNotContains(response, self.second.name)
        self.assertNotContains(response, self.third.name)
        self.assertIn("no-store", response.headers["Cache-Control"])
        suggestions = list(response.context["page_obj"])
        self.assertEqual([s.direction for s in suggestions], [Carpool.Direction.OUTBOUND, Carpool.Direction.RETURN])
        self.assertEqual([s.participant_count for s in suggestions], [2, 2])

    def test_duplicate_participants_different_schools_and_unresolved_seats_do_not_form_groups(self):
        self.seat(self.first, self.date, self.date + timedelta(hours=4))
        self.seat(self.first, self.date, self.date + timedelta(hours=4))
        self.seat(self.second, self.date, self.date + timedelta(hours=4), School.REGENSBURG)
        self.seat(self.third, self.date, self.date + timedelta(hours=4), status=BookingStatus.APPROVED)
        unresolved = self.seat(self.second, self.date, self.date + timedelta(hours=4))
        EmailEvent.objects.create(sha256="a" * 64, state=EmailEvent.State.NEEDS_REVIEW, seat=unresolved)

        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Keine passenden Fahrten")
        self.assertEqual(list(response.context["page_obj"]), [])

    def test_trip_window_excludes_old_past_and_later_appointments(self):
        for offset in (-7, 30):
            start = self.date + timedelta(days=offset)
            self.seat(self.first, start, start + timedelta(hours=4))
            self.seat(self.second, start, start + timedelta(hours=4))

        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Keine passenden Fahrten")

    def test_tolerance_does_not_merge_transitively(self):
        for participant, shift in [(self.first, 0), (self.second, 50), (self.third, 100)]:
            start = self.date + timedelta(minutes=shift)
            self.seat(participant, start, start + timedelta(hours=4))

        response = self.client.get("/fahrgemeinschaften/")

        self.assertEqual([s.participant_count for s in response.context["page_obj"]], [2, 2, 2, 2])

    def test_close_appointments_on_different_local_days_do_not_match(self):
        beginning = timezone.localtime(self.date).replace(hour=23, minute=50)
        self.seat(self.first, beginning, beginning + timedelta(hours=4))
        self.seat(self.second, beginning + timedelta(minutes=20), beginning + timedelta(hours=8))

        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Keine passenden Fahrten")
