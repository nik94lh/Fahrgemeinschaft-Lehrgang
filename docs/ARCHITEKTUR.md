# Architektur

## Komponenten

Django stellt die Übersicht unter `/`, die Buchungsliste unter `/buchungen/`, die Klärungsfälle unter `/klaerung/`, den Monatskalender unter `/kalender/`, mögliche Fahrten unter `/fahrgemeinschaften/` und die EML-Vorschau unter `/vorschau/` und den Statusendpunkt unter `/health/` bereit. Unter `/postfach/` lässt sich der IMAP-Abruf im HTTPS-Betrieb einrichten. Gunicorn betreibt den Webdienst. PostgreSQL hält die relationalen Daten. Compose startet den Webdienst `web` und die Datenbank `db`. Die Konfiguration kommt aus Umgebungsvariablen. Die Anwendung verwendet Deutsch und `Europe/Berlin`. Außerhalb von Compose nutzt Django ohne gesetzten `DB_HOST` eine lokale SQLite-Datenbank. Die isolierte EML-Vorschau nutzt die Python-Standardbibliothek und benötigt weder Datenbank noch Netzwerk.

Ein separater Prozess kann ein ausdrücklich freigegebenes IMAP-Postfach wiederkehrend per TLS und lesend abrufen. Compose startet für den Abruf den separaten Dienst `mailbox-worker`. Der Webprozess stellt die Konfiguration bereit und baut selbst keine IMAP-Verbindung auf. Die Datenbank speichert Postfachpasswörter mit Fernet verschlüsselt. Der dafür abgeleitete Schlüssel hängt am serverseitigen `BMS_SECRET_KEY`. Die Lehrgangsverwaltung läuft unabhängig vom späteren Vermittlungs- und Versandworkflow. SMTP, Celery und Redis sind weiterhin für spätere Phasen vorgesehen.

## Domänenmodell

| Entität | Zweck |
| --- | --- |
| `Course` | Lehrgangsart |
| `CourseSession` | Konkreter Termin mit Schulstandort und Beginn und Ende als zeitzonenbewusste Zeitpunkte |
| `CourseSeat` | Platz am Termin mit aktuellem Status, aktuellem Teilnehmer und optionaler eindeutiger Buchungsreferenz |
| `Participant` | Teilnehmer als eigenständige Entität, die an mehreren Terminen vorkommen kann |
| `FireBrigade` | Feuerwehr-Stammdaten für spätere Zuordnung und lokale Distanzberechnung |
| `EmailEvent` | Erkanntes Mailereignis einschließlich SHA-256-Fingerabdruck und optionalem Bezug zum Platz |
| `BookingHistory` | Änderung an einem Platz mit vorherigem und neuem Teilnehmer und Status sowie optionalem Bezug zum Ereignis |
| `Carpool` und `CarpoolMember` | Fahrgemeinschaft für eine einzelne Fahrtrichtung und ihre Mitglieder |
| `Consent` | Zweckgebundener Einwilligungsnachweis einschließlich Erteilung, Gültigkeitskontext und Widerruf |

Eine vorhandene externe Platzreferenz wird innerhalb der ausgewerteten Quelle als eindeutig angenommen. Gleichartige Termine dürfen vorkommen. Die spätere Zuordnung muss Mehrdeutigkeit erkennen. Ein Platz kann im Laufe der Zeit verschiedene Teilnehmer haben. Mehrere Ereignisse können denselben Platz betreffen. Die EML-Vorschau analysiert die Datei zunächst ohne Speicherung. Die separate Aktion **Testmail importieren** übernimmt fachliche Felder aus synthetischen Testdateien. Diese Funktion und der IMAP-Import nutzen dieselbe konservative Buchungszuordnung. Die Datenbank erzwingt eindeutige SHA-256-Fingerabdrücke für gespeicherte E-Mail-Ereignisse. Mehrdeutige Nachrichten erscheinen unter **Klärung**. Dort können Bedienpersonen Fälle zuordnen, begründet verwerfen oder nach Prüfung von Platz und Beteiligten eine Ummeldung erfassen. Die Freigabe von Fahrgemeinschaften folgt in späteren Phasen. Die Vorschau möglicher Fahrten arbeitet getrennt nach Richtung, Schule und Kalendertag. Sie vergleicht bestätigte Lehrgänge innerhalb eines rollierenden Fensters von 28 Tagen und höchstens 60 Minuten Abstand zwischen den jeweiligen Beginn- oder Endzeiten. Die Vorschau zeigt keine Namen oder Kontaktdaten. Heimatfeuerwehren, Kapazitäten und Einwilligung werden erst im späteren Vermittlungsworkflow berücksichtigt.

