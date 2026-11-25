from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import include, path

from bookings.models import EmailEvent


def unclassified_view(request):
    return HttpResponse("Internal data")


urlpatterns = [
    path("zusatz/", unclassified_view, name="unclassified"),
    path("", include("config.urls")),
]


def eml_upload():
    return SimpleUploadedFile(
        "probe.eml",
        b"Subject: Lehrgangsanmeldung wurde bestaetigt\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Teilnehmer: Alex Beispiel\r\n"
        b"Lehrgangsart: Synthetischer Berechtigungstest\r\n"
        b"Schulungsort: SFS Wuerzburg\r\n"
        b"Lehrgangstermin: 25.05.2030 09:00 Uhr - 28.05.2030 15:00 Uhr\r\n"
        b"Buchungsreferenz: DEMO-ACCESS-1\r\n",
        content_type="message/rfc822",
    )


class WorkspaceAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.user = users.create_user("bearbeitung")
        cls.staff = users.create_user("mitarbeit", is_staff=True)
        cls.admin = users.create_superuser("verwaltung")
        cls.inactive = users.create_user("deaktiviert", is_active=False)

    def grant(self, user, *codenames):
        permissions = list(Permission.objects.filter(
            content_type__app_label="accounts", codename__in=codenames,
        ))
        self.assertEqual({permission.codename for permission in permissions}, set(codenames))
        user.user_permissions.add(*permissions)

    def test_anonymous_requests_to_workspace_require_login(self):
        for url in (
            "/", "/buchungen/", "/buchungen/1/teilnehmer/", "/kalender/", "/kalender/1/",
            "/fahrgemeinschaften/", "/fahrgemeinschaften/abgleichen/", "/fahrgemeinschaften/1/",
            "/feuerwehren/", "/feuerwehren/neu/", "/feuerwehren/1/", "/klaerung/", "/klaerung/1/",
            "/vorschau/", "/importieren/", "/testmail.eml", "/postfach/", "/mail-eingang/",
            "/mail-eingang/1/", "/mail-eingang/1/adresse-pruefen/",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], f"/anmelden/?next={url}")

    def test_anonymous_live_update_returns_unauthorized(self):
        response = self.client.get("/buchungen/", headers={"X-BMS-Live": "1"})

        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_neutral_health_and_asset_are_public_and_health_is_read_only(self):
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/assets/live.js").status_code, 200)
        self.assertEqual(self.client.post("/health/").status_code, 405)

    def test_home_link_respects_authentication_state_without_section_grants(self):
        self.assertEqual(self.client.get("/anmelden/").context["workspace_home"], "/anmelden/")

        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/konto/").context["workspace_home"], "/konto/")

    def test_authenticated_user_without_grants_cannot_open_workspace(self):
        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/").status_code, 403)
        self.assertEqual(self.client.get("/buchungen/1/teilnehmer/").status_code, 403)
        self.assertEqual(self.client.get("/testmail.eml").status_code, 403)
        self.assertEqual(self.client.get("/buchungen/", headers={"X-BMS-Live": "1"}).status_code, 403)

    def test_group_membership_does_not_grant_workspace_access(self):
        legacy_group = Group.objects.create(name="Frühere Buchungsrechte")
        legacy_group.permissions.add(Permission.objects.get(
            content_type__app_label="accounts", codename="edit_bookings",
        ))
        self.user.groups.add(legacy_group)
        self.client.force_login(self.user)

        self.assertEqual(self.client.get("/buchungen/").status_code, 403)

        self.grant(self.user, "view_bookings")
        response = self.client.get("/buchungen/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["workspace_edit"]["bookings"])

    def test_workspace_administration_is_superuser_only(self):
        self.grant(self.staff, "edit_overview")
        self.client.force_login(self.staff)

        self.assertEqual(self.client.get("/benutzer/").status_code, 403)
        self.assertEqual(self.client.post(f"/benutzer/{self.user.pk}/passwort/").status_code, 403)

        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/benutzer/").status_code, 200)

    def test_inactive_user_with_a_grant_cannot_open_workspace(self):
        self.grant(self.inactive, "edit_bookings")
        self.client.force_login(self.inactive)

        response = self.client.get("/buchungen/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/anmelden/?next=/buchungen/")

    @override_settings(ROOT_URLCONF=__name__)
    def test_new_route_without_access_policy_is_denied(self):
        self.client.force_login(self.admin)

        self.assertEqual(self.client.get("/zusatz/").status_code, 403)

    def test_edit_grant_includes_view_and_defines_landing_page(self):
        self.grant(self.user, "edit_bookings")
        self.client.force_login(self.user)

        response = self.client.get("/buchungen/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["workspace_view"]["bookings"])
        self.assertTrue(response.context["workspace_edit"]["bookings"])
        self.assertFalse(response.context["workspace_view"]["overview"])
        self.assertEqual(response.context["workspace_home"], "/buchungen/")

    def test_preview_viewer_can_parse_and_download_without_importing(self):
        self.grant(self.user, "view_preview")
        self.client.force_login(self.user)

        response = self.client.post("/vorschau/", {"file": eml_upload()})

        self.assertContains(response, "Synthetischer Berechtigungstest")
        self.assertEqual(self.client.get("/testmail.eml").status_code, 200)
        self.assertEqual(self.client.post("/importieren/", {"file": eml_upload()}).status_code, 403)
        self.assertFalse(EmailEvent.objects.exists())

    @override_settings(HTTPS=True)
    def test_view_grants_do_not_allow_mutations_or_direct_edit_forms(self):
        self.grant(self.user, *(
            "view_bookings", "view_rides", "view_brigades", "view_review", "view_preview", "view_mailbox",
            "view_mail_receipts",
        ))
        self.client.force_login(self.user)

        for url in (
            "/buchungen/1/teilnehmer/", "/fahrgemeinschaften/abgleichen/", "/fahrgemeinschaften/1/",
            "/feuerwehren/neu/", "/feuerwehren/1/", "/klaerung/1/", "/importieren/", "/postfach/",
            "/mail-eingang/1/adresse-pruefen/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, secure=True).status_code, 403)
        self.assertEqual(self.client.get("/feuerwehren/neu/").status_code, 403)
        self.assertEqual(self.client.get("/feuerwehren/1/").status_code, 403)

    @override_settings(HTTPS=True)
    def test_actions_affecting_bookings_require_that_edit_grant_too(self):
        self.grant(self.user, "edit_preview", "edit_review", "edit_mail_receipts", "edit_mailbox")
        self.client.force_login(self.user)

        for url, data in (
            ("/importieren/", {"file": eml_upload()}),
            ("/klaerung/1/", {"action": "reassign"}),
            ("/mail-eingang/1/adresse-pruefen/", {}),
            ("/postfach/", {"action": "start-contact-replay"}),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, data, secure=True).status_code, 403)
        self.assertFalse(EmailEvent.objects.exists())

        self.grant(self.user, "edit_bookings")
        response = self.client.post("/importieren/", {"file": eml_upload()}, secure=True)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmailEvent.objects.count(), 1)
