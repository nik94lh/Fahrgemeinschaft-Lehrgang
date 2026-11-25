from django.urls import reverse

from bookings.models import EmailEvent, School
from tests.workspace_client import WorkspaceTestCase


class ReviewQueueTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.conflict = EmailEvent.objects.create(
            sha256="a" * 64,
            kind=EmailEvent.Kind.CONFIRMATION,
            state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT,
            review_context={
                "participant_name": "Synthetische Testperson",
                "course_name": "Beispiellehrgang",
                "school": School.WUERZBURG,
            },
        )
        cls.other = EmailEvent.objects.create(
            sha256="b" * 64,
            kind=EmailEvent.Kind.REASSIGNMENT,
            state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.REASSIGNMENT,
        )
        EmailEvent.objects.create(
            sha256="c" * 64,
            state=EmailEvent.State.REVIEWED,
            review_reason=EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT,
        )

    def test_open_case_card_shows_reason_mail_facts_and_detail_action(self):
        response = self.client.get(reverse("review"))

        self.assertContains(response, self.conflict.get_review_reason_display())
        self.assertContains(response, "Synthetische Testperson")
        self.assertContains(response, "Beispiellehrgang")
        self.assertContains(response, "SFS Würzburg")
        self.assertContains(
            response, (
                f'<a class="button-link" href="{reverse("review_detail", args=[self.conflict.pk])}"'
                f' aria-label="Fall prüfen, Nummer {self.conflict.pk}">Fall prüfen</a>'
            ),
            html=True,
        )
        self.assertEqual(response.context["page_obj"].paginator.count, 2)

    def test_reason_filter_includes_only_matching_open_cases(self):
        response = self.client.get(reverse("review"), {"grund": EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT})

        self.assertContains(response, self.conflict.get_review_reason_display())
        self.assertNotContains(response, reverse("review_detail", args=[self.other.pk]))
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

        empty = self.client.get(reverse("review"), {"grund": EmailEvent.ReviewReason.PERIOD_UNCLEAR})
        self.assertContains(empty, "Keine offenen Fälle mit diesem Grund.")
