from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from bookings.models import (
    BookingStatus,
    Course,
    CourseSeat,
    CourseSession,
    EmailEvent,
    Participant,
    School,
)
from tests.workspace_client import WorkspaceTestCase


class DashboardTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        upcoming = timezone.now() + timedelta(days=14)
        course = Course.objects.create(name="Synthetischer Übungslehrgang")
        cls.wuerzburg = CourseSession.objects.create(
            course=course,
            school=School.WUERZBURG,
            starts_at=upcoming,
            ends_at=upcoming + timedelta(hours=5),
        )
        regensburg = CourseSession.objects.create(
            course=course,
            school=School.REGENSBURG,
            starts_at=upcoming + timedelta(days=1),
            ends_at=upcoming + timedelta(days=1, hours=5),
        )
        CourseSeat.objects.create(
            session=cls.wuerzburg,
            participant=Participant.objects.create(name="Mara Musterfall"),
            status=BookingStatus.CONFIRMED,
        )
        cls.needs_review = CourseSeat.objects.create(
            session=cls.wuerzburg,
            participant=Participant.objects.create(name="Noah Klärtest"),
            status=BookingStatus.NEEDS_REVIEW,
        )
        CourseSeat.objects.create(
            session=regensburg,
            participant=Participant.objects.create(name="Alina Regensburgtest"),
            status=BookingStatus.CONFIRMED,
        )

    def test_overview_links_to_the_operational_pages(self):
        response = self.client.get("/")

        self.assertContains(response, 'href="/buchungen/"')
        self.assertContains(response, 'href="/klaerung/"')
        self.assertContains(response, 'href="/vorschau/"')
        self.assertContains(response, "Bestätigt")
        self.assertContains(response, "Klärungsbedarf")
        self.assertEqual(response.context["seats"], {"total": 3, "confirmed": 2, "pending": 0})

    def test_booking_list_shows_all_matching_seats(self):
        response = self.client.get("/buchungen/")

        self.assertContains(response, "Synthetischer Übungslehrgang")
        self.assertContains(response, "Mara Musterfall")
        self.assertContains(response, "Noah Klärtest")
        self.assertContains(response, "Alina Regensburgtest")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_booking_list_combines_status_and_school_filters(self):
        response = self.client.get(
            "/buchungen/", {"status": BookingStatus.CONFIRMED, "school": School.WUERZBURG}
        )

        self.assertContains(response, "Mara Musterfall")
        self.assertNotContains(response, "Noah Klärtest")
        self.assertNotContains(response, "Alina Regensburgtest")

    def test_booking_list_search_matches_participant_name(self):
        response = self.client.get("/buchungen/", {"q": "musterfall"})

        self.assertContains(response, "Mara Musterfall")
        self.assertNotContains(response, "Noah Klärtest")
        self.assertNotContains(response, "Alina Regensburgtest")

    def test_booking_list_escapes_participant_name(self):
        CourseSeat.objects.create(
            session=self.wuerzburg,
            participant=Participant.objects.create(name="<script>Testname</script>"),
        )

        response = self.client.get("/buchungen/")

        self.assertContains(response, "&lt;script&gt;Testname&lt;/script&gt;")
        self.assertNotContains(response, "<script>Testname</script>")

    def test_review_list_shows_unresolved_events(self):
        EmailEvent.objects.create(
            sha256="a" * 64,
            kind=EmailEvent.Kind.REASSIGNMENT,
            state=EmailEvent.State.NEEDS_REVIEW,
            seat=self.needs_review,
        )
        EmailEvent.objects.create(
            sha256="b" * 64,
            kind=EmailEvent.Kind.REMINDER,
            state=EmailEvent.State.APPLIED,
        )

        response = self.client.get("/klaerung/")

        self.assertContains(response, "Klärungsfälle")
        self.assertContains(response, "Ummeldung")
        self.assertNotContains(response, "Erinnerung")

    def test_manual_preview_is_still_accessible(self):
        response = self.client.get("/vorschau/")

        self.assertContains(response, 'enctype="multipart/form-data"')


class CoursePeriodTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = datetime(2027, 5, 1, tzinfo=UTC)
        cls.starts_at = datetime(2027, 5, 10, 7, 45, tzinfo=UTC)
        cls.ends_at = datetime(2027, 5, 14, 13, tzinfo=UTC)
        cls.session = CourseSession.objects.create(
            course=Course.objects.create(name="Synthetischer Lehrgang mit Zeitraum"),
            school=School.WUERZBURG,
            starts_at=cls.starts_at,
            ends_at=cls.ends_at,
        )
        for name in ("Kim Testperson", "Leo Beispielperson"):
            CourseSeat.objects.create(session=cls.session, participant=Participant.objects.create(name=name))

    def test_overview_lists_a_session_once_with_its_calendar_link_and_period(self):
        with patch("bookings.views.timezone", wraps=timezone) as view_timezone:
            view_timezone.now.return_value = self.now
            response = self.client.get(reverse("overview"))

        self.assertContains(response, "Anstehende Lehrgänge")
        self.assertContains(response, "Synthetischer Lehrgang mit Zeitraum", count=1)
        self.assertContains(response, "SFS Würzburg")
        self.assertContains(response, f'href="{reverse("calendar_detail", args=[self.session.pk])}"', count=1)
        self.assertContains(response, 'datetime="2027-05-10T09:45:00+02:00"')
        self.assertContains(response, 'datetime="2027-05-14T15:00:00+02:00"')

    def test_overview_limits_upcoming_sessions_with_stable_order(self):
        CourseSession.objects.create(
            course=self.session.course,
            school=School.GERETSRIED,
            starts_at=self.now - timedelta(days=2),
            ends_at=self.now - timedelta(days=1),
        )
        expected = [self.session]
        for position in range(7):
            created = CourseSession.objects.create(
                course=self.session.course,
                school=School.REGENSBURG,
                starts_at=self.starts_at + timedelta(days=position),
                ends_at=self.ends_at + timedelta(days=position),
            )
            expected.append(created)

        with patch("bookings.views.timezone", wraps=timezone) as view_timezone:
            view_timezone.now.return_value = self.now
            response = self.client.get(reverse("overview"))

        self.assertEqual(list(response.context["upcoming_sessions"]), expected[:6])

    def test_bookings_show_both_ends_of_a_period_in_berlin_time(self):
        response = self.client.get(reverse("bookings"))

        self.assertContains(response, '<th scope="col">Zeitraum</th>', count=1)
        self.assertContains(response, "10.05.2027, 09:45 Uhr", count=2)
        self.assertContains(response, "14.05.2027, 15:00 Uhr", count=2)
