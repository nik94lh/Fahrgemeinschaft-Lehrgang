from datetime import UTC, date, datetime

from django.utils.dateformat import format as format_date

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, School
from tests.workspace_client import WorkspaceTestCase


class CalendarTests(WorkspaceTestCase):
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

    def test_year_shows_day_occupancy_and_respects_exclusive_local_month_boundaries(self):
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
        self.assertEqual(response.context["period_end"], date(2031, 12, 31))
        self.assertContains(response, 'class="calendar-month-card"', count=12)
        self.assertNotContains(response, "Jahresübergang am Anfang")
        self.assertNotContains(response, "Jahresübergang am Ende")
        self.assertNotContains(response, 'class="calendar-bar')
        months = response.context["months"]
        january, february, *_, december = months
        self.assertEqual(january["days"][:2], [None, None])
        self.assertEqual(january["days"][2]["date"], date(2031, 1, 1))
        self.assertEqual(january["session_count"], 2)
        self.assertEqual(january["occupied_days"], [1, 2, 3, 30, 31])
        self.assertEqual(february["session_count"], 0)
        self.assertEqual(february["occupied_days"], [])
        self.assertEqual(december["session_count"], 1)
        self.assertEqual(december["occupied_days"], [29, 30, 31])

    def test_year_navigation_switches_zoom_and_preserves_school_and_detail_backlink(self):
        response = self.client.get("/kalender/", {"view": "year", "year": "2031", "school": School.REGENSBURG})

        self.assertContains(response, 'href="?month=2031-05&amp;school=regensburg"')
        self.assertNotContains(response, "Musterlehrgang Regensburg")
        self.assertNotContains(response, "Übergreifender Musterlehrgang")
        may = response.context["months"][4]
        self.assertEqual(may["session_count"], 1)
        self.assertEqual(may["occupied_days"], [12])
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

    def test_month_jump_uses_readable_month_and_year_controls_and_weekday_axis(self):
        response = self.client.get("/kalender/", {
            "year": "2031", "month_number": "05", "school": School.REGENSBURG,
        })

        self.assertContains(response, "Mai 2031")
        self.assertContains(response, '<select name="month_number"')
        self.assertContains(response, 'inputmode="numeric"')
        self.assertNotContains(response, 'type="month"')
        weekday = format_date(date(2031, 5, 1), "D")
        self.assertContains(response, f'<span class="calendar-weekday">{weekday}</span>')
        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertNotContains(response, "Übergreifender Musterlehrgang")
        self.assertEqual(response.context["current_query"], "month=2031-05&school=regensburg")

    def test_year_highlights_months_by_distinct_occupied_days_for_the_selected_school(self):
        for course in ("Synthetischer Julikurs A", "Synthetischer Julikurs B"):
            self.add_session(
                course, School.GERETSRIED,
                datetime(2031, 7, 15, 8, tzinfo=UTC), datetime(2031, 7, 15, 16, tzinfo=UTC),
            )
        self.add_session(
            "Sommerlehrgang", School.GERETSRIED,
            datetime(2031, 8, 3, 8, tzinfo=UTC), datetime(2031, 8, 10, 22, tzinfo=UTC),
        )
        self.add_session(
            "Herbstlehrgang", School.GERETSRIED,
            datetime(2031, 9, 1, 8, tzinfo=UTC), datetime(2031, 9, 21, 19, tzinfo=UTC),
        )
        self.add_session(
            "Andere Schule", School.REGENSBURG,
            datetime(2031, 7, 11, 8, tzinfo=UTC), datetime(2031, 7, 25, 19, tzinfo=UTC),
        )

        response = self.client.get("/kalender/", {
            "view": "year", "year": "2031", "school": School.GERETSRIED,
        })

        june, july, august, september = response.context["months"][5:9]
        self.assertEqual((june["occupied_count"], june["occupancy_level"]), (0, 0))
        self.assertEqual(july["session_count"], 2)
        self.assertEqual((july["occupied_count"], july["occupancy_level"]), (1, 1))
        self.assertEqual(august["occupied_days"], list(range(3, 11)))
        self.assertEqual((august["occupied_count"], august["occupancy_level"]), (8, 3))
        self.assertEqual((september["occupied_count"], september["occupancy_level"]), (21, 5))
        self.assertContains(response, 'class="calendar-month-card" data-occupancy="0"')
        self.assertContains(response, 'class="calendar-month-card" data-occupancy="5"')
        self.assertContains(response, 'aria-label="Juli 2031, 2 Lehrgänge, 1 belegter Tag"')
        self.assertContains(response, "21 belegte Tage")
