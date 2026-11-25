from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from bookings.models import BookingStatus, CourseSeat, CourseSession, EmailEvent, Participant
from bookings.services import ingest, set_participant_email
from tests.test_mail_parser import message
from tests.test_reconciliation import JUNE, MAY


PERSONAL = "alex.beispiel@example.invalid"
OTHER = "andere-adresse@example.invalid"


def booking_mail(*, name="Alex Beispiel", period=MAY, address=None, kind="approval", reference=None, to=None):
    bodies = {
        "approval": "Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:",
        "confirmation": f"Die Lehrgangsanmeldung des Teilnehmers {name} für den folgenden Lehrgangstermin wurde bestätigt:",
        "cancellation": "Ihre Lehrgangsanmeldung wurde storniert.",
    }
    body = (
        f"{bodies[kind]}\nTeilnehmer: {name}\nLehrgangsart: Gruppenführer\n"
        f"Lehrgangstermin: {period}\nSchulungsort: SFS Würzburg\n"
    )
    if reference:
        body += f"Buchungsreferenz: {reference}\n"
    mail = message("Ihre Lehrgangsanmeldung", body)
    if address:
        mail["To"] = f"{' '.join(name.split())} <{address}>"
    elif to:
        mail["To"] = to
    return mail.as_bytes()


