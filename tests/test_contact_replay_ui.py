from unittest.mock import patch

from django.test import override_settings

from mailimport.contact_replay import ContactReplayError
from mailimport.models import ContactReplayJob
from tests.workspace_client import WorkspaceTestCase


@override_settings(HTTPS=True)
class ContactReplayViewTests(WorkspaceTestCase):
    def test_secure_mailbox_page_exposes_status_and_counts(self):
        ContactReplayJob.objects.create(
            status=ContactReplayJob.Status.RUNNING, source_id="1" * 64,
            uid_validity=22, upper_uid=50, last_uid=25, scanned_count=25, enriched_count=2,
        )

        response = self.client.get("/postfach/", secure=True)

        self.assertContains(response, "Einmaliger Adressabgleich")
        self.assertContains(response, "<dt>Geprüfte Nachrichten</dt><dd>25</dd>")
        self.assertContains(response, "<dt>Ergänzte Adressen</dt><dd>2</dd>")
        self.assertContains(response, "Für 25 geprüfte Nachrichten wurden noch keine Einzelgründe erfasst.")
        self.assertNotContains(response, "Adressabgleich starten")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_mailbox_shows_diagnostic_counts_for_a_completed_job(self):
        ContactReplayJob.objects.create(
            status=ContactReplayJob.Status.COMPLETED, source_id="1" * 64,
            uid_validity=22, upper_uid=8, last_uid=8, scanned_count=8, enriched_count=1,
            outcome_counts={"ADDED": 1, "NO_ADDRESS": 4, "NO_APPLIED_EVENT": 3},
        )

        response = self.client.get("/postfach/", secure=True)

        self.assertContains(response, "Nachrichten ohne Adressnachtrag")
        self.assertContains(response, "<dt>Keine eindeutig passende Teilnehmeradresse erkannt</dt><dd>4</dd>")
        self.assertContains(response, "<dt>Kein bereits angewendetes Buchungsereignis</dt><dd>3</dd>")
        self.assertNotContains(response, "noch keine Einzelgründe")

    def test_manual_request_is_protected_and_calls_the_queue_only(self):
        payload = {"action": "start-contact-replay"}
        with patch("mailimport.mailbox_views.queue_contact_replay") as queue:
            denied = self.authenticated_client(enforce_csrf_checks=True).post("/postfach/", payload, secure=True)
            insecure = self.client.post("/postfach/", payload)
            response = self.client.post("/postfach/", payload, secure=True)

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(insecure.status_code, 404)
        self.assertRedirects(response, "/postfach/", fetch_redirect_response=False)
        queue.assert_called_once_with()

    def test_unavailable_mailbox_has_a_readable_feedback(self):
        with patch(
            "mailimport.mailbox_views.queue_contact_replay",
            side_effect=ContactReplayError("Warte zuerst auf einen erfolgreichen Import aus diesem Postfach."),
        ):
            response = self.client.post("/postfach/", {"action": "start-contact-replay"}, secure=True)

        self.assertContains(response, "Warte zuerst auf einen erfolgreichen Import", status_code=400)
