from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.utils import timezone

from bookings.models import Participant, RideCase, RideSimulationInvitation


INDEX = "/fahrgemeinschaften/simulation/"
OPEN = f"{INDEX}antwort/"


class RideSimulationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.editor = users.create_user("synthetic-editor")
        cls.other_editor = users.create_user("synthetic-other-editor")
        cls.viewer = users.create_user("synthetic-viewer")
        permissions = Permission.objects.filter(content_type__app_label="accounts")
        edit = permissions.get(codename="edit_rides")
        cls.editor.user_permissions.add(edit)
        cls.other_editor.user_permissions.add(edit)
        cls.viewer.user_permissions.add(permissions.get(codename="view_rides"))

    def start(self):
        response = self.client.post(INDEX)
        self.assertEqual(response.status_code, 200)
        return response

    def open_invitation(self, link, *, client=None):
        parsed = urlsplit(link)
        self.assertEqual(parsed.path, OPEN)
        self.assertFalse(parsed.query)
        return (client or self.client).post(OPEN, {"token": parsed.fragment})

    def test_separate_synthetic_invitations_accept_once_and_two_yes_update_card(self):
        self.client.force_login(self.editor)

        start = self.start()
        previews = start.context["previews"]
        self.assertEqual(len(previews), 3)
        self.assertEqual(len({urlsplit(item["link"]).fragment for item in previews}), 3)
        self.assertEqual(start.context["counts"], {"yes": 0, "no": 0, "open": 3, "expired": 0})
        self.assertIn("no-store", start.headers["Cache-Control"])
        self.assertFalse(Participant.objects.exists())
        self.assertFalse(RideCase.objects.exists())

        form_url = self.open_invitation(previews[0]["link"]).url
        form = self.client.get(form_url)
        self.assertContains(form, "@example.invalid")
        bad = self.client.post(form_url, {"interest": "yes", "driver": "yes", "phone": "0176 1234567"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get(INDEX).context["counts"]["open"], 3)

        first = self.client.post(form_url, {"interest": "yes", "driver": "no", "phone": "000 000 001"})
        self.assertRedirects(first, INDEX)
        repeated = self.client.post(form_url, {"interest": "no"})
        self.assertEqual(repeated.status_code, 404)
        self.assertEqual(self.client.get(INDEX).context["counts"], {"yes": 1, "no": 0, "open": 2, "expired": 0})

        second_form = self.open_invitation(previews[1]["link"]).url
        self.assertRedirects(self.client.post(second_form, {"interest": "yes", "driver": "no"}), INDEX)
        result = self.client.get(INDEX)
        self.assertContains(result, "Genügend Interesse")
        self.assertEqual(result.context["counts"], {"yes": 2, "no": 0, "open": 1, "expired": 0})

        remaining = self.open_invitation(previews[2]["link"]).url
        self.assertRedirects(self.client.post(remaining, {"interest": "no"}), INDEX)
        self.assertEqual(self.client.get(INDEX).context["counts"], {"yes": 2, "no": 1, "open": 0, "expired": 0})

    def test_signed_in_editor_owns_links_and_reset_invalidates_prior_grants(self):
        self.client.force_login(self.editor)
        link = self.start().context["previews"][0]["link"]
        allowed = self.open_invitation(link)
        self.assertEqual(allowed.status_code, 302)

        different_editor = Client()
        different_editor.force_login(self.other_editor)
        self.assertEqual(self.open_invitation(link, client=different_editor).status_code, 404)
        self.assertEqual(different_editor.get(allowed.url).status_code, 404)
        self.assertIsNone(different_editor.get(INDEX).context["counts"])
        ungranted_session = Client()
        ungranted_session.force_login(self.editor)
        self.assertEqual(ungranted_session.get(allowed.url).status_code, 404)

        self.start()
        self.assertEqual(self.client.get(allowed.url).status_code, 404)
        self.assertEqual(self.open_invitation(link).status_code, 404)

    def test_simulation_strictly_requires_authentication_and_edit_rights(self):
        self.assertRedirects(self.client.get(OPEN), f"/anmelden/?next={OPEN}", fetch_redirect_response=False)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(INDEX).status_code, 403)
        self.assertEqual(self.client.post(INDEX).status_code, 403)
        self.assertEqual(self.client.post(OPEN, {"token": "irrelevant"}).status_code, 403)

    def test_post_requires_csrf(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.editor)
        self.assertEqual(strict.post(INDEX).status_code, 403)
        self.assertEqual(strict.post(OPEN, {"token": "irrelevant"}).status_code, 403)

    def test_secret_is_stored_as_digest_and_expired_grants_cannot_submit(self):
        self.client.force_login(self.editor)
        start = self.start()
        link = start.context["previews"][0]["link"]
        token = urlsplit(link).fragment
        digest = sha256(token.encode()).hexdigest()
        invitation = RideSimulationInvitation.objects.get(token_digest=digest)
        self.assertNotEqual(invitation.token_digest, token)
        landing_page = self.client.get(OPEN)
        self.assertContains(landing_page, "/assets/ride-simulation.js")
        destination = self.open_invitation(link).url
        RideSimulationInvitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertEqual(self.client.post(destination, {"interest": "no"}).status_code, 404)
        self.assertEqual(self.open_invitation(link).status_code, 404)

    def test_no_open_invitations_and_too_few_yes_show_terminal_card_with_expiry(self):
        self.client.force_login(self.editor)
        previews = self.start().context["previews"]
        form_url = self.open_invitation(previews[0]["link"]).url
        self.assertRedirects(self.client.post(form_url, {"interest": "no"}), INDEX)
        form_url = self.open_invitation(previews[1]["link"]).url
        self.assertRedirects(self.client.post(form_url, {"interest": "yes", "driver": "yes", "brigade": "muster"}), INDEX)
        RideSimulationInvitation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        result = self.client.get(INDEX)

        self.assertContains(result, "Zu wenig Zusagen")
        self.assertEqual(result.context["counts"], {"yes": 1, "no": 1, "open": 0, "expired": 1})
