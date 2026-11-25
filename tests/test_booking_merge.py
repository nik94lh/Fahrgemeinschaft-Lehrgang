from email import policy
from email.parser import BytesParser

from django.test import TestCase

from bookings.models import BookingStatus, CourseSeat, CourseSession, EmailEvent, Participant, School
from bookings.services import ingest
from mailimport.models import MailReceipt, ParticipantRecognitionConfiguration
from tests.test_mail_parser import message
from tests.test_participant_identity import booking_mail
from tests.test_reconciliation import JUNE, eml


REFERENCE = "TEST-71"
PERSONAL = "alex@example.invalid"


def partial_confirmation(*, reference=REFERENCE, participant="Alex Beispiel", fields="", to=None):
    mail = message(
        "Teilnahmebestätigung",
        f"Teilnehmer: {participant}\nBuchungsreferenz: {reference}\n{fields}",
    )
    if to:
        mail["To"] = to
    return mail.as_bytes()


def online_confirmation(*, to=None):
    mail = BytesParser(policy=policy.default).parsebytes(eml("Teilnahmebestätigung", school="Online"))
    if to:
        mail["To"] = to
    return mail.as_bytes()


class BookingMailMergeTests(TestCase):
    def test_partial_confirmation_with_verified_reference_and_participant_enters_existing_booking(self):
        original = ingest(eml("Lehrgangsgenehmigung", reference=REFERENCE))

        result = ingest(partial_confirmation(to=f"Alex Beispiel <{PERSONAL}>"))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, original.seat_id)
        self.assertEqual(result.seat.participant.email, PERSONAL)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(CourseSession.objects.count(), 1)

    def test_partial_follow_up_requires_a_known_reference(self):
        result = ingest(partial_confirmation(to=f"Alex Beispiel <{PERSONAL}>"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.COURSE_UNCLEAR)
        self.assertFalse(CourseSeat.objects.exists())
        self.assertFalse(Participant.objects.exists())

    def test_partial_follow_up_cannot_use_conflicting_or_unusable_fields(self):
        original = ingest(eml("Lehrgangsgenehmigung", reference=REFERENCE))
        tests = (
            ("Lehrgangsart: Anderer Lehrgang", EmailEvent.ReviewReason.REFERENCE_MISMATCH),
            ("Schulungsort: SFS Regensburg", EmailEvent.ReviewReason.REFERENCE_MISMATCH),
            (f"Lehrgangstermin: {JUNE}", EmailEvent.ReviewReason.REFERENCE_MISMATCH),
            ("Lehrgangstermin: Nicht lesbar", EmailEvent.ReviewReason.PERIOD_UNCLEAR),
            ("Lehrgangsart: Gruppenführer\nLehrgangsart: Anderer Lehrgang", EmailEvent.ReviewReason.COURSE_UNCLEAR),
        )
        for fields, reason in tests:
            with self.subTest(fields=fields):
                result = ingest(partial_confirmation(fields=fields))
                self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
                self.assertEqual(result.review_reason, reason)
                self.assertEqual(result.seat_id, original.seat_id)
        original.seat.refresh_from_db()
        self.assertEqual(original.seat.status, BookingStatus.APPROVED)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_reference_does_not_override_a_different_participant_on_partial_follow_up(self):
        original = ingest(eml("Lehrgangsgenehmigung", reference=REFERENCE))

        result = ingest(partial_confirmation(participant="Toni Testperson"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
        original.seat.refresh_from_db()
        self.assertEqual(original.seat.participant.name, "Alex Beispiel")
        self.assertEqual(original.seat.status, BookingStatus.APPROVED)

    def test_confirmation_to_a_shared_address_preserves_address_from_full_approval(self):
        approval = ingest(booking_mail(address=PERSONAL))
        confirmation = booking_mail(kind="confirmation", to="Verwaltung <shared@example.invalid>")

        result = ingest(confirmation)

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, approval.seat_id)
        self.assertEqual(result.seat.participant.email, PERSONAL)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)

    def test_verified_email_joins_mails_to_the_third_same_name_booking(self):
        first = ingest(booking_mail(address="other@example.invalid"))
        second = CourseSeat.objects.create(
            session=first.seat.session, participant=Participant.objects.create(name="Alex Beispiel"),
        )
        target = CourseSeat.objects.create(
            session=first.seat.session, participant=Participant.objects.create(name="ALEX  Beispiel", email=PERSONAL),
            status=BookingStatus.APPROVED,
        )

        approval = ingest(booking_mail(address="ALEX@EXAMPLE.INVALID"))
        raw_confirmation = booking_mail(kind="confirmation", address=PERSONAL)
        confirmation = ingest(raw_confirmation)

        self.assertEqual(approval.state, EmailEvent.State.APPLIED)
        self.assertEqual(confirmation.state, EmailEvent.State.APPLIED)
        self.assertEqual(approval.seat_id, target.pk)
        self.assertEqual(confirmation.seat_id, target.pk)
        self.assertEqual(ingest(raw_confirmation).pk, confirmation.pk)
        self.assertCountEqual(target.email_events.all(), [approval, confirmation])
        target.refresh_from_db()
        first.seat.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(target.status, BookingStatus.CONFIRMED)
        self.assertEqual(first.seat.status, BookingStatus.APPROVED)
        self.assertEqual(second.status, BookingStatus.NEEDS_REVIEW)
        self.assertEqual(second.participant.email, "")
        self.assertEqual(CourseSeat.objects.count(), 3)

    def test_verified_email_does_not_resolve_two_records_for_the_same_person(self):
        original = ingest(booking_mail(address=PERSONAL))
        CourseSeat.objects.create(session=original.seat.session, participant=original.seat.participant)

        result = ingest(booking_mail(kind="confirmation", address=PERSONAL))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.MULTIPLE_SEATS)
        self.assertIsNone(result.seat_id)
        self.assertEqual(CourseSeat.objects.count(), 2)

    def test_verified_email_does_not_resolve_duplicate_sessions(self):
        original = ingest(booking_mail(address=PERSONAL)).seat
        session = original.session
        CourseSession.objects.create(
            course=session.course, school=session.school, starts_at=session.starts_at, ends_at=session.ends_at,
        )

        result = ingest(booking_mail(kind="confirmation", address=PERSONAL))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.MULTIPLE_SESSIONS)
        self.assertIsNone(result.seat_id)
        original.refresh_from_db()
        self.assertEqual(original.status, BookingStatus.APPROVED)

    def test_verified_email_cannot_override_a_different_booking_reference(self):
        first = ingest(booking_mail(address="other@example.invalid", reference="TEST-70"))
        target = CourseSeat.objects.create(
            session=first.seat.session, participant=Participant.objects.create(name="Alex Beispiel", email=PERSONAL),
            external_reference=REFERENCE, status=BookingStatus.APPROVED,
        )

        conflicting = ingest(booking_mail(kind="confirmation", address=PERSONAL, reference="TEST-70"))
        unknown = ingest(booking_mail(kind="confirmation", address=PERSONAL, reference="TEST-72"))

        self.assertEqual(conflicting.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(conflicting.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        self.assertEqual(conflicting.seat_id, first.seat_id)
        self.assertEqual(unknown.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(unknown.review_reason, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
        self.assertEqual(unknown.review_context["candidate_seat_id"], target.pk)
        target.refresh_from_db()
        first.seat.refresh_from_db()
        self.assertEqual(target.status, BookingStatus.APPROVED)
        self.assertEqual(first.seat.status, BookingStatus.APPROVED)
        self.assertEqual(CourseSeat.objects.count(), 2)

    def test_redelivered_partial_confirmation_can_fill_an_address_without_changing_status(self):
        config = ParticipantRecognitionConfiguration.objects.create(functional_addresses=[PERSONAL])
        original = ingest(eml("Lehrgangsgenehmigung", reference=REFERENCE))
        raw = partial_confirmation(to=f"Alex Beispiel <{PERSONAL}>")
        confirmation = ingest(raw)
        self.assertEqual(confirmation.state, EmailEvent.State.APPLIED)
        self.assertEqual(confirmation.seat.participant.email, "")
        config.delete()

        result = ingest(raw)

        self.assertEqual(result.pk, confirmation.pk)
        self.assertEqual(result.seat_id, original.seat_id)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(result.seat.participant.email, PERSONAL)
        self.assertEqual(result.seat.history.count(), 2)

    def test_online_only_follow_up_adds_format_without_erasing_known_organizer(self):
        original = ingest(eml("Lehrgangsgenehmigung", reference=REFERENCE))
        receipt = MailReceipt(source_id="synthetic", uid_validity=1, uid=7)

        result = ingest(partial_confirmation(fields="Schulungsort: Online"), receipt=receipt)

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, original.seat_id)
        self.assertEqual(result.seat.session.school, School.WUERZBURG)
        self.assertTrue(result.seat.session.is_online)
        self.assertTrue(receipt.fields["is_online"])
        self.assertEqual(receipt.fields["school"], School.ONLINE)

    def test_follow_up_can_add_organizer_to_existing_online_session(self):
        original = ingest(eml("Lehrgangsgenehmigung", school="Online", reference=REFERENCE))
        self.assertEqual(original.seat.session.school, School.ONLINE)

        result = ingest(eml("Teilnahmebestätigung", reference=REFERENCE))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, original.seat_id)
        self.assertEqual(result.seat.session.school, School.WUERZBURG)
        self.assertTrue(result.seat.session.is_online)

    def test_online_information_from_conflicting_notice_leaves_session_unchanged(self):
        original = ingest(eml("Lehrgangsgenehmigung", reference=REFERENCE))

        result = ingest(partial_confirmation(fields="Schulungsort: SFS Regensburg\nFormat: Online"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.REFERENCE_MISMATCH)
        original.seat.session.refresh_from_db()
        self.assertEqual(original.seat.session.school, School.WUERZBURG)
        self.assertFalse(original.seat.session.is_online)

    def test_online_follow_up_without_reference_uses_verified_address_and_matching_slot(self):
        original = ingest(booking_mail(address=PERSONAL))

        result = ingest(online_confirmation(to=f"Alex Beispiel <{PERSONAL}>"))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, original.seat_id)
        self.assertEqual(result.seat.session.school, School.WUERZBURG)
        self.assertTrue(result.seat.session.is_online)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_online_follow_up_uses_verified_email_with_same_name_in_one_session(self):
        original = ingest(booking_mail(address=PERSONAL))
        CourseSeat.objects.create(
            session=original.seat.session, participant=Participant.objects.create(name="Alex Beispiel"),
        )

        result = ingest(online_confirmation(to=f"Alex Beispiel <{PERSONAL}>"))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, original.seat_id)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(result.seat.session.school, School.WUERZBURG)
        self.assertTrue(result.seat.session.is_online)
        self.assertEqual(CourseSeat.objects.count(), 2)

    def test_online_follow_up_without_reference_or_matching_address_goes_to_review(self):
        original = ingest(booking_mail(address=PERSONAL))

        result = ingest(online_confirmation())

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_context["candidate_seat_id"], original.seat_id)
        self.assertEqual(CourseSeat.objects.count(), 1)
        original.seat.session.refresh_from_db()
        self.assertFalse(original.seat.session.is_online)

    def test_online_follow_up_with_multiple_possible_bookings_goes_to_review(self):
        ingest(booking_mail(address=PERSONAL))
        ingest(eml("Lehrgangsgenehmigung", school="SFS Regensburg"))

        result = ingest(online_confirmation(to=f"Alex Beispiel <{PERSONAL}>"))

        self.assertEqual(result.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(result.review_reason, EmailEvent.ReviewReason.MULTIPLE_SEATS)
        self.assertEqual(CourseSeat.objects.count(), 2)
        self.assertFalse(CourseSession.objects.filter(is_online=True).exists())

    def test_a_parallel_online_course_keeps_an_unrelated_in_person_course_intact(self):
        in_person = ingest(eml("Lehrgangsgenehmigung"))
        online = ingest(eml(
            "Lehrgangsgenehmigung", participant="Toni Testperson", extra="Format: Online",
        ))
        other_online = ingest(eml(
            "Lehrgangsgenehmigung", participant="Mara Muster", extra="Format: Online",
        ))

        self.assertNotEqual(in_person.seat.session_id, online.seat.session_id)
        self.assertEqual(online.seat.session_id, other_online.seat.session_id)
        in_person.seat.session.refresh_from_db()
        self.assertFalse(in_person.seat.session.is_online)
        self.assertTrue(online.seat.session.is_online)

    def test_confirmation_without_format_resolves_matching_participant_across_both_formats(self):
        in_person = ingest(eml("Lehrgangsgenehmigung"))
        online = ingest(eml(
            "Lehrgangsgenehmigung", participant="Toni Testperson", extra="Format: Online",
        ))

        result = ingest(eml("Teilnahmebestätigung", participant="Toni Testperson"))

        self.assertEqual(result.state, EmailEvent.State.APPLIED)
        self.assertEqual(result.seat_id, online.seat_id)
        self.assertEqual(result.seat.status, BookingStatus.CONFIRMED)
        in_person.seat.refresh_from_db()
        self.assertEqual(in_person.seat.status, BookingStatus.APPROVED)
        self.assertEqual(CourseSeat.objects.count(), 2)
