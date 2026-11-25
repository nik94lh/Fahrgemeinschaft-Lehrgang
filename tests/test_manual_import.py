from email.message import EmailMessage

from django.core.files.uploadedfile import SimpleUploadedFile

from bookings.models import CourseSeat, EmailEvent
from tests.workspace_client import WorkspaceTestCase


class ManualImportTests(WorkspaceTestCase):
    @staticmethod
    def fixture():
        mail = EmailMessage()
        mail["Subject"] = "Lehrgangsanmeldung wurde bestätigt"
        mail.set_content(
            "Teilnehmer: Alex Beispiel\n"
            "Lehrgangsart: Synthetischer Testlehrgang\n"
            "Schulungsort: SFS Würzburg\n"
            "Lehrgangstermin: 10.05.2031 09:45 Uhr – 14.05.2031 15:00 Uhr\n"
        )
        return SimpleUploadedFile("synthetisch.eml", mail.as_bytes())

    def test_explicit_test_import_is_repeatable_and_visible_in_bookings(self):
        file = self.fixture()
        data = file.read()
        for _ in range(2):
            self.assertRedirects(
                self.client.post("/importieren/", {"file": SimpleUploadedFile("synthetisch.eml", data)}),
                "/buchungen/?importiert=1",
            )

        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertContains(self.client.get("/buchungen/"), "Synthetischer Testlehrgang")

    def test_manual_import_requires_post_and_csrf(self):
        self.assertEqual(self.client.get("/importieren/").status_code, 405)
        client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(client.post("/importieren/", {"file": self.fixture()}).status_code, 403)
        self.assertFalse(EmailEvent.objects.exists())

    def test_downloadable_sample_can_be_imported(self):
        response = self.client.get("/testmail.eml")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.client.post("/importieren/", {
            "file": SimpleUploadedFile("testmail.eml", response.content),
        })
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_incomplete_test_mail_opens_the_specific_review(self):
        mail = EmailMessage()
        mail["Subject"] = "Lehrgangsgenehmigung"
        mail.set_content("Teilnehmer: Alex Beispiel")
        response = self.client.post("/importieren/", {
            "file": SimpleUploadedFile("synthetisch.eml", mail.as_bytes()),
        })
        event = EmailEvent.objects.get()
        self.assertRedirects(response, f"/klaerung/{event.pk}/")
