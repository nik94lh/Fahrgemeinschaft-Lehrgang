from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from bookings.models import (
    BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, RideMailDelivery, RideNotice,
    RideParticipation, School,
)
from bookings.ride_codes import code_digest, legacy_code
from bookings.ride_mail import deliver_ride_mail
from bookings.ride_participation import (
    expire_booking_participation, invite_code, participation_for_code, portal_overview, record_ride_reply,
    ride_mail_payload, start_ride_inquiry,
)
from bookings.rides_matching import BERLIN, sync_ride_cases


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.test")
class WeeklyRideParticipationTests(TestCase):
    before = datetime(2032, 1, 30, 9, tzinfo=UTC)
    during_first_week = datetime(2032, 2, 3, 9, tzinfo=UTC)

    def bookings(self, *, is_test):
        session = CourseSession.objects.create(
            course=Course.objects.create(name="Fiktiver Mehrwochenlehrgang"), school=School.WUERZBURG,
            starts_at=datetime(2032, 2, 2, 8, tzinfo=UTC), ends_at=datetime(2032, 2, 13, 14, tzinfo=UTC),
            is_test=is_test,
        )
        domain = "example.invalid" if is_test else "example.com"
        return [
            CourseSeat.objects.create(
                session=session, status=BookingStatus.CONFIRMED, is_test=is_test,
                participant=Participant.objects.create(name=f"Fiktive Person {i}", email=f"weekly-{i}@{domain}"),
            )
            for i in (1, 2)
        ]

    def weekly_cases(self):
        self.assertEqual(sync_ride_cases(now=self.before), 2)
        return list(RideCase.objects.order_by("travel_date"))

    def consent(self, entry):
        record_ride_reply(entry.pk, entry.token_digest, answer="yes", channels=["email"], consent=True)

    def legacy_case(self, seats):
        session = seats[0].session
        case = RideCase.objects.create(
            match_key="legacy-synthetic-whole-course", school=session.school, direction=RideCase.ROUND_TRIP,
            travel_date=session.starts_at.astimezone(BERLIN).date(), return_date=session.ends_at.astimezone(BERLIN).date(),
            earliest=session.starts_at, latest=session.starts_at, is_test=seats[0].is_test,
            return_earliest=session.ends_at, return_latest=session.ends_at,
        )
        case.seats.add(*seats)
        return case

    def legacy_invitation(self, case, seat):
        entry = RideParticipation(
            case=case, seat=seat, session=seat.session, participant=seat.participant,
            email=seat.participant.email, expires_at=seat.session.ends_at + timedelta(days=1),
        )
        code = legacy_code(entry.token_seed)
        entry.token_digest = code_digest(code)
        entry.save()
        notice = RideNotice.objects.create(participation=entry, kind=RideNotice.Kind.INVITATION)
        return entry, code, notice

    def test_later_segment_can_start_after_course_begins_with_independent_consent_and_expiry(self):
        first_seat, second_seat = self.bookings(is_test=True)
        first_week, next_week = self.weekly_cases()
        self.assertEqual(start_ride_inquiry(first_week.pk, now=self.before), 2)
        self.assertEqual(sync_ride_cases(now=self.during_first_week), 1)
        with self.assertRaises(ValueError):
            start_ride_inquiry(first_week.pk, now=self.during_first_week)
        self.assertEqual(start_ride_inquiry(next_week.pk, now=self.during_first_week), 2)

        first = first_week.participation_requests.get(seat=first_seat)
        next_entry = next_week.participation_requests.get(seat=first_seat)
        self.assertNotEqual(invite_code(first), invite_code(next_entry))
        self.assertEqual(first.expires_at, first_week.return_latest + timedelta(days=1))
        self.assertEqual(next_entry.expires_at, next_week.return_latest + timedelta(days=1))
        self.assertLess(first.expires_at, next_week.earliest)
        with patch("bookings.ride_participation.timezone.now", return_value=self.during_first_week):
            self.assertEqual(participation_for_code(invite_code(first)), first)
            self.assertEqual(participation_for_code(invite_code(next_entry)), next_entry)
            RideCase.objects.filter(pk=next_week.pk).update(currently_matches=False)
            self.assertIsNone(participation_for_code(invite_code(next_entry)))
            RideCase.objects.filter(pk=next_week.pk).update(currently_matches=True)
            self.consent(first)
            self.consent(first_week.participation_requests.get(seat=second_seat))
            self.consent(next_entry)
            self.assertEqual(len(portal_overview(first)[1]), 1)
            self.assertEqual(portal_overview(next_entry), (1, []))
        with patch("bookings.ride_participation.timezone.now", return_value=first.expires_at):
            self.assertIsNone(participation_for_code(invite_code(first)))
            self.assertEqual(participation_for_code(invite_code(next_entry)), next_entry)

    def test_booking_correction_and_cancellation_revoke_each_week(self):
        corrected, cancelled = self.bookings(is_test=True)
        cases = self.weekly_cases()
        with patch("bookings.ride_participation.timezone.now", return_value=self.before):
            for case in cases:
                self.assertEqual(start_ride_inquiry(case.pk), 2)
                for entry in case.participation_requests.all():
                    self.consent(entry)
            expire_booking_participation(corrected.pk)
            for case in cases:
                revised = case.participation_requests.get(seat=corrected)
                peer = case.participation_requests.get(seat=cancelled)
                self.assertEqual(revised.expires_at, self.before)
                self.assertIsNone(participation_for_code(invite_code(revised)))
                self.assertTrue(peer.notices.filter(kind=RideNotice.Kind.WITHDRAWAL).exists())
                self.assertEqual(portal_overview(peer)[1], [])
            CourseSeat.objects.filter(pk=cancelled.pk).update(status=BookingStatus.CANCELLED)
            for case in cases:
                self.assertIsNone(participation_for_code(invite_code(case.participation_requests.get(seat=cancelled))))

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    @patch("mailimport.outbound.send_ride_mail")
    def test_mail_worker_cancels_elapsed_initial_invitations_but_delivers_next_week(self, send, outbound):
        self.bookings(is_test=False)
        first_week, next_week = self.weekly_cases()
        for case in (first_week, next_week):
            self.assertEqual(start_ride_inquiry(case.pk, now=self.before), 2)

        with patch("bookings.ride_participation.timezone.now", return_value=self.during_first_week):
            self.assertEqual(deliver_ride_mail(), 2)

        for case, status in ((first_week, RideMailDelivery.Status.CANCELLED), (next_week, RideMailDelivery.Status.SENT)):
            self.assertEqual(RideMailDelivery.objects.filter(notice__participation__case=case, status=status).count(), 2)
        self.assertTrue(all("09.02.2032 bis 13.02.2032" in mail.kwargs["subject"] for mail in send.call_args_list))

    @patch("mailimport.outbound.outbound_enabled", return_value=True)
    @patch("mailimport.outbound.send_ride_mail")
    def test_legacy_started_code_is_usable_but_old_unsent_invitation_is_never_sent(self, send, outbound):
        seats = self.bookings(is_test=False)
        legacy = self.legacy_case(seats)
        entry, code, notice = self.legacy_invitation(legacy, seats[0])
        delivery = RideMailDelivery.objects.create(notice=notice)

        with patch("bookings.ride_participation.timezone.now", return_value=self.before):
            self.assertIsNone(participation_for_code(code))
            self.assertIsNone(ride_mail_payload(notice, now=self.before))
            with self.assertRaises(ValueError):
                start_ride_inquiry(legacy.pk)
            self.assertEqual(sync_ride_cases(), 2)
            self.assertIsNone(participation_for_code(code))
            self.assertIsNone(ride_mail_payload(notice, now=self.before))
        self.assertEqual(sync_ride_cases(now=self.during_first_week), 1)
        with patch("bookings.ride_participation.timezone.now", return_value=self.during_first_week):
            self.assertEqual(participation_for_code(code), entry)
            self.assertEqual(deliver_ride_mail(), 0)
        self.assertEqual(legacy.participation_requests.count(), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, RideMailDelivery.Status.CANCELLED)
        send.assert_not_called()

    def test_sent_or_uncertain_legacy_invitation_stays_valid_before_course_start_after_split_sync(self):
        seats = self.bookings(is_test=False)
        legacy = self.legacy_case(seats)
        invitations = [self.legacy_invitation(legacy, seat) for seat in seats]
        for (_, _, notice), status in zip(invitations, (RideMailDelivery.Status.SENT, RideMailDelivery.Status.UNCERTAIN)):
            RideMailDelivery.objects.create(notice=notice, status=status, finished_at=self.before)

        with patch("bookings.ride_participation.timezone.now", return_value=self.before):
            for entry, code, _ in invitations:
                self.assertEqual(participation_for_code(code), entry)
            self.assertEqual(sync_ride_cases(), 2)
            legacy.refresh_from_db()
            self.assertFalse(legacy.currently_matches)
            for entry, code, _ in invitations:
                self.assertEqual(participation_for_code(code), entry)

            self.assertIsNone(ride_mail_payload(invitations[1][2], now=self.before))
            legacy.seats.remove(seats[0])
            self.assertIsNone(participation_for_code(invitations[0][1]))
            self.assertEqual(participation_for_code(invitations[1][1]), invitations[1][0])

    def test_prepared_legacy_test_preview_remains_usable_after_split_sync(self):
        seats = self.bookings(is_test=True)
        legacy = self.legacy_case(seats)
        entry, code, _ = self.legacy_invitation(legacy, seats[0])

        with patch("bookings.ride_participation.timezone.now", return_value=self.before):
            self.assertEqual(participation_for_code(code), entry)
            self.assertEqual(sync_ride_cases(), 2)
            self.assertEqual(participation_for_code(code), entry)
            legacy.status = RideCase.Status.CLOSED
            legacy.save(update_fields=["status"])
            self.assertIsNone(participation_for_code(code))
        self.assertFalse(RideMailDelivery.objects.exists())
