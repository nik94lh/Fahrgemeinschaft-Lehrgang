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
END = START + timedelta(days=1, hours=8)
NOW = START - timedelta(days=1)


def make_seat(course, participant, *, start=START, end=END, school=School.WUERZBURG, status=BookingStatus.CONFIRMED):
    session = CourseSession.objects.create(course=course, school=school, starts_at=start, ends_at=end)
    return CourseSeat.objects.create(session=session, participant=participant, status=status)


class RideCaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name="Synthetischer Lehrgang")
        cls.alex = Participant.objects.create(name="Alex Beispiel")
        cls.sam = Participant.objects.create(name="Sam Testperson")
        cls.toni = Participant.objects.create(name="Toni Musterfall")

    def test_single_suggestion_combines_both_days_and_contains_matching_bookings(self):
        first = make_seat(self.course, self.alex)
        second = make_seat(self.course, self.sam, start=START + timedelta(hours=3), end=END + timedelta(hours=5))
        duplicate = make_seat(self.course, self.alex, start=START + timedelta(minutes=30))
        make_seat(self.course, self.toni, end=END + timedelta(days=1))

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        case = RideCase.objects.get()
        self.assertEqual(case.direction, "roundtrip")
        self.assertEqual((case.travel_date, case.return_date), (START.date(), END.date()))
        self.assertEqual((case.earliest, case.latest), (START, START + timedelta(hours=3)))
        self.assertEqual((case.return_earliest, case.return_latest), (END, END + timedelta(hours=5)))
        self.assertEqual(case.status, RideCase.Status.OPEN)
        self.assertTrue(case.currently_matches)
        self.assertCountEqual(case.seats.all(), [first, second, duplicate])

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        self.assertEqual(RideCase.objects.get().pk, case.pk)

    def test_shared_start_and_end_from_different_people_or_bookings_do_not_combine(self):
        make_seat(self.course, self.alex)
        make_seat(self.course, self.sam, end=END + timedelta(days=1))
        make_seat(self.course, self.sam, start=START + timedelta(days=1))

        self.assertEqual(sync_ride_cases(now=NOW), 0)
        self.assertFalse(RideCase.objects.exists())

    def test_each_matching_date_pair_has_its_own_suggestion(self):
        later_end = END + timedelta(days=1)
        first = make_seat(self.course, self.alex)
        second = make_seat(self.course, self.sam)
        third = make_seat(self.course, self.toni, end=later_end)
        fourth = make_seat(self.course, self.alex, end=later_end)

        self.assertEqual(sync_ride_cases(now=NOW), 2)
        early, late = RideCase.objects.order_by("return_date")
        self.assertEqual((early.return_date, late.return_date), (END.date(), later_end.date()))
        self.assertCountEqual(early.seats.all(), [first, second])
        self.assertCountEqual(late.seats.all(), [third, fourth])

    def test_matching_uses_berlin_start_and_end_dates_independently_of_active_timezone(self):
        early_start = START.replace(hour=0, minute=10)
        early_end = END.replace(hour=0, minute=10)
        late_start = START.replace(hour=23, minute=55)
        late_end = END.replace(hour=23, minute=55)
        first = make_seat(self.course, self.alex, start=early_start, end=early_end)
        second = make_seat(self.course, self.sam, start=late_start, end=late_end)
        make_seat(self.course, self.toni, start=late_start, end=late_end + timedelta(minutes=10))

        with timezone.override("UTC"):
            self.assertEqual(sync_ride_cases(now=NOW), 1)

        case = RideCase.objects.get()
        self.assertEqual((case.travel_date, case.return_date), (START.date(), END.date()))
        self.assertEqual((case.earliest, case.latest), (early_start, late_start))
        self.assertEqual((case.return_earliest, case.return_latest), (early_end, late_end))
        self.assertCountEqual(case.seats.all(), [first, second])

    def test_separate_schools_unconfirmed_reviews_and_duplicate_profiles_do_not_match(self):
        make_seat(self.course, self.alex)
        make_seat(self.course, self.alex)
        make_seat(self.course, self.sam, school=School.REGENSBURG)
        make_seat(self.course, self.sam, status=BookingStatus.APPROVED)
        unresolved = make_seat(self.course, self.toni)
        EmailEvent.objects.create(sha256="a" * 64, state=EmailEvent.State.NEEDS_REVIEW, seat=unresolved)

        self.assertEqual(sync_ride_cases(now=NOW), 0)
        self.assertFalse(RideCase.objects.exists())

    def test_closed_suggestion_retains_state_across_loss_and_return_of_matches(self):
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

    def test_matching_suggestion_updates_times_without_losing_review(self):
        first = make_seat(self.course, self.alex)
        second = make_seat(self.course, self.sam, end=END + timedelta(minutes=15))
        sync_ride_cases(now=NOW)
        case = RideCase.objects.get()
        set_ride_case_status(case.pk, RideCase.Status.IN_REVIEW, expected_status=RideCase.Status.OPEN)
        third = make_seat(self.course, self.toni, start=START + timedelta(hours=1), end=END + timedelta(hours=2))

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        case.refresh_from_db()

        self.assertEqual(case.status, RideCase.Status.IN_REVIEW)
        self.assertEqual(case.latest, START + timedelta(hours=1))
        self.assertEqual(case.return_latest, END + timedelta(hours=2))
        self.assertCountEqual(case.seats.all(), [first, second, third])

    def test_legacy_single_leg_decision_is_preserved_separately(self):
        first = make_seat(self.course, self.alex)
        second = make_seat(self.course, self.sam)
        legacy_key = f"{School.WUERZBURG}:return:{END.date()}"
        legacy = RideCase.objects.create(
            match_key=legacy_key, school=School.WUERZBURG, direction=Carpool.Direction.RETURN,
            travel_date=END.date(), earliest=END, latest=END, status=RideCase.Status.CLOSED,
        )
        legacy.seats.set([first, second])

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        legacy.refresh_from_db()
        suggestion = RideCase.objects.get(currently_matches=True)

        self.assertFalse(legacy.currently_matches)
        self.assertEqual(legacy.status, RideCase.Status.CLOSED)
        self.assertEqual(legacy.match_key, legacy_key)
        self.assertCountEqual(legacy.seats.all(), [first, second])
        self.assertNotEqual(suggestion.pk, legacy.pk)
        self.assertEqual(suggestion.status, RideCase.Status.OPEN)
        self.assertCountEqual(suggestion.seats.all(), [first, second])

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
    def test_concurrent_sync_creates_each_suggestion_once(self):
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
            self.assertCountEqual([job.result() for job in matches], [1, 1])

        self.assertEqual(RideCase.objects.get().seats.count(), 2)
