from django.urls import reverse

from bookings.models import BookingStatus, CourseSeat, CourseSession, EmailEvent, Participant, School
from bookings.services import ingest
from tests.test_reconciliation import eml
from tests.workspace_client import WorkspaceTestCase


class ReviewActionTests(WorkspaceTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.booking = ingest(eml("Teilnahmebestätigung", reference="TEST-17")).seat
        cls.reassignment = ingest(eml(
            "Ummeldung", participant="Toni Testperson", reference="TEST-17",
        ))
        cls.replacement = Participant.objects.create(name="Toni Testperson")

    def detail_url(self):
        return reverse("review_detail", kwargs={"event_id": self.reassignment.pk})

    def test_filtered_queue_links_to_detail_with_the_current_page(self):
        EmailEvent.objects.bulk_create([
            EmailEvent(
                sha256=f"{number:064x}", kind=EmailEvent.Kind.REASSIGNMENT,
                state=EmailEvent.State.NEEDS_REVIEW, review_reason=EmailEvent.ReviewReason.REASSIGNMENT,
            )
            for number in range(1, 26)
        ])

        response = self.client.get(reverse("review"), {"grund": "reassignment", "page": "2"})

        self.assertContains(response, f'href="{self.detail_url()}?grund=reassignment&amp;page=2"')

    def test_detail_keeps_the_filtered_queue_context_through_action_and_search(self):
        detail = self.client.get(self.detail_url(), {"grund": "reassignment", "page": "2"})
        search = self.client.get(self.detail_url(), {
            "grund": "reassignment", "page": "2", "aktion": "link", "platzsuche": "TEST-17",
        })

        for response in (detail, search):
            self.assertContains(response, f'href="{reverse("review")}?grund=reassignment&amp;page=2"')
        self.assertContains(detail, 'href="?aktion=link&amp;grund=reassignment&amp;page=2#entscheidung"')
        self.assertContains(search, '<input type="hidden" name="grund" value="reassignment">', count=2)
        self.assertContains(search, '<input type="hidden" name="page" value="2">', count=2)

    def test_decision_preserves_queue_context_after_invalid_and_successful_submissions(self):
        data = {"action": "link", "seat": self.booking.pk, "grund": "reassignment", "page": "2"}

        invalid = self.client.post(self.detail_url(), data)
        self.assertIn("confirm", invalid.context["form"].errors)
        self.assertContains(invalid, f'href="{reverse("review")}?grund=reassignment&amp;page=2"')
        self.assertContains(invalid, '<input type="hidden" name="page" value="2">', count=2)

        response = self.client.post(self.detail_url(), {**data, "confirm": "on"})

        self.assertRedirects(response, f'{reverse("review")}?grund=reassignment&page=2')

    def test_decision_ignores_invalid_queue_context_and_external_return_url(self):
        response = self.client.post(self.detail_url(), {
            "action": "link", "seat": self.booking.pk, "confirm": "on",
            "grund": "unknown", "page": "-1", "next": "https://example.invalid/",
        })

        self.assertRedirects(response, reverse("review"))

    def test_resolution_requires_post_csrf_and_explicit_confirmation(self):
        payload = {"action": "link", "seat": self.booking.pk, "confirm": "on"}
        client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(client.post(self.detail_url(), payload).status_code, 403)
        self.assertEqual(self.client.get(self.detail_url(), payload).status_code, 200)
        payload.pop("confirm")
        self.assertEqual(self.client.post(self.detail_url(), payload).status_code, 200)
        self.reassignment.refresh_from_db()
        self.assertEqual(self.reassignment.state, EmailEvent.State.NEEDS_REVIEW)

    def test_link_records_a_deliberate_review_without_changing_booking(self):
        response = self.client.post(self.detail_url(), {
            "action": "link", "seat": self.booking.pk, "confirm": "on",
        })

        self.assertRedirects(response, reverse("review"))
        self.reassignment.refresh_from_db()
        self.assertEqual(self.reassignment.state, EmailEvent.State.REVIEWED)
        self.assertEqual(self.reassignment.review_resolution, EmailEvent.ReviewResolution.LINKED)
        self.assertIsNotNone(self.reassignment.reviewed_at)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_dismiss_uses_structured_reason_and_cannot_be_repeated(self):
        first = self.client.post(self.detail_url(), {
            "action": "dismiss", "reason": "duplicate", "confirm": "on",
        })
        second = self.client.post(self.detail_url(), {
            "action": "dismiss", "reason": "not_relevant", "confirm": "on",
        })

        self.assertRedirects(first, reverse("review"))
        self.assertEqual(second.status_code, 409)
        self.reassignment.refresh_from_db()
        self.assertEqual(self.reassignment.state, EmailEvent.State.DISMISSED)
        self.assertEqual(self.reassignment.review_resolution, EmailEvent.ReviewResolution.DUPLICATE)

    def test_reassign_records_current_and_new_participant_on_existing_seat(self):
        previous = self.booking.participant
        response = self.client.post(self.detail_url(), {
            "action": "reassign", "seat": self.booking.pk, "previous_participant": previous.pk,
            "replacement": self.replacement.pk, "confirm": "on",
        })

        self.assertRedirects(response, reverse("review"))
        self.booking.refresh_from_db()
        history = self.booking.history.last()
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(self.booking.participant, self.replacement)
        self.assertEqual(self.booking.status, BookingStatus.REASSIGNED)
        self.assertEqual(history.previous_participant, previous)
        self.assertEqual(history.participant, self.replacement)
        self.assertEqual(history.source_event, self.reassignment)

    def test_reassign_stale_or_unverified_previous_participant_has_no_effect(self):
        different_person = Participant.objects.create(name="Pat Muster")
        response = self.client.post(self.detail_url(), {
            "action": "reassign", "seat": self.booking.pk, "previous_participant": different_person.pk,
            "replacement": self.replacement.pk, "confirm": "on",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bisherige Person stimmt nicht überein")
        self.booking.refresh_from_db()
        self.reassignment.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.reassignment.state, EmailEvent.State.NEEDS_REVIEW)

    def test_reassign_cannot_apply_old_mail_to_manually_moved_booking(self):
        session = self.booking.session
        self.booking.history.create(
            previous_session=session, previous_name=self.booking.participant.name,
            previous_participant=self.booking.participant, participant=self.booking.participant,
            previous_status=self.booking.status, status=self.booking.status, edited_fields=["Schulungsort"],
        )
        self.booking.session = CourseSession.objects.create(
            course=session.course, school=School.GERETSRIED, starts_at=session.starts_at, ends_at=session.ends_at,
        )
        self.booking.save(update_fields=["session"])

        response = self.client.post(self.detail_url(), {
            "action": "reassign", "seat": self.booking.pk, "previous_participant": self.booking.participant_id,
            "replacement": self.replacement.pk, "confirm": "on",
        })

        self.assertContains(response, "nach Eingang dieser Meldung manuell geändert")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)

    def test_detail_distinguishes_recognized_fields_and_current_booking(self):
        response = self.client.get(self.detail_url())

        self.assertContains(response, "Erkannte Angaben")
        self.assertContains(response, "Verknüpfte Lehrgangsbuchung")
        self.assertContains(response, "Toni Testperson")
        self.assertContains(response, "Alex Beispiel")
        self.assertContains(response, "Bestätigt")
        self.assertContains(response, "TEST-17")
        self.assertContains(response, "E-Mail zuordnen")
        self.assertContains(response, "Ummeldung in BMS übernehmen")
        self.assertContains(response, "Fall verwerfen")
        self.assertNotContains(response, 'name="replacement"')

    def test_legacy_case_has_no_automatic_seat_options_but_can_search(self):
        legacy = EmailEvent.objects.create(
            sha256="0" * 64, kind=EmailEvent.Kind.CONFIRMATION, state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.PARTICIPANT_UNCLEAR,
        )
        url = reverse("review_detail", kwargs={"event_id": legacy.pk})
        empty = self.client.get(url, {"aktion": "link"})
        self.assertContains(empty, "Keine Zuordnung")
        self.assertContains(empty, "Originalnachricht im Postfach")
        self.assertNotContains(empty, "Alex Beispiel")

        found = self.client.get(url, {"aktion": "link", "platzsuche": "TEST-17"})
        self.assertContains(found, "Alex Beispiel")
        self.assertContains(found, f'<option value="{self.booking.pk}"')
        legacy.refresh_from_db()
        self.assertIsNone(legacy.seat_id)

    def test_unlinked_reassignment_offers_only_context_supported_candidates(self):
        other = ingest(eml("Teilnahmebestätigung", participant="Pat Muster", course="Truppführer"))
        event = ingest(eml(
            "Ummeldung", participant="Toni Testperson", extra="Bisheriger Teilnehmer: Alex Beispiel",
        ))
        self.assertIsNone(event.seat_id)
        url = reverse("review_detail", kwargs={"event_id": event.pk})
        response = self.client.get(url, {"aktion": "link"})

        self.assertContains(response, f'<option value="{self.booking.pk}"')
        self.assertNotContains(response, f'<option value="{other.seat_id}"')
        self.assertNotContains(response, "Pat Muster")
        self.assertNotContains(response, 'name="replacement"')
        self.assertNotContains(response, 'name="reason"')

    def test_dismiss_action_renders_only_its_own_fields(self):
        response = self.client.get(self.detail_url(), {"aktion": "dismiss"})

        self.assertContains(response, 'name="reason"')
        self.assertContains(response, 'name="confirm"')
        self.assertNotContains(response, 'name="seat"')
        self.assertNotContains(response, 'name="replacement"')
