from datetime import UTC, date, datetime

from django.test import TestCase

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, School


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

    def test_month_rows_use_local_day_columns_and_clip_overlapping_courses(self):
        self.add_session(
            "Abschluss um Mitternacht", School.WUERZBURG,
            datetime(2031, 5, 13, 18, tzinfo=UTC), datetime(2031, 5, 13, 22, tzinfo=UTC),
        )
        self.add_session(
            "Monatswechsel am Ende", School.WUERZBURG,
            datetime(2031, 5, 30, 15, tzinfo=UTC), datetime(2031, 6, 2, 10, tzinfo=UTC),
        )

        response = self.client.get("/kalender/", {"month": "2031-05"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["day_count"], 31)
        self.assertEqual(response.context["ticks"][0]["date"], date(2031, 5, 1))
        self.assertEqual(response.context["ticks"][-1]["date"], date(2031, 5, 31))

        groups = response.context["school_groups"]
        self.assertEqual([group["school"] for group in groups], [School.WUERZBURG, School.REGENSBURG])
        rows = {row["session"].course.name: row for row in groups[0]["rows"]}

        overlapping = rows["Übergreifender Musterlehrgang"]
        self.assertEqual((overlapping["start_column"], overlapping["end_column"]), (1, 3))
        self.assertTrue(overlapping["starts_before"])
        self.assertFalse(overlapping["ends_after"])

        midnight = rows["Abschluss um Mitternacht"]
        self.assertEqual((midnight["start_column"], midnight["end_column"]), (13, 14))

        month_end = rows["Monatswechsel am Ende"]
        self.assertEqual((month_end["start_column"], month_end["end_column"]), (30, 32))
        self.assertTrue(month_end["ends_after"])

    def test_week_shows_an_iso_week_and_retains_school_in_zoom_links(self):
        response = self.client.get("/kalender/", {
            "view": "week", "day": "2031-05-14", "school": School.REGENSBURG
        })

        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertNotContains(response, "Übergreifender Musterlehrgang")
        self.assertEqual(response.context["period_start"], date(2031, 5, 12))
        self.assertEqual(response.context["period_end"], date(2031, 5, 18))
        self.assertEqual(response.context["day_count"], 7)
        self.assertEqual(response.context["month_query"], "month=2031-05&school=regensburg")
        self.assertEqual(response.context["previous_query"], "view=week&day=2031-05-05&school=regensburg")
        self.assertEqual(response.context["current_query"], "view=week&day=2031-05-12&school=regensburg")

    def test_course_detail_shows_booking_seats_and_preserves_calendar_view(self):
        session = CourseSession.objects.get(course__name="Musterlehrgang Regensburg")
        CourseSeat.objects.create(
            session=session,
            participant=Participant.objects.create(name="Jana Kalenderbeispiel"),
            status=BookingStatus.CONFIRMED,
        )

        response = self.client.get(f"/kalender/{session.pk}/", {
            "view": "week", "day": "2031-05-14", "school": School.REGENSBURG
        })

        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertContains(response, "Jana Kalenderbeispiel")
        self.assertContains(response, "Bestätigt")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(
            response.context["back_href"],
            "/kalender/?view=week&day=2031-05-12&school=regensburg",
        )
        self.assertEqual(self.client.post(f"/kalender/{session.pk}/").status_code, 405)

    def test_switching_from_another_month_to_week_uses_first_scheduled_week(self):
        self.add_session(
            "Später im September", School.GERETSRIED,
            datetime(2032, 9, 13, 8, tzinfo=UTC), datetime(2032, 9, 14, 11, tzinfo=UTC),
        )
        response = self.client.get("/kalender/", {"month": "2032-09", "school": School.GERETSRIED})
        self.assertEqual(response.context["week_query"], "view=week&day=2032-09-13&school=geretsried")

    def test_year_uses_month_columns_and_respects_exclusive_local_month_boundaries(self):
        self.add_session(
            "Jahresübergang am Anfang", School.WUERZBURG,
            datetime(2030, 12, 30, 15, tzinfo=UTC), datetime(2031, 1, 3, 10, tzinfo=UTC),
        )
        self.add_session(
            "Ende genau zum Monatswechsel", School.WUERZBURG,
            datetime(2031, 1, 30, 11, tzinfo=UTC), datetime(2031, 1, 31, 23, tzinfo=UTC),
        )
        self.add_session(
            "Jahresübergang am Ende", School.WUERZBURG,
            datetime(2031, 12, 29, 9, tzinfo=UTC), datetime(2032, 1, 2, 13, tzinfo=UTC),
        )
        self.add_session(
            "Termin im Folgejahr", School.WUERZBURG,
            datetime(2031, 12, 31, 23, tzinfo=UTC), datetime(2032, 1, 1, 12, tzinfo=UTC),
        )

        response = self.client.get("/kalender/", {"view": "year", "year": "2031"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["day_count"], 12)
        self.assertEqual(response.context["period_end"], date(2031, 12, 31))
        self.assertEqual([tick["number"] for tick in response.context["ticks"]], list(range(1, 13)))
        self.assertEqual(response.context["ticks"][-1]["date"], date(2031, 12, 1))
        groups = response.context["school_groups"]
        self.assertEqual([group["school"] for group in groups], [School.WUERZBURG, School.REGENSBURG])
        rows = {row["session"].course.name: row for row in groups[0]["rows"]}

        beginning = rows["Jahresübergang am Anfang"]
        self.assertEqual((beginning["start_column"], beginning["end_column"]), (1, 2))
        self.assertTrue(beginning["starts_before"])
        january = rows["Ende genau zum Monatswechsel"]
        self.assertEqual((january["start_column"], january["end_column"]), (1, 2))
        ending = rows["Jahresübergang am Ende"]
        self.assertEqual((ending["start_column"], ending["end_column"]), (12, 13))
        self.assertTrue(ending["ends_after"])
        self.assertNotIn("Termin im Folgejahr", rows)

    def test_year_navigation_switches_zoom_and_preserves_school_and_detail_backlink(self):
        response = self.client.get("/kalender/", {"view": "year", "year": "2031", "school": School.REGENSBURG})

        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertNotContains(response, "Übergreifender Musterlehrgang")
        self.assertEqual(response.context["previous_query"], "view=year&year=2030&school=regensburg")
        self.assertEqual(response.context["next_query"], "view=year&year=2032&school=regensburg")
        self.assertEqual(response.context["current_query"], "view=year&year=2031&school=regensburg")
        self.assertEqual(response.context["month_query"], "month=2031-05&school=regensburg")
        self.assertEqual(response.context["week_query"], "view=week&day=2031-05-12&school=regensburg")
        session = CourseSession.objects.get(course__name="Musterlehrgang Regensburg")
        detail = self.client.get(f"/kalender/{session.pk}/?{response.context['current_query']}")

        self.assertContains(detail, "12.05.2031, 16:00")
        self.assertEqual(detail.context["back_href"], "/kalender/?view=year&year=2031&school=regensburg")

    def test_month_and_week_links_offer_the_matching_year(self):
        month = self.client.get("/kalender/", {"month": "2031-05", "school": School.REGENSBURG})
        week = self.client.get("/kalender/", {"view": "week", "day": "2031-05-12", "school": School.REGENSBURG})

        self.assertEqual(month.context["year_query"], "view=year&year=2031&school=regensburg")
        self.assertEqual(week.context["year_query"], "view=year&year=2031&school=regensburg")
