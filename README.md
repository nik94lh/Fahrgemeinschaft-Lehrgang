# Lehrgangs- und Fahrgemeinschaftsmanagement

LFM verwaltet Lehrgänge und Buchungen für die Feuerwehren eines Landkreises. Die Anwendung enthält eine Buchungsübersicht, einen Kalender mit Wochen-, Monats- und Jahresansicht, eine EML-Vorschau und einen optionalen lesenden IMAP-Import. Sie zeigt mögliche Hin- und Rückfahrten anhand bestätigter Lehrgänge. Einladung, Teilnahme und Kontaktfreigabe folgen in späteren Entwicklungsphasen.

## Lokaler Start mit Docker Compose

Voraussetzungen sind Docker und Docker Compose v2. Lege beim ersten Start eine lokale Konfiguration aus der Vorlage an:

```bash
test -e .env || cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait db
docker compose -f compose.yaml -f compose.dev.yaml run --no-deps --rm --build web python manage.py migrate
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait web
curl --fail http://127.0.0.1:8000/health/
```

Die Weboberfläche erreichst du unter http://127.0.0.1:8000/. Über `/health/` kannst du den Start technisch prüfen. Der Entwicklungsport ist nur lokal gebunden. PostgreSQL bleibt im Docker-Netz des Projekts.

Führe die Tests und die Prüfung auf fehlende Migrationen aus:

```bash
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py test
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py makemigrations --check --dry-run
```

## Lokale EML-Vorschau

Öffne die EML-Vorschau in der Weboberfläche, wähle eine synthetische oder ausreichend anonymisierte `.eml`-Datei bis 1 MiB und klicke auf **Prüfen**. BMS zeigt erkannte Felder und Hinweise an, ohne die Datei oder Buchungsdaten zu speichern. Wenn du die synthetische Testmail tatsächlich übernehmen möchtest, wähle **Testmail importieren**. BMS speichert dann das Ereignis und verarbeitet eindeutige Buchungen. Prüfe das Ergebnis unter **Buchungen** und **Klärung**. Für einen schnellen Einstieg kannst du unter **E-Mail-Vorschau** eine synthetische Testmail herunterladen. Alternativ liest dieser Befehl die Datei vom lokalen Rechner ein:

```bash
docker compose -f compose.yaml -f compose.dev.yaml exec -T web python manage.py preview_eml - < beispiel.eml
```

In einer Python-Umgebung mit den Projektabhängigkeiten funktioniert auch `python manage.py preview_eml beispiel.eml`. Die Vorschau verarbeitet die übergebenen Bytes lokal. Der automatische IMAP-Abruf bleibt standardmäßig deaktiviert. Die lokale Vorschau braucht keinen Mailzugang.

## Automatischer Postfachimport

Auf dem zugriffsgeschützten HTTPS-Testhost richtest du unter **Postfach** eine separate IMAP-Adresse ein. Das Passwort wird verschlüsselt gespeichert. Aktiviere den Abruf nur für ein freigegebenes Postfach, das Kopien der Lehrgangskommunikation erhält. **Postfach** zeigt den letzten Abrufstatus. Unter **Übersicht**, **Buchungen** und **Klärung** siehst du die verarbeiteten Ereignisse, Buchungen und offenen Fälle. Bei neu importierten Klärungsfällen siehst du, weshalb BMS die Nachricht nicht automatisch zuordnen oder anwenden konnte. Für ältere Fälle wurde dieser Grund noch nicht erfasst. SMTP-Versand ist technisch deaktiviert. [Mailimport](docs/MAILIMPORT.md)

Ein aktivierter Postfachdienst verarbeitet standardmäßig bis zu 25 Nachrichten pro Abrufversuch und wartet danach 120 Sekunden bis zum nächsten Versuch. Die Weboberfläche zeigt den aktuellen Datenbankstand beim Öffnen oder Neuladen der Seite.

## Termine, Buchungen und mögliche Fahrten

Die **Übersicht** zeigt anstehende Lehrgänge mit Beginn und Ende pro Termin. Unter **Buchungen** stehen die beiden Zeitpunkte in der Spalte **Zeitraum**. Die Anzeige verwendet die Zeitzone `Europe/Berlin`.

Im **Kalender** kannst du zwischen Woche, Monat und Jahr wechseln und nach Schule filtern. In der Jahresansicht markieren die Balken die Monate, in denen ein Lehrgang stattfindet. Die genauen Zeiten stehen in der zugänglichen Terminliste und in den Details. Wähle einen Termin, um die zugehörigen Buchungen zu sehen.

Unter **Fahrgemeinschaften** erscheinen mögliche Hin- oder Rückfahrten in den nächsten 28 Tagen, wenn mindestens zwei bestätigte Teilnehmer zur selben Schule fahren und ihre Lehrgangszeiten am selben Tag höchstens 60 Minuten auseinanderliegen. Diese Übersicht zeigt Zeitraum und Anzahl der Personen. Sie sendet keine Einladungen und gibt keine Kontaktdaten frei.

## Dokumentation

- Fachliche Anforderungen und offene Fragen [Pflichtenheft](docs/PFLICHTENHEFT.md)
- Komponenten, Datenmodell und Bereitstellung über Caddy [Architektur](docs/ARCHITEKTUR.md)
- Vorgaben zur Datenverarbeitung und Freigabe [Datenschutz](docs/DATENSCHUTZ.md)
- Vorschau, Parsergrenzen und Postfachanbindung [Mailimport](docs/MAILIMPORT.md)
- Entwicklungsphasen und Prüfkriterien [Roadmap](docs/ROADMAP.md)
- Verbindliche Entwicklungs- und Sicherheitsregeln [AGENTS.md](AGENTS.md)

## Versionen und Testbereitstellung

Der FFP-Testserver verfolgt `feat/bms-foundation`. Änderungen werden lokal geprüft und erst danach als Fast-Forward auf diesen Branch gepusht. Der beim Einrichten konfigurierte systemd-Timer plant den nächsten Deployment-Lauf zwei Minuten nach Ende des letzten Laufs ein. Findet ein Lauf einen neuen Stand, holt der Server die Änderungen, baut das Image, migriert die Datenbank und startet Webdienst und Postfachdienst. Build, Migration und Neustart dauern zusätzlich zur Wartezeit.

Ein annotierter Tag `vMAJOR.MINOR.PATCH` bezeichnet einen geprüften Stand. GitHub Actions wiederholt die PostgreSQL-Tests und veröffentlicht bei Erfolg die Notizen aus dem Tag als Vorabversion. Die Änderungen stehen im [Changelog](CHANGELOG.md).
