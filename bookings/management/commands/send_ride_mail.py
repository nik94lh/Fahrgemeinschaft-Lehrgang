from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from bookings.ride_mail import deliver_ride_mail


class Command(BaseCommand):
    help = "Verarbeitet eingeplante Fahrgemeinschaftsmails."

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true", help="Fordert regelmäßig einen Versandlauf an.")
        parser.add_argument("--interval", type=int, default=15, help="Wartezeit zwischen Versandläufen in Sekunden.")
        parser.add_argument("--limit", type=int, default=25, help="Höchstens so viele Aufträge pro Durchlauf prüfen.")

    def handle(self, *args, **options):
        interval, limit, watch = options["interval"], options["limit"], options["watch"]
        if interval < 1 or not 1 <= limit <= 100:
            raise CommandError("Wartezeit mindestens eine Sekunde und Auftragslimit zwischen 1 und 100 wählen.")
        try:
            while True:
                try:
                    sent = deliver_ride_mail(limit=limit)
                except DatabaseError:
                    message = "Der Versandlauf konnte wegen eines Datenbankfehlers nicht abgeschlossen werden."
                    if not watch:
                        raise CommandError(message) from None
                    self.stderr.write(message)
                else:
                    if sent or not watch:
                        self.stdout.write(f"Versendet: {sent}.")
                if not watch:
                    return
                sleep(interval)
        except KeyboardInterrupt:
            return
