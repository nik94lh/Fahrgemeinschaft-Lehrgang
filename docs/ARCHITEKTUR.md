# Architektur

## Komponenten

Django stellt die Übersicht unter `/`, die Buchungsliste unter `/buchungen/`, die Klärungsfälle unter `/klaerung/`, den Monatskalender unter `/kalender/`, mögliche Fahrten unter `/fahrgemeinschaften/` und die EML-Vorschau unter `/vorschau/` und den Statusendpunkt unter `/health/` bereit. Unter `/postfach/` lässt sich der IMAP-Abruf im HTTPS-Betrieb einrichten. Gunicorn betreibt den Webdienst. PostgreSQL hält die relationalen Daten. Compose startet den Webdienst `web` und die Datenbank `db`. Die Konfiguration kommt aus Umgebungsvariablen. Die Anwendung verwendet Deutsch und `Europe/Berlin`. Außerhalb von Compose nutzt Django ohne gesetzten `DB_HOST` eine lokale SQLite-Datenbank. Die isolierte EML-Vorschau nutzt die Python-Standardbibliothek und benötigt weder Datenbank noch Netzwerk.

Der Dienst `mailbox-worker` ruft bis zu zwei freigegebene IMAP-Eingänge per TLS und lesend ab. Die Eingänge haben eigene Abrufpositionen. Der Dienst verarbeitet außerdem Vormerkungen für Fahrgemeinschaftsmails, sobald SMTP auf dem Server und in der Anwendung aktiviert ist. Der Webprozess stellt die Konfiguration bereit und legt Versandaufträge in der Datenbank ab. Die Datenbank speichert Postfachpasswörter mit Fernet verschlüsselt. Der dafür abgeleitete Schlüssel hängt am serverseitigen `BMS_SECRET_KEY`.

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
| `RideCase` | Gespeicherter Fahrgemeinschaftsvorschlag mit Schulstandort, Hin- und Rückfahrt, Buchungen und Bearbeitungsstand. Frühere Einzelfahrten bleiben als Altfälle erhalten. |
| `Carpool` und `CarpoolMember` | Modell für eine spätere tatsächlich vermittelte Fahrt in einer Richtung und ihre Mitglieder |
| `Consent` | Zweckgebundener Einwilligungsnachweis einschließlich Erteilung, Gültigkeitskontext und Widerruf |

Eine vorhandene externe Platzreferenz wird innerhalb der ausgewerteten Quelle als eindeutig angenommen. Gleichartige Termine dürfen vorkommen. Die spätere Zuordnung muss Mehrdeutigkeit erkennen. Ein Platz kann im Laufe der Zeit verschiedene Teilnehmer haben. Mehrere Ereignisse können denselben Platz betreffen. Die EML-Vorschau analysiert die Datei zunächst ohne Speicherung. Die separate Aktion **Testmail importieren** übernimmt fachliche Felder aus synthetischen Testdateien. Diese Funktion und der IMAP-Import nutzen dieselbe konservative Buchungszuordnung. Die Datenbank erzwingt eindeutige SHA-256-Fingerabdrücke für gespeicherte E-Mail-Ereignisse. Der manuell beauftragte Adressnachtrag liest historische Nachrichten bis zur beim Start festgehaltenen UID-Grenze des regulären Abrufs. Er ergänzt eine fehlende Adresse nur, wenn der Fingerabdruck einem bereits angewendeten Ereignis entspricht und die Nachricht dasselbe Teilnehmerprofil eindeutig belegt. Die Buchungszuordnung, Buchungsstatus und vorhandene Adressen werden durch den Nachtrag nicht verändert. Mehrdeutige Nachrichten erscheinen unter **Klärung**. Dort können Bedienpersonen Fälle zuordnen, begründet verwerfen oder nach Prüfung von Platz und Beteiligten eine Ummeldung erfassen. Die Freigabe von Fahrgemeinschaften folgt in späteren Phasen.

