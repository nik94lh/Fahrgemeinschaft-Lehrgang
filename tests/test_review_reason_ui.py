from django.test import TestCase

from bookings.models import EmailEvent


class ReviewReasonViewTests(TestCase):
    def test_legacy_case_label_is_explicit_and_not_a_guess(self):
        event = EmailEvent.objects.create(
            sha256="1" * 64,
            kind=EmailEvent.Kind.REASSIGNMENT,
            state=EmailEvent.State.NEEDS_REVIEW,
        )
        self.assertContains(self.client.get("/klaerung/"), "Grund nicht erfasst")
        self.assertContains(self.client.get(f"/klaerung/{event.pk}/"), "Grund nicht erfasst")

    def test_reason_has_a_readable_label_in_list_and_detail(self):
        event = EmailEvent.objects.create(
            sha256="2" * 64,
            state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.PARTICIPANT_UNCLEAR,
        )
        for route in ("/klaerung/", f"/klaerung/{event.pk}/"):
            self.assertContains(self.client.get(route), "Teilnehmerangabe fehlt oder ist mehrdeutig")
