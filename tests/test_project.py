from config import settings
from django.test import SimpleTestCase
from django.urls import reverse


class ProjectTest(SimpleTestCase):
    def test_health_returns_expected_service_status(self):
        self.assertEqual(self.client.get(reverse("health")).json(), {"status": "ok"})

    def test_version_appears_in_the_shared_header(self):
        response = self.client.get(reverse("preview"))
        self.assertEqual(response.status_code, 200)
        header = response.content.decode().split("</header>", 1)[0]
        self.assertRegex(header, r"Version \d+\.\d+\.\d+")
        self.assertIn("Testbetrieb", header)

    def test_mail_backend_is_disabled(self):
        self.assertEqual(settings.EMAIL_BACKEND, "django.core.mail.backends.dummy.EmailBackend")

    def test_preview_accepts_stdin_as_a_local_file_source(self):
        import io
        from unittest.mock import patch

        from django.core.management import call_command

        source = (
            b"Subject: Lehrgangsanmeldung wurde bestaetigt\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Lehrgangsart: Synthetischer Lehrgang\r\n"
            b"https://example.invalid/token-avoid-in-preview\r\n"
        )
        output = io.StringIO()
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(source), encoding="utf-8")):
            call_command("preview_eml", "-", stdout=output)

        self.assertIn("Synthetischer Lehrgang", output.getvalue())
        self.assertNotIn("token-avoid-in-preview", output.getvalue())
