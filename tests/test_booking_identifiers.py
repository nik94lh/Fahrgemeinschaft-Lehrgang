from datetime import UTC, datetime

from django.urls import reverse

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, School
from tests.workspace_client import WorkspaceTestCase


class BookingIdentifierTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        person = Participant.objects.create(name="Synthetischer Teilnehmer")
        course = Course.objects.create(name="Technische Hilfe Beispiel")
        first = CourseSession.objects.create(
            course=course, school=School.WUERZBURG,
            starts_at=datetime(2031, 5, 6, 8, tzinfo=UTC), ends_at=datetime(2031, 5, 6, 16, tzinfo=UTC),
        )
        second = CourseSession.objects.create(
            course=course, school=School.REGENSBURG,
            starts_at=datetime(2031, 5, 13, 8, tzinfo=UTC), ends_at=datetime(2031, 5, 13, 16, tzinfo=UTC),
        )
        cls.target = CourseSeat.objects.create(
            pk=13, session=first, participant=person, status=BookingStatus.CONFIRMED,
        )
        cls.text_match = CourseSeat.objects.create(
            pk=14, session=first, external_reference="EXTERN-13",
            participant=Participant.objects.create(name="Referenz Beispiel"),
        )
        cls.label_match = CourseSeat.objects.create(
            pk=16, session=first,
            participant=Participant.objects.create(name="Buchungs-ID 13 Beispiel"),
        )
        cls.related = CourseSeat.objects.create(pk=15, session=second, participant=person)
        cls.event = EmailEvent.objects.create(
            sha256="b" * 64, state=EmailEvent.State.NEEDS_REVIEW, kind=EmailEvent.Kind.CONFIRMATION,
            review_reason=EmailEvent.ReviewReason.NO_MATCHING_BOOKING,
        )

    def search(self, query, **filters):
        return self.client.get(reverse("bookings"), {"q": query, **filters})

    def test_prefixed_id_matches_only_exact_internal_id(self):
        for query in ("Buchungs-ID 13", "buchungs-id: 13", "buchungs id 13"):
            with self.subTest(query=query):
                response = self.search(query)

                self.assertEqual(list(response.context["page_obj"]), [self.target])
                detail = reverse("booking_detail", args=[self.target.pk], query={"q": query})
                self.assertContains(response, f'<a href="{detail}">Buchungs-ID 13</a>', html=True)
                self.assertNotContains(response, 'href="/buchungen/13/teilnehmer/"')

    def test_plain_number_keeps_text_matches_and_adds_exact_internal_id(self):
        response = self.search("13")

        self.assertEqual(list(response.context["page_obj"]), [self.target, self.text_match, self.label_match])
        self.assertEqual(list(self.search("extern").context["page_obj"]), [self.text_match])
        self.assertEqual(list(self.search("Buchungs-ID 13", school=School.REGENSBURG).context["page_obj"]), [])
        self.assertEqual(list(self.search("Buchungs-ID 9223372036854775808").context["page_obj"]), [])

    def test_unassigned_booking_in_list_has_its_own_detail_link(self):
        CourseSeat.objects.create(pk=17, session=self.target.session)

        response = self.search("Buchungs-ID 17")

        detail = reverse("booking_detail", args=[17], query={"q": "Buchungs-ID 17"})
        self.assertContains(response, f'<a href="{detail}">Buchungs-ID 17</a>', html=True)
        self.assertContains(response, "Teilnehmer nicht zugeordnet")
        self.assertNotContains(response, 'href="/buchungen/17/teilnehmer/"')

    def test_profile_and_linked_bookings_show_their_internal_ids(self):
        page = self.client.get(reverse("booking_participant", args=[self.target.pk]))

        self.assertContains(page, "Buchungs-ID 13", count=2)
        self.assertContains(page, "Buchungs-ID 15")

    def test_review_booking_lookup_uses_exact_explicit_id(self):
        page = self.client.get(reverse("review_detail", args=[self.event.pk]), {
            "aktion": "link", "platzsuche": "Buchungs-ID 13",
        })

        self.assertEqual(list(page.context["form"].fields["seat"].queryset), [self.target])
        self.assertContains(page, "Buchungs-ID 13 · Synthetischer Teilnehmer")
