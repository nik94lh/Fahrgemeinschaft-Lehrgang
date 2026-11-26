from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase

from bookings.models import RideConfiguration


SETTINGS_URL = "/einstellungen/"
LEGACY_URL = "/fahrgemeinschaften/einstellungen/"


class RideSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.viewer = users.create_user("test-ride-viewer")
        cls.editor = users.create_user("test-ride-editor")
        cls.admin = users.create_superuser("test-ride-admin")
        permissions = Permission.objects.filter(content_type__app_label="accounts")
        cls.viewer.user_permissions.add(permissions.get(codename="view_rides"))
        cls.editor.user_permissions.add(permissions.get(codename="edit_rides"))

    def test_editor_sees_initial_fourteen_day_lead_time(self):
        self.client.force_login(self.editor)

        response = self.client.get(SETTINGS_URL)

        self.assertContains(response, "Einstellungen")
        self.assertContains(response, "Vermittlungsvorlauf")
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

        for path in (SETTINGS_URL, LEGACY_URL):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
                self.assertEqual(self.client.post(path, {"lead_days": 1}).status_code, 403)
        self.assertNotContains(self.client.get("/konto/"), f'href="{SETTINGS_URL}"')

    def test_editor_and_administrator_see_settings_in_workspace_navigation(self):
        for user in (self.editor, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)

                settings_page = self.client.get(SETTINGS_URL)
                ride_list = self.client.get("/fahrgemeinschaften/")

                self.assertContains(settings_page, f'href="{SETTINGS_URL}" aria-current="page"', count=2)
                self.assertContains(settings_page, '<strong class="workspace-nav-current">Einstellungen</strong>', html=True)
                self.assertContains(ride_list, f'href="{SETTINGS_URL}"', count=2)
                self.assertNotContains(ride_list, f'href="{LEGACY_URL}"')

    def test_legacy_bookmarks_and_post_forms_continue_at_canonical_settings(self):
        self.client.force_login(self.editor)

        bookmarked = self.client.get(f"{LEGACY_URL}?gespeichert=1")
        self.assertRedirects(bookmarked, f"{SETTINGS_URL}?gespeichert=1")
        saved = self.client.post(LEGACY_URL, {"lead_days": 27})

        self.assertRedirects(saved, f"{SETTINGS_URL}?gespeichert=1")
        self.assertEqual(RideConfiguration.objects.get(pk=1).lead_days, 27)

    def test_editor_cannot_save_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)

        for path in (SETTINGS_URL, LEGACY_URL):
            with self.subTest(path=path):
                response = client.post(path, {"lead_days": 1})
                self.assertEqual(response.status_code, 403)

        current = client.get(SETTINGS_URL)
        self.assertEqual(current.context["form"]["lead_days"].value(), 14)
