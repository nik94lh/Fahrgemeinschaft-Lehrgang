from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from bookings.models import CourseSession
from bookings.ride_segments import ride_segments


BERLIN = ZoneInfo("Europe/Berlin")


class RideSegmentTests(SimpleTestCase):
    def test_internal_boundaries_follow_berlin_time_across_a_dst_weekend(self):
        start = datetime(2027, 3, 22, 7, tzinfo=UTC)
        end = datetime(2027, 4, 2, 14, tzinfo=UTC)
        first, second = ride_segments(CourseSession(starts_at=start, ends_at=end))

        self.assertEqual(first.starts_at, start)
        self.assertEqual(first.ends_at, datetime.combine(date(2027, 3, 26), time.max, tzinfo=BERLIN))
        self.assertEqual((first.start_known, first.end_known), (True, False))
        self.assertEqual(second.departure_day, date(2027, 3, 29))
        self.assertEqual(second.starts_at.astimezone(UTC), datetime(2027, 3, 28, 22, tzinfo=UTC))
        self.assertEqual(second.ends_at, end)
        self.assertEqual((second.start_known, second.end_known), (False, True))

    def test_course_ending_at_monday_midnight_has_no_zero_length_segment(self):
        start = datetime(2027, 5, 10, 8, tzinfo=BERLIN)
        end = datetime(2027, 5, 24, 0, tzinfo=BERLIN)
        first, second = ride_segments(CourseSession(starts_at=start, ends_at=end))

        self.assertEqual(first.return_day, date(2027, 5, 14))
        self.assertEqual(second.departure_day, date(2027, 5, 17))
        self.assertEqual(second.ends_at, end)
        self.assertTrue(second.end_known)
