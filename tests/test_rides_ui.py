from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Carpool, Course, CourseSeat, CourseSession, Participant, RideCase, School


class RideCaseWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        start = timezone.localtime(timezone.now() + timedelta(days=4)).replace(hour=8, minute=15, second=0, microsecond=0)
        ends = start + timedelta(days=2, hours=9, minutes=15)
        course = Course.objects.create(name="Übungslehrgang")
        session = CourseSession.objects.create(
            course=course,
            school=School.WUERZBURG,
            starts_at=start,
            ends_at=ends,
        )
        later_session = CourseSession.objects.create(
            course=course,
            school=School.WUERZBURG,
            starts_at=start + timedelta(minutes=45),
            ends_at=ends - timedelta(minutes=45),
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
            session=later_session,
            participant=Participant.objects.create(name="Zweite Testperson"),
            status=BookingStatus.CONFIRMED,
        )

    def sync(self):
        response = self.client.post(reverse("rides_sync"))
        self.assertRedirects(response, f"{reverse('rides')}?abgleich=1")

        return RideCase.objects.get(direction=RideCase.ROUND_TRIP)

    def test_list_waits_for_a_post_and_shows_persisted_matches(self):
        listing = self.client.get(reverse("rides"))
        self.assertContains(listing, "Abgleich starten")
        self.assertContains(listing, "Noch keine Fahrgemeinschaftsvorschläge")
        self.assertNotContains(listing, "Frühere Einzelstrecken")
        self.assertEqual(self.client.get(reverse("rides_sync")).status_code, 405)
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse("rides_sync")).status_code, 403)

        with patch("django.core.mail.send_mail", side_effect=AssertionError("Kein Mailversand erwartet")):
            case = self.sync()
        response = self.client.get(f"{reverse('rides')}?abgleich=1")

        self.assertContains(response, "Abgleich abgeschlossen.")
        self.assertContains(response, reverse("ride_detail", args=[case.pk]))
        self.assertContains(response, "Fahrgemeinschaftsvorschläge")
        self.assertContains(response, "Bearbeitungsstand")
        self.assertContains(response, case.travel_date.strftime("%d.%m.%Y"))
        self.assertContains(response, case.return_date.strftime("%d.%m.%Y"))
        self.assertContains(response, "08:15–09:00")
        self.assertContains(response, "16:45–17:30")
        self.assertNotContains(response, "Fallstatus")
        self.assertEqual(response.context["active_count"], 1)
        self.assertEqual(response.context["pending_count"], 1)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_detail_lists_linked_bookings_without_contact_addresses(self):
        case = self.sync()
        response = self.client.get(reverse("ride_detail", args=[case.pk]))

        self.assertContains(response, "Synthetische Person")
        self.assertContains(response, "Zweite Testperson")
        self.assertContains(response, reverse("booking_participant", args=[self.first_seat.pk]))
        self.assertContains(response, "Fahrgemeinschaftsvorschlag")
        self.assertContains(response, "Lehrgangsbeginn")
        self.assertContains(response, "Lehrgangsende")
        self.assertContains(response, f"{case.travel_date:%d.%m.%Y}, 08:15")
        self.assertContains(response, f"{case.return_date:%d.%m.%Y}, 17:30")
        self.assertNotContains(response, "Fallstatus")
        self.assertNotContains(response, "kontakt@example.invalid")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_single_leg_history_isolated_from_combined_list_and_counts(self):
        current = self.sync()
        legacy = RideCase.objects.create(
            match_key="legacy-ui-fixture",
            school=School.WUERZBURG,
            direction=Carpool.Direction.OUTBOUND,
            travel_date=current.travel_date,
            earliest=current.earliest,
            latest=current.latest,
            status=RideCase.Status.IN_REVIEW,
            currently_matches=True,
        )
        url = reverse("rides")
        current_detail = reverse("ride_detail", args=[current.pk])
        legacy_detail = reverse("ride_detail", args=[legacy.pk])

        listing = self.client.get(url)
        self.assertContains(listing, "Frühere Einzelstrecken")
        self.assertContains(listing, current_detail)
        self.assertNotContains(listing, legacy_detail)
        self.assertEqual(listing.context["active_count"], 1)
        self.assertEqual(listing.context["pending_count"], 1)

        archive = self.client.get(url, {"archiv": "1", "status": RideCase.Status.IN_REVIEW})
        self.assertContains(archive, "Frühere Einzelstrecken")
        self.assertContains(archive, legacy_detail)
        self.assertNotContains(archive, current_detail)
        self.assertNotContains(archive, "Beim letzten Abgleich passend")

        fetched = self.client.get(legacy_detail)
        self.assertContains(fetched, f'{url}?archiv=1')
        self.assertContains(fetched, "Hinfahrt")
        self.assertContains(fetched, "Bearbeitungsstand")

    def test_archive_pagination_preserves_archive_and_workflow_filter(self):
        session = self.first_seat.session
        RideCase.objects.bulk_create([
            RideCase(
                match_key=f"legacy-archive-page-{index}",
                school=session.school,
                direction=Carpool.Direction.OUTBOUND,
                travel_date=timezone.localdate(session.starts_at),
                earliest=session.starts_at,
                latest=session.starts_at,
                status=RideCase.Status.CLOSED,
            )
            for index in range(26)
        ])

        response = self.client.get(reverse("rides"), {"archiv": "1", "status": RideCase.Status.CLOSED})
        self.assertContains(response, "?archiv=1&amp;status=closed&amp;page=2")

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
