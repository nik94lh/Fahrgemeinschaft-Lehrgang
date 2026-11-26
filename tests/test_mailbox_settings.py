from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from mailimport.imap_sync import mailbox_source_id
from mailimport.mailbox_settings import MailboxSettingsError, load_mailbox_settings
from mailimport.models import MailboxConfiguration, MailboxCursor
from tests.workspace_client import WorkspaceTestCase


@override_settings(HTTPS=True)
class StoredMailboxSettingsTests(TestCase):
    def test_missing_browser_configuration_returns_none(self):
        self.assertIsNone(load_mailbox_settings())

    def test_unreadable_secret_does_not_fall_back(self):
        MailboxConfiguration.objects.create(
            host="imap.example.org",
            username="bms@example.org",
            encrypted_password="invalid-ciphertext",
            enabled=True,
        )
        with self.assertRaises(MailboxSettingsError):
            load_mailbox_settings()


@override_settings(HTTPS=True)
class MailboxSettingsViewTests(WorkspaceTestCase):
    def config(self, **overrides):
        settings = {
            "host": "imap.example.org",
            "port": "993",
            "username": "bms@example.org",
            "mailbox": "INBOX",
            "password": "test-confidential-mailbox-value",
            "enabled": "on",
        }
        return settings | overrides

    def server_configuration(self, **overrides):
        return {
            "BMS_IMAP_ENABLED": "1",
            "BMS_IMAP_HOST": "imap.server.example.invalid",
            "BMS_IMAP_PORT": "993",
            "BMS_IMAP_USERNAME": "server-import@example.invalid",
            "BMS_IMAP_PASSWORD": "test-server-only-password",
            "BMS_IMAP_MAILBOX": "IMPORT",
        } | overrides

    def test_insecure_request_cannot_change_configuration(self):
        plain = self.client.post("/postfach/", self.config())
        self.assertIn(plain.status_code, (301, 404))
        self.assertFalse(MailboxConfiguration.objects.exists())

        with override_settings(HTTPS=False):
            disabled = self.client.post("/postfach/", self.config(), secure=True)
        self.assertEqual(disabled.status_code, 404)
        self.assertFalse(MailboxConfiguration.objects.exists())

    def test_post_requires_csrf_token(self):
        response = self.authenticated_client(enforce_csrf_checks=True).post("/postfach/", self.config(), secure=True)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MailboxConfiguration.objects.exists())

    def test_post_encrypts_and_never_displays_password(self):
        data = self.config()
        saved = self.client.post("/postfach/", data, secure=True)
        self.assertEqual(saved.status_code, 302)
        record = MailboxConfiguration.objects.get()
        self.assertNotIn(data["password"], record.encrypted_password)
        config = load_mailbox_settings()
        self.assertEqual(config.password, data["password"])
        self.assertNotIn(data["password"], repr(config))

        response = self.client.get(saved.url, secure=True)
        self.assertContains(response, "Postfachkonfiguration gespeichert.")
        self.assertNotContains(response, data["password"])
        self.assertNotContains(response, record.encrypted_password)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_blank_password_retains_only_for_original_destination(self):
        self.client.post("/postfach/", self.config(), secure=True)
        self.client.post("/postfach/", self.config(password="", enabled=""), secure=True)
        config = load_mailbox_settings()
        self.assertEqual(config.password, "")
        self.assertFalse(config.enabled)
        self.assertTrue(MailboxConfiguration.objects.get().encrypted_password)
        self.client.post("/postfach/", self.config(password=""), secure=True)
        self.assertEqual(load_mailbox_settings().password, self.config()["password"])

        invalid = self.client.post(
            "/postfach/", self.config(host="other.example.org", password=""), secure=True,
        )
        self.assertContains(invalid, "Bitte gib das Passwort")
        self.assertEqual(load_mailbox_settings().host, self.config()["host"])

        saved = self.client.post(
            "/postfach/", self.config(host="other.example.org", password="", enabled=""), secure=True,
        )
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(load_mailbox_settings().password, "")

    def test_private_destination_is_rejected_without_preserving_password(self):
        data = self.config(host="127.0.0.1")
        response = self.client.post("/postfach/", data, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MailboxConfiguration.objects.exists())
        self.assertNotContains(response, data["password"])

    def test_shows_only_sync_status_for_configured_mailbox(self):
        self.client.post("/postfach/", self.config(), secure=True)
        data = self.config()
        source_id = mailbox_source_id(data["host"], 993, data["username"], data["mailbox"])
        cursor = MailboxCursor.objects.create(source_id=source_id, last_sync_at=timezone.now(), last_sync_ok=True)
        response = self.client.get("/postfach/", secure=True)
        self.assertContains(response, "Letzter Abruf erfolgreich.")
        self.assertNotContains(response, source_id)

        cursor.last_sync_ok = False
        cursor.save(update_fields=["last_sync_ok"])
        response = self.client.get("/postfach/", secure=True)
        self.assertContains(response, "Letzter Abruf fehlgeschlagen.")
        self.client.post("/postfach/", self.config(host="other.example.org"), secure=True)
        response = self.client.get("/postfach/", secure=True)
        self.assertNotContains(response, "Letzter Abruf fehlgeschlagen.")
        self.assertContains(response, "Für dieses Postfach liegt noch kein Abrufstatus vor.")

    def test_server_configured_mailbox_shows_its_status_without_connection_details(self):
        env = self.server_configuration()
        source_id = mailbox_source_id(env["BMS_IMAP_HOST"], 993, env["BMS_IMAP_USERNAME"], env["BMS_IMAP_MAILBOX"])
        MailboxCursor.objects.create(source_id=source_id, last_sync_at=timezone.now(), last_sync_ok=True)

        with patch.dict("os.environ", env), patch("mailimport.imap_sync._open_mailbox") as open_mailbox:
            response = self.client.get("/postfach/", secure=True)

        open_mailbox.assert_not_called()
        self.assertContains(response, "Lesender Abruf über die Serverkonfiguration aktiviert.")
        self.assertContains(response, "Letzter Abruf erfolgreich.")
        self.assertNotContains(response, source_id)
        for key in ("BMS_IMAP_HOST", "BMS_IMAP_USERNAME", "BMS_IMAP_PASSWORD", "BMS_IMAP_MAILBOX"):
            self.assertNotContains(response, env[key])

    def test_invalid_server_configuration_is_reported(self):
        env = self.server_configuration(BMS_IMAP_PORT="invalid")
        with patch.dict("os.environ", env):
            response = self.client.get("/postfach/", secure=True)

        self.assertContains(response, "Die Importkonfiguration des Servers ist unvollständig oder konnte nicht geladen werden.")
        self.assertNotContains(response, env["BMS_IMAP_PASSWORD"])

    def test_browser_pause_still_overrides_server_configuration(self):
        with patch.dict("os.environ", self.server_configuration()):
            saved = self.client.post("/postfach/", self.config(enabled=""), secure=True)
            self.assertEqual(saved.status_code, 302)
            response = self.client.get(saved.url, secure=True)

        self.assertContains(response, "Abruf deaktiviert.")
        self.assertNotContains(response, "über die Serverkonfiguration aktiviert")
