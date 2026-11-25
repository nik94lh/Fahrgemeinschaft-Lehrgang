from datetime import UTC, date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.utils import timezone

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
        return CourseSession.objects.create(
            course=Course.objects.create(name=name), school=school, starts_at=start, ends_at=end,
        )

    def test_appointment_list_includes_only_sessions_overlapping_the_selected_local_month(self):
        response = self.client.get("/kalender/", {"month": "2031-05"})

        self.assertContains(response, "Mai 2031")
        self.assertContains(response, "Übergreifender Musterlehrgang")
        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertEqual([session.course.name for session in response.context["sessions"]], [
            "Übergreifender Musterlehrgang", "Musterlehrgang Regensburg",
        ])
        self.assertContains(response, "Terminliste mit Uhrzeiten")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_calendar_filter_and_navigation_keep_the_school(self):
        response = self.client.get("/kalender/", {"month": "2031-05", "school": School.WUERZBURG})

        self.assertContains(response, "Übergreifender Musterlehrgang")
        self.assertNotContains(response, "Musterlehrgang Regensburg")
        self.assertContains(response, "?month=2031-04&amp;school=wuerzburg")
        self.assertContains(response, "?month=2031-06&amp;school=wuerzburg")
        self.assertIn("timeline_groups", response.context)
        self.assertEqual({group["school"] for group in response.context["timeline_groups"]}, {School.WUERZBURG})

    def test_week_and_month_bars_show_distinct_people_per_appointment(self):
        session = CourseSession.objects.get(course__name="Musterlehrgang Regensburg")
        other_session = CourseSession.objects.create(
            course=session.course, school=session.school,
            starts_at=session.starts_at + timedelta(days=1), ends_at=session.ends_at + timedelta(days=1),
        )
        first = Participant.objects.create(name="Synthetische Testperson")
        same_name = Participant.objects.create(name=first.name)
        cancelled = Participant.objects.create(name="Ausgeschiedene Testperson")
        for participant, status in (
            (first, BookingStatus.CONFIRMED),
            (first, BookingStatus.CONFIRMED),
            (same_name, BookingStatus.APPROVED),
            (cancelled, BookingStatus.CANCELLED),
            (None, BookingStatus.CONFIRMED),
        ):
            CourseSeat.objects.create(session=session, participant=participant, status=status)
        CourseSeat.objects.create(session=other_session, participant=first, status=BookingStatus.CONFIRMED)

        for params in ({"month": "2031-05"}, {"view": "week", "day": "2031-05-12"},
                       {"month": "2031-05", "range": "2031-06"}):
            with self.subTest(params=params):
                response = self.client.get("/kalender/", params)

                self.assertContains(response, '<span class="calendar-participant-count">2x</span>', count=1)
                self.assertContains(response, "Musterlehrgang Regensburg, 2 Personen, SFS Regensburg", count=1)
                self.assertNotContains(response, first.name)
                self.assertNotContains(response, cancelled.name)

    def test_invalid_month_falls_back_to_the_current_month(self):
        default_response = self.client.get("/kalender/")
        invalid_response = self.client.get("/kalender/", {"month": "2031-13"})

        self.assertContains(default_response, "Lehrgangskalender")
        self.assertContains(invalid_response, "Lehrgangskalender")
        self.assertEqual(invalid_response.context["month_id"], default_response.context["month_id"])

    def test_school_filter_without_courses_keeps_a_single_continuous_axis(self):
        response = self.client.get("/kalender/", {"month": "2031-05", "school": School.GERETSRIED})

        self.assertContains(response, "Keine Lehrgänge in diesem Zeitraum gefunden.")
        self.assertNotContains(response, "Musterlehrgang Regensburg")
        self.assertNotContains(response, "Übergreifender Musterlehrgang")
        self.assertIn("timeline_days", response.context)
        days = response.context["timeline_days"]
        self.assertEqual([item["date"] for item in days], [date(2031, 3, 1) + timedelta(days=n) for n in range(153)])
        self.assertEqual(response.context["timeline_day_count"], 153)
        self.assertEqual(response.context["timeline_anchor"], date(2031, 5, 1))
        self.assertContains(response, 'id="calendar-month-timeline"', count=1)
        self.assertContains(response, '<time datetime="2031-05-01"', count=1)
        self.assertEqual(response.context["timeline_groups"], [])
        self.assertEqual(response.context["timeline_center"], "2031-05")
        self.assertTrue(response.context["timeline_can_extend_before"])
        self.assertTrue(response.context["timeline_can_extend_after"])
        self.assertEqual(response.context["timeline_months"], [
            {"start": date(2031, 3, 1), "start_column": 1, "end_column": 32},
            {"start": date(2031, 4, 1), "start_column": 32, "end_column": 62},
            {"start": date(2031, 5, 1), "start_column": 62, "end_column": 93},
            {"start": date(2031, 6, 1), "start_column": 93, "end_column": 123},
            {"start": date(2031, 7, 1), "start_column": 123, "end_column": 154},
        ])
        self.assertContains(response, '?month=2031-06&amp;school=geretsried')

    def test_timeline_uses_berlin_days_and_clips_courses_only_at_loaded_range_limits(self):
        self.add_session(
            "Abschluss um Mitternacht", School.WUERZBURG,
            datetime(2031, 5, 13, 18, tzinfo=UTC), datetime(2031, 5, 13, 22, tzinfo=UTC),
        )
        self.add_session(
            "Monatswechsel am Ende", School.WUERZBURG,
            datetime(2031, 5, 30, 15, tzinfo=UTC), datetime(2031, 6, 2, 10, tzinfo=UTC),
        )
        self.add_session(
            "Beginn vor dem Ladebereich", School.WUERZBURG,
            datetime(2031, 2, 28, 18, tzinfo=UTC), datetime(2031, 3, 1, 14, tzinfo=UTC),
        )
        self.add_session(
            "Ende nach dem Ladebereich", School.WUERZBURG,
            datetime(2031, 7, 31, 18, tzinfo=UTC), datetime(2031, 8, 1, 14, tzinfo=UTC),
        )

        with timezone.override("UTC"):
            response = self.client.get("/kalender/", {"month": "2031-05"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("timeline_days", response.context)
        days = response.context["timeline_days"]
        bars = {
            bar["session"].course.name: (
                days[bar["start_column"] - 1]["date"],
                days[bar["end_column"] - 2]["date"],
                bar["starts_before"], bar["ends_after"],
            )
            for group in response.context["timeline_groups"]
            for bar in group["bars"]
        }
        self.assertEqual(bars, {
            "Beginn vor dem Ladebereich": (date(2031, 3, 1), date(2031, 3, 1), True, False),
            "Übergreifender Musterlehrgang": (date(2031, 4, 30), date(2031, 5, 2), False, False),
            "Musterlehrgang April": (date(2031, 4, 30), date(2031, 4, 30), False, False),
            "Musterlehrgang Regensburg": (date(2031, 5, 12), date(2031, 5, 12), False, False),
            "Abschluss um Mitternacht": (date(2031, 5, 13), date(2031, 5, 13), False, False),
            "Monatswechsel am Ende": (date(2031, 5, 30), date(2031, 6, 2), False, False),
            "Musterlehrgang Juni": (date(2031, 6, 1), date(2031, 6, 1), False, False),
            "Ende nach dem Ladebereich": (date(2031, 7, 31), date(2031, 7, 31), False, True),
        })

    def test_timeline_bar_detail_links_keep_the_school_and_open_the_relevant_month(self):
        session = self.add_session(
            "Lehrgang über das Wochenende", School.WUERZBURG,
            datetime(2031, 5, 9, 8, tzinfo=UTC), datetime(2031, 5, 14, 14, tzinfo=UTC),
        )

        response = self.client.get("/kalender/", {"month": "2031-05", "school": School.WUERZBURG})

        self.assertIn("timeline_groups", response.context)
        bars = response.context["timeline_groups"][0]["bars"]
        self.assertEqual([bar["detail_query"] for bar in bars if bar["session"] == session], [
            "month=2031-05&school=wuerzburg",
        ])
        self.assertContains(response, f'href="/kalender/{session.pk}/?month=2031-05&amp;school=wuerzburg"', count=2)
        self.assertEqual({bar["session"].course.name: bar["detail_query"] for bar in bars}, {
            "Übergreifender Musterlehrgang": "month=2031-05&school=wuerzburg",
            "Musterlehrgang April": "month=2031-04&school=wuerzburg",
            "Lehrgang über das Wochenende": "month=2031-05&school=wuerzburg",
            "Musterlehrgang Juni": "month=2031-06&school=wuerzburg",
        })

    def test_timeline_does_not_split_at_week_boundaries_and_reuses_lanes_for_disjoint_bars(self):
        over_weekend = self.add_session(
            "Lehrgang über das Wochenende", School.WUERZBURG,
            datetime(2031, 5, 9, 8, tzinfo=UTC), datetime(2031, 5, 14, 14, tzinfo=UTC),
        )
        tuesday = self.add_session(
            "Kurs am Dienstag", School.WUERZBURG,
            datetime(2031, 5, 13, 8, tzinfo=UTC), datetime(2031, 5, 13, 10, tzinfo=UTC),
        )
        monday = self.add_session(
            "Kurs am Montag", School.WUERZBURG,
            datetime(2031, 5, 12, 12, tzinfo=UTC), datetime(2031, 5, 12, 13, tzinfo=UTC),
        )
        ends_sunday = self.add_session(
            "Ende um Mitternacht zum Wochenwechsel", School.WUERZBURG,
            datetime(2031, 5, 24, 8, tzinfo=UTC), datetime(2031, 5, 25, 22, tzinfo=UTC),
        )
        regensburg = CourseSession.objects.get(course__name="Musterlehrgang Regensburg")

        response = self.client.get("/kalender/", {"month": "2031-05"})

        self.assertIn("timeline_groups", response.context)
        groups = response.context["timeline_groups"]
        self.assertEqual([(group["school"], group["lane_count"]) for group in groups], [
            (School.WUERZBURG, 2), (School.REGENSBURG, 1),
        ])
        days = response.context["timeline_days"]
        bars = {
            bar["session"].pk: (
                days[bar["start_column"] - 1]["date"], days[bar["end_column"] - 2]["date"], bar["lane"],
            )
            for group in groups for bar in group["bars"]
        }
        self.assertEqual(len([bar for group in groups for bar in group["bars"] if bar["session"] == over_weekend]), 1)
        self.assertEqual(bars[over_weekend.pk], (date(2031, 5, 9), date(2031, 5, 14), 1))
        self.assertEqual(bars[monday.pk], (date(2031, 5, 12), date(2031, 5, 12), 2))
        self.assertEqual(bars[tuesday.pk], (date(2031, 5, 13), date(2031, 5, 13), 2))
        self.assertEqual(bars[ends_sunday.pk], (date(2031, 5, 24), date(2031, 5, 25), 1))
        self.assertEqual(bars[regensburg.pk], (date(2031, 5, 12), date(2031, 5, 12), 1))

    def test_range_can_move_without_changing_the_selected_month_or_appointment_list(self):
        upcoming = self.add_session(
            "Termin im angeforderten Ladebereich", School.REGENSBURG,
            datetime(2032, 1, 30, 9, tzinfo=UTC), datetime(2032, 1, 30, 13, tzinfo=UTC),
        )
        self.add_session(
            "Termin nach dem angeforderten Ladebereich", School.REGENSBURG,
            datetime(2032, 1, 31, 23, tzinfo=UTC), datetime(2032, 2, 1, 12, tzinfo=UTC),
        )
        response = self.client.get("/kalender/", {
            "month": "2031-05", "range": "2031-11", "school": School.REGENSBURG,
        }, headers={"X-BMS-Live": "1"})

        self.assertEqual(response.context["month_id"], "2031-05")
        self.assertEqual(response.context["week_query"], "view=week&day=2031-05-12&school=regensburg")
        self.assertEqual([session.course.name for session in response.context["sessions"]], ["Musterlehrgang Regensburg"])
        self.assertIn("timeline_center", response.context)
        self.assertEqual(response.context["timeline_center"], "2031-11")
        self.assertEqual(response.context["timeline_anchor"], date(2031, 11, 1))
        self.assertEqual(response.context["timeline_days"][0]["date"], date(2031, 9, 1))
        self.assertEqual(response.context["timeline_days"][-1]["date"], date(2032, 1, 31))
        bars = [bar for group in response.context["timeline_groups"] for bar in group["bars"]]
        self.assertEqual([bar["session"] for bar in bars], [upcoming])
        self.assertEqual(bars[0]["detail_query"], "month=2032-01&school=regensburg")
        self.assertContains(response, f'href="/kalender/{upcoming.pk}/?month=2032-01&amp;school=regensburg"', count=1)

    def test_selected_month_day_one_remains_the_anchor_inside_loaded_range(self):
        response = self.client.get("/kalender/", {"month": "2031-06", "range": "2031-05"})

        self.assertIn("timeline_anchor", response.context)
        self.assertEqual(response.context["timeline_anchor"], date(2031, 6, 1))
        self.assertEqual(response.context["timeline_center"], "2031-05")
        self.assertEqual([session.course.name for session in response.context["sessions"]], ["Musterlehrgang Juni"])

    def test_invalid_range_falls_back_to_the_selected_month(self):
        for value in ("2032-2", "2032-W09", "2032-13", "0001-10", "9999-01"):
            with self.subTest(range=value):
                response = self.client.get("/kalender/", {"month": "2031-05", "range": value})
                self.assertIn("timeline_center", response.context)
                self.assertEqual(response.context["timeline_center"], "2031-05")
                self.assertEqual(response.context["timeline_anchor"], date(2031, 5, 1))

    def test_loaded_axis_includes_leap_day_and_crosses_year_boundary(self):
        response = self.client.get("/kalender/", {"month": "2032-02"})

        self.assertIn("timeline_days", response.context)
        days = {item["date"]: item for item in response.context["timeline_days"]}
        self.assertEqual(days[date(2032, 2, 29)]["number"], 29)
        self.assertTrue(days[date(2032, 2, 29)]["is_weekend"])
        self.assertFalse(days[date(2032, 2, 29)]["is_month_start"])
        self.assertTrue(days[date(2032, 1, 1)]["is_month_start"])
        self.assertEqual(list(days)[30:32], [date(2031, 12, 31), date(2032, 1, 1)])

    def test_loaded_axis_is_clipped_to_selectable_years(self):
        cases = (
            ("0002-01", date(2, 1, 1), date(2, 3, 31), False, True),
            ("9998-12", date(9998, 10, 1), date(9998, 12, 31), True, False),
        )
        for month, start, end, before, after in cases:
            with self.subTest(month=month):
                response = self.client.get("/kalender/", {"month": month})
                self.assertIn("timeline_days", response.context)
                self.assertEqual(response.context["timeline_days"][0]["date"], start)
                self.assertEqual(response.context["timeline_days"][-1]["date"], end)
                self.assertEqual(response.context["timeline_can_extend_before"], before)
                self.assertEqual(response.context["timeline_can_extend_after"], after)

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
        seat = CourseSeat.objects.create(
            session=session,
            participant=Participant.objects.create(name="Jana Kalenderbeispiel"),
            status=BookingStatus.CONFIRMED,
        )

        response = self.client.get(f"/kalender/{session.pk}/", {
            "view": "week", "day": "2031-05-14", "school": School.REGENSBURG
        })

        self.assertContains(response, "Musterlehrgang Regensburg")
        self.assertContains(response, "Jana Kalenderbeispiel")
        self.assertContains(response, f"Buchungs-ID {seat.pk}")
        self.assertContains(response, "Bestätigt")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(
            response.context["back_href"],
            "/kalender/?view=week&day=2031-05-12&school=regensburg",
        )
        self.assertEqual(self.client.post(f"/kalender/{session.pk}/").status_code, 405)

        viewer = get_user_model().objects.create_user("kalender-ansicht")
        viewer.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_calendar"))
        self.client.force_login(viewer)
        calendar_only = self.client.get(f"/kalender/{session.pk}/")
        self.assertNotContains(calendar_only, f"Buchungs-ID {seat.pk}")

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
        self.assertIn("timeline_days", response.context)
        anchor_day = next(item for item in response.context["timeline_days"] if item["date"] == date(2031, 5, 1))
        self.assertEqual(anchor_day["number"], 1)
        self.assertTrue(anchor_day["is_month_start"])
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