class ParticipantIdentityTests(TestCase):
    def test_matching_email_and_normalized_name_reuse_the_person_across_sessions(self):
        first = ingest(booking_mail(address="Alex.Beispiel@EXAMPLE.INVALID"))
        second = ingest(booking_mail(period=JUNE, address=PERSONAL, name="ALEX   BEISPIEL"))

        self.assertEqual(first.seat.participant_id, second.seat.participant_id)
        self.assertNotEqual(first.seat.session_id, second.seat.session_id)
        self.assertEqual(first.seat.participant.email, PERSONAL)
        self.assertEqual(Participant.objects.count(), 1)

    def test_name_only_is_not_identity_across_sessions(self):
        first = ingest(booking_mail())
        second = ingest(booking_mail(period=JUNE))

        self.assertNotEqual(first.seat.participant_id, second.seat.participant_id)
        self.assertEqual(first.seat.participant.email, "")
        self.assertEqual(second.seat.participant.email, "")

    def test_same_email_with_different_name_does_not_create_records(self):
        first = ingest(booking_mail(address=PERSONAL))
        conflicting = ingest(booking_mail(period=JUNE, address=PERSONAL, name="Pat Musterfall"))

        self.assertEqual(conflicting.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        self.assertEqual(conflicting.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(CourseSession.objects.count(), 1)
        self.assertEqual(Participant.objects.get(), first.seat.participant)
        self.assertEqual(first.seat.participant.name, "Alex Beispiel")

    def test_current_booking_email_is_never_overwritten(self):
        first = ingest(booking_mail(address=PERSONAL, reference="TEST-17"))
        conflicting = ingest(booking_mail(kind="confirmation", address=OTHER, reference="TEST-17"))

        self.assertEqual(conflicting.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        self.assertEqual(conflicting.seat_id, first.seat_id)
        self.assertEqual(conflicting.seat.status, BookingStatus.APPROVED)
        self.assertEqual(conflicting.seat.participant.email, PERSONAL)
        self.assertEqual(Participant.objects.count(), 1)

    def test_confirmation_can_fill_existing_blank_email_without_changing_identity(self):
        approval = ingest(booking_mail())
        confirmed = ingest(booking_mail(kind="confirmation", address=PERSONAL))

        self.assertEqual(confirmed.seat.participant_id, approval.seat.participant_id)
        self.assertEqual(confirmed.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(confirmed.seat.participant.email, PERSONAL)

    def test_byte_identical_redelivery_fills_missing_contact_without_reapplying_event(self):
        raw = booking_mail(address=PERSONAL)
        with patch("bookings.services.get_functional_addresses", return_value=(PERSONAL,)):
            approval = ingest(raw)
        confirmation = ingest(booking_mail(kind="confirmation"))
        self.assertEqual(confirmation.seat.participant.email, "")

        redelivered = ingest(raw)
        approval.seat.refresh_from_db()
        self.assertEqual(redelivered.pk, approval.pk)
        self.assertEqual(EmailEvent.objects.count(), 2)
        self.assertEqual(approval.seat.history.count(), 2)
        self.assertEqual(approval.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(approval.seat.participant.email, PERSONAL)

    def test_provisional_seat_may_link_to_same_name_and_email_with_history(self):
        provisional = ingest(booking_mail())
        owner = ingest(booking_mail(address=PERSONAL, period=JUNE))
        linked = ingest(booking_mail(address=PERSONAL))

        self.assertEqual(linked.seat_id, provisional.seat_id)
        self.assertEqual(linked.seat.participant_id, owner.seat.participant_id)
        updated = linked.seat.history.get(source_event=linked)
        self.assertEqual(updated.previous_participant_id, provisional.seat.participant_id)
        self.assertEqual(updated.participant_id, owner.seat.participant_id)
        self.assertEqual(updated.previous_status, updated.status)
        self.assertEqual(Participant.objects.count(), 2)

    def test_provisional_profile_with_multiple_seats_is_not_moved_automatically(self):
        provisional = ingest(booking_mail())
        owner = ingest(booking_mail(address=PERSONAL, period=JUNE))
        another_session = CourseSession.objects.create(
            course=owner.seat.session.course,
            school=owner.seat.session.school,
            starts_at=owner.seat.session.starts_at + timedelta(days=30),
            ends_at=owner.seat.session.ends_at + timedelta(days=30),
        )
        another_seat = CourseSeat.objects.create(session=another_session, participant=provisional.seat.participant)

        import_event = ingest(booking_mail(kind="confirmation", address=PERSONAL))

        self.assertEqual(import_event.review_reason, EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR)
        self.assertEqual(import_event.seat.participant_id, provisional.seat.participant_id)
        self.assertEqual(import_event.seat.status, BookingStatus.APPROVED)
        another_seat.refresh_from_db()
        self.assertEqual(another_seat.participant_id, provisional.seat.participant_id)

    def test_cancellation_cannot_add_email_to_the_profile(self):
        approval = ingest(booking_mail())
        cancelled = ingest(booking_mail(kind="cancellation", address=PERSONAL))

        self.assertEqual(cancelled.seat.participant_id, approval.seat.participant_id)
        self.assertEqual(cancelled.seat.participant.email, "")
        self.assertEqual(cancelled.seat.status, BookingStatus.CANCELLED)

    def test_cancellation_with_conflicting_email_preserves_status_and_address(self):
        first = ingest(booking_mail(address=PERSONAL))
        conflict = ingest(booking_mail(kind="cancellation", address=OTHER))

        self.assertEqual(conflict.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        self.assertEqual(conflict.seat_id, first.seat_id)
        self.assertEqual(conflict.seat.status, BookingStatus.APPROVED)
        self.assertEqual(conflict.seat.participant.email, PERSONAL)

    def test_ambiguous_recipients_do_not_create_or_update_a_booking(self):
        event = ingest(booking_mail(to=f"{PERSONAL}, {OTHER}"))

        self.assertEqual(event.state, EmailEvent.State.NEEDS_REVIEW)
        self.assertEqual(event.review_reason, EmailEvent.ReviewReason.PARTICIPANT_IDENTITY_UNCLEAR)
        self.assertFalse(CourseSeat.objects.exists())
        self.assertFalse(Participant.objects.exists())

    def test_functional_addresses_are_applied_in_automated_and_manual_paths(self):
        with patch("bookings.services.get_functional_addresses", return_value=(PERSONAL,)):
            event = ingest(booking_mail(address=PERSONAL))

            self.assertEqual(event.state, EmailEvent.State.APPLIED)
            self.assertEqual(event.seat.participant.email, "")
            with self.assertRaises(ValueError):
                set_participant_email(
                    event.seat_id, PERSONAL.upper(), expected_participant_id=event.seat.participant_id,
                )

    def test_database_default_allows_legacy_workers_to_insert_without_email(self):
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO bookings_participant (name) VALUES (%s)", ["Alex Beispiel"])

        self.assertEqual(Participant.objects.get().email, "")

    def test_database_prevents_duplicate_nonempty_addresses_independent_of_case(self):
        Participant.objects.create(name="Alex Beispiel", email=PERSONAL)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Participant.objects.create(name="Pat Musterfall", email=PERSONAL.upper())
        self.assertEqual(Participant.objects.count(), 1)

    def test_unicode_and_punycode_domains_share_one_canonical_identity(self):
        seat = ingest(booking_mail()).seat
        unicode_address = "Alex.Beispiel@BÜCHER.Example.Invalid"
        ascii_address = "alex.beispiel@xn--bcher-kva.example.invalid"

        saved = set_participant_email(seat.pk, unicode_address, expected_participant_id=seat.participant_id)
        subsequent = ingest(booking_mail(period=JUNE, address=ascii_address))

        self.assertEqual(saved.email, ascii_address)
        self.assertEqual(subsequent.seat.participant_id, saved.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Participant.objects.create(name="Pat Musterfall", email=unicode_address)

    def test_manual_email_rejects_equivalent_functional_address(self):
        seat = ingest(booking_mail()).seat
        with patch("bookings.services.get_functional_addresses", return_value=("alex@bücher.example.invalid",)):
            with self.assertRaisesRegex(ValueError, "Funktionspostfach"):
                set_participant_email(
                    seat.pk, "Alex@XN--BCHER-KVA.example.invalid", expected_participant_id=seat.participant_id,
                )

        self.assertEqual(Participant.objects.get().email, "")

    def test_direct_save_does_not_clear_or_replace_existing_address_with_invalid_value(self):
        participant = Participant.objects.create(name="Alex Beispiel", email=PERSONAL)
        participant.email = "ungültig"

        with self.assertRaises(ValueError):
            participant.save(update_fields=["email"])

        participant.refresh_from_db()
        self.assertEqual(participant.email, PERSONAL)

    def test_manual_email_cannot_apply_to_a_stale_or_existing_different_identity(self):
        booking = ingest(booking_mail()).seat
        stale_participant = Participant.objects.create(name="Pat Musterfall")

        with self.assertRaises(ValueError):
            set_participant_email(booking.pk, PERSONAL, expected_participant_id=stale_participant.pk)
        self.assertEqual(booking.participant.email, "")

        saved = set_participant_email(booking.pk, PERSONAL.upper(), expected_participant_id=booking.participant_id)
        self.assertEqual(saved.email, PERSONAL)
        with self.assertRaises(ValueError):
            set_participant_email(booking.pk, OTHER, expected_participant_id=booking.participant_id)
        self.assertEqual(Participant.objects.get(pk=saved.pk).email, PERSONAL)

    def test_manual_email_links_matching_canonical_profile_and_keeps_audit_history(self):
        provisional = ingest(booking_mail()).seat
        owner = ingest(booking_mail(period=JUNE, address=PERSONAL)).seat
        old_participant_id = provisional.participant_id

        result = set_participant_email(provisional.pk, PERSONAL, expected_participant_id=old_participant_id)

        provisional.refresh_from_db()
        self.assertEqual(result.pk, owner.participant_id)
        self.assertEqual(provisional.participant_id, owner.participant_id)
        log = provisional.history.get(source_event__isnull=True)
        self.assertEqual(log.previous_participant_id, old_participant_id)
        self.assertEqual(log.participant_id, owner.participant_id)
        self.assertEqual(log.previous_status, log.status)


class ParticipantEmailConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_manual_and_mail_import_do_not_overwrite_one_another(self):
        booking = ingest(booking_mail()).seat
        ready = Barrier(2)

        def manual():
            try:
                ready.wait(timeout=5)
                set_participant_email(booking.pk, OTHER, expected_participant_id=booking.participant_id)
            except ValueError:
                return False
            finally:
                connections.close_all()
            return True

        def poll():
            try:
                ready.wait(timeout=5)
                return ingest(booking_mail(kind="confirmation", address=PERSONAL)).state
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            manual_result = pool.submit(manual)
            poll_result = pool.submit(poll)
            written_manually = manual_result.result()
            event_state = poll_result.result()

        booking.refresh_from_db()
        if written_manually:
            self.assertEqual(event_state, EmailEvent.State.NEEDS_REVIEW)
            self.assertEqual(booking.participant.email, OTHER)
            self.assertEqual(booking.status, BookingStatus.APPROVED)
        else:
            self.assertEqual(event_state, EmailEvent.State.APPLIED)
            self.assertEqual(booking.participant.email, PERSONAL)
            self.assertEqual(booking.status, BookingStatus.CONFIRMED)
