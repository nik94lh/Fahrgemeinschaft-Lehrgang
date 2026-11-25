from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, RideCase, School
from bookings.rides_matching import sync_ride_cases
from bookings.services import ingest
from tests.test_reconciliation import eml
from tests.test_ride_cases import NOW
from tests.workspace_client import WorkspaceTestCase


class OnlineCourseTests(WorkspaceTestCase):
    def test_online_location_produces_a_confirmed_booking_without_a_physical_school(self):
        event = ingest(eml("Teilnahmebestätigung", school="Online", reference="ONLINE-1"))

        self.assertEqual(event.state, EmailEvent.State.APPLIED)
        self.assertEqual(event.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(event.seat.session.school, "online")
        self.assertTrue(event.seat.session.is_online)

    def test_bookings_and_calendar_include_online_courses_in_the_online_filter(self):
        start = timezone.localtime() + timedelta(days=2)
        end = start + timedelta(hours=3)
        online = CourseSession.objects.create(
            course=Course.objects.create(name="Musterlehrgang im Netz"), school="online",
            starts_at=start, ends_at=end,
        )
        online_with_school = CourseSession.objects.create(
            course=Course.objects.create(name="Musterlehrgang mit Veranstalter"), school=School.WUERZBURG,
            is_online=True, starts_at=start, ends_at=end,
        )
        onsite = CourseSession.objects.create(
            course=Course.objects.create(name="Musterlehrgang vor Ort"), school=School.WUERZBURG,
            starts_at=start, ends_at=end,
        )
        for session in (online, online_with_school, onsite):
            CourseSeat.objects.create(session=session, status=BookingStatus.CONFIRMED)

        booking_response = self.client.get(reverse("bookings"), {"school": "online"})
        calendar_response = self.client.get(reverse("calendar"), {
            "month": start.strftime("%Y-%m"), "school": "online",
        })
        for response in (booking_response, calendar_response):
            self.assertContains(response, "Musterlehrgang im Netz")
            self.assertContains(response, "Musterlehrgang mit Veranstalter")
            self.assertNotContains(response, "Musterlehrgang vor Ort")
            self.assertContains(response, "Online · SFS Würzburg")
            self.assertContains(response, 'value="online" selected')

    def test_online_bookings_do_not_join_or_create_ride_cases(self):
        onsite = [
            ingest(eml("Teilnahmebestätigung", participant=person)).seat
            for person in ("Alex Beispiel", "Sam Beispiel")
        ]
        online = [
            ingest(eml("Teilnahmebestätigung", participant=person, school=venue))
            for person, venue in (
                ("Jana Beispiel", "Online"),
                ("Toni Beispiel", "Online"),
            )
        ]
        self.assertEqual([event.state for event in online], [EmailEvent.State.APPLIED] * 2)
        online[0].seat.session.is_online = False
        online[0].seat.session.save(update_fields=["is_online"])
        online_at_school = CourseSession.objects.create(
            course=onsite[0].session.course, school=School.WUERZBURG, is_online=True,
            starts_at=onsite[0].session.starts_at, ends_at=onsite[0].session.ends_at,
        )
        CourseSeat.objects.create(
            session=online_at_school, participant=online[0].seat.participant, status=BookingStatus.CONFIRMED,
        )

        self.assertEqual(sync_ride_cases(now=NOW), 1)
        self.assertCountEqual(RideCase.objects.get().seats.all(), onsite)

    def test_next_ride_sync_retires_a_case_after_an_online_session_is_recognized(self):
        first = ingest(eml("Teilnahmebestätigung", reference="ONLINE-OLD"))
        ingest(eml("Teilnahmebestätigung", participant="Sam Beispiel"))
        self.assertEqual(sync_ride_cases(now=NOW), 1)

        session = first.seat.session
        session.is_online = True
        session.save(update_fields=["is_online"])

        self.assertEqual(sync_ride_cases(now=NOW), 0)
        self.assertFalse(RideCase.objects.get().currently_matches)

    def test_profile_does_not_label_online_and_in_person_bookings_as_duplicates(self):
        start = timezone.now() + timedelta(days=1)
        end = start + timedelta(hours=2)
        course = Course.objects.create(name="Lehrgang mit zwei Formaten")
        participant = Participant.objects.create(name="Alex Beispiel")
        seats = []
        for is_online in (False, True):
            session = CourseSession.objects.create(
                course=course, school=School.WUERZBURG, is_online=is_online, starts_at=start, ends_at=end,
            )
            seats.append(CourseSeat.objects.create(session=session, participant=participant))

        response = self.client.get(reverse("booking_participant", args=[seats[0].pk]))

        self.assertNotContains(response, "Möglicher Doppeleintrag")
        self.assertContains(response, "Zeitliche Überschneidung", count=2)


class OnlineCourseMigrationTests(TransactionTestCase):
    def test_marks_existing_explicit_online_titles_and_keeps_their_school(self):
        before = [("bookings", "0008_ride_round_trip")]
        after = [("bookings", "0009_course_session_online")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        old_apps = executor.migrate(before).apps
        old_course = old_apps.get_model("bookings", "Course")
        old_session = old_apps.get_model("bookings", "CourseSession")
        start = timezone.now()
        expected = {
            "Online-Lehrgang: Funk": True,
            "Webinar Motorkunde": True,
            "Atemschutz (Online)": True,
            "Online-Anmeldung zur Atemschutzübung": False,
            "Hybrid-Lehrgang (Online)": False,
            "Synthetischer Präsenzlehrgang": False,
        }
        for name in expected:
            old_session.objects.create(
                course=old_course.objects.create(name=name), school=School.WUERZBURG,
                starts_at=start, ends_at=start + timedelta(hours=1),
            )

        new_apps = MigrationExecutor(connection).migrate(after).apps
        records = new_apps.get_model("bookings", "CourseSession").objects.values_list(
            "course__name", "is_online", "school",
        )
        self.assertCountEqual(records, [(name, flag, "wuerzburg") for name, flag in expected.items()])
