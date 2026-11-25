from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings

from tests.workspace_client import WorkspaceTestCase


class WorkspaceNavigationTests(WorkspaceTestCase):
    def test_desktop_primary_links_and_secondary_disclosure_keep_existing_routes(self):
        response = self.client.get("/konto/")

        html = response.content.decode()
        desktop = html.split('class="workspace-nav-desktop"', 1)[-1].split('class="workspace-nav-mobile"', 1)[0]
        self.assertRegex(desktop, r"(?s)>Übersicht<.*>Buchungen<.*>Personen<.*>Kalender<.*>Fahrgemeinschaften<.*>Klärung<")
        self.assertRegex(desktop, r"(?s)<summary[^>]*>Weitere</summary>.*href=\"/feuerwehren/\".*href=\"/benutzer/\"")
        self.assertContains(response, '>Navigation</summary>')
        self.assertContains(response, '>Mein Konto</summary>')
        self.assertContains(response, 'href="/konto/passwort/"')

    def test_current_section_is_marked_in_both_responsive_lists(self):
        response = self.client.get("/kalender/")

        self.assertContains(response, 'href="/kalender/" aria-current="page"', count=2)
        self.assertContains(response, '<strong class="workspace-nav-current">Kalender</strong>', html=True)

    def test_account_menu_contains_a_working_csrf_logout(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.workspace_admin)
        page = client.get("/konto/")

        self.assertContains(page, 'method="post" action="/abmelden/"')
        token = client.cookies["csrftoken"].value
        result = client.post("/abmelden/", {"csrfmiddlewaretoken": token})

        self.assertRedirects(result, "/anmelden/")

    def test_live_response_replaces_content_without_reintroducing_navigation(self):
        response = self.client.get("/kalender/", headers={"X-BMS-Live": "1"})

        self.assertContains(response, '<main id="main-content"')
        self.assertNotContains(response, 'class="workspace-nav-desktop"')
        self.assertNotContains(response, 'id="live-update-status"')


class WorkspaceNavigationPermissionsTests(TestCase):
    def sign_in_with(self, *permissions):
        user = get_user_model().objects.create_user("navigation-test")
        user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="accounts", codename__in=permissions,
        ))
        self.client.force_login(user)

    def test_no_secondary_disclosure_when_no_secondary_section_is_allowed(self):
        self.sign_in_with("view_calendar")

        response = self.client.get("/konto/")

        self.assertContains(response, 'href="/kalender/"')
        self.assertNotContains(response, '>Weitere</summary>')
        self.assertNotContains(response, 'href="/buchungen/"')
        self.assertNotContains(response, 'href="/benutzer/"')

    @override_settings(HTTPS=True)
    def test_secure_secondary_links_are_shown_only_for_allowed_https_request(self):
        self.sign_in_with("view_calendar", "view_mailbox")

        insecure = self.client.get("/konto/")
        secure = self.client.get("/konto/", secure=True)

        self.assertNotContains(insecure, 'href="/postfach/"')
        self.assertContains(secure, 'href="/postfach/"', count=2)
        self.assertNotContains(secure, 'href="/mail-eingang/"')

    def test_public_login_still_has_version_without_account_or_workspace_navigation(self):
        response = self.client.get("/anmelden/")

        self.assertContains(response, "Testbetrieb")
        self.assertContains(response, 'class="version"')
        self.assertNotContains(response, 'aria-label="Hauptnavigation"')
        self.assertNotContains(response, '>Mein Konto</summary>')
