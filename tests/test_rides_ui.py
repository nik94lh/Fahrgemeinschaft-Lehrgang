from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Course, CourseSeat, CourseSession, Participant, School


class RideCaseWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        start = timezone.now() + timedelta(days=4)
        session = CourseSession.objects.create(
            course=Course.objects.create(name="Übungslehrgang"),
            school=School.WUERZBURG,
            starts_at=start,
            ends_at=start + timedelta(hours=5),
        )
        cls.first_seat = CourseSeat.objects.create(
            session=session,
            participant=Participant.objects.create(
                name="Synthetische Person",
                email="kontakt@example.invalid",
            ),
            status=BookingStatus.CONFIRMED,
        )
        CourseSeat.objects.create(
            session=session,
            participant=Participant.objects.create(name="Zweite Testperson"),
            status=BookingStatus.CONFIRMED,
        )

    def sync(self):
        response = self.client.post(reverse("rides_sync"))
        self.assertRedirects(response, f"{reverse('rides')}?abgleich=1")

        from bookings.models import RideCase

        return RideCase.objects.filter(direction="outbound").get()

    def test_list_waits_for_a_post_and_shows_persisted_matches(self):
        listing = self.client.get(reverse("rides"))
        self.assertContains(listing, "Abgleich starten")
        self.assertContains(listing, "Noch keine Fahrgemeinschaftsfälle")
        self.assertEqual(self.client.get(reverse("rides_sync")).status_code, 405)
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse("rides_sync")).status_code, 403)

        with patch("django.core.mail.send_mail", side_effect=AssertionError("Kein Mailversand erwartet")):
            case = self.sync()
        response = self.client.get(f"{reverse('rides')}?abgleich=1")

        self.assertContains(response, "Abgleich abgeschlossen.")
        self.assertContains(response, reverse("ride_detail", args=[case.pk]))
        self.assertEqual(response.context["active_count"], 2)
        self.assertEqual(response.context["pending_count"], 2)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_detail_lists_linked_bookings_without_contact_addresses(self):
        case = self.sync()
        response = self.client.get(reverse("ride_detail", args=[case.pk]))

        self.assertContains(response, "Synthetische Person")
        self.assertContains(response, "Zweite Testperson")
        self.assertContains(response, reverse("booking_participant", args=[self.first_seat.pk]))
        self.assertNotContains(response, "kontakt@example.invalid")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_status_is_explicit_requires_csrf_and_does_not_change_the_match(self):
        case = self.sync()
        detail = reverse("ride_detail", args=[case.pk])
        form = {"status": "in_review", "expected_status": "open"}

        self.assertEqual(Client(enforce_csrf_checks=True).post(detail, form).status_code, 403)
        self.assertRedirects(self.client.post(detail, form), f"{detail}?gespeichert=1")

        stale = self.client.post(detail, {"status": "closed", "expected_status": "open"})
        self.assertEqual(stale.status_code, 409)
        case.refresh_from_db()
        self.assertEqual(case.status, "in_review")

        self.assertRedirects(
            self.client.post(detail, {"status": "closed", "expected_status": "in_review"}),
            f"{detail}?gespeichert=1",
        )
        self.sync()
        case.refresh_from_db()
        self.assertEqual(case.status, "closed")
        self.assertTrue(case.currently_matches)

        listing = reverse("rides")
        self.assertContains(self.client.get(listing, {"status": "closed"}), detail)
        self.assertNotContains(self.client.get(listing, {"status": "open"}), detail)
        self.assertRedirects(
            self.client.post(detail, {"status": "open", "expected_status": "closed"}),
            f"{detail}?gespeichert=1",
        )
        self.first_seat.refresh_from_db()
        self.assertEqual(self.first_seat.status, BookingStatus.CONFIRMED)

    def test_inactive_case_preserves_related_bookings_for_review(self):
        case = self.sync()
        self.first_seat.status = BookingStatus.CANCELLED
        self.first_seat.save(update_fields=["status"])
        self.assertEqual(self.client.post(reverse("rides_sync")).status_code, 302)

        case.refresh_from_db()
        self.assertFalse(case.currently_matches)
        response = self.client.get(reverse("ride_detail", args=[case.pk]))
        self.assertContains(response, "Kein Treffer beim letzten Abgleich")
        self.assertContains(response, "Zuletzt zugeordnete Buchungen")
        self.assertContains(response, "Storniert")
