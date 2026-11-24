from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from zoneinfo import ZoneInfo

from django.db import connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from bookings.models import BookingStatus, Carpool, Course, CourseSeat, CourseSession, EmailEvent, Participant, RideCase, School
from bookings.rides_matching import set_ride_case_status, sync_ride_cases


BERLIN = ZoneInfo("Europe/Berlin")
START = datetime(2027, 5, 10, 8, tzinfo=BERLIN)
NOW = START - timedelta(days=1)


def make_seat(course, participant, *, start=START, end=None, school=School.WUERZBURG, status=BookingStatus.CONFIRMED):
    session = CourseSession.objects.create(
        course=course, school=school, starts_at=start, ends_at=end or start + timedelta(hours=8),
    )
    return CourseSeat.objects.create(session=session, participant=participant, status=status)


class RideCaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name="Synthetischer Lehrgang")
        cls.alex = Participant.objects.create(name="Alex Beispiel")
        cls.sam = Participant.objects.create(name="Sam Testperson")
        cls.toni = Participant.objects.create(name="Toni Musterfall")

    def test_same_day_outbound_case_has_complete_seat_links_and_stable_key(self):
        first = make_seat(self.course, self.alex)
        second = make_seat(self.course, self.sam, start=START + timedelta(hours=3))
        same_person = make_seat(self.course, self.alex, start=START + timedelta(minutes=30))

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        case = RideCase.objects.get(direction=Carpool.Direction.OUTBOUND)
        self.assertEqual(case.travel_date, START.date())
        self.assertEqual(case.earliest, START)
        self.assertEqual(case.latest, START + timedelta(hours=3))
        self.assertEqual(case.status, RideCase.Status.OPEN)
        self.assertTrue(case.currently_matches)
        self.assertCountEqual(case.seats.all(), [first, second, same_person])

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        self.assertEqual(RideCase.objects.get(match_key=case.match_key).pk, case.pk)

    def test_return_cases_use_maximal_windows_with_inclusive_sixty_minutes(self):
        end = START + timedelta(hours=8)
        first = make_seat(self.course, self.alex, end=end)
        second = make_seat(self.course, self.sam, end=end + timedelta(minutes=60))
        third = make_seat(self.course, self.toni, end=end + timedelta(minutes=110))

        self.assertEqual(sync_ride_cases(now=NOW), 3)
        cases = list(RideCase.objects.filter(direction=Carpool.Direction.RETURN).order_by("earliest"))
        self.assertEqual([(case.earliest, case.latest) for case in cases], [
            (end, end + timedelta(minutes=60)),
            (end + timedelta(minutes=60), end + timedelta(minutes=110)),
        ])
        self.assertCountEqual(cases[0].seats.all(), [first, second])
        self.assertCountEqual(cases[1].seats.all(), [second, third])

    def test_matching_uses_berlin_days_independently_of_active_timezone(self):
        first = START.replace(hour=0, minute=10)
        second = START.replace(hour=23, minute=55)
        make_seat(self.course, self.alex, start=first)
        make_seat(self.course, self.sam, start=second)

        with timezone.override("UTC"):
            self.assertEqual(sync_ride_cases(now=NOW), 1)

        case = RideCase.objects.get()
        self.assertEqual(case.travel_date, START.date())
        self.assertEqual(case.earliest, first)
        self.assertEqual(case.latest, second)

    def test_separate_schools_unconfirmed_reviews_and_duplicate_profiles_do_not_match(self):
        make_seat(self.course, self.alex)
        make_seat(self.course, self.alex)
        make_seat(self.course, self.sam, school=School.REGENSBURG)
        make_seat(self.course, self.sam, status=BookingStatus.APPROVED)
        unresolved = make_seat(self.course, self.toni)
        EmailEvent.objects.create(sha256="a" * 64, state=EmailEvent.State.NEEDS_REVIEW, seat=unresolved)
        for participant in (self.sam, self.toni):
            make_seat(self.course, participant, start=NOW + timedelta(days=28))

        self.assertEqual(sync_ride_cases(now=NOW), 0)
        self.assertFalse(RideCase.objects.exists())

    def test_closed_case_retains_state_across_loss_and_return_of_matches(self):
        first = make_seat(self.course, self.alex)
        second = make_seat(self.course, self.sam, start=START + timedelta(hours=3))
        sync_ride_cases(now=NOW)
        case = RideCase.objects.get()
        set_ride_case_status(case.pk, RideCase.Status.CLOSED, expected_status=RideCase.Status.OPEN)
        second.status = BookingStatus.CANCELLED
        second.save(update_fields=["status"])

        self.assertEqual(sync_ride_cases(now=NOW), 0)
        case.refresh_from_db()
        self.assertFalse(case.currently_matches)
        self.assertEqual(case.status, RideCase.Status.CLOSED)
        self.assertCountEqual(case.seats.all(), [first, second])

        replacement = make_seat(self.course, self.toni, start=START + timedelta(hours=4))
        self.assertEqual(sync_ride_cases(now=NOW), 1)
        case.refresh_from_db()
        self.assertEqual(RideCase.objects.count(), 1)
        self.assertTrue(case.currently_matches)
        self.assertEqual(case.status, RideCase.Status.CLOSED)
        self.assertCountEqual(case.seats.all(), [first, replacement])

    def test_return_window_keeps_review_and_key_when_later_booking_extends_it(self):
        end = START + timedelta(hours=8)
        first = make_seat(self.course, self.alex, end=end)
        second = make_seat(self.course, self.sam, end=end + timedelta(minutes=15))
        sync_ride_cases(now=NOW)
        case = RideCase.objects.get(direction=Carpool.Direction.RETURN)
        set_ride_case_status(case.pk, RideCase.Status.IN_REVIEW, expected_status=RideCase.Status.OPEN)
        third = make_seat(self.course, self.toni, end=end + timedelta(minutes=40))

        sync_ride_cases(now=NOW)
        case.refresh_from_db()

        self.assertEqual(RideCase.objects.filter(direction=Carpool.Direction.RETURN).count(), 1)
        self.assertEqual(case.status, RideCase.Status.IN_REVIEW)
        self.assertEqual(case.latest, end + timedelta(minutes=40))
        self.assertCountEqual(case.seats.all(), [first, second, third])

    def test_stale_and_invalid_workflow_changes_fail_without_overwrite(self):
        make_seat(self.course, self.alex)
        make_seat(self.course, self.sam, start=START + timedelta(hours=2))
        sync_ride_cases(now=NOW)
        case = RideCase.objects.get()
        set_ride_case_status(case.pk, RideCase.Status.IN_REVIEW, expected_status=RideCase.Status.OPEN)

        with self.assertRaises(ValueError):
            set_ride_case_status(case.pk, RideCase.Status.CLOSED, expected_status=RideCase.Status.OPEN)
        with self.assertRaises(ValueError):
            set_ride_case_status(case.pk, "unknown", expected_status=RideCase.Status.IN_REVIEW)
        case.refresh_from_db()
        self.assertEqual(case.status, RideCase.Status.IN_REVIEW)


class ConcurrentRideCaseTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_sync_creates_each_match_once(self):
        course = Course.objects.create(name="Synthetischer Übungslehrgang")
        make_seat(course, Participant.objects.create(name="Alex Beispiel"))
        make_seat(course, Participant.objects.create(name="Sam Testperson"))
        ready = Barrier(2)

        def reconcile():
            try:
                ready.wait(timeout=5)
                return sync_ride_cases(now=NOW)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            matches = [pool.submit(reconcile) for _ in range(2)]
            self.assertCountEqual([job.result() for job in matches], [2, 2])

        self.assertEqual(RideCase.objects.count(), 2)
        self.assertTrue(all(case.seats.count() == 2 for case in RideCase.objects.all()))
