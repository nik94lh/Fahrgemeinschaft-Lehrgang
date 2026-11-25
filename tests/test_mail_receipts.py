from contextlib import nullcontext
from email import policy
from email.parser import BytesParser
from dataclasses import replace
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from bookings.models import BookingStatus, CourseSeat, EmailEvent
from bookings.services import _reconcile, ingest, set_participant_email
from mailimport.imap_sync import ImapConfig, sync_mailbox
from mailimport.models import ContactReplayJob, MailReceipt, MailboxCursor
from mailimport.parser import MAX_EML_BYTES, parse_eml
from tests.test_imap_sync import body_result, mock_mailbox
from tests.test_participant_email_parser import APPROVAL, booking_message, nested_booking_forward
from tests.test_participant_identity import PERSONAL, booking_mail
from tests.test_reconciliation import JUNE, eml
from tests.workspace_client import WorkspaceTestCase


class MailReceiptTests(WorkspaceTestCase):
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

    def test_alias_is_recorded_for_confirmation_and_reused_after_saving(self):
        alias = "mx-4472@example.invalid"
        self.sync({1: booking_mail(to=alias)})
        receipt = MailReceipt.objects.get()
        seat = receipt.event.seat
        self.assertEqual(receipt.fields["participant_email_candidate"], alias)
        self.assertNotIn("participant_email", receipt.fields)
        self.assertEqual(seat.participant.email, "")

        set_participant_email(seat.pk, alias, expected_participant_id=seat.participant_id)
        next_booking = ingest(booking_mail(to=alias, period=JUNE))
        self.assertEqual(next_booking.seat.participant_id, seat.participant_id)
        self.assertEqual(next_booking.seat.participant.email, alias)

        different_person = ingest(booking_mail(name="Jana Testperson", to=alias, period=JUNE))
        self.assertNotEqual(different_person.seat.participant_id, seat.participant_id)
        self.assertEqual(different_person.seat.participant.email, "")

    def test_personal_confirmation_saves_single_original_alias_on_existing_booking(self):
        original = ingest(booking_mail())
        alias = "lernplatz97@example.invalid"
        body = APPROVAL.replace(
            "Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:",
            "Sie sind für den folgenden Lehrgangstermin angemeldet:",
        )
        mail = booking_message(alias, subject="Ihr Lehrgangstermin wurde bestätigt", body=body)

        self.sync({1: nested_booking_forward(mail).as_bytes()})

        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.ADDRESS_ADDED)
        self.assertEqual(receipt.fields["participant_email"], alias)
        self.assertEqual(receipt.event.seat_id, original.seat_id)
        self.assertEqual(receipt.event.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(receipt.event.seat.participant.email, alias)
        self.assertEqual(CourseSeat.objects.count(), 1)

    def test_personal_confirmation_with_different_single_alias_creates_an_address_conflict(self):
        original = ingest(booking_mail(address=PERSONAL))
        alias = "lernplatz97@example.invalid"
        body = APPROVAL.replace(
            "Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:",
            "Sie haben Ihre Teilnahme an folgendem Lehrgangstermin bestätigt:",
        )
        mail = booking_message(alias, subject="Bestätigung Ihrer Lehrgangsanmeldung", body=body)

        self.sync({1: mail.as_bytes()})

        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.REVIEW)
        self.assertEqual(receipt.fields["participant_email"], alias)
        self.assertEqual(receipt.event.review_reason, EmailEvent.ReviewReason.PARTICIPANT_EMAIL_CONFLICT)
        self.assertEqual(receipt.event.seat_id, original.seat_id)
        self.assertEqual(receipt.event.seat.status, BookingStatus.APPROVED)
        self.assertEqual(receipt.event.seat.participant.email, PERSONAL)
        self.assertEqual(CourseSeat.objects.count(), 1)

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
        email = "mail@alexbeispiel.example"
        raw = nested_booking_forward(booking_message(f'"{email}" <{email}>')).as_bytes()
        original = _reconcile(replace(parse_eml(raw), participant_email=None), occurred_at=None)
        confirmation = ingest(booking_mail(kind="confirmation"))
        self.assertEqual(confirmation.seat_id, original.seat_id)

        self.sync({1: raw})

        receipt = MailReceipt.objects.get()
        self.assertEqual(receipt.outcome, MailReceipt.Outcome.ADDRESS_ADDED)
        self.assertEqual(receipt.event_id, original.pk)
        self.assertEqual(receipt.fields["participant_email"], email)
        self.assertEqual(EmailEvent.objects.count(), 2)
        seat = CourseSeat.objects.get()
        self.assertEqual(seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(seat.participant_id, original.seat.participant_id)
        self.assertEqual(seat.participant.email, email)

    def test_redelivery_applies_a_newly_recognized_confirmation_to_its_open_case(self):
        approval = ingest(eml("Lehrgangsgenehmigung"))
        raw = eml("Ihr Lehrgangstermin wurde bestätigt")
        old_parse = replace(parse_eml(raw), event_type="unknown")
        case = _reconcile(old_parse, occurred_at=None)
        self.assertEqual(case.review_reason, EmailEvent.ReviewReason.UNRECOGNIZED_TYPE)

        self.sync({1: raw, 2: raw})

        case.refresh_from_db()
        self.assertEqual(case.kind, EmailEvent.Kind.CONFIRMATION)
        self.assertEqual(case.state, EmailEvent.State.APPLIED)
        self.assertEqual(case.review_reason, EmailEvent.ReviewReason.UNKNOWN)
        self.assertEqual(case.seat_id, approval.seat_id)
        self.assertEqual(case.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(case.seat.history.count(), 2)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(EmailEvent.objects.count(), 2)
        first, second = MailReceipt.objects.order_by("uid")
        self.assertEqual((first.event_id, second.event_id), (case.pk, case.pk))
        self.assertEqual((first.outcome, second.outcome), (MailReceipt.Outcome.APPLIED, MailReceipt.Outcome.DUPLICATE))

    def test_redelivery_does_not_reopen_a_dismissed_unknown_message(self):
        raw = eml("Bestätigung Ihrer Lehrgangsanmeldung")
        case = EmailEvent.objects.create(
            sha256=parse_eml(raw).fingerprint,
            kind=EmailEvent.Kind.UNKNOWN,
            state=EmailEvent.State.DISMISSED,
            review_reason=EmailEvent.ReviewReason.UNRECOGNIZED_TYPE,
            review_resolution=EmailEvent.ReviewResolution.NOT_RELEVANT,
        )

        self.sync({1: raw})

        case.refresh_from_db()
        self.assertEqual(case.state, EmailEvent.State.DISMISSED)
        self.assertEqual(MailReceipt.objects.get().outcome, MailReceipt.Outcome.DUPLICATE)
        self.assertFalse(CourseSeat.objects.exists())

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
    def test_contact_recheck_post_returns_to_receipt_and_shows_active_check_only(self):
        self.sync({1: booking_mail(), 2: booking_mail(name="Pat Musterfall")})
        receipt = MailReceipt.objects.get(uid=2)
        detail = reverse("mail_receipt", args=[receipt.pk])
        action = f"{detail}adresse-pruefen/"
        client = self.authenticated_client(enforce_csrf_checks=True)

        with (
            patch("mailimport.receipt_views.load_configuration", return_value=self.config),
            patch("mailimport.contact_replay.load_configuration", return_value=self.config),
        ):
            original = client.get(detail, secure=True)
            self.assertContains(original, f'action="{action}"')
            self.assertContains(original, "Adresse erneut prüfen")
            self.assertContains(original, receipt.fields["address_note"])
            self.assertEqual(client.get(action, secure=True).status_code, 405)
            self.assertEqual(client.post(action, secure=True).status_code, 403)
            response = client.post(
                action, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value},
                secure=True, HTTP_REFERER=f"https://testserver{detail}",
            )
            self.assertRedirects(response, detail, fetch_redirect_response=False)

            job = ContactReplayJob.objects.get()
            self.assertEqual((job.last_uid, job.upper_uid), (receipt.uid - 1, receipt.uid))
            self.assertContains(client.get(detail, secure=True), "Adressprüfung vorgemerkt")
            job.status = ContactReplayJob.Status.COMPLETED
            job.save(update_fields=["status"])
            receipt.address_checked_at = timezone.now()
            receipt.address_check_detail = "Ergebnis der synthetischen Adressprüfung"
            receipt.save(update_fields=["address_checked_at", "address_check_detail"])
            refreshed = client.get(detail, secure=True, HTTP_X_BMS_LIVE="1")
            self.assertContains(refreshed, receipt.address_check_detail)
            self.assertNotContains(refreshed, "Adressprüfung vorgemerkt")
            self.assertNotContains(refreshed, receipt.fields["address_note"])

    @override_settings(HTTPS=True)
    def test_contact_recheck_rejects_receipt_from_another_mailbox(self):
        self.sync({1: booking_mail()})
        receipt = MailReceipt.objects.get()
        detail = reverse("mail_receipt", args=[receipt.pk])
        action = reverse("mail_receipt_recheck", args=[receipt.pk])
        other_mailbox = replace(self.config, mailbox="Old")
        MailboxCursor.objects.create(source_id=other_mailbox.source_id, uid_validity=11, last_uid=1)
        with (
            patch("mailimport.receipt_views.load_configuration", return_value=other_mailbox),
            patch("mailimport.contact_replay.load_configuration", return_value=other_mailbox),
        ):
            self.assertNotContains(self.client.get(detail, secure=True), "Adresse erneut prüfen")
            response = self.client.post(action, secure=True)
            self.assertRedirects(response, detail, fetch_redirect_response=False)
            self.assertContains(self.client.get(detail, secure=True), "aktuellen Importordner")
            self.assertNotContains(self.client.get(detail, secure=True), "aktuellen Importordner")
        self.assertFalse(ContactReplayJob.objects.exists())

    @override_settings(HTTPS=True)
    def test_receipt_reports_targeted_failure_until_a_new_result_exists(self):
        self.sync({1: booking_mail(), 2: booking_mail(name="Pat Musterfall")})
        receipt = MailReceipt.objects.get(uid=2)
        detail = reverse("mail_receipt", args=[receipt.pk])
        failure = ContactReplayJob.objects.create(
            source_id=receipt.source_id, uid_validity=receipt.uid_validity,
            last_uid=1, upper_uid=2, status=ContactReplayJob.Status.FAILED,
            error_message="Der synthetische Abruf wurde unterbrochen.",
        )
        with patch("mailimport.receipt_views.load_configuration", return_value=self.config):
            failed = self.client.get(detail, secure=True, HTTP_X_BMS_LIVE="1")
            self.assertContains(failed, failure.error_message)
            self.assertContains(failed, "Adresse erneut prüfen")

            receipt.address_checked_at = timezone.now()
            receipt.address_check_detail = "Ein neuer Adresscheck liegt vor."
            receipt.save(update_fields=["address_checked_at", "address_check_detail"])
            refreshed = self.client.get(detail, secure=True, HTTP_X_BMS_LIVE="1")
            self.assertContains(refreshed, receipt.address_check_detail)
            self.assertNotContains(refreshed, failure.error_message)

    @override_settings(HTTPS=False)
    def test_contact_recheck_requires_https_configuration(self):
        self.sync({1: booking_mail()})
        action = reverse("mail_receipt_recheck", args=[MailReceipt.objects.get().pk])
        self.assertEqual(self.client.post(action, secure=True).status_code, 404)
        self.assertFalse(ContactReplayJob.objects.exists())

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
