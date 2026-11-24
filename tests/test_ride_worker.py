from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import DatabaseError
from django.test import SimpleTestCase


COMMAND = "mailimport.management.commands.sync_mailbox"


class RideWorkerTests(SimpleTestCase):
    def test_watch_reconciles_ride_cases_even_without_active_imap(self):
        with (
            patch(f"{COMMAND}.load_configuration", return_value=None),
            patch(f"{COMMAND}.sync_ride_cases") as reconcile,
            patch(f"{COMMAND}.process_contact_replay") as replay,
            patch(f"{COMMAND}.sleep", side_effect=KeyboardInterrupt),
        ):
            call_command("sync_mailbox", watch=True, stdout=StringIO())

        reconcile.assert_called_once_with()
        replay.assert_called_once_with()


    def test_watch_retries_a_failed_ride_sync_without_exposing_error_details(self):
        errors = StringIO()
        with (
            patch(f"{COMMAND}.load_configuration", return_value=None),
            patch(f"{COMMAND}.sync_ride_cases", side_effect=[DatabaseError("internal detail"), None]) as reconcile,
            patch(f"{COMMAND}.process_contact_replay"),
            patch(f"{COMMAND}.sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            call_command("sync_mailbox", watch=True, stderr=errors)

        self.assertEqual(reconcile.call_count, 2)
        self.assertIn("Abgleich der Fahrgemeinschaften ist fehlgeschlagen", errors.getvalue())
        self.assertNotIn("internal detail", errors.getvalue())
