from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, School


class PeopleOverviewTests(TestCase):
    url = "/personen/"

    @classmethod
    def setUpTestData(cls):
        cls.person = Participant.objects.create(name="Alex Beispiel", email="alex.profil@example.invalid")
        cls.other = Participant.objects.create(name="Mara Muster")
        starts_at = timezone.now() + timedelta(days=14)
        course = Course.objects.create(name="Künftiger Übungslehrgang")
        session = CourseSession.objects.create(
            course=course, school=School.REGENSBURG, starts_at=starts_at,
            ends_at=starts_at + timedelta(days=2),
        )
        cls.booking = CourseSeat.objects.create(
            participant=cls.person, session=session, status=BookingStatus.CONFIRMED,
        )
        old_session = CourseSession.objects.create(
            course=Course.objects.create(name="Früherer Übungslehrgang"), school=School.WUERZBURG,
            starts_at=starts_at - timedelta(days=30), ends_at=starts_at - timedelta(days=28),
        )
        cls.old_booking = CourseSeat.objects.create(
            participant=cls.person, session=old_session, status=BookingStatus.CONFIRMED,
        )
        CourseSeat.objects.create(participant=cls.other, session=old_session, status=BookingStatus.CANCELLED)

    def login_with(self, *codenames):
        user = get_user_model().objects.create_user(username="test-personen")
        permissions = Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames)
        self.assertEqual(permissions.count(), len(codenames))
        user.user_permissions.add(*permissions)
        self.client.force_login(user)

    def test_directory_requires_its_own_permission(self):
        self.assertRedirects(self.client.get(self.url), "/anmelden/?next=/personen/", fetch_redirect_response=False)
        self.login_with("view_bookings")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertNotContains(self.client.get(reverse("bookings")), f'href="{self.url}"')

    def test_searches_names_and_email_with_people_right_without_disclosing_bookings(self):
        self.login_with("view_people")

        response = self.client.get(self.url, {"q": "  ALEX.PROFIL  "})

        self.assertContains(response, self.person.name)
        self.assertContains(response, self.person.email)
        self.assertNotContains(response, self.other.name)
        self.assertNotContains(response, self.booking.session.course.name)
        booking_url = reverse("booking_participant", args=[self.booking.pk])
        self.assertNotContains(response, booking_url)
        self.assertContains(response, f'href="{self.url}" aria-current="page"')
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.get(booking_url).status_code, 403)

        by_name = self.client.get(self.url, {"q": "muster"})
        self.assertContains(by_name, self.other.name)
        self.assertNotContains(by_name, self.person.name)

    def test_people_and_booking_right_show_booking_count_and_relevant_profile_link(self):
        self.login_with("view_people", "view_bookings")

        response = self.client.get(self.url)

        self.assertContains(response, '<td data-label="Buchungen">2</td>', html=True)
        self.assertContains(response, self.booking.session.course.name)
        self.assertContains(response, "SFS Regensburg")
        self.assertContains(response, "Bestätigt")
        self.assertContains(response, self.old_booking.session.course.name)
        self.assertContains(response, "Storniert")
        booking_url = reverse("booking_participant", args=[self.booking.pk])
        self.assertContains(response, f'href="{booking_url}"')
        self.assertContains(self.client.get(booking_url), "Lehrgangsbuchungen dieses Profils")

    def test_search_paginates_without_losing_the_filter(self):
        self.login_with("view_people")
        Participant.objects.bulk_create(
            Participant(name=f"Übung {index:02d}") for index in range(26)
        )

        first_page = self.client.get(self.url, {"q": "Übung"})
        second_page = self.client.get(self.url, {"q": "Übung", "page": "2"})

        self.assertContains(first_page, "?q=%C3%9Cbung&amp;page=2")
        self.assertContains(first_page, "Übung 00")
        self.assertNotContains(first_page, "Übung 25")
        self.assertContains(second_page, "Übung 25")
        self.assertNotContains(second_page, "Übung 00")
        self.assertNotContains(second_page, self.person.name)

    def test_directory_is_read_only(self):
        self.login_with("view_people", "edit_bookings")

        response = self.client.post(self.url, {"name": "Anderer Name"})

        self.assertEqual(response.status_code, 405)
        self.person.refresh_from_db()
        self.assertEqual(self.person.name, "Alex Beispiel")