Die Ermittlung kombiniert Hin- und Rückfahrt zu einem Fahrgemeinschaftsvorschlag. Der Schlüssel ist die Kombination aus Schule, Kalendertag des Lehrgangsbeginns und Kalendertag des Lehrgangsendes. Beide Tage stammen für eine Person aus derselben bestätigten Buchung und werden in `Europe/Berlin` berechnet. Die Uhrzeit spielt innerhalb eines Tags für die Zuordnung keine Rolle. BMS berücksichtigt alle erfassten künftigen Abfahrten. Ein Vorschlag erfordert mindestens zwei unterschiedliche Teilnehmerprofile. Buchungen mit offenem Klärungsfall bleiben ausgeschlossen. Ein Hintergrunddienst gleicht die Treffer standardmäßig im Abstand von zwei Minuten mit den gespeicherten Fahrgemeinschaftsfällen ab. Die Verwaltungsoberfläche zeigt Fälle und ihren Bearbeitungsstand. Der Abgleich kann auch manuell gestartet werden. Die aktuelle Eignung und der manuelle Bearbeitungsstand werden getrennt geführt. Entfällt ein Treffer, bleibt der Vorschlag mit seinem Stand und einer Kennzeichnung erhalten. Einzelfälle aus früheren Versionen bleiben im Archiv. Ihr Bearbeitungsstand wird bei der Bildung kombinierter Vorschläge nicht übernommen. Einladungen und Kontaktfreigaben werden dadurch nicht ausgelöst. Heimatfeuerwehren, Kapazitäten und Einwilligung werden erst im späteren Vermittlungsworkflow berücksichtigt.

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

### Persönliche Konten für den Testserver

Lege vor der Umstellung im BMS-Compose-Verzeichnis das erste persönliche Administratorkonto an. Der Befehl fragt Benutzername und Passwort interaktiv ab. Bei einer Neuinstallation führe zuerst die Migrationen aus.

```bash
sudo docker compose -f compose.yaml -f compose.server.yaml exec web python manage.py createsuperuser
```

1. Stelle den geprüften BMS-Stand bereit und behalte während der Prüfung den Caddy-Testzugang bei. Melde dich zusätzlich mit dem persönlichen Konto an und prüfe unter **Benutzer**, ob du ein weiteres Testkonto anlegen kannst.
2. Prüfe mit diesem Testkonto **Kein Zugriff**, **Ansehen** und **Bearbeiten** an einer synthetischen Buchung. Prüfe den direkten Aufruf eines gesperrten Reiters und die Buchungsdetails aus dem Kalender. Melde dich ab und prüfe, dass direkte URLs zur Anmeldeseite führen.
3. Entferne im Caddyfile den `basic_auth`-Block des BMS-Hosts. Prüfe die Caddy-Konfiguration mit `caddy validate` und lade sie mit `caddy reload` wie oben. In einem privaten Browserfenster soll die Domain zur BMS-Anmeldung führen. Prüfe `/health/`, einen direkten Buchungslink und die persönliche Anmeldung.

Bei einer Rückkehr zu einer älteren BMS-Version ohne Kontoschutz aktiviere den Caddy-Testzugang vor der Rückkehr. Die BMS-Superuser verwalten die Konten. Die Reiterrechte normaler Konten werden direkt pro Benutzer gespeichert. **Bearbeiten** umfasst **Ansehen**. Funktionen, die Buchungen verändern, brauchen zusätzlich das Bearbeitungsrecht für Buchungen.

Die Anmeldung begrenzt Formularversuche pro Adresse und Minute in der gemeinsamen Datenbank. Im HTTPS-Betrieb ermittelt BMS die Adresse aus dem letzten von Caddy weitergegebenen Eintrag. Der Webdienst ist nur über den vertrauenswürdigen Proxy erreichbar. BMS speichert für die Begrenzung einen zeitgebundenen Hash und räumt abgelaufene Einträge bei weiteren Anmeldungen auf.
