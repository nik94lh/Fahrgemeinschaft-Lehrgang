from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from bookings.rides_matching import sync_ride_cases

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

        sync_ride_cases()
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

        sync_ride_cases()
        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Noch keine Fahrgemeinschaftsfälle")
        self.assertEqual(list(response.context["page_obj"]), [])

    def test_trip_window_excludes_old_past_and_later_appointments(self):
        for offset in (-7, 30):
            start = self.date + timedelta(days=offset)
            self.seat(self.first, start, start + timedelta(hours=4))
            self.seat(self.second, start, start + timedelta(hours=4))

        sync_ride_cases()
        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Noch keine Fahrgemeinschaftsfälle")

    def test_outbound_groups_same_day_starts_from_eight_to_eleven(self):
        beginning = timezone.localtime(self.date).replace(hour=8)
        for participant, start_offset, end_offset in (
            (self.first, 0, 0), (self.second, 1, 50), (self.third, 3, 100),
        ):
            self.seat(
                participant,
                beginning + timedelta(hours=start_offset),
                beginning + timedelta(hours=4, minutes=end_offset),
            )

        sync_ride_cases()
        response = self.client.get("/fahrgemeinschaften/")

        suggestions = list(response.context["page_obj"])
        outbound = [item for item in suggestions if item.direction == Carpool.Direction.OUTBOUND]
        returns = [item for item in suggestions if item.direction == Carpool.Direction.RETURN]
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].participant_count, 3)
        self.assertEqual(outbound[0].earliest, beginning)
        self.assertEqual(outbound[0].latest, beginning + timedelta(hours=3))
        self.assertEqual([item.participant_count for item in returns], [2, 2])

    def test_close_appointments_on_different_local_days_do_not_match(self):
        beginning = timezone.localtime(self.date).replace(hour=23, minute=50)
        self.seat(self.first, beginning, beginning + timedelta(hours=4))
        self.seat(self.second, beginning + timedelta(minutes=20), beginning + timedelta(hours=8))

        sync_ride_cases()
        response = self.client.get("/fahrgemeinschaften/")

        self.assertContains(response, "Noch keine Fahrgemeinschaftsfälle")
