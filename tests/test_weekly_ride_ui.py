from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, School
from bookings.rides_matching import sync_ride_cases
from tests.workspace_client import WorkspaceTestCase


class WeeklyRideWorkspaceTests(WorkspaceTestCase):
    def test_weekly_cards_and_details_do_not_display_internal_cutoff_times(self):
        berlin = ZoneInfo("Europe/Berlin")
        today = timezone.localdate(timezone=berlin)
        monday = today + timedelta(days=14 - today.weekday())
        course = Course.objects.create(name="Synthetischer zweiwöchiger Lehrgang")
        session = CourseSession.objects.create(
            course=course, school=School.WUERZBURG,
            starts_at=datetime.combine(monday, time(9, 15), berlin),
            ends_at=datetime.combine(monday + timedelta(days=11), time(14, 45), berlin),
        )
        for number in range(2):
            participant = Participant.objects.create(
                name=f"Testperson {number}", email=f"wochenabschnitt-{number}@example.invalid",
            )
            CourseSeat.objects.create(session=session, participant=participant, status=BookingStatus.CONFIRMED)

        self.assertEqual(sync_ride_cases(), 2)
        first, second = RideCase.objects.order_by("travel_date")
        listing = self.client.get(reverse("rides"))
        self.assertContains(listing, "Uhrzeit abstimmen", count=2)
        self.assertContains(listing, "Beginn 09:15")
        self.assertContains(listing, "Ende 14:45")
        self.assertNotContains(listing, "00:00")
        self.assertNotContains(listing, "23:59")

        for case in (first, second):
            with self.subTest(week=case.travel_date):
                details = self.client.get(reverse("ride_detail", args=[case.pk]))
                self.assertContains(details, "Uhrzeit abstimmen", count=1)
                self.assertContains(details, session.starts_at.astimezone(berlin).strftime("%d.%m.%Y, %H:%M"))
                self.assertContains(details, session.ends_at.astimezone(berlin).strftime("%d.%m.%Y, %H:%M"))
