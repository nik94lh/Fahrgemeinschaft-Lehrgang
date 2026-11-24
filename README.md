# Lehrgangs- und Fahrgemeinschaftsmanagement

LFM verwaltet Lehrgänge und Buchungen für die Feuerwehren eines Landkreises. Die freiwillige Fahrgemeinschaftsvermittlung folgt in späteren Entwicklungsphasen. Der erste Stand enthält die Django-Grundstruktur, das Datenmodell und eine lokale EML-Vorschau für synthetische Beispiele.

## Lokaler Start mit Docker Compose

Voraussetzungen sind Docker und Docker Compose v2. Lege beim ersten Start eine lokale Konfiguration aus der Vorlage an:

```bash
test -e .env || cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up --build -d
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py migrate
curl --fail http://127.0.0.1:8000/health/
```

Der Start lässt sich über den technischen Statusendpunkt prüfen. Eine Verwaltungsoberfläche folgt in einer späteren Phase. [Statusendpunkt](http://127.0.0.1:8000/health/) Der Entwicklungsport ist nur lokal gebunden. PostgreSQL bleibt im Docker-Netz des Projekts.

Führe die Tests und die Prüfung auf fehlende Migrationen aus:

```bash
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py test
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py makemigrations --check --dry-run
```

## Lokale EML-Vorschau

Verwende eine synthetische oder ausreichend anonymisierte Datei. Die Umleitung liest sie vom lokalen Rechner ein:

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
