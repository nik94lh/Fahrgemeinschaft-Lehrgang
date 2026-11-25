from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bookings.models import RideCase, School
from tests.workspace_client import WorkspaceTestCase


class DashboardTests(WorkspaceTestCase):
    def make_suggestion(self, key, days, **extra_fields):
        start = timezone.now() + timedelta(days=days)
        end = start + timedelta(days=2)
        return RideCase.objects.create(
            match_key=key,
            school=School.WUERZBURG,
            direction=RideCase.ROUND_TRIP,
            travel_date=timezone.localdate(start),
            earliest=start,
            latest=start,
            return_date=timezone.localdate(end),
            return_earliest=end,
            return_latest=end,
            **extra_fields,
        )

    def test_ride_counts_match_the_open_vorschlaege_and_refresh_with_the_page(self):
        self.make_suggestion("dashboard-ready", 14)
        self.make_suggestion("dashboard-standby", 15)
        self.make_suggestion("dashboard-closed", 2, status=RideCase.Status.CLOSED)
        self.make_suggestion("dashboard-inactive", 3, currently_matches=False)
        self.make_suggestion("dashboard-started", -1)

        response = self.client.get(reverse("overview"), HTTP_X_BMS_LIVE="1")

        self.assertContains(response, "Standby")
        self.assertContains(response, "Bereit zur Vermittlung")
        self.assertEqual(response.context["ride_overview"], {"ready": 1, "standby": 1})
        self.assertEqual(RideCase.objects.count(), 5)

    def test_ride_metrics_respect_the_independent_view_grant(self):
        user = get_user_model().objects.create_user("dashboard-reader")
        user.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_overview"))
        self.client.force_login(user)

        response = self.client.get(reverse("overview"))

        self.assertIsNone(response.context["ride_overview"])
        self.assertNotContains(response, "Fahrgemeinschaftsvorschläge")

    def test_favicon_is_available_on_the_public_login(self):
        client = Client()
        icon_path = reverse("favicon")
        response = client.get(icon_path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertContains(client.get(reverse("login")), f'href="{icon_path}"')
