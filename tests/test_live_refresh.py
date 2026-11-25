from django.test import override_settings
from django.urls import reverse

from bookings.services import ingest
from tests.test_reconciliation import eml
from tests.workspace_client import WorkspaceTestCase


class LiveRefreshTests(WorkspaceTestCase):
    def test_local_script_is_available_with_javascript_content_type(self):
        response = self.client.get(reverse("live_script"))
        self.assertEqual(response.headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"X-BMS-Live", b"".join(response.streaming_content))

    def test_current_booking_filter_is_applied_to_the_live_fragment(self):
        url = reverse("bookings") + "?q=Live-Test"
        page = self.client.get(url)
        self.assertContains(page, reverse("live_script"))
        self.assertContains(page, "Hauptnavigation")
        self.assertIn("X-BMS-Live", page.headers["Vary"])
        self.assertIn("script-src 'self'; connect-src 'self'", page.headers["Content-Security-Policy"])

        ingest(eml("Lehrgangsgenehmigung", course="Live-Test-Lehrgang"))
        ingest(eml("Lehrgangsgenehmigung", course="Anderer Lehrgang"))
        fragment = self.client.get(url, HTTP_X_BMS_LIVE="1")

        self.assertContains(fragment, 'data-live-region="main"')
        self.assertContains(fragment, "Live-Test-Lehrgang")
        self.assertNotContains(fragment, "Anderer Lehrgang")
        self.assertNotContains(fragment, "Hauptnavigation")
        self.assertNotContains(fragment, "<script")
        self.assertIn("no-store", fragment.headers["Cache-Control"])

    @override_settings(HTTPS=True)
    def test_mailbox_forms_are_outside_the_live_regions(self):
        fragment = self.client.get(reverse("mailbox"), secure=True, HTTP_X_BMS_LIVE="1")

        self.assertContains(fragment, 'data-live-region="mailbox-status"')
        self.assertContains(fragment, 'data-live-region="contact-replay"')
        self.assertNotContains(fragment, 'data-live-region="main"')

    def test_eml_preview_keeps_its_script_free_policy(self):
        page = self.client.get(reverse("preview"))

        self.assertNotContains(page, reverse("live_script"))
        self.assertNotIn("script-src", page.headers["Content-Security-Policy"])
