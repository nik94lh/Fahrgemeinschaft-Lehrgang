from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from bookings import rides_matching
from bookings.booking_deletion import BookingDeletionResult, delete_booking, deletion_token
from bookings.models import BookingStatus, Carpool, Course, CourseSeat, CourseSession, Participant, RideCase, School
from bookings.ride_participation import (
    active_participation,
    invite_code,
    participation_for_code,
    portal_overview,
    record_ride_reply,
    run_automatic_ride_inquiries,
    set_ride_automation,
    start_ride_inquiry,
)
from mailimport.models import ParticipantRecognitionConfiguration


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.test")
class RideLifecycleTests(TestCase):
    now = datetime(2032, 2, 1, 10, tzinfo=UTC)
    starts = now + timedelta(days=3)

    def booking(self, index, *, hours=0, is_test=True):
        begins = self.starts + timedelta(hours=hours)
        session = CourseSession.objects.create(
            course=Course.objects.create(name=f"Fiktiver Lehrgang {index}"),
            school=School.WUERZBURG, starts_at=begins, ends_at=begins + timedelta(days=1),
        )
        return CourseSeat.objects.create(
            session=session, status=BookingStatus.CONFIRMED, is_test=is_test,
            participant=Participant.objects.create(name=f"Testperson {index}", email=f"ride-{index}@example.invalid"),
        )

    def match_case(self):
        self.assertEqual(rides_matching.sync_ride_cases(now=self.now), 1)
        return RideCase.objects.get()

    def cancel(self, seat):
        seat.status = BookingStatus.CANCELLED
        seat.save(update_fields=["status"])

    def test_future_unmatched_invitation_is_suspended_and_can_recover_on_same_case(self):
        first = self.booking(1)
        second = self.booking(2)
        case = self.match_case()
        self.assertEqual(start_ride_inquiry(case.pk, now=self.now), 2)
        entry = case.participation_requests.get(seat=first)
        code = invite_code(entry)

        with patch("bookings.ride_participation.timezone.now", return_value=self.now):
            self.assertEqual(participation_for_code(code), entry)
            self.cancel(second)
            self.assertEqual(rides_matching.sync_ride_cases(now=self.now), 0)
            self.assertIsNone(participation_for_code(code))
            self.assertIsNone(active_participation(entry.pk, entry.token_digest))

            replacement = self.booking(3)
            self.assertEqual(rides_matching.sync_ride_cases(now=self.now), 1)
            case.refresh_from_db()
            self.assertTrue(case.currently_matches)
            self.assertEqual(RideCase.objects.count(), 1)
            self.assertCountEqual(case.seats.all(), [first, replacement])
            self.assertEqual(participation_for_code(code), entry)

    def test_unmatched_case_keeps_only_its_started_course_access_until_expiry(self):
        first = self.booking(1)
        second = self.booking(2, hours=2)
        case = self.match_case()
        start_ride_inquiry(case.pk, now=self.now)
        early = case.participation_requests.get(seat=first)
        later = case.participation_requests.get(seat=second)
        midway = self.starts + timedelta(hours=1)
        self.assertEqual(rides_matching.sync_ride_cases(now=midway), 0)
        with patch("bookings.ride_participation.timezone.now", return_value=midway):
            self.assertEqual(participation_for_code(invite_code(early)), early)
            self.assertIsNone(participation_for_code(invite_code(later)))
        with patch("bookings.ride_participation.timezone.now", return_value=early.expires_at):
            self.assertIsNone(participation_for_code(invite_code(early)))

    def test_seat_excluded_from_active_case_cannot_reply_or_share_contacts(self):
        excluded, remaining, pending = [self.booking(index) for index in (1, 2, 3)]
        case = self.match_case()
        start_ride_inquiry(case.pk, now=self.now)
        excluded_entry = case.participation_requests.get(seat=excluded)
        remaining_entry = case.participation_requests.get(seat=remaining)
        editor = get_user_model().objects.create_superuser("synthetic-ride-editor")

        with patch("bookings.ride_participation.timezone.now", return_value=self.now):
            self.client.force_login(editor)
            for entry in (excluded_entry, remaining_entry):
                record_ride_reply(entry.pk, entry.token_digest, answer="yes", channels=["email"], consent=True)
            self.assertEqual(portal_overview(remaining_entry)[1][0]["email"], excluded_entry.email)

            ParticipantRecognitionConfiguration.objects.create(pk=1, functional_addresses=[excluded_entry.email])
            self.assertEqual(rides_matching.sync_ride_cases(now=self.now), 1)
            case.refresh_from_db()
            self.assertTrue(case.currently_matches)
            self.assertCountEqual(case.seats.all(), [remaining, pending])
            self.assertIsNone(participation_for_code(invite_code(excluded_entry)))
            self.assertIsNone(active_participation(excluded_entry.pk, excluded_entry.token_digest))
            self.assertEqual(active_participation(remaining_entry.pk, remaining_entry.token_digest), remaining_entry)
            self.assertEqual(portal_overview(remaining_entry), (1, []))
            self.assertContains(
                self.client.get(reverse("rides"), secure=True),
                "Kontaktfreigabe beendet, weitere Antworten offen",
            )

    def test_manual_removal_freezes_memberships_and_prevents_automatic_inquiry(self):
        first = self.booking(1)
        second = self.booking(2)
        case = self.match_case()
        removed = rides_matching.remove_ride_case(case.pk, now=self.now)
        self.assertEqual(removed.removed_at, self.now)
        self.assertFalse(removed.currently_matches)
        self.assertEqual(rides_matching.remove_ride_case(case.pk, now=self.now + timedelta(hours=1)).removed_at, self.now)
        self.cancel(second)
        replacement = self.booking(3, hours=1)

        self.assertEqual(rides_matching.sync_ride_cases(now=self.now), 0)
        self.assertEqual(RideCase.objects.count(), 1)
        self.assertCountEqual(case.seats.all(), [first, second])
        with self.assertRaises(ValueError):
            rides_matching.set_ride_case_status(case.pk, RideCase.Status.IN_REVIEW, expected_status=RideCase.Status.OPEN)
        with self.assertRaises(ValueError):
            start_ride_inquiry(case.pk, now=self.now)
        set_ride_automation(True)
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 0)

        restored = rides_matching.restore_ride_case(case.pk, now=self.now)
        self.assertTrue(restored.currently_matches)
        self.assertIsNone(restored.removed_at)
        self.assertEqual(restored.latest, replacement.session.starts_at)
        self.assertCountEqual(restored.seats.all(), [first, replacement])
        self.assertEqual(run_automatic_ride_inquiries(now=self.now), 2)

    def test_restoring_an_unmatched_case_keeps_same_case_hidden_until_it_qualifies(self):
        self.booking(1)
        second = self.booking(2)
        case = self.match_case()
        rides_matching.remove_ride_case(case.pk, now=self.now)
        self.cancel(second)

        restored = rides_matching.restore_ride_case(case.pk, now=self.now)
        self.assertIsNone(restored.removed_at)
        self.assertFalse(restored.currently_matches)
        self.booking(3)
        self.assertEqual(rides_matching.sync_ride_cases(now=self.now), 1)
        self.assertEqual(RideCase.objects.get(currently_matches=True).pk, case.pk)

    def test_manual_removal_always_disables_existing_grants_including_started_courses(self):
        first = self.booking(1)
        self.booking(2)
        case = self.match_case()
        start_ride_inquiry(case.pk, now=self.now)
        entry = case.participation_requests.get(seat=first)
        during = self.starts + timedelta(minutes=30)

        with patch("bookings.ride_participation.timezone.now", return_value=during):
            self.assertEqual(participation_for_code(invite_code(entry)), entry)
            rides_matching.remove_ride_case(case.pk)
            self.assertIsNone(participation_for_code(invite_code(entry)))
            self.assertIsNone(active_participation(entry.pk, entry.token_digest))

    def test_deleting_one_of_three_bookings_keeps_two_person_match_current(self):
        remaining = [self.booking(1), self.booking(2)]
        removed = self.booking(3, hours=1)
        case = self.match_case()
        with patch("bookings.booking_deletion.timezone.now", return_value=self.now):
            self.assertEqual(delete_booking(removed.pk, deletion_token(removed)), BookingDeletionResult.DELETED)
        case.refresh_from_db()
        self.assertTrue(case.currently_matches)
        self.assertEqual(case.latest, self.starts)
        self.assertCountEqual(case.seats.all(), remaining)

    def test_confirming_a_ride_is_distinct_from_closing_and_requires_return_time(self):
        self.booking(1, is_test=False)
        self.booking(2, hours=2, is_test=False)
        case = self.match_case()
        set_confirmed = rides_matching.set_ride_confirmed
        end = case.return_latest

        with self.assertRaises(ValueError):
            set_confirmed(case.pk, True, now=end - timedelta(microseconds=1))
        self.assertEqual(set_confirmed(case.pk, True, now=end).ride_confirmed_at, end)
        self.assertEqual(set_confirmed(case.pk, True, now=end + timedelta(hours=1)).ride_confirmed_at, end)
        case.refresh_from_db()
        self.assertEqual(case.status, RideCase.Status.OPEN)
        self.assertIsNone(set_confirmed(case.pk, False, now=end).ride_confirmed_at)
        self.assertIsNone(set_confirmed(case.pk, False, now=end).ride_confirmed_at)

    def test_manual_ride_confirmation_rejects_removed_and_legacy_cases(self):
        self.booking(1)
        self.booking(2)
        case = self.match_case()
        past = case.return_latest + timedelta(hours=1)
        rides_matching.remove_ride_case(case.pk, now=past)
        earlier = self.starts - timedelta(days=1)
        legacy = RideCase.objects.create(
            match_key="synthetic-legacy", direction=Carpool.Direction.RETURN,
            school=School.WUERZBURG, travel_date=earlier.date(), earliest=earlier, latest=earlier,
        )
        for case_id in (case.pk, legacy.pk, 999999):
            with self.subTest(case_id=case_id), self.assertRaises(ValueError):
                rides_matching.set_ride_confirmed(case_id, True, now=past)
