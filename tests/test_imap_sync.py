import imaplib
from contextlib import nullcontext
from email.message import EmailMessage
from io import StringIO
from unittest.mock import ANY, Mock, call, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from bookings.models import CourseSeat, EmailEvent
from mailimport.imap_sync import ImapConfig, ImapSyncError, SyncSummary, mailbox_source_id, sync_mailbox
from mailimport.mailbox_settings import encrypt_password
from mailimport.models import MailReceipt, MailboxConfiguration, MailboxCursor
from mailimport.parser import MAX_EML_BYTES
from tests.test_participant_identity import booking_mail


def synthetic_message():
    message = EmailMessage()
    message["Subject"] = "Lehrgangsgenehmigung"
    message.set_content("Teilnehmer: Alex Beispiel")
    return message.as_bytes()


def mock_mailbox(uid_validity=11, uid_next=3):
    client = Mock(spec=imaplib.IMAP4)
    client.login.return_value = "OK", [b""]
    client.select.return_value = "OK", [b"2"]
    client.response.side_effect = lambda name: (
        name,
        [str({"UIDVALIDITY": uid_validity, "UIDNEXT": uid_next}[name]).encode("ascii")],
    )
    return client


def second_mailbox_setting(*, enabled=True):
    return MailboxConfiguration.objects.create(
        pk=2, host="imap.second.example.org", username="second@example.invalid", mailbox="IMPORT",
        encrypted_password=encrypt_password("second-fixture-mail-password"), enabled=enabled,
    )


def body_result(uid, raw, *, uid_after_literal=False):
    literal = f"BODY[] {{{len(raw)}}}"
    if uid_after_literal:
        return "OK", [(f"1 ({literal}".encode(), raw), f" UID {uid})".encode()]
    return "OK", [(f"1 (UID {uid} {literal}".encode(), raw), b")"]