## Testbereitstellung auf dem Planspiel-Server

Der vorgesehene Host ist `bms.planspielplatte.de`. Der DNS-Eintrag zeigt auf `62.83.94.19`. Caddy läuft im Container `proxy-caddy-1` und ist auf dem Server mit dem Docker-Netz `fpp-web` verbunden. Die folgenden Schritte sind für einen getrennten und zugriffsgeschützten Testbetrieb mit synthetischen Daten vorgesehen.

1. Lege auf dem Server eine eigene Konfiguration auf Basis von `.env.example` an. Erzeuge einen eigenen zufälligen `BMS_SECRET_KEY` mit mindestens 50 Zeichen und ein eigenes Datenbankpasswort. Bewahre den Schlüssel geschützt und getrennt von Datenbanksicherungen auf. Nach einem Schlüsselwechsel muss das Postfachpasswort erneut eingetragen werden. Setze `BMS_ALLOWED_HOSTS=bms.planspielplatte.de`, `BMS_CSRF_TRUSTED_ORIGINS=https://bms.planspielplatte.de`, `BMS_HTTPS=1` und `BMS_DEBUG=0`.
2. Starte die Datenbank, führe die Migrationen aus und starte die App mit dem Server-Overlay. Der Webdienst ist im bestehenden externen Netz `fpp-web` unter `bms-web:8000` erreichbar. Compose veröffentlicht keinen BMS-Port am Server.
3. Richte den Zugriffsschutz für den Testhost ein. Verwende eigene Zugangsdaten und verwalte den Kennworthash außerhalb der Versionsverwaltung. Ergänze den Hostblock in `/srv/fpp/proxy/conf/Caddyfile`, sobald die App läuft. Prüfe die gesamte Caddy-Konfiguration und lade sie bei erfolgreicher Prüfung neu.
4. Prüfe den Statusendpunkt `/health/` per HTTPS und öffne die Weboberfläche mit den eingerichteten Zugangsdaten. Halte den IMAP-Abruf bis zur ausdrücklichen Freigabe deaktiviert. Für Prüfung aus Codex benötigt die Umgebung eine Netzwerkfreigabe für den Hostnamen.

```bash
docker compose -f compose.yaml -f compose.server.yaml up -d --wait db
docker compose -f compose.yaml -f compose.server.yaml run --no-deps --rm --build web python manage.py migrate
docker compose -f compose.yaml -f compose.server.yaml up -d
```

```caddyfile
bms.planspielplatte.de {
    basic_auth {
        testadmin <CADDY_KENNWORTHASH>
    }
    reverse_proxy bms-web:8000
}
```

```bash
sudo docker exec proxy-caddy-1 caddy validate --config /etc/caddy/Caddyfile &&
sudo docker exec proxy-caddy-1 caddy reload --config /etc/caddy/Caddyfile

curl --fail --user testadmin https://bms.planspielplatte.de/health/
```

Ersetze `<CADDY_KENNWORTHASH>` durch einen Hash aus `sudo docker exec -it proxy-caddy-1 caddy hash-password`. Caddy fragt das Passwort interaktiv ab. Caddy beschafft das Zertifikat, wenn DNS und öffentliche Erreichbarkeit für die Zertifikatsprüfung stimmen. Bis ein Bereitstellungszugang für dieses Projekt eingerichtet ist, führt der Serverbetreiber die Befehle aus. Die konkreten Deployment-Rechte sowie Sicherung und Wiederherstellung sind vor einem Produktivbetrieb festzulegen. Binde an `fpp-web` ausschließlich vertrauenswürdige Dienste. Django vertraut im HTTPS-Betrieb der vom Proxy übermittelten Protokollinformation.
