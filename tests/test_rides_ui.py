from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, Carpool, Course, CourseSeat, CourseSession, Participant, RideCase, School
from tests.workspace_client import WorkspaceTestCase


class RideCaseWorkspaceTests(WorkspaceTestCase):
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
            participant=Participant.objects.create(name="Zweite Testperson", email="zweite.testperson@example.invalid"),
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
        client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("rides_sync")).status_code, 403)

        with patch("django.core.mail.send_mail", side_effect=AssertionError("Kein Mailversand erwartet")):
            case = self.sync()
        response = self.client.get(f"{reverse('rides')}?abgleich=1")

        self.assertContains(response, "Abgleich abgeschlossen.")
        self.assertContains(response, reverse("ride_detail", args=[case.pk]))
        self.assertContains(response, "Fahrgemeinschaftsvorschläge")
        self.assertContains(response, 'class="ride-card"')
        self.assertContains(response, "SFS Würzburg")
        self.assertContains(response, '<strong class="ride-party-count">2</strong>', html=True)
        self.assertContains(
            response, f'aria-label="Details zum Vorschlag für SFS Würzburg vom {case.travel_date:%d.%m.%Y} bis {case.return_date:%d.%m.%Y}"',
        )
        self.assertNotContains(response, "<table")
        self.assertNotContains(response, "Synthetische Person")
        self.assertNotContains(response, "kontakt@example.invalid")
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

    def test_standby_switches_at_local_midnight_and_preserves_the_operator_status(self):
        case = self.sync()
        case.status = RideCase.Status.IN_REVIEW
        case.save(update_fields=["status"])
        window_opens = case.travel_date - timedelta(days=14)
        cutoff = datetime.combine(window_opens, time.min, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC)
        listing_url = reverse("rides")
        detail_url = reverse("ride_detail", args=[case.pk])
        params = {"status": RideCase.Status.IN_REVIEW}

        with patch("django.utils.timezone.now", return_value=cutoff - timedelta(seconds=1)):
            standby = self.client.get(listing_url, params, HTTP_X_BMS_LIVE="1")
            self.assertContains(standby, '<span class="status-label ride-phase-standby">Standby</span>', html=True)
            self.assertContains(
                standby, f'<time datetime="{window_opens:%Y-%m-%d}">{window_opens:%d.%m.%Y}</time>', html=True,
            )
            self.assertNotContains(standby, 'class="status-label ride-phase-ready"')
            self.assertContains(self.client.get(detail_url), "Standby")
        with patch("django.utils.timezone.now", return_value=cutoff):
            ready = self.client.get(listing_url, params, HTTP_X_BMS_LIVE="1")
            self.assertContains(ready, '<span class="status-label ride-phase-ready">Bereit zur Vermittlung</span>', html=True)
            self.assertNotContains(ready, 'class="status-label ride-phase-standby"')
            self.assertContains(self.client.get(detail_url), "Bereit zur Vermittlung")
        case.refresh_from_db()
        self.assertEqual(case.status, RideCase.Status.IN_REVIEW)
        self.assertTrue(case.currently_matches)

    def test_saved_lead_time_applies_without_refreshing_the_matches(self):
        case = self.sync()
        listing_url = reverse("rides")
        self.assertContains(self.client.get(listing_url), 'class="status-label ride-phase-ready"')

        response = self.client.post(reverse("rides_settings"), {"lead_days": 0})
        self.assertEqual(response.status_code, 302)
        listing = self.client.get(listing_url)
        self.assertContains(listing, 'class="status-label ride-phase-standby"')
        self.assertContains(listing, reverse("ride_detail", args=[case.pk]))
        self.assertEqual(listing.context["active_count"], 1)

    def test_phase_filters_use_berlin_calendar_cutoff_and_only_active_unstarted_cases(self):
        berlin = ZoneInfo("Europe/Berlin")
        now = datetime.combine(timezone.localdate(timezone=berlin), time.min, tzinfo=berlin).astimezone(UTC)
        initial_day = now.astimezone(berlin)

        def make_case(key, departure, **overrides):
            return_at = departure + timedelta(days=1)
            return RideCase.objects.create(**({
                "match_key": key, "school": School.WUERZBURG, "direction": RideCase.ROUND_TRIP,
                "travel_date": departure.astimezone(berlin).date(), "earliest": departure, "latest": departure,
                "return_date": return_at.astimezone(berlin).date(), "return_earliest": return_at, "return_latest": return_at,
            } | overrides))

        cutoff = initial_day + timedelta(days=14, hours=8)
        ready = make_case("phase-ready", cutoff)
        standby = make_case("phase-standby", cutoff + timedelta(days=1), status=RideCase.Status.IN_REVIEW)
        make_case("phase-closed", cutoff, status=RideCase.Status.CLOSED)
        make_case("phase-stale", cutoff, currently_matches=False)
        make_case("phase-started", now)

        with patch("django.utils.timezone.now", return_value=now):
            for phase, expected in (("ready", ready), ("standby", standby)):
                response = self.client.get(reverse("rides"), {"phase": phase})
                self.assertEqual(list(response.context["page_obj"]), [expected])
            filtered = self.client.get(reverse("rides"), {"phase": "ready", "status": RideCase.Status.IN_REVIEW})
            self.assertEqual(list(filtered.context["page_obj"]), [])

    def test_phase_and_status_stay_in_pager_details_and_post_redirects(self):
        case = self.sync()
        RideCase.objects.bulk_create([
            RideCase(
                match_key=f"phase-page-{index}", school=case.school, direction=RideCase.ROUND_TRIP,
                travel_date=case.travel_date, earliest=case.earliest - timedelta(hours=1), latest=case.latest,
                return_date=case.return_date, return_earliest=case.return_earliest, return_latest=case.return_latest,
            ) for index in range(25)
        ])
        list_url = reverse("rides")
        detail = reverse("ride_detail", args=[case.pk])
        filters = {"status": "open", "phase": "ready"}
        first_page = self.client.get(list_url, filters)
        self.assertContains(first_page, "?status=open&amp;phase=ready&amp;page=2")
        filters["page"] = "2"
        second_page = self.client.get(list_url, filters)
        query = "status=open&phase=ready&page=2"
        selected_detail = f"{detail}?{query}"
        self.assertContains(second_page, f'{detail}?status=open&amp;phase=ready&amp;page=2')

        status_update = self.client.post(selected_detail, {"status": "in_review", "expected_status": "open"})
        self.assertRedirects(status_update, f"{detail}?gespeichert=1&{query}")
        self.assertContains(self.client.get(status_update.url), f"{list_url}?status=open&amp;phase=ready&amp;page=2")

        refreshed = self.client.post(reverse("rides_sync"), filters)
        self.assertRedirects(refreshed, f"{list_url}?abgleich=1&{query}")

    def test_started_and_closed_cases_show_their_state(self):
        case = self.sync()
        with patch("django.utils.timezone.now", return_value=case.earliest):
            started = self.client.get(reverse("rides"))
        self.assertContains(started, "Lehrgang begonnen")
        self.assertNotContains(started, 'class="status-label ride-phase-ready"')

        case.status = RideCase.Status.CLOSED
        case.save(update_fields=["status"])
        closed = self.client.get(reverse("rides"), {"status": RideCase.Status.CLOSED})
        self.assertContains(closed, "Abgeschlossen")
        self.assertContains(closed, '<span class="status-label">Passend</span>', html=True)
        self.assertNotContains(closed, 'class="status-label ride-phase-ready"')

    def test_past_proposal_keeps_a_distinct_time_state_after_reconciliation(self):
        case = self.sync()
        with patch("django.utils.timezone.now", return_value=case.earliest):
            self.client.post(reverse("rides_sync"))
            listing = self.client.get(reverse("rides"))
        self.assertContains(listing, "Terminbeginn verstrichen")
        self.assertNotContains(listing, 'class="status-label ride-phase-ready"')

    def test_empty_after_sync_shows_match_requirements_and_permitted_next_actions(self):
        self.first_seat.status = BookingStatus.AWAITING_CONFIRMATION
        self.first_seat.save(update_fields=["status"])
        response = self.client.post(reverse("rides_sync"), follow=True)

        self.assertContains(response, "Keine aktuell passenden Vorschläge.")
        self.assertContains(response, "mögliche Doppelbuchungen")
        self.assertContains(response, f'href="{reverse("bookings")}?status=confirmed"')
        self.assertContains(response, "Offene Klärungsfälle ansehen")

        reader = get_user_model().objects.create_user("ride-reader")
        reader.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_rides"))
        self.client.force_login(reader)
        listing = self.client.get(reverse("rides"))
        self.assertContains(listing, "mögliche Doppelbuchungen")
        self.assertNotContains(listing, "Bestätigte Buchungen ansehen")
        self.assertNotContains(listing, "Offene Klärungsfälle ansehen")

    def test_detail_lists_linked_bookings_without_contact_addresses(self):
        case = self.sync()
        response = self.client.get(reverse("ride_detail", args=[case.pk]))

        self.assertContains(response, "Synthetische Person")
        self.assertContains(response, "Zweite Testperson")
        booking_link = reverse("booking_detail", args=[self.first_seat.pk])
        self.assertContains(response, f'<a href="{booking_link}">Buchungs-ID {self.first_seat.pk}</a>', html=True)
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
        self.assertNotContains(listing, "Frühere Einzelstrecken")
        self.assertNotContains(listing, 'href="?archiv=1"')
        self.assertContains(listing, current_detail)
        self.assertNotContains(listing, legacy_detail)
        self.assertEqual(listing.context["active_count"], 1)
        self.assertEqual(listing.context["pending_count"], 1)

        archive = self.client.get(url, {"archiv": "1", "status": RideCase.Status.IN_REVIEW})
        self.assertContains(archive, "Frühere Einzelstrecken")
        self.assertContains(archive, legacy_detail)
        self.assertNotContains(archive, current_detail)
        self.assertNotContains(archive, 'class="ride-card"')
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

        client = self.authenticated_client(enforce_csrf_checks=True)
        self.assertEqual(client.post(detail, form).status_code, 403)
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
        filtered = self.client.get(listing, {"status": "open"})
        self.assertNotContains(filtered, detail)
        self.assertContains(filtered, "Keine Vorschläge mit diesem Bearbeitungsstand.")
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
        listing = self.client.get(reverse("rides"))
        self.assertContains(listing, "Kein aktueller Treffer")
        self.assertNotContains(listing, 'class="status-label ride-phase-ready"')
        response = self.client.get(reverse("ride_detail", args=[case.pk]))
        self.assertContains(response, "Kein Treffer beim letzten Abgleich")
        self.assertContains(response, "Zuletzt zugeordnete Buchungen")
        self.assertContains(response, "Storniert")
