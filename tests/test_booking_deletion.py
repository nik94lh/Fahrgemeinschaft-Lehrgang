from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase

from bookings.booking_deletion import BookingDeletionResult, delete_booking, deletion_token
from bookings.models import BookingHistory, BookingStatus, Course, CourseSeat, CourseSession, EmailEvent, Participant, RideCase
from bookings.services import ingest
from mailimport.models import MailReceipt
from tests.test_participant_identity import PERSONAL, booking_mail


class BookingDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.raw = booking_mail(kind="confirmation", address=PERSONAL, reference="TEST-41")
        cls.receipt = MailReceipt(source_id="1" * 64, uid_validity=1, uid=41)
        cls.event = ingest(cls.raw, receipt=cls.receipt)
        cls.seat = cls.event.seat
        cls.other = CourseSeat.objects.create(
            session=cls.seat.session, participant=Participant.objects.create(name=cls.seat.participant.name),
            status=BookingStatus.CONFIRMED, external_reference="TEST-42",
        )
        cls.direct_review = EmailEvent.objects.create(
            sha256="2" * 64, seat=cls.seat, state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.EVENT_ORDER_UNCLEAR,
        )
        cls.candidate_review = EmailEvent.objects.create(
            sha256="3" * 64, state=EmailEvent.State.NEEDS_REVIEW,
            review_context={"candidate_seat_id": cls.seat.pk},
        )
        cls.multi_review = EmailEvent.objects.create(
            sha256="4" * 64, seat=cls.seat, state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.MULTIPLE_SEATS,
        )
        cls.other_review = EmailEvent.objects.create(
            sha256="5" * 64, seat=cls.other, state=EmailEvent.State.NEEDS_REVIEW,
        )
        cls.reference_review = EmailEvent.objects.create(
            sha256="8" * 64, seat=cls.seat, state=EmailEvent.State.NEEDS_REVIEW,
            review_reason=EmailEvent.ReviewReason.REFERENCE_MISMATCH,
        )
        session = cls.seat.session
        trip = {
            "school": session.school, "direction": RideCase.ROUND_TRIP,
            "travel_date": session.starts_at.date(), "earliest": session.starts_at, "latest": session.starts_at,
            "return_date": session.ends_at.date(), "return_earliest": session.ends_at, "return_latest": session.ends_at,
        }
        cls.shared_ride = RideCase.objects.create(match_key="test-shared", status=RideCase.Status.IN_REVIEW, **trip)
        cls.shared_ride.seats.add(cls.seat, cls.other)
        cls.unrelated_ride = RideCase.objects.create(match_key="test-unrelated", **trip)
        cls.unrelated_ride.seats.add(cls.other)

    def test_delete_preserves_other_booking_person_course_mail_and_dedup(self):
        seat_id = self.seat.pk
        participant_id = self.seat.participant_id
        session_id = self.seat.session_id
        course_id = self.seat.session.course_id
        snapshot = deletion_token(self.seat)
        receipt_fields = self.receipt.fields.copy()

        self.assertEqual(delete_booking(seat_id, snapshot), BookingDeletionResult.DELETED)

        self.assertFalse(CourseSeat.objects.filter(pk=seat_id).exists())
        self.assertFalse(BookingHistory.objects.filter(seat_id=seat_id).exists())
        self.assertTrue(CourseSeat.objects.filter(pk=self.other.pk, status=BookingStatus.CONFIRMED).exists())
        self.assertTrue(CourseSession.objects.filter(pk=session_id).exists())
        self.assertTrue(Course.objects.filter(pk=course_id).exists())
        self.assertTrue(Participant.objects.filter(pk=participant_id, email=PERSONAL).exists())
        self.event.refresh_from_db()
        self.receipt.refresh_from_db()
        self.assertIsNone(self.event.seat_id)
        self.assertEqual(self.event.processed_participant_id, participant_id)
        self.assertEqual(self.receipt.event_id, self.event.pk)
        self.assertEqual(self.receipt.fields, receipt_fields)
        self.assertEqual(ingest(self.raw).pk, self.event.pk)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(delete_booking(seat_id, snapshot), BookingDeletionResult.MISSING)

    def test_delete_closes_direct_reviews_and_invalidates_only_linked_ride_cases(self):
        self.assertEqual(delete_booking(self.seat.pk, deletion_token(self.seat)), BookingDeletionResult.DELETED)

        self.direct_review.refresh_from_db()
        self.assertEqual(self.direct_review.state, EmailEvent.State.REVIEWED)
        self.assertEqual(self.direct_review.review_resolution, "booking_deleted")
        self.assertIsNotNone(self.direct_review.reviewed_at)
        self.assertIsNone(self.direct_review.seat_id)
        for review in (self.candidate_review, self.multi_review, self.other_review, self.reference_review):
            review.refresh_from_db()
            self.assertEqual(review.state, EmailEvent.State.NEEDS_REVIEW)
            self.assertEqual(review.review_resolution, "")
        self.shared_ride.refresh_from_db()
        self.unrelated_ride.refresh_from_db()
        self.assertFalse(self.shared_ride.currently_matches)
        self.assertEqual(self.shared_ride.status, RideCase.Status.IN_REVIEW)
        self.assertQuerySetEqual(self.shared_ride.seats.all(), [self.other])
        self.assertTrue(self.unrelated_ride.currently_matches)
        self.assertQuerySetEqual(self.unrelated_ride.seats.all(), [self.other])

    def test_booking_and_related_changes_invalidate_the_confirmation(self):
        snapshot = deletion_token(self.seat)
        self.assertEqual(delete_booking(self.other.pk, snapshot), BookingDeletionResult.STALE)
        CourseSession.objects.filter(pk=self.seat.session_id).update(ends_at=self.seat.session.ends_at + timedelta(hours=1))
        self.assertEqual(delete_booking(self.seat.pk, snapshot), BookingDeletionResult.STALE)

        self.seat.refresh_from_db()
        snapshot = deletion_token(self.seat)
        BookingHistory.objects.create(seat=self.seat, participant=self.seat.participant, status=self.seat.status)
        self.assertEqual(delete_booking(self.seat.pk, snapshot), BookingDeletionResult.STALE)

        snapshot = deletion_token(self.seat)
        EmailEvent.objects.create(sha256="6" * 64, seat=self.seat, state=EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(delete_booking(self.seat.pk, snapshot), BookingDeletionResult.STALE)
        self.assertTrue(CourseSeat.objects.filter(pk=self.seat.pk).exists())

    def test_last_unassigned_booking_can_be_deleted_without_deleting_the_session(self):
        original = self.seat.session
        session = CourseSession.objects.create(
            course=original.course, school=original.school,
            starts_at=original.starts_at + timedelta(days=9), ends_at=original.ends_at + timedelta(days=9),
        )
        seat = CourseSeat.objects.create(session=session)

        self.assertEqual(delete_booking(seat.pk, deletion_token(seat)), BookingDeletionResult.DELETED)

        self.assertTrue(CourseSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(session.seats.exists())

    def test_cleanup_rolls_back_if_deletion_fails(self):
        snapshot = deletion_token(self.seat)
        with patch.object(CourseSeat, "delete", side_effect=RuntimeError("synthetischer Fehler")):
            with self.assertRaises(RuntimeError):
                delete_booking(self.seat.pk, snapshot)

        self.assertTrue(CourseSeat.objects.filter(pk=self.seat.pk).exists())
        self.direct_review.refresh_from_db()
        self.shared_ride.refresh_from_db()
        self.assertEqual(self.direct_review.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertTrue(self.shared_ride.currently_matches)
