from dataclasses import replace
from html import escape

from django.test import TestCase

from bookings.contact_replay import ContactReplayResult, contact_replay_outcome, enrich_contact_from_imported_mail
from bookings.models import BookingHistory, BookingStatus, CourseSeat, EmailEvent, Participant
from bookings.services import _reconcile, ingest
from mailimport.models import ParticipantRecognitionConfiguration
from mailimport.parser import parse_eml
from tests.test_participant_email_parser import CONFIRMATION, booking_message
from tests.test_participant_identity import PERSONAL, booking_mail


FUNCTIONAL = "lehrgaenge@example.invalid"


def import_as_legacy(raw):
    mail = parse_eml(raw, functional_addresses=(FUNCTIONAL,))
    return _reconcile(replace(mail, participant_email=None), occurred_at=None)


class ForwardedContactReplayTests(TestCase):
    address = "lernplatz97@example.invalid"

    def forwarded_confirmation(self, plain_to, html_to):
        def quoted(to):
            return (
                "----- Weitergeleitete Nachricht -----\n"
                "Von: Schule <schule@example.invalid>\n"
                "Datum: 10. Mai 2027 um 09:00\n"
                f"An: {to}\nBetreff: Teilnahmebestätigung\n\n{CONFIRMATION}"
            )

        message = booking_message(subject="WG: Teilnahmebestätigung", body=quoted(plain_to))
        message.add_alternative(escape(quoted(html_to)).replace("\n", "<br>"), subtype="html")
        return message.as_bytes()

    def test_import_saves_address_with_matching_name_in_html(self):
        raw = self.forwarded_confirmation(self.address, f"Alex Beispiel <{self.address}>")

        event = ingest(raw)

        self.assertEqual(event.state, EmailEvent.State.APPLIED)
        self.assertEqual(event.seat.status, BookingStatus.CONFIRMED)
        event.seat.participant.refresh_from_db()
        self.assertEqual(event.seat.participant.email, self.address)

    def test_replay_keeps_legacy_contact_blank_when_original_recipient_names_conflict(self):
        raw = self.forwarded_confirmation(f"Alex Beispiel <{self.address}>", f"Robin Muster <{self.address}>")
        legacy_mail = replace(parse_eml(raw), participant_email=None, participant_email_ambiguous=False)
        event = _reconcile(legacy_mail, occurred_at=None)
        self.assertEqual(event.state, EmailEvent.State.APPLIED)
        participant = event.seat.participant
        self.assertEqual(participant.email, "")

        self.assertEqual(contact_replay_outcome(raw), ContactReplayResult.AMBIGUOUS_ADDRESS)

        participant.refresh_from_db()
        self.assertEqual(participant.email, "")


