"""Inspect an EML locally without importing a booking or sending mail."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from mailimport.parser import MAX_EML_BYTES, MailParseError, parse_eml


class Command(BaseCommand):
    help = "Zeigt erkannte Felder einer lokalen EML-Datei oder von stdin (-) als JSON an."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Lokale EML-Datei oder - für stdin")

    def handle(self, *args, **options):
        source = options["source"]
        try:
            if source == "-":
                raw = sys.stdin.buffer.read(MAX_EML_BYTES + 1)
            else:
                with Path(source).open("rb") as stream:
                    raw = stream.read(MAX_EML_BYTES + 1)
        except OSError as exc:
            raise CommandError("Die lokale Maildatei konnte nicht gelesen werden.") from exc
        if len(raw) > MAX_EML_BYTES:
            raise CommandError("Die Mail ist größer als 1 MiB.")

        try:
            parsed = parse_eml(raw)
        except MailParseError as exc:
            raise CommandError("Die EML-Datei konnte nicht ausgewertet werden.") from exc
        return json.dumps(asdict(parsed), cls=DjangoJSONEncoder, ensure_ascii=False, indent=2)
