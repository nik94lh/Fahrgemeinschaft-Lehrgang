from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from bookings.rides_matching import sync_ride_cases

from mailimport.imap_sync import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    ImapSyncError,
    load_configuration,
    sync_mailbox,
)


class Command(BaseCommand):
    help = "Ruft neue E-Mails aus dem freigegebenen Postfach ausschließlich lesend ab."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE, help="Nachrichten pro Abruf")
        parser.add_argument("--watch", action="store_true", help="Vordergrundprozess für wiederholten Abruf")
        parser.add_argument("--interval", type=int, default=120, help="Wartezeit in Sekunden zwischen Abrufen")

    def handle(self, *args, **options):
        limit, interval, watch = options["limit"], options["interval"], options["watch"]
        if not 1 <= limit <= MAX_BATCH_SIZE or interval < 1:
            raise CommandError("Abruflimit muss zwischen 1 und 100 liegen, Wartezeit mindestens 1 Sekunde.")

        try:
            while True:
                self._poll(limit, watch)
                if not watch:
                    return
                try:
                    sync_ride_cases()
                except DatabaseError:
                    self.stderr.write("Der Abgleich der Fahrgemeinschaften ist fehlgeschlagen.")
                sleep(interval)
        except KeyboardInterrupt:
            return

    def _poll(self, limit, watch):
        try:
            config = load_configuration()
            if config is None:
                if not watch:
                    self.stdout.write("Der IMAP-Abruf ist deaktiviert.")
                return
            result = sync_mailbox(config, limit=limit)
        except ImapSyncError as error:
            if not watch:
                raise CommandError(str(error)) from None
            self.stderr.write(str(error))
            return
        except Exception:
            message = "Der IMAP-Abruf wurde wegen eines unerwarteten Fehlers abgebrochen."
            if not watch:
                raise CommandError(message) from None
            self.stderr.write(message)
            return

        if not watch or result.processed or result.skipped or result.has_more:
            self.stdout.write(f"Gelesen: {result.processed}. Übersprungen: {result.skipped}.")
