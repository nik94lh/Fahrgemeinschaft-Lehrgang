from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from django.db import connections
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from bookings import ride_mail
from bookings.booking_deletion import BookingDeletionResult, delete_booking, deletion_token
from bookings.models import (
    BookingStatus, Course, CourseSeat, CourseSession, Participant, RideCase, RideMailDelivery, RideNotice,
    RideParticipation, School,
)
from bookings.ride_codes import code_digest, compact_code_v1, legacy_code
from bookings.ride_participation import invite_code
from mailimport import outbound


def make_notice(*, is_test=False, sequence=1):
    start = timezone.now() + timedelta(days=3)
    end = start + timedelta(days=1)
    session = CourseSession.objects.create(
        course=Course.objects.create(name="Synthetischer Lehrgang"), school=School.WUERZBURG,
        starts_at=start, ends_at=end, is_test=is_test,
    )
    case = RideCase.objects.create(
        match_key=f"synthetic-ride-mail-{sequence}", school=session.school, is_test=is_test,
        direction=RideCase.ROUND_TRIP, travel_date=timezone.localdate(start), return_date=timezone.localdate(end),
        earliest=start, latest=start, return_earliest=end, return_latest=end,
    )
    seats = [CourseSeat.objects.create(
        session=session, participant=Participant.objects.create(
            name=f"Synthetische Person {sequence}-{index}",
            email=f"ride-{sequence}-{index}@{'example.invalid' if is_test else 'example.com'}",
        ), status=BookingStatus.CONFIRMED, is_test=is_test,
    ) for index in (1, 2)]
    case.seats.set(seats)
    seat = seats[0]
    entry = RideParticipation(
        case=case, seat=seat, session=session, participant=seat.participant,
        email=seat.participant.email, expires_at=end + timedelta(days=1),
    )
    entry.token_digest = code_digest(legacy_code(entry.token_seed))
    entry.short_token_digest = code_digest(compact_code_v1(entry.token_seed))
    entry.save()
    return RideNotice.objects.create(participation=entry, kind=RideNotice.Kind.INVITATION)


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.test")
class RideMailTests(TestCase):
    def setUp(self):
        super().setUp()
        self.enabled = self.enterContext(patch.object(outbound, "outbound_enabled", return_value=True))
        self.transport = self.enterContext(patch.object(outbound, "send_ride_mail"))

    def test_disabled_or_synthetic_notices_are_not_queued_or_retroactively_sent(self):
        live, synthetic = make_notice(), make_notice(is_test=True, sequence=2)
        self.enabled.return_value = False
        self.assertEqual(ride_mail.queue_ride_notices([live]), 0)
        self.enabled.return_value = True
        self.assertEqual(ride_mail.queue_ride_notices([synthetic]), 0)
        with self.assertRaises(ValueError):
            ride_mail.queue_ride_resend(synthetic.pk)

        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        self.assertFalse(RideMailDelivery.objects.exists())
        self.transport.assert_not_called()

    def test_failed_transport_is_recorded_and_only_explicit_resend_uses_same_code(self):
        notice = make_notice()
        self.transport.side_effect = outbound.SMTPDeliveryError()
        self.assertEqual(ride_mail.queue_ride_notices([notice]), 1)
        self.assertEqual(ride_mail.queue_ride_notices([notice]), 0)
        first = notice.deliveries.get()
        self.assertEqual(first.status, RideMailDelivery.Status.PENDING)

        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        first.refresh_from_db()
        self.assertEqual(first.status, RideMailDelivery.Status.FAILED)
        self.assertIsNotNone(first.finished_at)
        original_message = self.transport.call_args.kwargs
        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        self.assertEqual(self.transport.call_count, 1)

        second = ride_mail.queue_ride_resend(notice.pk)
        with self.assertRaises(ValueError):
            ride_mail.queue_ride_resend(notice.pk)
        self.transport.side_effect = None
        self.assertEqual(ride_mail.deliver_ride_mail(), 1)
        second.refresh_from_db()
        self.assertEqual(second.status, RideMailDelivery.Status.SENT)
        self.assertIsNotNone(second.finished_at)
        resent_message = self.transport.call_args.kwargs
        self.assertEqual(resent_message["recipient"], "ride-1-1@example.com")
        self.assertTrue(invite_code(notice.participation) in resent_message["body"])
        self.assertTrue(resent_message["body"] == original_message["body"])
        self.assertNotEqual(resent_message["message_id"], original_message["message_id"])
        self.assertEqual(list(notice.deliveries.all()), [second, first])

    def test_removed_booking_is_rechecked_before_smtp(self):
        notice = make_notice()
        ride_mail.queue_ride_notices([notice])
        CourseSeat.objects.filter(pk=notice.participation.seat_id).update(status=BookingStatus.CANCELLED)

        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        self.assertEqual(notice.deliveries.get().status, RideMailDelivery.Status.CANCELLED)
        self.assertFalse(ride_mail.can_resend_ride_notice(notice))
        with self.assertRaises(ValueError):
            ride_mail.queue_ride_resend(notice.pk)
        self.transport.assert_not_called()

    def test_abandoned_claim_is_shown_as_uncertain_and_is_never_replayed(self):
        notice = make_notice()
        ride_mail.queue_ride_notices([notice])
        notice.deliveries.update(status=RideMailDelivery.Status.SENDING, claimed_at=timezone.now() - timedelta(hours=1))

        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        self.assertEqual(notice.deliveries.get().status, RideMailDelivery.Status.UNCERTAIN)
        self.assertTrue(ride_mail.can_resend_ride_notice(notice))
        self.transport.assert_not_called()

    def test_unacknowledged_smtp_attempt_is_not_replayed_automatically(self):
        notice = make_notice()
        ride_mail.queue_ride_notices([notice])
        self.transport.side_effect = outbound.SMTPDeliveryUncertain()

        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        self.assertEqual(notice.deliveries.get().status, RideMailDelivery.Status.UNCERTAIN)
        self.assertEqual(ride_mail.deliver_ride_mail(), 0)
        self.assertEqual(self.transport.call_count, 1)
        self.assertTrue(ride_mail.can_resend_ride_notice(notice))


