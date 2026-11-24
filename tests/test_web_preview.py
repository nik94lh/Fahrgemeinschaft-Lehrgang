from email.message import EmailMessage
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase

from mailimport.parser import MAX_EML_BYTES


def eml_file(html, name="beispiel.eml"):
    message = EmailMessage()
    message["Subject"] = "Teilnahmebestätigung"
    message.set_content(html, subtype="html")
    return SimpleUploadedFile(name, message.as_bytes(), content_type="message/rfc822")


class MailPreviewTests(SimpleTestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def post_file(self, file):
        self.client.get("/")
        token = self.client.cookies["csrftoken"].value
        return self.client.post("/", {"file": file}, HTTP_X_CSRFTOKEN=token)

    def test_get_renders_a_non_cacheable_form_with_restrictive_csp(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'name="file"')
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("form-action 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("csrftoken", response.cookies)

    def test_valid_post_renders_structured_fields_without_links_or_network(self):
        file = eml_file(
            "<p>Teilnehmer: Alex Beispiel</p><p>Schulungsort: Geretsried</p>"
            '<img src="https://tracker.invalid/pixel"><a href="https://sfs.invalid/t">Bestätigen</a>'
        )
        with (
            patch("socket.socket", side_effect=AssertionError("Unerwarteter Netzwerkzugriff")),
            patch("socket.getaddrinfo", side_effect=AssertionError("Unerwarteter DNS-Zugriff")),
            patch("django.core.files.uploadhandler.TemporaryFileUploadHandler.new_file") as temporary,
        ):
            response = self.post_file(file)

        temporary.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alex Beispiel")
        self.assertContains(response, "Geretsried")
        self.assertContains(response, "Teilnahmebestätigung")
        self.assertNotContains(response, "https://tracker.invalid")
        self.assertNotContains(response, "https://sfs.invalid")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_browser_post_without_csrf_token_is_forbidden(self):
        response = self.client.post("/", {"file": eml_file("<p>Simuliert</p>")})
        self.assertEqual(response.status_code, 403)

    def test_malformed_upload_returns_a_safe_validation_error(self):
        file = SimpleUploadedFile("beispiel.eml", b"<script>raw payload marker</script>")
        response = self.post_file(file)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.context["form"].errors["file"])
        self.assertNotContains(response, "raw payload marker", status_code=400)

    def test_unsupported_extension_is_rejected(self):
        response = self.post_file(eml_file("<p>Simuliert</p>", name="beispiel.txt"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.context["form"].errors)

    def test_oversized_upload_never_spills_to_disk_or_reaches_parser(self):
        file = SimpleUploadedFile("beispiel.eml", b"x" * (MAX_EML_BYTES * 3))
        with (
            patch("django.core.files.uploadhandler.TemporaryFileUploadHandler.new_file") as temporary,
            patch("mailimport.views.parse_eml") as parser,
        ):
            response = self.post_file(file)

        temporary.assert_not_called()
        parser.assert_not_called()
        self.assertEqual(response.status_code, 413)
        self.assertIn("file", response.context["form"].errors)

    def test_more_than_one_file_is_rejected(self):
        files = [eml_file("<p>Erste E-Mail</p>"), eml_file("<p>Zweite E-Mail</p>")]
        response = self.post_file(files)

        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.context["form"].errors)