class ContactReplayTests(TestCase):
    def setUp(self):
        self.raw = booking_mail(to=f"Lehrgangsstelle <{FUNCTIONAL}>, {PERSONAL}")
        self.event = import_as_legacy(self.raw)
        self.seat = self.event.seat
        self.participant = self.seat.participant
        self.addresses = ParticipantRecognitionConfiguration.objects.create(
            functional_addresses=[FUNCTIONAL],
        )

    def replay(self, raw=None):
        return enrich_contact_from_imported_mail(self.raw if raw is None else raw)

    def test_exact_legacy_approval_adds_address_once_without_reimporting(self):
        self.assertEqual(self.participant.email, "")
        self.assertEqual(self.event.state, EmailEvent.State.APPLIED)
        self.assertTrue(self.event.booking_changes.exists())

        self.assertTrue(self.replay())
        self.assertFalse(self.replay())

        self.participant.refresh_from_db()
        self.seat.refresh_from_db()
        self.event.refresh_from_db()
        self.assertEqual(self.participant.email, PERSONAL)
        self.assertEqual(self.seat.status, BookingStatus.APPROVED)
        self.assertEqual(self.seat.participant_id, self.participant.pk)
        self.assertEqual(self.event.state, EmailEvent.State.APPLIED)
        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(BookingHistory.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_new_digest_or_invalid_message_has_no_effect(self):
        self.assertFalse(self.replay(self.raw + b"\n"))
        self.assertFalse(self.replay(b"malformed"))
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, "")
        self.assertEqual(EmailEvent.objects.count(), 1)

    def test_diagnostics_distinguish_unreadable_unsupported_and_unmatched_messages(self):
        cases = (
            (b"malformed", ContactReplayResult.UNREADABLE),
            (booking_mail(kind="cancellation"), ContactReplayResult.UNSUPPORTED),
            (booking_mail(), ContactReplayResult.NO_ADDRESS),
            (
                booking_mail(to=f"Alex Beispiel <{PERSONAL}>, Alex Beispiel <other@example.invalid>"),
                ContactReplayResult.AMBIGUOUS_ADDRESS,
            ),
            (booking_mail(address=PERSONAL, reference="TEST-27"), ContactReplayResult.NO_APPLIED_EVENT),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected.name):
                self.assertEqual(contact_replay_outcome(raw), expected)
        self.assertEqual(EmailEvent.objects.count(), 1)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, "")

    def test_needs_review_event_and_inactive_or_unresolved_seat_have_no_effect(self):
        self.event.state = EmailEvent.State.NEEDS_REVIEW
        self.event.save(update_fields=["state"])
        self.assertFalse(self.replay())

        self.event.state = EmailEvent.State.APPLIED
        self.event.save(update_fields=["state"])
        self.seat.status = BookingStatus.CANCELLED
        self.seat.save(update_fields=["status"])
        self.assertFalse(self.replay())

        self.seat.status = BookingStatus.CONFIRMED
        self.seat.save(update_fields=["status"])
        EmailEvent.objects.create(
            sha256="b" * 64, state=EmailEvent.State.NEEDS_REVIEW, seat=self.seat,
        )
        self.assertFalse(self.replay())
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, "")

    def test_functional_address_and_conflicting_profile_are_ignored(self):
        self.addresses.functional_addresses = [PERSONAL]
        self.addresses.save(update_fields=["functional_addresses"])
        self.assertFalse(self.replay())
        self.addresses.functional_addresses = [FUNCTIONAL]
        self.addresses.save(update_fields=["functional_addresses"])

        Participant.objects.create(name="Andere Testperson", email=PERSONAL)
        self.assertFalse(self.replay())
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, "")
        self.assertEqual(self.seat.participant_id, self.participant.pk)

    def test_later_reassignment_is_rejected_even_with_an_identical_name(self):
        replacement = Participant.objects.create(name=self.participant.name)
        self.seat.participant = replacement
        self.seat.save(update_fields=["participant"])
        BookingHistory.objects.create(
            seat=self.seat, previous_participant=self.participant, participant=replacement,
            previous_status=self.seat.status, status=self.seat.status,
        )

        self.assertFalse(self.replay())
        replacement.refresh_from_db()
        self.participant.refresh_from_db()
        self.assertEqual(replacement.email, "")
        self.assertEqual(self.participant.email, "")

    def test_existing_address_is_never_overwritten(self):
        self.participant.email = "verifiziert@example.invalid"
        self.participant.save(update_fields=["email"])

        self.assertFalse(self.replay())
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, "verifiziert@example.invalid")

    def test_unchanged_status_mail_can_use_unambiguous_earlier_history(self):
        later_mail = self.raw + b"\n"
        later_event = import_as_legacy(later_mail)
        self.assertEqual(later_event.state, EmailEvent.State.APPLIED)
        self.assertFalse(later_event.booking_changes.exists())

        self.assertTrue(self.replay(later_mail))
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, PERSONAL)
        self.assertEqual(BookingHistory.objects.count(), 1)

    def test_missing_history_has_no_effect(self):
        self.event.booking_changes.all().delete()

        self.assertFalse(self.replay())
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.email, "")
