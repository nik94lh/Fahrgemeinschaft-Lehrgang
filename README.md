# Lehrgangs- und Fahrgemeinschaftsmanagement

LFM verwaltet Lehrgänge und Buchungen für die Feuerwehren eines Landkreises. Die freiwillige Fahrgemeinschaftsvermittlung folgt in späteren Entwicklungsphasen. Der erste Stand enthält die Django-Grundstruktur, das Datenmodell und eine EML-Vorschau für synthetische Beispiele im Browser und in der Konsole.

## Lokaler Start mit Docker Compose

Voraussetzungen sind Docker und Docker Compose v2. Lege beim ersten Start eine lokale Konfiguration aus der Vorlage an:

```bash
test -e .env || cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py migrate
curl --fail http://127.0.0.1:8000/health/
```

Die Web-Vorschau erreichst du unter http://127.0.0.1:8000/. Über `/health/` kannst du den Start technisch prüfen. Der Entwicklungsport ist nur lokal gebunden. PostgreSQL bleibt im Docker-Netz des Projekts.

Führe die Tests und die Prüfung auf fehlende Migrationen aus:

```bash
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py test
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py makemigrations --check --dry-run
```

## Lokale EML-Vorschau

Öffne die Startseite, wähle eine synthetische oder ausreichend anonymisierte `.eml`-Datei bis 1 MiB und klicke auf **Prüfen**. BMS zeigt erkannte Felder und Hinweise an, ohne die Datei oder Buchungsdaten zu speichern. Alternativ liest dieser Befehl die Datei vom lokalen Rechner ein:

```bash
docker compose -f compose.yaml -f compose.dev.yaml exec -T web python manage.py preview_eml - < beispiel.eml
```

In einer Python-Umgebung mit den Projektabhängigkeiten funktioniert auch `python manage.py preview_eml beispiel.eml`. Die Vorschau verarbeitet die übergebenen Bytes lokal. Ein Postfach oder SMTP-Zugang ist für diesen Stand nicht erforderlich.

## Dokumentation

- Fachliche Anforderungen und offene Fragen [Pflichtenheft](docs/PFLICHTENHEFT.md)
- Komponenten, Datenmodell und Bereitstellung über Caddy [Architektur](docs/ARCHITEKTUR.md)
- Vorgaben zur Datenverarbeitung und Freigabe [Datenschutz](docs/DATENSCHUTZ.md)
- Vorschau, Parsergrenzen und spätere Anbindung [Mailimport](docs/MAILIMPORT.md)
- Entwicklungsphasen und Prüfkriterien [Roadmap](docs/ROADMAP.md)
- Verbindliche Entwicklungs- und Sicherheitsregeln [AGENTS.md](AGENTS.md)
