from django.test import override_settings

from mailimport.mailbox_settings import load_mailbox_settings
from mailimport.models import ParticipantRecognitionConfiguration
from mailimport.participant_settings import get_functional_addresses
from tests.workspace_client import WorkspaceTestCase


@override_settings(HTTPS=True)
class FunctionalAddressesTests(WorkspaceTestCase):
    def save_addresses(self, addresses, *, secure=True):
        return self.client.post("/postfach/", {
            "action": "save-functional-addresses",
            "functional_addresses": addresses,
        }, secure=secure)

    def test_saves_normalized_addresses_and_allows_clearing_without_imap_configuration(self):
        self.assertEqual(get_functional_addresses(), ())
        response = self.save_addresses("  Lehrgaenge@Example.org \r\n\nLEHRGAENGE@example.org\nPostfach@Example.org")
        self.assertRedirects(response, "/postfach/?funktionsadressen_gespeichert=1", fetch_redirect_response=False)

        self.assertEqual(get_functional_addresses(), ("lehrgaenge@example.org", "postfach@example.org"))
        self.assertIsNone(load_mailbox_settings())
        self.assertContains(self.client.get(response.url, secure=True), "lehrgaenge@example.org")

        self.assertEqual(self.save_addresses("").status_code, 302)
        self.assertEqual(get_functional_addresses(), ())

    def test_invalid_email_does_not_replace_an_existing_configuration(self):
        self.assertEqual(self.save_addresses("postfach@example.org").status_code, 302)

        invalid = self.save_addresses("keine-adresse\nlehrgaenge@example.org")

        self.assertContains(invalid, "gültige E-Mail-Adresse")
        self.assertEqual(get_functional_addresses(), ("postfach@example.org",))

    def test_mailbox_password_save_preserves_addresses_and_clearing_preserves_password(self):
        self.assertEqual(self.save_addresses("postfach@example.org").status_code, 302)
        mailbox_form = {
            "host": "imap.example.org",
            "port": "993",
            "username": "bms@example.org",
            "mailbox": "INBOX",
            "password": "test-only-mailbox-secret",
            "enabled": "on",
        }
        response = self.client.post("/postfach/", mailbox_form, secure=True)
        self.assertEqual(response.status_code, 302)

        self.assertEqual(get_functional_addresses(), ("postfach@example.org",))
        self.assertEqual(self.save_addresses("").status_code, 302)
        self.assertEqual(load_mailbox_settings().password, "test-only-mailbox-secret")

    def test_addresses_require_existing_https_and_csrf_guards(self):
        data = {"action": "save-functional-addresses", "functional_addresses": "postfach@example.org"}
        client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/postfach/", data, secure=True).status_code, 403)
        self.assertIn(self.save_addresses(data["functional_addresses"], secure=False).status_code, (301, 404))
        with override_settings(HTTPS=False):
            self.assertEqual(self.save_addresses(data["functional_addresses"]).status_code, 404)
        self.assertContains(self.client.get("/postfach/", secure=True), 'name="functional_addresses"')

    def test_idn_and_punycode_are_saved_once_in_the_same_canonical_form(self):
        response = self.save_addresses("Postfach@büro.example\nPOSTFACH@XN--BRO-HOA.EXAMPLE")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_functional_addresses(), ("postfach@xn--bro-hoa.example",))
        self.assertEqual(
            ParticipantRecognitionConfiguration.objects.get().functional_addresses,
            ["postfach@xn--bro-hoa.example"],
        )

    def test_legacy_idn_setting_is_canonicalized_on_retrieval(self):
        ParticipantRecognitionConfiguration.objects.create(
            functional_addresses=["Postfach@büro.example", "POSTFACH@XN--BRO-HOA.EXAMPLE"],
        )

        self.assertEqual(get_functional_addresses(), ("postfach@xn--bro-hoa.example",))