class ImapSyncTests(TestCase):
    def setUp(self):
        self.config = ImapConfig("imap.example.org", 993, "sync user", "test-password", 'folder "name"')
        self.raw = synthetic_message()

    def run_sync(self, client, *, limit=25):
        with (
            patch("mailimport.imap_sync._open_mailbox", return_value=nullcontext(client)),
            patch("mailimport.imap_sync.ingest") as ingest,
        ):
            summary = sync_mailbox(self.config, limit=limit)
        return summary, ingest

    def test_examines_and_checks_each_size_before_peek(self):
        client = mock_mailbox()
        client.uid.side_effect = [
            ("OK", [b"2 1"]),
            ("OK", [f"1 (UID 1 RFC822.SIZE {MAX_EML_BYTES + 1})".encode()]),
            ("OK", [f"2 (UID 2 RFC822.SIZE {len(self.raw)})".encode()]),
            body_result(2, self.raw, uid_after_literal=True),
        ]

        summary, ingest = self.run_sync(client)

        client.login.assert_called_once_with('"sync user"', "test-password")
        client.select.assert_called_once_with('"folder \\"name\\""', readonly=True)
        self.assertEqual(client.uid.call_args_list, [
            call("SEARCH", None, "UID", "1:2"),
            call("FETCH", "1", "(UID RFC822.SIZE)"),
            call("FETCH", "2", "(UID RFC822.SIZE)"),
            call("FETCH", "2", "(UID BODY.PEEK[])"),
        ])
        ingest.assert_called_once_with(self.raw, occurred_at=None, receipt=ANY)
        client.close.assert_not_called()
        client.store.assert_not_called()
        client.expunge.assert_not_called()
        self.assertEqual((summary.processed, summary.skipped), (1, 1))
        cursor = MailboxCursor.objects.get()
        self.assertEqual(cursor.last_uid, 2)
        self.assertTrue(cursor.last_sync_ok)
        self.assertIsNotNone(cursor.last_sync_at)

    def test_batch_resumes_from_durable_last_uid(self):
        first = mock_mailbox(uid_next=4)
        first.uid.side_effect = [
            ("OK", [b"1 2 3"]),
            ("OK", [f"1 (UID 1 RFC822.SIZE {len(self.raw)})".encode()]),
            body_result(1, self.raw),
        ]
        summary, _ = self.run_sync(first, limit=1)
        self.assertTrue(summary.has_more)
        self.assertEqual(MailboxCursor.objects.get().last_uid, 1)

        second = mock_mailbox(uid_next=4)
        second.uid.side_effect = [
            ("OK", [b"2 3"]),
            ("OK", [f"2 (UID 2 RFC822.SIZE {len(self.raw)})".encode()]),
            body_result(2, self.raw),
        ]
        self.run_sync(second, limit=1)
        second.uid.assert_any_call("SEARCH", None, "UID", "2:3")
        self.assertEqual(MailboxCursor.objects.get().last_uid, 2)

    def test_uidvalidity_change_resets_cursor(self):
        MailboxCursor.objects.create(source_id=self.config.source_id, uid_validity=10, last_uid=5500)
        client = mock_mailbox(uid_validity=11)
        client.uid.return_value = "OK", [b""]

        self.run_sync(client)

        client.uid.assert_called_once_with("SEARCH", None, "UID", "1:2")
        cursor = MailboxCursor.objects.get()
        self.assertEqual((cursor.uid_validity, cursor.last_uid), (11, 2))

    def test_bad_fetch_response_is_safe_and_preserves_cursor(self):
        MailboxCursor.objects.create(source_id=self.config.source_id, uid_validity=11, last_uid=1)
        client = mock_mailbox()
        client.uid.side_effect = [
            ("OK", [b"2"]),
            ("OK", [f"2 (UID 2 RFC822.SIZE {len(self.raw)})".encode()]),
            body_result(11, self.raw),
        ]
        with self.assertRaises(ImapSyncError):
            self.run_sync(client)
        cursor = MailboxCursor.objects.get()
        self.assertEqual(cursor.last_uid, 1)
        self.assertFalse(cursor.last_sync_ok)

    def test_command_defaults_to_disabled_and_does_not_open_a_connection(self):
        output = StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("mailimport.imap_sync._open_mailbox") as open_mailbox,
        ):
            call_command("sync_mailbox", stdout=output)
        open_mailbox.assert_not_called()
        self.assertIn("deaktiviert", output.getvalue())

    def test_watch_checks_outbox_even_when_both_imap_inputs_are_disabled(self):
        command = "mailimport.management.commands.sync_mailbox"
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(f"{command}.deliver_ride_mail") as deliver,
            patch(f"{command}.sleep", side_effect=KeyboardInterrupt),
        ):
            call_command("sync_mailbox", stdout=StringIO())
            deliver.assert_not_called()
            call_command("sync_mailbox", watch=True, stdout=StringIO())

        deliver.assert_called_once_with()

    def test_operator_errors_do_not_leak_server_or_config_details(self):
        with (
            patch("mailimport.management.commands.sync_mailbox.load_configuration", return_value=self.config),
            patch("mailimport.imap_sync._open_mailbox", side_effect=imaplib.IMAP4.error("sensitive fragment")),
            self.assertRaises(CommandError) as caught,
        ):
            call_command("sync_mailbox")
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertNotIn("test-password", str(caught.exception))

    def test_watch_reloads_configuration_and_retries_after_error(self):
        output = StringIO()
        errors = StringIO()
        command = "mailimport.management.commands.sync_mailbox"
        with (
            patch(f"{command}.load_configuration", side_effect=[None, self.config, self.config]) as load,
            patch(f"{command}.sync_mailbox", side_effect=[ImapSyncError("Abruf fehlgeschlagen."), SyncSummary(1, 0, False)]) as sync,
            patch(f"{command}.sleep", side_effect=[None, None, KeyboardInterrupt]) as pause,
        ):
            call_command("sync_mailbox", watch=True, interval=9, stdout=output, stderr=errors)

        self.assertEqual(load.call_count, 3)
        self.assertEqual(sync.call_args_list, [call(self.config, limit=25)] * 2)
        self.assertEqual(pause.call_args_list, [call(9)] * 3)
        self.assertIn("Abruf fehlgeschlagen.", errors.getvalue())
        self.assertIn("Gelesen: 1.", output.getvalue())

    def test_second_input_still_reads_when_first_connection_fails(self):
        # The new account can be configured before moving the existing server-based input.
        env = {
            "BMS_IMAP_ENABLED": "1", "BMS_IMAP_HOST": "imap.first.example.invalid",
            "BMS_IMAP_USERNAME": "first@example.invalid", "BMS_IMAP_PASSWORD": "first-fixture-password",
        }
        primary_source = mailbox_source_id(env["BMS_IMAP_HOST"], 993, env["BMS_IMAP_USERNAME"], "INBOX")
        secondary = second_mailbox_setting()
        secondary_source = mailbox_source_id(secondary.host, secondary.port, secondary.username, secondary.mailbox)
        client = mock_mailbox(uid_next=2)
        client.uid.return_value = "OK", [b""]

        with (
            patch.dict("os.environ", env, clear=True),
            patch("mailimport.imap_sync._open_mailbox", side_effect=[OSError(), nullcontext(client)]),
            self.assertRaises(CommandError),
        ):
            call_command("sync_mailbox", stderr=StringIO(), stdout=StringIO())

        self.assertFalse(MailboxCursor.objects.get(pk=primary_source).last_sync_ok)
        self.assertTrue(MailboxCursor.objects.get(pk=secondary_source).last_sync_ok)
        client.select.assert_called_once_with('"IMPORT"', readonly=True)

    def test_same_mail_in_two_inboxes_creates_two_receipts_and_one_import(self):
        env = {
            "BMS_IMAP_ENABLED": "1", "BMS_IMAP_HOST": "imap.first.example.invalid",
            "BMS_IMAP_USERNAME": "first@example.invalid", "BMS_IMAP_PASSWORD": "first-fixture-password",
        }
        second_mailbox_setting()
        raw = booking_mail()
        clients = [mock_mailbox(uid_validity=11, uid_next=2) for _ in range(2)]
        for client in clients:
            client.uid.side_effect = [
                ("OK", [b"1"]), ("OK", [f"1 (UID 1 RFC822.SIZE {len(raw)})".encode()]),
                body_result(1, raw),
            ]

        with (
            patch.dict("os.environ", env, clear=True),
            patch("mailimport.imap_sync._open_mailbox", side_effect=[nullcontext(client) for client in clients]),
        ):
            call_command("sync_mailbox", stdout=StringIO())

        self.assertEqual(EmailEvent.objects.count(), 1)
        self.assertEqual(CourseSeat.objects.count(), 1)
        self.assertEqual(MailReceipt.objects.count(), 2)
        self.assertEqual(MailReceipt.objects.get(mailbox_slot=1).outcome, MailReceipt.Outcome.APPLIED)
        self.assertEqual(MailReceipt.objects.get(mailbox_slot=2).outcome, MailReceipt.Outcome.DUPLICATE)
        self.assertEqual(MailReceipt.objects.values("source_id").distinct().count(), 2)
        self.assertEqual(set(MailReceipt.objects.values_list("mailbox_slot", flat=True)), {1, 2})
