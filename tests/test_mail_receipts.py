from contextlib import nullcontext
from email import policy
from email.parser import BytesParser
from dataclasses import replace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from bookings.models import BookingStatus, CourseSeat, EmailEvent
from bookings.services import _reconcile, ingest
from mailimport.imap_sync import ImapConfig, sync_mailbox
from mailimport.models import MailReceipt, MailboxCursor
from mailimport.parser import MAX_EML_BYTES, parse_eml
from tests.test_imap_sync import body_result, mock_mailbox
from tests.test_participant_email_parser import forwarded_approval_with_repeated_mailto
from tests.test_participant_identity import PERSONAL, booking_mail


class MailReceiptTests(TestCase):
    config = ImapConfig("imap.example.invalid", 993, "test", "test-password")

    def sync(self, entries):
        client = mock_mailbox(uid_next=max(entries, default=0) + 1)
        responses = [("OK", [" ".join(map(str, entries)).encode()])]
        for uid, raw in entries.items():
            size = raw if isinstance(raw, int) else len(raw)
            responses.append(("OK", [f"1 (UID {uid} RFC822.SIZE {size})".encode()]))
            if isinstance(raw, bytes):
                responses.append(body_result(uid, raw))
        client.uid.side_effect = responses
        with patch("mailimport.imap_sync._open_mailbox", return_value=nullcontext(client)):
            return sync_mailbox(self.config)

    def test_records_each_delivery_and_skipped_mail_without_reapplying_booking(self):
        raw = booking_mail(address=PERSONAL)
        summary = self.sync({1: raw, 2: raw, 3: MAX_EML_BYTES + 1, 4: b"invalid"})
        self.assertEqual((summary.processed, summary.skipped), (2, 2))
        first, second, oversize, invalid = MailReceipt.objects.order_by("uid")
        self.assertEqual(first.outcome, MailReceipt.Outcome.APPLIED)
        self.assertEqual(first.fields["participant_email"], PERSONAL)
        self.assertEqual(first.fields["participant_name"], "Alex Beispiel")
        self.assertEqual(first.fields["course_name"], "Gruppenführer")
        self.assertEqual(second.outcome, MailReceipt.Outcome.DUPLICATE)
        self.assertIn("bereits hinterlegt", second.fields["address_note"])
        self.assertEqual(first.event_id, second.event_id)
        self.assertIn("1 MiB", oversize.detail)
        self.assertIn("Kopfzeilen", invalid.detail)
        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.get().history.count(), 1)
        cursor = MailboxCursor.objects.get()
        self.assertEqual((cursor.last_processed, cursor.last_skipped), (2, 2))

    def test_redelivery_records_address_backfill_even_when_status_is_unchanged(self):
        raw = booking_mail(address=PERSONAL)
        with patch("bookings.services.get_functional_addresses", return_value=(PERSONAL,)):
            first = ingest(raw)
        ingest(booking_mail(kind="confirmation"))
        self.sync({1: raw})
        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.ADDRESS_ADDED)
        self.assertEqual(receipt.event_id, first.pk)
        first.seat.refresh_from_db()
        self.assertEqual(first.seat.participant.email, PERSONAL)
        self.assertEqual(first.seat.status, BookingStatus.CONFIRMED)

    def test_replays_previously_addressless_forward_and_keeps_the_confirmed_booking(self):
        raw = forwarded_approval_with_repeated_mailto().as_bytes()
        original = _reconcile(replace(parse_eml(raw), participant_email=None), occurred_at=None)
        confirmation = ingest(booking_mail(kind="confirmation"))
        self.assertEqual(confirmation.seat_id, original.seat_id)

        self.sync({1: raw})

        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.ADDRESS_ADDED)
        self.assertEqual(receipt.event_id, original.pk)
        self.assertEqual(receipt.fields["participant_email"], "alex.beispiel@example.invalid")
        self.assertEqual(EmailEvent.objects.count(), 2)
        seat = CourseSeat.objects.get()
        self.assertEqual(seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(seat.participant_id, original.seat.participant_id)
        self.assertEqual(seat.participant.email, "alex.beispiel@example.invalid")

    def test_resent_headers_are_a_new_delivery_and_report_an_address_added_to_existing_booking(self):
        first = ingest(booking_mail())
        resent = BytesParser(policy=policy.default).parsebytes(booking_mail(address=PERSONAL))
        resent["Resent-Date"] = "Mon, 10 May 2027 09:00:00 +0200"
        resent["Resent-To"] = "import@example.invalid"
        self.sync({1: resent.as_bytes()})
        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.ADDRESS_ADDED)
        self.assertNotEqual(receipt.event_id, first.pk)
        self.assertEqual(receipt.event.seat_id, first.seat_id)
        self.assertEqual(receipt.fields["participant_email"], PERSONAL)
        self.assertEqual(CourseSeat.objects.get().participant.email, PERSONAL)

    @override_settings(HTTPS=True)
    def test_receipt_distinguishes_an_old_extraction_from_current_booking_contact(self):
        self.sync({1: booking_mail()})
        receipt = MailReceipt.objects.get()
        participant = receipt.event.seat.participant
        participant.email = PERSONAL
        participant.save(update_fields=["email"])

        response = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(response, "Erkannte Angaben beim Abruf")
        self.assertContains(response, "Nicht erkannt")
        self.assertContains(response, "Adresse an der Buchung aktuell</strong> · Hinterlegt")

    @override_settings(HTTPS=True)
    def test_receipt_links_review_to_detected_fields_and_specific_disagreement(self):
        ingest(booking_mail(reference="DEMO-1"))
        self.sync({1: booking_mail(name="Pat Musterfall", reference="DEMO-1")})
        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.REVIEW)
        review = self.client.get(reverse("review_detail", args=[receipt.event_id]), secure=True)
        self.assertContains(review, "Teilnehmer weicht ab")
        self.assertContains(review, reverse("mail_receipt", args=[receipt.pk]))
        detail = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(detail, "Pat Musterfall")
        self.assertContains(detail, reverse("review_detail", args=[receipt.event_id]))
        self.assertIn("no-store", detail.headers["Cache-Control"])

    def test_import_journal_requires_https(self):
        self.assertEqual(self.client.get(reverse("mail_receipts")).status_code, 404)

    def test_unexpected_worker_error_rolls_back_the_batch_and_marks_the_failed_poll(self):
        raw = booking_mail()
        client = mock_mailbox()
        size = len(raw)
        client.uid.side_effect = [
            ("OK", [b"1 2"]),
            ("OK", [f"1 (UID 1 RFC822.SIZE {size})".encode()]),
            body_result(1, raw),
            RuntimeError("simulated unexpected worker failure"),
        ]
        with (
            patch("mailimport.imap_sync._open_mailbox", return_value=nullcontext(client)),
            self.assertRaises(RuntimeError),
        ):
            sync_mailbox(self.config)
        self.assertFalse(MailReceipt.objects.exists())
        self.assertFalse(EmailEvent.objects.exists())
        cursor = MailboxCursor.objects.get()
        self.assertEqual(cursor.last_uid, 0)
        self.assertFalse(cursor.last_sync_ok)

    @override_settings(HTTPS=True)
    def test_search_by_detected_name_and_note_when_imap_has_more_ranges(self):
        self.sync({1: booking_mail(), 2: booking_mail(name="Pat Musterfall")})
        cursor = MailboxCursor.objects.get()
        cursor.last_processed, cursor.last_skipped, cursor.last_has_more = 0, 0, True
        cursor.save()
        with patch("mailimport.receipt_views.load_configuration", return_value=self.config):
            response = self.client.get(reverse("mail_receipts"), {"q": "musterfall"}, secure=True)
        self.assertContains(response, "Pat Musterfall")
        self.assertNotContains(response, "Alex Beispiel")
        self.assertContains(response, "Weitere Nachrichten werden noch geprüft")

    @override_settings(HTTPS=True)
    def test_reference_disagreement_can_show_candidate_without_linking_booking(self):
        first = ingest(booking_mail(reference="DEMO-1"))
        self.sync({1: booking_mail(reference="DEMO-2")})
        event = MailReceipt.objects.get().event
        self.assertIsNone(event.seat_id)
        response = self.client.get(reverse("review_detail", args=[event.pk]), secure=True)
        self.assertContains(response, "Buchungsreferenz weicht ab")
        self.assertContains(response, f"Nr. {first.seat_id}")

    @override_settings(HTTPS=True)
    def test_older_events_are_visible_in_archive_without_inventing_source_metadata(self):
        event = ingest(booking_mail())
        response = self.client.get(reverse("mail_receipts"), {"ansicht": "frueher"}, secure=True)
        self.assertContains(response, f"Ereignis {event.pk}")
        self.assertContains(response, "Ursprüngliche Maildaten wurden nicht erfasst")
