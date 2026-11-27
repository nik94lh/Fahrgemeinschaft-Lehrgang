from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from bookings.models import Carpool, Participant, RideCase, RideNotice, RideParticipation, School
from tests.workspace_client import WorkspaceTestCase


class RideHistoryTests(WorkspaceTestCase):
    url = "/fahrgemeinschaften/verlauf/"

    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def history(self, **filters):
        with patch("bookings.ride_history.timezone.now", return_value=self.now):
            return self.client.get(self.url, filters)

    def case(self, name, *, days=-1, direction=RideCase.ROUND_TRIP, **fields):
        start = self.now + timedelta(days=days)
        end = start + timedelta(days=1)
        round_trip = direction == RideCase.ROUND_TRIP
        return RideCase.objects.create(
            match_key=name, school=School.WUERZBURG, direction=direction,
            travel_date=timezone.localdate(start), earliest=start, latest=start,
            return_date=timezone.localdate(end) if round_trip else None,
            return_earliest=end if round_trip else None, return_latest=end if round_trip else None,
            **fields,
        )

    def notice(self, case, participant, *kinds):
        entry = RideParticipation.objects.create(
            case=case, participant=participant, email=participant.email,
            token_digest=f"{case.pk:032x}{participant.pk:032x}", expires_at=self.now - timedelta(minutes=1),
            answer=RideParticipation.Answer.NO,
        )
        RideNotice.objects.bulk_create([RideNotice(participation=entry, kind=kind) for kind in kinds])

    def test_history_requires_ride_view_access(self):
        self.assertRedirects(Client().get(self.url), f"/anmelden/?next={self.url}", fetch_redirect_response=False)
        reader = get_user_model().objects.create_user("ride-history-reader")
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        reader.user_permissions.add(Permission.objects.get(content_type__app_label="accounts", codename="view_rides"))
        self.assertContains(self.client.get(self.url), "Verlauf der Fahrgemeinschaften")
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_history_separates_regular_test_and_removed_proposals(self):
        past = self.case("past", days=-3)
        started = self.case("started", days=0)
        closed = self.case("closed", days=4, status=RideCase.Status.CLOSED)
        self.case("future-open", days=7)
        self.case("legacy", direction=Carpool.Direction.OUTBOUND)
        test = self.case("past-test", is_test=True)
        removed = self.case("future-removed", days=9, removed_at=self.now)
        removed_test = self.case("past-removed-test", is_test=True, removed_at=self.now - timedelta(minutes=1))

        for mode, cases in (
            ("regulaer", [closed, started, past]),
            ("test", [test]),
            ("entfernt", [removed, removed_test]),
        ):
            with self.subTest(mode=mode):
                response = self.history(verlauf=mode)
                self.assertEqual(list(response.context["page_obj"]), cases)
                for case in cases:
                    self.assertContains(response, f'{reverse("ride_detail", args=[case.pk])}?verlauf={mode}')
        self.assertEqual(list(self.history(verlauf="invalid").context["page_obj"]), [closed, started, past])
        self.assertEqual(list(self.history().context["page_obj"]), [closed, started, past])

    def test_counts_only_explicit_confirmation_and_deduplicates_release_evidence(self):
        confirmed = self.case("confirmed", ride_confirmed_at=self.now)
        closed = self.case("closed", status=RideCase.Status.CLOSED)
        test = self.case("test", is_test=True, ride_confirmed_at=self.now)
        removed = self.case("removed", removed_at=self.now, ride_confirmed_at=self.now)
        first, second, third = [
            Participant.objects.create(name=f"Synthetische Person {number}", email=f"history-{number}@example.invalid")
            for number in range(3)
        ]
        self.notice(confirmed, first, RideNotice.Kind.CONTACTS, RideNotice.Kind.CONTACTS, RideNotice.Kind.WITHDRAWAL)
        self.notice(closed, first, RideNotice.Kind.CONTACTS)
        self.notice(closed, second, RideNotice.Kind.CONTACTS)
        self.notice(closed, third, RideNotice.Kind.INVITATION, RideNotice.Kind.NEW_PEER)
        self.notice(test, third, RideNotice.Kind.CONTACTS)
        self.notice(removed, third, RideNotice.Kind.CONTACTS)

        response = self.history()
        self.assertEqual(response.context["totals"], {"proposals": 2, "people": 2, "confirmed": 1})
        self.assertContains(response, "Fahrt bestätigt")
        self.assertContains(response, "Abgeschlossen")
        for participant in (first, second, third):
            self.assertNotContains(response, participant.name)
            self.assertNotContains(response, participant.email)
        for mode in ("test", "entfernt"):
            self.assertEqual(self.history(verlauf=mode).context["totals"], {"proposals": 1, "people": 1, "confirmed": 1})

    def test_pagination_preserves_mode_and_totals_cover_the_full_selection(self):
        cases = [self.case(f"test-{index}", is_test=True) for index in range(26)]
        first = self.history(verlauf="test")
        self.assertContains(first, "?verlauf=test&amp;page=2")

        second = self.history(verlauf="test", page=2)
        self.assertEqual(list(second.context["page_obj"]), [cases[0]])
        self.assertEqual(second.context["totals"], {"proposals": 26, "people": 0, "confirmed": 0})
        self.assertContains(second, f'{reverse("ride_detail", args=[cases[0].pk])}?verlauf=test&amp;page=2')
