from datetime import datetime, timezone as datetime_timezone

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from bookings.models import (
    BookingHistory,
    BookingStatus,
    Carpool,
    CarpoolMember,
    Consent,
    Course,
    CourseSeat,
    CourseSession,
    EmailEvent,
    FireBrigade,
    Participant,
    School,
)


class BookingModelsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name="Synthetischer Übungslehrgang")
        cls.starts_at = datetime(2027, 5, 10, 7, 45, tzinfo=datetime_timezone.utc)
        cls.ends_at = datetime(2027, 5, 14, 13, tzinfo=datetime_timezone.utc)
        cls.session = CourseSession.objects.create(
            course=cls.course,
            school=School.WUERZBURG,
            starts_at=cls.starts_at,
            ends_at=cls.ends_at,
        )
        cls.first = Participant.objects.create(name="Testperson Eins")
        cls.second = Participant.objects.create(name="Testperson Zwei")

    def test_several_people_can_hold_seats_on_the_same_session(self):
        CourseSeat.objects.create(session=self.session, participant=self.first)
        CourseSeat.objects.create(session=self.session, participant=self.second)

        self.assertEqual(self.session.seats.count(), 2)

    def test_person_can_hold_seats_on_multiple_sessions(self):
        next_session = CourseSession.objects.create(
            course=self.course,
            school=School.REGENSBURG,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
        )
        CourseSeat.objects.create(session=self.session, participant=self.first)
        CourseSeat.objects.create(session=next_session, participant=self.first)

        self.assertEqual(self.first.seats.count(), 2)

    def test_session_end_must_follow_start(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseSession.objects.create(
                course=self.course,
                school=School.GERETSRIED,
                starts_at=self.ends_at,
                ends_at=self.starts_at,
            )

    def test_supplied_booking_reference_is_unique(self):
        CourseSeat.objects.create(session=self.session, external_reference="SYNTH-001")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CourseSeat.objects.create(session=self.session, external_reference="SYNTH-001")

    def test_multiple_events_can_be_linked_to_one_seat(self):
        seat = CourseSeat.objects.create(session=self.session, participant=self.first)
        EmailEvent.objects.create(sha256="a" * 64, kind=EmailEvent.Kind.APPROVAL, seat=seat)
        EmailEvent.objects.create(sha256="b" * 64, kind=EmailEvent.Kind.CONFIRMATION, seat=seat)

        self.assertEqual(seat.email_events.count(), 2)
        self.assertEqual(self.session.seats.count(), 1)

    def test_exact_duplicate_event_is_rejected(self):
        EmailEvent.objects.create(sha256="a" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailEvent.objects.create(sha256="a" * 64)

    def test_reassignment_history_records_seat_and_both_participants(self):
        seat = CourseSeat.objects.create(
            session=self.session,
            participant=self.first,
            status=BookingStatus.CONFIRMED,
        )
        event = EmailEvent.objects.create(
            sha256="c" * 64,
            kind=EmailEvent.Kind.REASSIGNMENT,
            seat=seat,
        )
        history = BookingHistory.objects.create(
            seat=seat,
            previous_participant=self.first,
            participant=self.second,
            previous_status=BookingStatus.CONFIRMED,
            status=BookingStatus.REASSIGNED,
            source_event=event,
        )
        seat.participant = self.second
        seat.status = BookingStatus.REASSIGNED
        seat.save(update_fields=["participant", "status"])

        self.assertEqual(self.session.seats.count(), 1)
        self.assertEqual(seat.history.get(), history)
        self.assertEqual(history.previous_participant, self.first)
        self.assertEqual(history.participant, self.second)

    def test_fire_brigade_coordinates_require_a_pair(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            FireBrigade.objects.create(name="Testwehr", municipality="Testort", latitude=49)

    def test_carpool_has_individual_members_and_scoped_consent(self):
        carpool = Carpool.objects.create(school=School.WUERZBURG, direction=Carpool.Direction.RETURN)
        member = CarpoolMember.objects.create(carpool=carpool, participant=self.first)
        CarpoolMember.objects.create(carpool=carpool, participant=self.second)

        with self.assertRaises(IntegrityError), transaction.atomic():
            CarpoolMember.objects.create(carpool=carpool, participant=self.first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Consent.objects.create(purpose=Consent.Purpose.CONTACT_SHARING, notice_version="test-v1")

        grant = Consent.objects.create(
            member=member,
            purpose=Consent.Purpose.CONTACT_SHARING,
            notice_version="test-v1",
        )
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])

        self.assertEqual(carpool.members.count(), 2)
        self.assertIsNotNone(member.consents.get().revoked_at)
