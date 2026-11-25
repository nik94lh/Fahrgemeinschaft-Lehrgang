from contextlib import nullcontext
from dataclasses import replace
from email.message import EmailMessage
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from bookings.contact_replay import ContactReplayResult
from bookings.models import BookingStatus, EmailEvent
from bookings.services import _reconcile
from mailimport.contact_replay import process_contact_replay
from mailimport.imap_sync import ImapConfig, mailbox_source_id
from mailimport.models import ContactReplayJob, MailboxConfiguration, MailboxCursor, MailReceipt
from mailimport.parser import parse_eml
from tests.test_imap_sync import body_result, mock_mailbox
from tests.test_participant_identity import PERSONAL, booking_mail
from tests.test_reconciliation import JUNE


queue_ffp_contact_replay = import_module(
    "mailimport.migrations.0005_queue_requested_ffp_contact_replay"
).queue_ffp_contact_replay


class RequestedContactReplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mailbox = MailboxConfiguration.objects.create(
            host="imap.example.invalid",
            username="test@example.invalid",
            mailbox="INBOX",
            enabled=True,
            encrypted_password="synthetischer-platzhalter",
        )
        cls.source_id = mailbox_source_id(cls.mailbox.host, cls.mailbox.port, cls.mailbox.username, cls.mailbox.mailbox)
        cls.cursor = MailboxCursor.objects.create(source_id=cls.source_id, uid_validity=42, last_uid=90)

    def replay_from_mailbox(self, raw):
        config = ImapConfig(self.mailbox.host, self.mailbox.port, self.mailbox.username, "synthetisch")
        client = mock_mailbox(uid_validity=42, uid_next=91)
        if raw is None:
            client.uid.side_effect = [("OK", [b""])]
        else:
            client.uid.side_effect = [
                ("OK", [b"43"]),
                ("OK", [f"1 (UID 43 RFC822.SIZE {len(raw)})".encode()]),
                body_result(43, raw),
            ]
        with (
            patch("mailimport.contact_replay.load_configuration", return_value=config),
            patch("mailimport.imap_sync._open_mailbox", return_value=nullcontext(client)),
        ):
            response = self.client.post(reverse("mailbox"), {"action": "start-contact-replay"}, secure=True)
            self.assertRedirects(response, reverse("mailbox"), fetch_redirect_response=False)
            return process_contact_replay()

    @override_settings(HTTPS=True, ALLOWED_HOSTS=["bms.planspielplatte.de"])
    def test_existing_enabled_ffp_instance_enqueues_one_bounded_job(self):
        executor = SimpleNamespace(connection=connection)
        queue_ffp_contact_replay(apps, executor)
        queue_ffp_contact_replay(apps, executor)

        job = ContactReplayJob.objects.get()
        self.assertEqual((job.source_id, job.uid_validity, job.upper_uid), (self.source_id, 42, 90))
        self.assertEqual(job.last_uid, 0)
        self.cursor.refresh_from_db()
        self.assertEqual(self.cursor.last_uid, 90)

    @override_settings(HTTPS=True, ALLOWED_HOSTS=["another-host.example.invalid"])
    def test_another_host_does_not_receive_the_one_time_request(self):
        queue_ffp_contact_replay(apps, SimpleNamespace(connection=connection))
        self.assertFalse(ContactReplayJob.objects.exists())

    @override_settings(HTTPS=True, ALLOWED_HOSTS=["bms.planspielplatte.de"])
    def test_disabled_mailbox_does_not_receive_the_request(self):
        self.mailbox.enabled = False
        self.mailbox.save(update_fields=["enabled"])

        queue_ffp_contact_replay(apps, SimpleNamespace(connection=connection))

        self.assertFalse(ContactReplayJob.objects.exists())

    @override_settings(HTTPS=True)
    def test_operator_replay_uses_logged_mail_and_records_the_result_in_its_details(self):
        raw = booking_mail(address=PERSONAL)
        receipt = MailReceipt(source_id=self.source_id, uid_validity=42, uid=43)
        event = _reconcile(replace(parse_eml(raw), participant_email=None), occurred_at=None, receipt=receipt)
        refetched = raw + b"\n"
        self.assertNotEqual(parse_eml(refetched).fingerprint, event.sha256)

        job = self.replay_from_mailbox(refetched)

        self.assertEqual(job.outcome_counts, {"ADDED": 1})
        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(event.seat.history.count(), 1)
        event.seat.participant.refresh_from_db()
        self.assertEqual(event.seat.participant.email, PERSONAL)
        receipt.refresh_from_db()
        self.assertNotIn("participant_email", receipt.fields)
        detail = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(detail, "Letzter Adressabgleich")
        self.assertContains(detail, ContactReplayResult.ADDED.value)

    @override_settings(HTTPS=True)
    def test_logged_mail_does_not_authorize_an_address_for_a_different_course_date(self):
        raw = booking_mail(address=PERSONAL)
        receipt = MailReceipt(source_id=self.source_id, uid_validity=42, uid=43)
        event = _reconcile(replace(parse_eml(raw), participant_email=None), occurred_at=None, receipt=receipt)
        different = booking_mail(address=PERSONAL, period=JUNE)

        job = self.replay_from_mailbox(different)

        self.assertEqual(job.outcome_counts, {"BOOKING_CHANGED": 1})
        event.seat.participant.refresh_from_db()
        self.assertEqual(event.seat.participant.email, "")

    @override_settings(HTTPS=True)
    def test_replay_reports_mail_missing_from_the_folder_on_its_receipt(self):
        receipt = MailReceipt.objects.create(
            source_id=self.source_id, uid_validity=42, uid=43, outcome=MailReceipt.Outcome.SKIPPED,
        )

        job = self.replay_from_mailbox(None)

        self.assertEqual(job.status, job.Status.COMPLETED)
        self.assertEqual(job.outcome_counts, {"UNREADABLE": 1})
        detail = self.client.get(reverse("mail_receipt", args=[receipt.pk]), secure=True)
        self.assertContains(detail, "Die Mail ist im Importordner nicht mehr verfügbar.")


    @override_settings(HTTPS=True, ALLOWED_HOSTS=["bms.planspielplatte.de"])
    def test_requested_replay_fills_a_contact_without_reimporting_the_booking(self):
        message = EmailMessage()
        message["Subject"] = "Lehrgangsanmeldung wurde bestätigt"
        message["To"] = "Jana Testperson <jana@example.invalid>"
        message.set_content(
            "Teilnehmer: Jana Testperson\nLehrgangsart: Synthetischer Lehrgang\n"
            "Schulungsort: SFS Würzburg\n"
            "Lehrgangstermin: 10.05.2031 09:45 Uhr – 14.05.2031 15:00 Uhr"
        )
        raw = message.as_bytes()
        old_event = _reconcile(replace(parse_eml(raw), participant_email=None), occurred_at=None)
        self.assertEqual(old_event.seat.participant.email, "")
        queue_ffp_contact_replay(apps, SimpleNamespace(connection=connection))

        config = ImapConfig(self.mailbox.host, self.mailbox.port, self.mailbox.username, "synthetisch")
        client = mock_mailbox(uid_validity=42, uid_next=91)
        client.uid.side_effect = [
            ("OK", [b"43"]),
            ("OK", [f"1 (UID 43 RFC822.SIZE {len(raw)})".encode()]),
            body_result(43, raw),
        ]
        with (
            patch("mailimport.contact_replay.load_configuration", return_value=config),
            patch("mailimport.imap_sync._open_mailbox", return_value=nullcontext(client)),
        ):
            job = process_contact_replay()

        self.assertEqual((job.status, job.scanned_count, job.enriched_count), (job.Status.COMPLETED, 1, 1))
        self.assertEqual(job.outcome_counts, {"ADDED": 1})
        self.assertEqual(EmailEvent.objects.count(), 1)
        old_event.refresh_from_db()
        self.assertEqual(old_event.state, EmailEvent.State.APPLIED)
        self.assertEqual(old_event.seat.status, BookingStatus.CONFIRMED)
        self.assertEqual(old_event.seat.participant.email, "jana@example.invalid")
        self.assertEqual(old_event.seat.history.count(), 1)
        self.cursor.refresh_from_db()
        self.assertEqual(self.cursor.last_uid, 90)
