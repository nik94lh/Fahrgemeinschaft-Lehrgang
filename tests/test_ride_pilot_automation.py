from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, RideNotice, School
from bookings.ride_participation import (
    invite_code,
    participation_for_code,
    run_automatic_ride_inquiries,
    set_ride_automation,
    start_ride_inquiry,
)
from bookings.rides_matching import sync_ride_cases


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.test")
class RidePilotAutomationTests(TestCase):
    now = datetime(2032, 2, 1, 10, tzinfo=UTC)

    def booking(self, index, *, days, hours=0, is_test=True):
        starts = self.now + timedelta(days=days, hours=hours)
        course = Course.objects.create(name=f"Fiktiver Lehrgang {index}")
        session = CourseSession.objects.create(
            course=course, school=School.WUERZBURG, starts_at=starts, ends_at=starts + timedelta(hours=7),
        )
        return CourseSeat.objects.create(
            session=session, is_test=is_test, status=BookingStatus.CONFIRMED,
            participant=Participant.objects.create(name=f"Testperson {index}", email=f"test-{index}@example.invalid"),
        )

    def test_worker_uses_the_lead_window_and_only_prepares_test_invites(self):
        for offset, days, mode in ((0, 3, True), (2, 30, True), (4, 3, False)):
            self.booking(offset, days=days, is_test=mode)
            self.booking(offset + 1, days=days, is_test=mode)
        self.assertEqual(sync_ride_cases(now=self.now), 3)
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 0)

        set_ride_automation(True)
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 2)
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 0)

        started = RideCase.objects.distinct().get(participation_requests__isnull=False, is_test=True, travel_date__day=4)
        self.assertEqual(started.participation_requests.count(), 2)
        self.assertEqual(RideNotice.objects.filter(kind=RideNotice.Kind.INVITATION).count(), 2)
        self.assertFalse(RideCase.objects.filter(is_test=False, participation_requests__isnull=False).exists())

    def test_a_started_booking_keeps_its_code_during_the_course(self):
        early = self.booking(0, days=3)
        self.booking(1, days=3, hours=2)
        self.booking(2, days=3, hours=2)
        sync_ride_cases(now=self.now)
        case = RideCase.objects.get(is_test=True)
        self.assertEqual(start_ride_inquiry(case.pk, now=self.now), 3)
        entry = case.participation_requests.get(seat=early)
        after_start = early.session.starts_at + timedelta(minutes=30)

        sync_ride_cases(now=after_start)
        self.assertFalse(case.seats.filter(pk=early.pk).exists())
        with patch("bookings.ride_participation.timezone.now", return_value=after_start):
            self.assertEqual(participation_for_code(invite_code(entry)), entry)

    def test_automatic_inquiry_invites_the_new_peer_when_a_case_matches_again(self):
        first, leaving = (self.booking(index, days=3) for index in (0, 1))
        sync_ride_cases(now=self.now)
        case = RideCase.objects.get(is_test=True)
        set_ride_automation(True)
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 2)
        original = case.participation_requests.get(seat=first)
        leaving.status = BookingStatus.CANCELLED
        leaving.save(update_fields=["status"])
        self.assertEqual(sync_ride_cases(now=self.now), 0)

        newcomer = self.booking(2, days=3)
        self.assertEqual(sync_ride_cases(now=self.now), 1)

        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 1)
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 0)
        self.assertEqual(RideCase.objects.count(), 1)
        self.assertEqual(case.participation_requests.get(seat=first).pk, original.pk)
        new_entry = case.participation_requests.get(seat=newcomer)
        self.assertEqual(new_entry.notices.filter(kind=RideNotice.Kind.INVITATION).count(), 1)
