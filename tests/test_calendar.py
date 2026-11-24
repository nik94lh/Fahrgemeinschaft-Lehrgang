from datetime import UTC, datetime

from django.test import TestCase

from bookings.models import Course, CourseSession, School


class CalendarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.next_month_start = datetime(2031, 5, 31, 22, tzinfo=UTC)
        cls.may_start = datetime(2031, 4, 30, 22, tzinfo=UTC)

        cls.add_session(
            "Übergreifender Musterlehrgang", School.WUERZBURG,
            datetime(2031, 4, 30, 12, tzinfo=UTC), datetime(2031, 5, 2, 11, tzinfo=UTC),
        )
        cls.add_session(
            "Musterlehrgang Regensburg", School.REGENSBURG,
            datetime(2031, 5, 12, 14, tzinfo=UTC), datetime(2031, 5, 12, 17, tzinfo=UTC),
        )
        cls.add_session(
            "Musterlehrgang Juni", School.WUERZBURG,
            cls.next_month_start, datetime(2031, 6, 1, 10, tzinfo=UTC),
        )
        cls.add_session(
            "Musterlehrgang April", School.WUERZBURG,
            datetime(2031, 4, 30, 15, tzinfo=UTC), cls.may_start,
        )

    @staticmethod
    def add_session(name, school, start, end):
        CourseSession.objects.create(
            course=Course.objects.create(name=name), school=school, starts_at=start, ends_at=end,
        )

    def test_calendar_includes_sessions_overlapping_the_local_month(self):
        response = self.client.get("/kalender/", {"month": "2031-05"})

        self.assertContains(response, "Mai 2031")
        self.assertContains(response, "Übergreifender Musterlehrgang")
        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertNotContains(response, "Musterlehrgang April")
        self.assertNotContains(response, "Musterlehrgang Juni")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_calendar_filter_and_navigation_keep_the_school(self):
        response = self.client.get("/kalender/", {"month": "2031-05", "school": School.WUERZBURG})

        self.assertContains(response, "Übergreifender Musterlehrgang")
        self.assertNotContains(response, "Musterlehrgang Regensburg")
        self.assertContains(response, "?month=2031-04&amp;school=wuerzburg")
        self.assertContains(response, "?month=2031-06&amp;school=wuerzburg")

    def test_invalid_month_falls_back_to_the_current_month(self):
        default_response = self.client.get("/kalender/")
        invalid_response = self.client.get("/kalender/", {"month": "2031-13"})

        self.assertContains(default_response, "Lehrgangskalender")
        self.assertContains(invalid_response, "Lehrgangskalender")
        self.assertEqual(invalid_response.context["month_id"], default_response.context["month_id"])
