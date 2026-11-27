from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.utils import timezone

from bookings.models import BookingStatus, Carpool, Course, CourseSeat, CourseSession, Participant, RideCase, School
from bookings.rides_matching import sync_ride_cases


RIDES = "/fahrgemeinschaften/"
ACTIONS = ("entfernen", "wiederherstellen", "fahrt-bestaetigen")


class RideManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.editor = users.create_user("test-ride-management-editor")
        cls.viewer = users.create_user("test-ride-management-viewer")
        for user, codename in ((cls.editor, "edit_rides"), (cls.viewer, "view_rides")):
            grant = Permission.objects.get(content_type__app_label="accounts", codename=codename)
            user.user_permissions.add(grant)

        now = timezone.now()
        course = Course.objects.create(name="Synthetischer Lehrgang zur Fahrtenpflege")
        session = CourseSession.objects.create(
            course=course, school=School.WUERZBURG,
            starts_at=now + timedelta(days=3), ends_at=now + timedelta(days=4),
        )
        cls.seat_ids = []
        for index in (1, 2):
            participant = Participant.objects.create(
                name=f"Testperson {index}", email=f"fahrt-{index}@example.invalid",
            )
            cls.seat_ids.append(CourseSeat.objects.create(
                session=session, participant=participant, status=BookingStatus.CONFIRMED,
            ).pk)
        sync_ride_cases(now=now)
        cls.future = RideCase.objects.get()

        starts, ends = now - timedelta(days=4), now - timedelta(days=3)
        cls.past = RideCase.objects.create(
            school=School.WUERZBURG, direction=RideCase.ROUND_TRIP, match_key="management-past",
            travel_date=timezone.localdate(starts), earliest=starts, latest=starts,
            return_date=timezone.localdate(ends), return_earliest=ends, return_latest=ends,
            currently_matches=False,
        )
        cls.single = RideCase.objects.create(
            school=School.WUERZBURG, direction=Carpool.Direction.OUTBOUND, match_key="management-single",
            travel_date=timezone.localdate(starts), earliest=starts, latest=starts, currently_matches=False,
        )

    def action(self, case, action):
        return f"{RIDES}{case.pk}/{action}/"

    def test_removal_requires_confirmation_and_keeps_bookings_until_restored(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)
        remove = self.action(self.future, "entfernen")
        confirmation = client.get(remove)

        self.assertContains(confirmation, "Vorschlag entfernen")
        self.assertContains(confirmation, "Im Verlauf unter „Entfernte“ kannst du ihn wiederherstellen.")
        self.assertIn("no-store", confirmation.headers["Cache-Control"])
        self.future.refresh_from_db()
        self.assertIsNone(self.future.removed_at)

        token = client.cookies["csrftoken"].value
        unconfirmed = client.post(remove, {"csrfmiddlewaretoken": token})
        self.assertEqual(unconfirmed.status_code, 400)
        self.future.refresh_from_db()
        self.assertIsNone(self.future.removed_at)

        removed = client.post(remove, {"confirm": "1", "csrfmiddlewaretoken": token})
        self.assertRedirects(removed, f"{RIDES}?entfernt=1", fetch_redirect_response=False)
        self.future.refresh_from_db()
        self.assertIsNotNone(self.future.removed_at)
        self.assertCountEqual(self.future.seats.values_list("pk", flat=True), self.seat_ids)
        self.assertEqual(CourseSeat.objects.filter(pk__in=self.seat_ids).count(), 2)

        restored = client.post(self.action(self.future, "wiederherstellen"), {
            "csrfmiddlewaretoken": token, "verlauf": "entfernt",
        })
        self.assertRedirects(restored, f"{RIDES}?wiederhergestellt=1", fetch_redirect_response=False)
        self.future.refresh_from_db()
        self.assertIsNone(self.future.removed_at)
        self.assertTrue(self.future.currently_matches)

    def test_completed_round_trip_can_be_confirmed_and_confirmation_can_be_cleared(self):
        self.client.force_login(self.editor)
        action = self.action(self.past, "fahrt-bestaetigen")
        self.assertEqual(self.client.get(action).status_code, 405)
        self.assertEqual(self.client.post(action, {"confirmed": "unknown"}).status_code, 400)
        self.past.refresh_from_db()
        self.assertIsNone(self.past.ride_confirmed_at)

        for value in ("1", "0"):
            with self.subTest(value=value):
                response = self.client.post(action, {"confirmed": value, "verlauf": "regulaer"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, f"{RIDES}{self.past.pk}/?verlauf=regulaer&fahrt={value}")
                self.past.refresh_from_db()
                self.assertEqual(self.past.ride_confirmed_at is not None, value == "1")

    def test_past_test_proposal_returns_to_the_matching_history_mode(self):
        self.client.force_login(self.editor)
        RideCase.objects.filter(pk=self.past.pk).update(is_test=True)
        remove = self.action(self.past, "entfernen")
        confirmation = self.client.get(remove, {"verlauf": "test", "page": "2"})
        self.assertContains(confirmation, 'name="verlauf" value="test"')
        self.assertContains(confirmation, 'name="page" value="2"')

        removed = self.client.post(remove, {"confirm": "1", "verlauf": "test", "page": "2"})
        self.assertRedirects(removed, f"{RIDES}verlauf/?verlauf=test&page=2&entfernt=1", fetch_redirect_response=False)
        restored = self.client.post(self.action(self.past, "wiederherstellen"), {"verlauf": "entfernt"})

        self.assertRedirects(restored, f"{RIDES}verlauf/?verlauf=test&wiederhergestellt=1", fetch_redirect_response=False)
        self.past.refresh_from_db()
        self.assertIsNone(self.past.removed_at)

    def test_restored_future_proposal_without_a_current_match_returns_to_its_detail(self):
        self.client.force_login(self.editor)
        self.client.post(self.action(self.future, "entfernen"), {"confirm": "1"})
        CourseSeat.objects.filter(pk=self.seat_ids[0]).update(status=BookingStatus.CANCELLED)

        restored = self.client.post(self.action(self.future, "wiederherstellen"), {"verlauf": "entfernt"})

        self.assertRedirects(restored, f"{RIDES}{self.future.pk}/?wiederhergestellt=1", fetch_redirect_response=False)
        self.future.refresh_from_db()
        self.assertIsNone(self.future.removed_at)
        self.assertFalse(self.future.currently_matches)

    def test_future_ride_cannot_be_marked_as_completed(self):
        self.client.force_login(self.editor)

        response = self.client.post(self.action(self.future, "fahrt-bestaetigen"), {
            "confirmed": "1", "verlauf": "https://example.invalid/",
        })

        self.assertRedirects(response, f"{RIDES}{self.future.pk}/?aktion=nicht-moeglich", fetch_redirect_response=False)
        self.future.refresh_from_db()
        self.assertIsNone(self.future.ride_confirmed_at)

    def test_all_actions_require_edit_rights_and_csrf(self):
        editor = Client(enforce_csrf_checks=True)
        editor.force_login(self.editor)
        self.client.force_login(self.viewer)

        for action in ACTIONS:
            with self.subTest(action=action):
                path = self.action(self.future, action)
                data = {"confirm": "1", "confirmed": "1"}
                self.assertEqual(self.client.get(path).status_code, 403)
                self.assertEqual(self.client.post(path, data).status_code, 403)
                self.assertEqual(editor.post(path, data).status_code, 403)

    def test_older_single_leg_is_not_manageable_through_round_trip_actions(self):
        self.client.force_login(self.editor)

        for action in ACTIONS:
            with self.subTest(action=action):
                path = self.action(self.single, action)
                response = self.client.post(path, {"confirm": "1", "confirmed": "1"})
                self.assertEqual(response.status_code, 404)
