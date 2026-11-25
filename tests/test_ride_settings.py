from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase

from bookings.models import RideConfiguration


SETTINGS_URL = "/fahrgemeinschaften/einstellungen/"


class RideSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.viewer = users.create_user("test-ride-viewer")
        cls.editor = users.create_user("test-ride-editor")
        permissions = Permission.objects.filter(content_type__app_label="accounts")
        cls.viewer.user_permissions.add(permissions.get(codename="view_rides"))
        cls.editor.user_permissions.add(permissions.get(codename="edit_rides"))

    def test_editor_sees_initial_fourteen_day_lead_time(self):
        self.client.force_login(self.editor)

        response = self.client.get(SETTINGS_URL)

        self.assertContains(response, "Vorlauf")
        self.assertEqual(response.context["form"]["lead_days"].value(), 14)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertFalse(RideConfiguration.objects.exists())

    def test_editor_can_save_zero_or_full_year_as_calendar_days(self):
        self.client.force_login(self.editor)

        for lead_days in (0, 365):
            with self.subTest(lead_days=lead_days):
                response = self.client.post(SETTINGS_URL, {"lead_days": lead_days})

                self.assertRedirects(response, f"{SETTINGS_URL}?gespeichert=1")
                self.assertIn("no-store", response.headers["Cache-Control"])
                current = self.client.get(SETTINGS_URL)
                self.assertEqual(current.context["form"]["lead_days"].value(), lead_days)
        self.assertEqual(RideConfiguration.objects.count(), 1)

    def test_invalid_lead_times_do_not_replace_saved_value(self):
        self.client.force_login(self.editor)
        self.client.post(SETTINGS_URL, {"lead_days": 23})

        for value in ("-1", "366", "23.5", "vierzehn", ""):
            with self.subTest(value=value):
                response = self.client.post(SETTINGS_URL, {"lead_days": value})

                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context["form"].has_error("lead_days"))
                current = self.client.get(SETTINGS_URL)
                self.assertEqual(current.context["form"]["lead_days"].value(), 23)

    def test_setting_requires_login_and_ride_edit_permission(self):
        response = self.client.get(SETTINGS_URL)
        self.assertRedirects(response, f"/anmelden/?next={SETTINGS_URL}", fetch_redirect_response=False)
        self.client.force_login(self.viewer)

        self.assertEqual(self.client.get(SETTINGS_URL).status_code, 403)
        self.assertEqual(self.client.post(SETTINGS_URL, {"lead_days": 1}).status_code, 403)

    def test_editor_cannot_save_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)

        response = client.post(SETTINGS_URL, {"lead_days": 1})

        self.assertEqual(response.status_code, 403)
        current = client.get(SETTINGS_URL)
        self.assertEqual(current.context["form"]["lead_days"].value(), 14)
