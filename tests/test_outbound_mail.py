import smtplib
import socket
import ssl
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings

from mailimport.mailbox_settings import encrypt_password
from mailimport.models import MailboxConfiguration, OutboundMailConfiguration
from mailimport.outbound import (
    SMTPDeliveryError, SMTPDeliveryUncertain, encrypt_smtp_password,
    outbound_automation_enabled, outbound_enabled, send_ride_mail,
)
from tests.workspace_client import WorkspaceTestCase


URL = "/postfach/versand/"
SMTP_USER = "sender@example.org"
SMTP_PASSWORD = "synthetic-smtp-secret"


@override_settings(HTTPS=True, BMS_SMTP_ENABLED=True)
class OutboundSettingsTests(WorkspaceTestCase):
    def separate_account(self, **overrides):
        return {
            "sender_source": "separate", "host": "smtp.example.org", "port": "587", "security": "starttls",
            "sender_address": SMTP_USER, "username": SMTP_USER, "password": SMTP_PASSWORD, "enabled": "on",
        } | overrides

    def test_admin_can_open_the_smtp_settings(self):
        response = self.client.get(URL, secure=True)

        self.assertContains(response, "SMTP-Versand")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_smtp_settings_require_https_an_admin_and_csrf(self):
        self.assertEqual(self.client.get(URL).status_code, 404)
        strict = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(URL, self.separate_account(), secure=True).status_code, 403)
        viewer = get_user_model().objects.create_user("synthetic-mailbox-editor")
        viewer.user_permissions.add(Permission.objects.get(
            content_type__app_label="accounts", codename="edit_mailbox",
        ))
        other = Client()
        other.force_login(viewer)
        self.assertEqual(other.get(URL, secure=True).status_code, 403)
        self.assertEqual(other.post(URL, self.separate_account(), secure=True).status_code, 403)
        self.assertFalse(OutboundMailConfiguration.objects.exists())

    def test_smtp_password_is_encrypted_and_reused_only_for_the_same_account(self):
        saved = self.client.post(URL, self.separate_account(), secure=True)
        self.assertRedirects(saved, f"{URL}?gespeichert=1", fetch_redirect_response=False)
        config = OutboundMailConfiguration.objects.get()
        self.assertTrue(outbound_enabled())
        self.assertFalse(outbound_automation_enabled())
        self.assertNotIn(SMTP_PASSWORD, config.encrypted_password)
        page = self.client.get(saved.url, secure=True)
        self.assertNotContains(page, SMTP_PASSWORD)
        self.assertNotContains(page, config.encrypted_password)

        retained = self.client.post(URL, self.separate_account(password=""), secure=True)
        self.assertEqual(retained.status_code, 302)
        self.assertEqual(OutboundMailConfiguration.objects.get().encrypted_password, config.encrypted_password)

        changed = self.client.post(URL, self.separate_account(host="other.example.org", password=""), secure=True)
        self.assertContains(changed, "Gib das Passwort des SMTP-Versandkontos ein.", status_code=400)
        self.assertEqual(OutboundMailConfiguration.objects.get().host, config.host)

    def test_smtp_requires_both_opt_ins_for_real_automation(self):
        self.assertFalse(outbound_enabled())
        self.assertFalse(outbound_automation_enabled())
        data = self.separate_account(allow_automatic_inquiries="on")
        with override_settings(BMS_SMTP_ENABLED=False):
            denied = self.client.post(URL, data, secure=True)
            self.assertContains(denied, "Der SMTP-Versand ist in der Serverkonfiguration gesperrt.", status_code=400)
            self.assertFalse(OutboundMailConfiguration.objects.exists())

        accepted = self.client.post(URL, data, secure=True)
        self.assertEqual(accepted.status_code, 302)
        self.assertTrue(outbound_enabled())
        self.assertTrue(outbound_automation_enabled())
        with override_settings(BMS_SMTP_ENABLED=False):
            self.assertFalse(outbound_enabled())
            self.assertFalse(outbound_automation_enabled())

    @override_settings(BMS_SMTP_LINKED_HOSTS=("smtp.example.org",))
    def test_linked_second_input_reuses_credentials_without_enabling_import(self):
        mailbox = MailboxConfiguration.objects.create(
            pk=2, host="imap.example.org", username="input-two@example.org",
            encrypted_password=encrypt_password("synthetic-first-input-secret"), enabled=False,
        )
        data = self.separate_account(sender_source="input2", password="")
        self.assertEqual(self.client.post(URL, data, secure=True).status_code, 302)
        config = OutboundMailConfiguration.objects.get()
        self.assertEqual((config.username, config.encrypted_password), ("", ""))
        self.assertTrue(outbound_enabled())

        mailbox.encrypted_password = encrypt_password("synthetic-new-input-secret")
        mailbox.save(update_fields=["encrypted_password"])
        self.assertTrue(outbound_enabled())
        mailbox.username = "new-input-two@example.org"
        mailbox.save(update_fields=["username"])
        self.assertFalse(outbound_enabled())
        self.assertEqual(self.client.post(URL, data, secure=True).status_code, 302)
        self.assertTrue(outbound_enabled())
        mailbox.refresh_from_db()
        self.assertFalse(mailbox.enabled)

    @override_settings(BMS_SMTP_LINKED_HOSTS=())
    def test_first_input_accepts_the_same_normalized_host_from_the_environment(self):
        environment = {
            "BMS_IMAP_HOST": "imap.example.org", "BMS_IMAP_USERNAME": "input-one@example.org",
            "BMS_IMAP_PASSWORD": "synthetic-environment-secret", "BMS_IMAP_ENABLED": "0",
        }
        with patch.dict("os.environ", environment):
            data = self.separate_account(sender_source="input1", host="IMAP.EXAMPLE.ORG.", password="")
            self.assertEqual(self.client.post(URL, data, secure=True).status_code, 302)
            self.assertEqual(OutboundMailConfiguration.objects.get().host, "imap.example.org")
            self.assertTrue(outbound_enabled())
            with patch.dict("os.environ", {"BMS_IMAP_USERNAME": "another-input@example.org"}):
                self.assertFalse(outbound_enabled())

    @override_settings(BMS_SMTP_LINKED_HOSTS=("smtp.example.org",))
    def test_rejects_a_related_looking_but_unapproved_host_for_input_credentials(self):
        MailboxConfiguration.objects.create(
            pk=1, host="imap.example.org", username="input@example.org",
            encrypted_password=encrypt_password("synthetic-input-secret"),
        )
        data = self.separate_account(sender_source="input1", host="relay.smtp.example.org", password="")
        refused = self.client.post(URL, data, secure=True)

        self.assertContains(refused, "Für diesen SMTP-Server fehlt die Freigabe zur Verwendung der Zugangsdaten des Eingangs.", status_code=400)
        self.assertFalse(OutboundMailConfiguration.objects.exists())

        separate = self.client.post(URL, self.separate_account(host="relay.smtp.example.org"), secure=True)
        self.assertEqual(separate.status_code, 302)
        self.assertTrue(outbound_enabled())

    @override_settings(BMS_SMTP_LINKED_HOSTS=("smtp.example.org",))
    def test_removing_server_approval_stops_linked_smtp_before_dns(self):
        MailboxConfiguration.objects.create(
            pk=1, host="imap.example.org", username="input@example.org",
            encrypted_password=encrypt_password("synthetic-input-secret"),
        )
        data = self.separate_account(sender_source="input1", password="", allow_automatic_inquiries="on")
        self.assertEqual(self.client.post(URL, data, secure=True).status_code, 302)
        self.assertTrue(outbound_enabled())
        self.assertTrue(outbound_automation_enabled())

        with (
            override_settings(BMS_SMTP_LINKED_HOSTS=()),
            patch("mailimport.outbound.resolve_public_host") as dns,
            patch("mailimport.outbound._SMTPConnection") as connect,
        ):
            connect.return_value.send_message.return_value = {}
            with self.assertRaises(SMTPDeliveryError):
                send_ride_mail(
                    recipient="participant@example.org", subject="Synthetische Abfrage",
                    body="Test", message_id="<synthetic-ride.01@example.org>",
                )
            self.assertFalse(outbound_enabled())
            self.assertFalse(outbound_automation_enabled())
        dns.assert_not_called()
        connect.assert_not_called()


