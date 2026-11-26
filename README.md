# Lehrgangs- und Fahrgemeinschaftsmanagement

LFM verwaltet Lehrgänge und Buchungen für die Feuerwehren eines Landkreises. Die Anwendung enthält eine Buchungsübersicht, einen Kalender mit Tages-, Wochen-, Monats- und Jahresansicht, eine EML-Vorschau und einen optionalen lesenden IMAP-Import. Sie zeigt Fahrgemeinschaftsvorschläge für gemeinsame Hin- und Rückfahrten anhand bestätigter Lehrgänge. Einladung, Teilnahme und Kontaktfreigabe folgen in späteren Entwicklungsphasen.

Der BMS-Arbeitsbereich verwendet persönliche Konten. Administratoren legen unter **Benutzer** weitere Konten an und wählen je Reiter **Kein Zugriff**, **Ansehen** oder **Bearbeiten**. Unter **Mein Konto** kannst du dein Passwort ändern. Die Einrichtung des ersten Administrators und der Wechsel vom gemeinsamen Testzugang sind in der [Architektur-Dokumentation](docs/ARCHITEKTUR.md#persönliche-konten-für-den-testserver) beschrieben.

## Lokaler Start mit Docker Compose

Voraussetzungen sind Docker und Docker Compose v2. Lege beim ersten Start eine lokale Konfiguration aus der Vorlage an:

```bash
test -e .env || cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait db
docker compose -f compose.yaml -f compose.dev.yaml run --no-deps --rm --build web python manage.py migrate
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait web
curl --fail http://127.0.0.1:8000/health/
```

Beim ersten Start legst du ein persönliches Administratorkonto an. Django fragt das Passwort im Terminal ab.

```bash
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py createsuperuser
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

Auf dem zugriffsgeschützten HTTPS-Testhost richtest du unter **Postfach** eine separate IMAP-Adresse ein. Das Passwort wird verschlüsselt gespeichert. Aktiviere den Abruf nur für ein freigegebenes Postfach, das Kopien der Lehrgangskommunikation erhält. **Postfach** zeigt den letzten Abrufstatus. Unter **Übersicht**, **Buchungen** und **Klärung** siehst du die verarbeiteten Ereignisse, Buchungen und offenen Fälle. Bei neuen Klärungsfällen siehst du den Grund und die erkannten Angaben. Ältere Fälle zeigen die zum damaligen Import gespeicherten Daten. SMTP-Versand ist technisch deaktiviert. [Mailimport](docs/MAILIMPORT.md)

Ein aktivierter Postfachdienst verarbeitet standardmäßig bis zu 25 Nachrichten pro Abrufversuch und wartet danach 120 Sekunden bis zum nächsten Versuch. Sichtbare BMS-Ansichten prüfen den Datenbankstand alle 15 Sekunden im Hintergrund. Der Abgleich verwendet die aktuellen Filter und wartet in bearbeiteten Bereichen auf das Ende der Eingabe.

Unter **Mail-Eingang** findest du den letzten Abruf und jede ab diesem Stand erfasste IMAP-Nachricht. Suche nach Teilnehmer, Lehrgang, Referenz oder IMAP-Nummer und öffne **Mail ansehen**. Dort stehen Verarbeitungsergebnis, erkannte Felder, Gründe für eine ausgebliebene Adressübernahme und der Link zur Buchung oder Klärung. Frühere Buchungsereignisse ohne protokollierten Postfachabruf stehen unter **Frühere Importe**.

## Buchungen, Teilnehmer und Klärung

- Eine **Lehrgangsbuchung** ist die Belegung eines Platzes bei einem konkreten Termin. Sie hat einen eigenen Buchungsstatus.
- Ein **Teilnehmerprofil** enthält die Angaben zur Person. Dem Profil können mehrere Buchungen zugeordnet sein.
- Ein **Klärungsfall** ist eine importierte Nachricht, deren Angaben oder Auswirkungen du prüfen musst.

Öffne den Teilnehmernamen in der Buchungsliste, um die Buchungen des Profils zu sehen. Eine mögliche Doppelbuchung liegt vor, wenn Lehrgangsname, Schule, Beginn und Ende übereinstimmen. Eine Überschneidung liegt vor, wenn zwei unterschiedliche Lehrgangstermine gemeinsame Zeit haben. Beide Hinweise dienen der Prüfung und verändern keine Buchungen.

Öffne einen Fall unter **Klärung**, um die erkannten Angaben neben der betroffenen Buchung zu vergleichen. BMS nennt fehlende Felder und, bei einem erkennbaren Konflikt mit einer Buchung, die Felder, die vom aktuellen Stand abweichen. Über **Mail-Eingang ansehen** findest du den Auszug und die Angaben zum Auffinden der Originalnachricht im Importpostfach. **E-Mail zuordnen** verbindet das Ereignis mit der Buchung. Der Buchungsstatus bleibt dabei gleich. **Fall verwerfen** schließt das Ereignis mit dem gewählten Grund ab. Bei einer Ummeldung wechselt **Ummeldung in BMS übernehmen** die Person auf dem ausgewählten Platz und dokumentiert die Änderung in der Buchungshistorie.

## Teilnehmerprofile und E-Mail-Adressen

Bei erkannten Genehmigungen und Teilnahmebestätigungen prüft BMS das ursprüngliche An-Feld. Eine Adresse mit passendem Anzeigenamen oder eindeutigem Bezug zum vollständigen Namen kann der Buchung hinzugefügt werden. Hinterlege die auszuschließenden Adressen unter **Postfach → Funktionsadressen**. Wiederkehrende Teilnehmer mit derselben normalisierten Adresse und übereinstimmendem Namen verwenden dasselbe Profil. [Regeln für die Adresszuordnung](docs/MAILIMPORT.md#teilnehmeradresse-aus-dem-ursprünglichen-an-feld)

Unter **Postfach → Einmaliger Adressabgleich** kannst du den Fortschritt sehen oder unter **Adressabgleich starten** einen Auftrag auslösen. Für das bestehende aktive FFP-Importpostfach wird mit 0.7.1 einmalig der angeforderte Auftrag vorgemerkt. BMS prüft bereits importierte Nachrichten bis zum aktuellen Stand des regulären Abrufs. Fehlende Adressen werden nur bei eindeutiger Zuordnung ergänzt. Bestehende Adressen und Buchungen bleiben erhalten.

Fehlt bei einer Buchung die E-Mail-Adresse, wähle unter **Buchungen** den Teilnehmernamen aus. Prüfe die Originalnachricht, ergänze die Adresse unter **Teilnehmer zur Buchung** und wähle **Adresse speichern**. Bereits hinterlegte Adressen bleiben erhalten. Konflikte aus dem Import bleiben unter **Klärung** offen. Das Ergänzen einer Adresse löst keinen Versand aus. [Regeln für die Zuordnung](docs/MAILIMPORT.md#teilnehmeradresse-aus-dem-ursprünglichen-an-feld)

Unter **Personen** kannst du nach Profilen suchen. Das Ansichtsrecht für Personen gibt den Reiter frei. Buchungsdaten und Links erfordern zusätzlich das Ansichtsrecht für Buchungen.

## Termine, Buchungen und mögliche Fahrten

Die **Übersicht** zeigt anstehende Lehrgänge mit Beginn und Ende pro Termin. Unter **Buchungen** stehen die beiden Zeitpunkte in der Spalte **Zeitraum**. Die Anzeige verwendet die Zeitzone `Europe/Berlin`.

Unter **Buchungen** hat jede Lehrgangsteilnahme eine interne Buchungs-ID. Suche nach `Buchungs-ID 13`, um genau diesen Eintrag zu finden. Ein Klick auf die ID zeigt den Lehrgang, die zugeordneten Meldungen und die Statusänderungen dieses Platzes. Das Teilnehmerprofil öffnest du separat. Wer Buchungen bearbeiten darf, kann einen einzelnen Platz aus den Details löschen. Die Bestätigung nennt die betroffene Buchung und verlangt die Eingabe ihrer ID. Gelöscht werden dieser Platz und sein Verlauf. Eindeutig zugeordnete offene Klärungsfälle werden abgeschlossen. Je Platz zeigen drei Kacheln **Angemeldet**, **Genehmigt** und **Storniert**. **Offen** bedeutet, dass für diesen Schritt ein eindeutiger Nachweis fehlt. Die Liste zeigt auch, welche Angaben zur Vollständigkeit der Buchung fehlen.

**Statistik** zeigt Buchungen, Lehrgänge, Teilnehmende, Heimatfeuerwehren und Fahrgemeinschaftsvorschläge nach Zeitraum und Schulungsort. Lehrgänge werden nach ihrem Beginn ausgewertet und Fahrgemeinschaften nach dem Tag der Hinfahrt. Der Monatsverlauf zeigt das ausgewählte Jahr. Mit den jeweiligen Rechten kommen offene Klärungsfälle nach Eingang und Mail-Eingänge nach Abrufzeit hinzu.

Im **Kalender** kannst du zwischen Tag, Woche, Monat und Jahr wechseln und nach Schule filtern. Die Tagesansicht zeigt die Lehrgänge eines Tags mit Uhrzeiten und Schulgruppen. In der Wochenansicht öffnet ein Klick auf einen Wochentag dessen Tagesansicht. In der Jahresansicht zeigen die Monatskacheln, an wie vielen verschiedenen Tagen mindestens ein Lehrgang zum gewählten Schulfilter stattfindet. Die Kacheln sind bei 0 Tagen neutral, bei 1 Tag blau, bei 2–5 Tagen indigoblau, bei 6–10 Tagen violett, bei 11–20 Tagen dunkelrosa und bei 21–31 Tagen dunkelrot. Die genaue Anzahl steht auf der Kachel. Wähle einen Monat, um den Ersten links in der durchgehenden Tagesreihe zu sehen. Scroll seitlich über Monats- und Jahreswechsel. Unter der Tagesreihe stehen die Lehrgänge nach Schule gruppiert. Farbige Balken zeigen ihre Dauer und bleiben über Wochen- und Monatsgrenzen zusammenhängend. Die Zeitleiste bleibt auch ohne Termine sichtbar. Die genauen Zeiten stehen in der Terminliste und in den Details. Wähle einen Balken, um die zugehörigen Buchungen zu sehen.

Ein **Fahrgemeinschaftsvorschlag** verbindet Hin- und Rückfahrt. BMS sucht unter allen erfassten künftigen Abfahrten nach mindestens zwei unterschiedlichen Teilnehmern mit vollständigen, bestätigten Buchungen vor Ort an derselben Schule. Eine vollständige Buchung hat einen Namen, eine persönliche E-Mail-Adresse, einen Lehrgang, einen Schulungsort und einen Zeitraum. Offene Klärungsfälle und mögliche Doppelbuchungen sperren die betroffenen Plätze. Online-Lehrgänge bleiben in Buchungen und Kalender sichtbar. Der Starttag und der Endtag müssen jeweils bei beiden Buchungen übereinstimmen. BMS verwendet für jede Person Beginn und Ende aus derselben Buchung. Die Kalendertage richten sich nach `Europe/Berlin`. Die Uhrzeiten sind für den Abgleich unerheblich.

Der Hintergrunddienst gleicht die Buchungen standardmäßig alle zwei Minuten ab. Mit **Abgleich starten** kannst du die Prüfung manuell auslösen. Unter **Fahrgemeinschaften** kannst du einen Vorschlag öffnen und seinen Bearbeitungsstand ändern. Wenn die Buchungen nicht mehr zusammenpassen, kennzeichnet BMS den Vorschlag und erhält seinen Bearbeitungsstand. Frühere Fälle für einzelne Hin- oder Rückfahrten bleiben im Archiv. Die kombinierten Vorschläge beginnen mit einem eigenen Bearbeitungsstand. BMS verschickt dabei keine Einladungen und gibt keine Kontaktdaten frei.

Jeder Vorschlag erscheint als Kachel mit Schule, Anzahl der Personen und den Daten für Hin- und Rückfahrt. **Standby** zeigt das Datum, ab dem der Vorschlag **Bereit zur Vermittlung** ist. Wer Fahrgemeinschaften bearbeiten darf, kann den Vorlauf unter **Fahrgemeinschaften → Einstellungen** festlegen. BMS zählt standardmäßig 14 Kalendertage ab dem Tag der Hinfahrt zurück. Der manuell gepflegte Bearbeitungsstand ist separat verfügbar.

Mit **Teilnehmerabfrage testen** startest du eine persönliche Simulation mit drei fiktiven Personen. Sie zeigt Einladung, einmalige Antwort und den Stand der Zusagen in einer eigenen Kachel. Die Simulation verwendet keine Teilnehmerprofile und löst keinen Versand aus.

## Dokumentation

- Fachliche Anforderungen und offene Fragen [Pflichtenheft](docs/PFLICHTENHEFT.md)
- Komponenten, Datenmodell und Bereitstellung über Caddy [Architektur](docs/ARCHITEKTUR.md)
- Vorgaben zur Datenverarbeitung und Freigabe [Datenschutz](docs/DATENSCHUTZ.md)
- Vorschau, Parsergrenzen und Postfachanbindung [Mailimport](docs/MAILIMPORT.md)
- Entwicklungsphasen und Prüfkriterien [Roadmap](docs/ROADMAP.md)
- Verbindliche Entwicklungs- und Sicherheitsregeln [AGENTS.md](AGENTS.md)

## Versionen und Testbereitstellung

Entwicklungsänderungen gehen per Pull Request nach `main`. Der FFP-Testserver verfolgt `feat/bms-foundation`. Für eine Testbereitstellung wird dieser Branch per Fast-Forward auf den geprüften Commit von `main` gesetzt. Der beim Einrichten konfigurierte systemd-Timer plant den nächsten Deployment-Lauf zwei Minuten nach Ende des letzten Laufs ein. Findet ein Lauf einen neuen Stand, holt der Server die Änderungen, baut das Image, migriert die Datenbank und startet Webdienst und Postfachdienst. Build, Migration und Neustart dauern zusätzlich zur Wartezeit.

Für eine sofortige Bereitstellung den eingerichteten Dienst auf dem FFP-Server manuell starten. Anschließend den installierten Commit im Protokoll mit dem erwarteten Commit vergleichen und die BMS-Seite neu öffnen.

```bash
sudo systemctl start bms-deploy.service
sudo journalctl -u bms-deploy.service --grep='BMS erfolgreich installiert:' -n 1 --no-pager
```

Die Meldung muss den erwarteten vollständigen Commit enthalten. Ist der Commit älter oder fehlt die Meldung, zeigt `sudo journalctl -u bms-deploy.service -n 60 --no-pager` den letzten Lauf. Ein erfolgreicher Push oder ein aktiver Timer allein bestätigt die Installation noch nicht.

Ein annotierter Tag `vMAJOR.MINOR.PATCH` bezeichnet einen geprüften Stand. GitHub Actions wiederholt die PostgreSQL-Tests und veröffentlicht bei Erfolg eine Vorabversion mit dem passenden Abschnitt aus dem [Changelog](CHANGELOG.md) des getaggten Commits. Fehlt der Abschnitt oder ist er leer, verwendet GitHub Actions die Notiz des Tags.

BMS zeigt die Version aus `config/version.py` oben rechts an. Aktualisiere `BMS_VERSION` im Release-Commit und verwende dieselbe Nummer für den Changelog-Eintrag und den Git-Tag mit vorangestelltem `v`.
