from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from bookings.ride_mail import deliver_ride_mail
from bookings.ride_participation import run_automatic_ride_inquiries
from bookings.rides_matching import sync_ride_cases

from mailimport.contact_replay import process_contact_replay
from mailimport.imap_sync import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    ImapSyncError,
    load_configuration,
    load_second_configuration,
    sync_mailbox,
)
from mailimport.models import MailboxConfiguration


class Command(BaseCommand):
    help = "Ruft neue E-Mails aus den freigegebenen Postfächern ausschließlich lesend ab."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE, help="Nachrichten pro Postfach und Abruf")
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
                    run_automatic_ride_inquiries()
                except DatabaseError:
                    self.stderr.write("Der Abgleich der Fahrgemeinschaften ist fehlgeschlagen.")
                try:
                    process_contact_replay()
                except DatabaseError:
                    self.stderr.write("Der Adressabgleich konnte nicht fortgesetzt werden.")
                try:
                    deliver_ride_mail()
                except DatabaseError:
                    self.stderr.write("Die Warteschlange für Fahrgemeinschaftsmails konnte nicht verarbeitet werden.")
                except Exception:
                    self.stderr.write("Der Versand von Fahrgemeinschaftsmails wurde wegen eines unerwarteten Fehlers unterbrochen.")
                sleep(interval)
        except KeyboardInterrupt:
            return

    def _poll(self, limit, watch):
        sources = set()
        errors = []
        for slot, load in zip(MailboxConfiguration.Slot, (load_configuration, load_second_configuration), strict=True):
            try:
                config = load()
                if config is None:
                    continue
                if config.source_id in sources:
                    raise ImapSyncError("Dieses Postfach ist beim anderen Eingang bereits aktiv.")
                sources.add(config.source_id)
                result = sync_mailbox(config, limit=limit)
            except ImapSyncError as error:
                errors.append(f"{slot.label}: {error}")
            except Exception:
                errors.append(f"{slot.label}: Der IMAP-Abruf wurde wegen eines unerwarteten Fehlers abgebrochen.")
            else:
                if not watch or result.processed or result.skipped or result.has_more:
                    self.stdout.write(f"{slot.label}: Gelesen: {result.processed}. Übersprungen: {result.skipped}.")

        if watch:
            for message in errors:
                self.stderr.write(message)
        elif errors:
            raise CommandError("\n".join(errors))
        elif not sources:
            self.stdout.write("Der IMAP-Abruf ist deaktiviert.")
