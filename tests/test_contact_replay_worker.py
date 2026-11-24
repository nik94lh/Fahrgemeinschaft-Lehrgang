import imaplib
from contextlib import nullcontext
from unittest.mock import Mock, call, patch

from django.test import TestCase

from bookings.models import Participant
from mailimport.contact_replay import ContactReplayError, process_contact_replay, queue_contact_replay
from mailimport.imap_sync import ImapConfig
from mailimport.models import ContactReplayJob, MailboxCursor
from mailimport.parser import MAX_EML_BYTES
from tests.test_imap_sync import body_result, mock_mailbox, synthetic_message


class ContactReplayWorkerTests(TestCase):
    def setUp(self):
        self.config = ImapConfig("imap.example.invalid", 993, "synthetic", "fixture-password")
        self.raw = synthetic_message()

    def queue(self, upper_uid, *, uid_validity=11):
        MailboxCursor.objects.update_or_create(
            source_id=self.config.source_id,
            defaults={"uid_validity": uid_validity, "last_uid": upper_uid},
        )
        with patch("mailimport.contact_replay.load_configuration", return_value=self.config):
            return queue_contact_replay()

    def step(self, client, callback=None):
        if callback is None:
            callback = Mock(return_value=True)
        with (
            patch("mailimport.contact_replay.load_configuration", return_value=self.config),
            patch("mailimport.imap_sync._open_mailbox", return_value=nullcontext(client)),
            patch("mailimport.contact_replay.enrich_contact_from_imported_mail", callback),
        ):
            return process_contact_replay()

    def fetch(self, uid):
        return [
            ("OK", [f"1 (UID {uid} RFC822.SIZE {len(self.raw)})".encode()]),
            body_result(uid, self.raw),
        ]

    def test_no_replay_starts_implicitly(self):
        with patch("mailimport.contact_replay.load_configuration") as load:
            self.assertIsNone(process_contact_replay())
        load.assert_not_called()

    def test_queue_requires_enabled_source_and_prior_successful_uid_checkpoint(self):
        for config in (None, self.config):
            with (
                patch("mailimport.contact_replay.load_configuration", return_value=config),
                self.assertRaises(ContactReplayError),
            ):
                queue_contact_replay()
        self.assertFalse(ContactReplayJob.objects.exists())

    def test_queue_snapshots_cursor_and_is_idempotent_while_active(self):
        job = self.queue(31)
        again = self.queue(45)

        self.assertEqual(again.pk, job.pk)
        self.assertEqual(again.source_id, self.config.source_id)
        self.assertEqual(again.uid_validity, 11)
        self.assertEqual(again.upper_uid, 31)
        self.assertEqual(again.last_uid, 0)
        self.assertEqual(again.status, ContactReplayJob.Status.QUEUED)

    def test_replay_resumes_separately_from_new_normal_mail_and_uses_peek(self):
        job = self.queue(26)
        MailboxCursor.objects.filter(source_id=self.config.source_id).update(last_uid=70)
        client = mock_mailbox(uid_next=80)
        client.uid.side_effect = [
            ("OK", [b" ".join(str(uid).encode() for uid in range(1, 27))]),
            *(result for uid in range(1, 26) for result in self.fetch(uid)),
        ]
        callback = Mock(side_effect=[True] + [False] * 24)

        first = self.step(client, callback)

        self.assertEqual(first.pk, job.pk)
        self.assertEqual(first.status, ContactReplayJob.Status.RUNNING)
        self.assertEqual((first.last_uid, first.scanned_count, first.enriched_count), (25, 25, 1))
        client.select.assert_called_once_with('"INBOX"', readonly=True)
        self.assertEqual(client.uid.call_args_list[:3], [
            call("SEARCH", None, "UID", "1:26"),
            call("FETCH", "1", "(UID RFC822.SIZE)"),
            call("FETCH", "1", "(UID BODY.PEEK[])"),
        ])
        client.close.assert_not_called()
        client.store.assert_not_called()
        client.expunge.assert_not_called()
        self.assertEqual(callback.call_args_list, [call(self.raw)] * 25)

        second_client = mock_mailbox(uid_next=80)
        second_client.uid.side_effect = [("OK", [b"26"]), *self.fetch(26)]
        second = self.step(second_client)

        second_client.uid.assert_any_call("SEARCH", None, "UID", "26:26")
        self.assertEqual(second.status, ContactReplayJob.Status.COMPLETED)
        self.assertEqual((second.last_uid, second.scanned_count, second.enriched_count), (26, 26, 2))
        self.assertEqual(MailboxCursor.objects.get().last_uid, 70)
        self.assertIsNotNone(second.finished_at)

    def test_oversized_message_is_skipped_without_fetching_its_body(self):
        self.queue(1)
        client = mock_mailbox()
        client.uid.side_effect = [
            ("OK", [b"1"]),
            ("OK", [f"1 (UID 1 RFC822.SIZE {MAX_EML_BYTES + 1})".encode()]),
        ]
        callback = Mock()
        job = self.step(client, callback)
        self.assertEqual(job.status, ContactReplayJob.Status.COMPLETED)
        self.assertEqual((job.scanned_count, job.enriched_count), (1, 0))
        self.assertEqual(client.uid.call_count, 2)
        callback.assert_not_called()

    def test_new_uid_validity_halts_the_job_before_any_fetch(self):
        self.queue(2)
        client = mock_mailbox(uid_validity=12)
        job = self.step(client)
        self.assertEqual(job.status, ContactReplayJob.Status.FAILED)
        self.assertEqual(job.last_uid, 0)
        self.assertIsNotNone(job.finished_at)
        client.uid.assert_not_called()

    def test_changed_mailbox_halts_before_connecting(self):
        self.queue(2)
        switched = ImapConfig("imap.example.invalid", 993, "different-fixture", "fixture-password")
        with (
            patch("mailimport.contact_replay.load_configuration", return_value=switched),
            patch("mailimport.imap_sync._open_mailbox") as opening,
        ):
            job = process_contact_replay()
        opening.assert_not_called()
        self.assertEqual(job.status, ContactReplayJob.Status.FAILED)

    def test_transient_failure_rolls_back_partial_progress_and_retries_same_range(self):
        self.queue(2)
        client = mock_mailbox()
        client.uid.side_effect = [
            ("OK", [b"1 2"]), *self.fetch(1), imaplib.IMAP4.abort("untrusted connection text"),
        ]

        def update_profile(_raw):
            Participant.objects.create(name="Alex Beispiel")
            return True

        interrupted = self.step(client, update_profile)
        self.assertFalse(Participant.objects.exists())
        self.assertEqual(interrupted.status, ContactReplayJob.Status.RUNNING)
        self.assertEqual((interrupted.last_uid, interrupted.scanned_count, interrupted.enriched_count), (0, 0, 0))
        self.assertNotIn("untrusted", interrupted.error_message)

        retry = mock_mailbox()
        retry.uid.side_effect = [("OK", [b"1 2"]), *self.fetch(1), *self.fetch(2)]
        completed = self.step(retry)
        self.assertEqual(completed.status, ContactReplayJob.Status.COMPLETED)
        self.assertEqual(completed.error_message, "")
        self.assertEqual(retry.uid.call_args_list[0], call("SEARCH", None, "UID", "1:2"))
