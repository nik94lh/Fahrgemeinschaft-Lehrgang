from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from mailimport.contact_replay import ContactReplayError
from mailimport.models import ContactReplayJob


@override_settings(HTTPS=True)
class ContactReplayViewTests(TestCase):
    def test_secure_mailbox_page_exposes_status_and_counts(self):
        ContactReplayJob.objects.create(
            status=ContactReplayJob.Status.RUNNING, source_id="1" * 64,
            uid_validity=22, upper_uid=50, last_uid=25, scanned_count=25, enriched_count=2,
        )

        response = self.client.get("/postfach/", secure=True)

        self.assertContains(response, "Einmaliger Adressabgleich")
        self.assertContains(response, "<dt>Geprüfte Nachrichten</dt><dd>25</dd>")
        self.assertContains(response, "<dt>Ergänzte Adressen</dt><dd>2</dd>")
        self.assertNotContains(response, "Adressabgleich starten")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_manual_request_is_protected_and_calls_the_queue_only(self):
        payload = {"action": "start-contact-replay"}
        with patch("mailimport.mailbox_views.queue_contact_replay") as queue:
            denied = Client(enforce_csrf_checks=True).post("/postfach/", payload, secure=True)
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