@override_settings(BMS_SMTP_ENABLED=True)
class OutboundTransportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.config = OutboundMailConfiguration.objects.create(
            host="smtp.example.org", username=SMTP_USER, sender_address=SMTP_USER,
            encrypted_password=encrypt_smtp_password(SMTP_PASSWORD), enabled=True,
        )

    def send(self, **overrides):
        return send_ride_mail(**({
            "recipient": "participant@example.org", "subject": "Teilnahmeabfrage",
            "body": "Synthetische Vorschau zur Fahrt.", "message_id": "<synthetic-ride.01@example.org>",
        } | overrides))

    def test_only_one_recipient_is_sent_after_verified_starttls(self):
        addresses = ((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("93.184.215.14", 587)),)
        with (
            patch("mailimport.outbound.resolve_public_host", return_value=addresses) as resolve,
            patch("mailimport.outbound._SMTPConnection") as connect,
        ):
            smtp = connect.return_value
            smtp.send_message.return_value = {}
            smtp.quit.side_effect = smtplib.SMTPServerDisconnected("synthetic-server-quit-failure")
            self.assertIsNone(self.send())

        resolve.assert_called_once_with("smtp.example.org", 587)
        connect.assert_called_once_with("smtp.example.org", 587, addresses=addresses, tls_context=None)
        self.assertEqual(
            [item[0] for item in smtp.method_calls], ["starttls", "login", "send_message", "quit", "close"],
        )
        context = smtp.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        smtp.login.assert_called_once_with(SMTP_USER, SMTP_PASSWORD)
        delivery = smtp.send_message.call_args
        message, = delivery.args
        self.assertEqual(message["Message-ID"], "<synthetic-ride.01@example.org>")
        self.assertEqual(message["To"], "participant@example.org")
        self.assertEqual(message.get_content().strip(), "Synthetische Vorschau zur Fahrt.")
        self.assertEqual(delivery.kwargs, {"from_addr": SMTP_USER, "to_addrs": ["participant@example.org"]})

    def test_implicit_tls_connects_with_certificate_checks_and_skips_starttls(self):
        OutboundMailConfiguration.objects.filter(pk=1).update(port=465, security="tls")
        with patch("mailimport.outbound.resolve_public_host"), patch("mailimport.outbound._SMTPConnection") as connect:
            connect.return_value.send_message.return_value = {}
            self.assertIsNone(self.send())

        self.assertEqual(connect.call_args.args, ("smtp.example.org", 465))
        context = connect.call_args.kwargs["tls_context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        connect.return_value.starttls.assert_not_called()
        connect.return_value.login.assert_called_once_with(SMTP_USER, SMTP_PASSWORD)

    def test_no_credentials_sent_without_tls_and_errors_hide_server_text(self):
        with patch("mailimport.outbound.resolve_public_host"), patch("mailimport.outbound._SMTPConnection") as connect:
            connect.return_value.starttls.side_effect = smtplib.SMTPNotSupportedError(SMTP_PASSWORD)
            with self.assertRaises(SMTPDeliveryError) as failure:
                self.send()
        self.assertNotIn(SMTP_PASSWORD, str(failure.exception))
        connect.return_value.login.assert_not_called()
        connect.return_value.send_message.assert_not_called()

    def test_disconnect_after_transfer_begins_has_an_uncertain_delivery_result(self):
        with patch("mailimport.outbound.resolve_public_host"), patch("mailimport.outbound._SMTPConnection") as connect:
            connect.return_value.send_message.side_effect = smtplib.SMTPServerDisconnected(SMTP_PASSWORD)
            with self.assertRaises(SMTPDeliveryUncertain) as failure:
                self.send()
        self.assertNotIn(SMTP_PASSWORD, str(failure.exception))

    def test_disabled_or_private_destinations_and_test_recipients_never_connect(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 587))]
        with (
            patch("mailimport.imap_network.socket.getaddrinfo", return_value=private) as dns,
            patch("mailimport.outbound._SMTPConnection") as connect,
        ):
            with override_settings(BMS_SMTP_ENABLED=False), self.assertRaises(SMTPDeliveryError):
                self.send()
            with self.assertRaises(SMTPDeliveryError):
                self.send(recipient="synthetic@example.invalid")
            dns.assert_not_called()
            with self.assertRaises(SMTPDeliveryError):
                self.send()
            dns.assert_called_once()
            connect.assert_not_called()