@override_settings(HTTPS=True, RIDE_PORTAL_HOST="mitfahren.example.test")
class ConcurrentRideMailTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_waiting_for_smtp_does_not_block_a_booking_change(self):
        notice = make_notice()
        other_seat = notice.participation.case.seats.exclude(pk=notice.participation.seat_id).get()
        snapshot = deletion_token(other_seat)
        sending, acknowledged, booking_changed = Event(), Event(), Event()

        def transport(**kwargs):
            sending.set()
            if not acknowledged.wait(timeout=10):
                raise AssertionError("The test did not release the transport.")

        def send():
            try:
                return ride_mail.deliver_ride_mail()
            finally:
                connections.close_all()

        def remove_booking():
            try:
                return delete_booking(other_seat.pk, snapshot)
            finally:
                connections.close_all()
                booking_changed.set()

        with (
            patch.object(outbound, "outbound_enabled", return_value=True),
            patch.object(outbound, "send_ride_mail", side_effect=transport),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            ride_mail.queue_ride_notices([notice])
            delivery = pool.submit(send)
            try:
                self.assertTrue(sending.wait(timeout=5))
                canceled = pool.submit(remove_booking)
                self.assertTrue(booking_changed.wait(timeout=3), "SMTP blocked the booking change.")
                self.assertEqual(canceled.result(), BookingDeletionResult.DELETED)
            finally:
                acknowledged.set()
            self.assertEqual(delivery.result(timeout=5), 1)

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_workers_never_send_the_same_queued_job(self):
        notice = make_notice()
        started, finish = Event(), Event()

        def transport(**kwargs):
            started.set()
            if not finish.wait(timeout=5):
                raise AssertionError("The test did not release the transport.")

        def worker():
            try:
                return ride_mail.deliver_ride_mail()
            finally:
                connections.close_all()

        with (
            patch.object(outbound, "outbound_enabled", return_value=True),
            patch.object(outbound, "send_ride_mail", side_effect=transport) as sender,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            ride_mail.queue_ride_notices([notice])
            first = pool.submit(worker)
            try:
                self.assertTrue(started.wait(timeout=5))
                self.assertEqual(pool.submit(worker).result(timeout=5), 0)
            finally:
                finish.set()
            self.assertEqual(first.result(timeout=5), 1)
            self.assertEqual(sender.call_count, 1)

        self.assertEqual(notice.deliveries.get().status, RideMailDelivery.Status.SENT)
