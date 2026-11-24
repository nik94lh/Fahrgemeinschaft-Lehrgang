# Änderungen

## 0.9.0

- **Mail-Eingang** zeigt die abgerufenen Nachrichten, erkannte Buchungsdaten, Importergebnisse und Gründe für fehlende Teilnehmeradressen.
- **Klärung** stellt erkannte Angaben und Felder der Buchung gegenüber und verweist auf den zugehörigen Mail-Eingang.
- Die gemeinsame Kopfzeile zeigt die installierte BMS-Version neben dem Hinweis auf den Testbetrieb.

## 0.8.0

- BMS bildet einen gemeinsamen **Fahrgemeinschaftsvorschlag** für Hin- und Rückfahrt. Mindestens zwei verschiedene Teilnehmer müssen bestätigte, eindeutige Buchungen an derselben Schule mit übereinstimmendem Start- und Endtag haben. Maßgeblich sind die Kalendertage in `Europe/Berlin`, unabhängig von der Uhrzeit.
- Die Monatskacheln der Kalender-Jahresansicht zeigen die genaue Anzahl belegter Tage und färben sich je nach Anzahl und Schulfilter.
- Frühere Fälle für einzelne Fahrtrichtungen bleiben im Archiv. Ein kombinierter Vorschlag erhält einen eigenen Bearbeitungsstand.
- Die Planung umfasst alle künftigen Abfahrten zu erfassten Lehrgängen. Der Versand und die Kontaktfreigabe bleiben deaktiviert.

## 0.7.2

- BMS ermittelt mögliche Fahrgemeinschaften für alle bereits erfassten künftigen Hin- und Rückfahrten. Passende Fälle für Lehrgänge im nächsten Jahr können dadurch schon jetzt zur Planung erscheinen.

## 0.7.1

- Mögliche Rückfahrten werden wie Hinfahrten nach Schule und Kalendertag ermittelt. Hin- und Rückrichtung bleiben getrennt.
- Im **Postfach** lässt sich einmalig ein Adressnachtrag für bereits verarbeitete Nachrichten starten. Der lesende Abgleich ergänzt ausschließlich fehlende Teilnehmeradressen bei eindeutiger Zuordnung und zeigt den Fortschritt an.

## 0.7.0

- Mögliche Hinfahrten werden bei bestätigten Lehrgängen an derselben Schule und mit Beginn am selben Kalendertag auch bei unterschiedlichen Startzeiten angezeigt. Für Rückfahrten dürfen die Endzeiten am selben Tag höchstens 60 Minuten auseinanderliegen.
- BMS speichert passende Fahrgemeinschaften als Fälle. Unter **Fahrgemeinschaften** lassen sich offene Fälle prüfen und abschließen. Der Hintergrunddienst gleicht die Fälle standardmäßig im Abstand von zwei Minuten ab. Der Abgleich kann auch manuell gestartet werden.
- Bestätigungsmails können eine Teilnehmeradresse liefern, wenn der Anzeigename im ursprünglichen An-Feld eindeutig mit dem Teilnehmer übereinstimmt. Frühere Importe werden dadurch nicht erneut verarbeitet. Der Versand bleibt deaktiviert.

## 0.6.0

- Die Jahresansicht verlinkt jeden Monat direkt mit der Monatsansicht. Im Monatsplan stehen Wochentage neben den Datumsangaben. Die Navigation benennt die verfügbaren Ansichten und Zeiträume.
- Klärungsfälle zeigen die beim Import erkannten Angaben und erklären die Folgen der jeweiligen Entscheidung. Bei älteren Fällen ist die Anzeige auf die damals gespeicherten Angaben beschränkt.
- Die Buchungen eines Teilnehmerprofils unterscheiden Hinweise auf mögliche Doppelbuchungen von zeitlichen Überschneidungen. Die Hinweise lösen keine automatische Zusammenführung aus.

## 0.5.0

- Der Import kann die Teilnehmeradresse aus dem ursprünglichen An-Feld einer Lehrgangsmail übernehmen. CC und hinterlegte Funktionsadressen bleiben dabei unberücksichtigt.
- Weitere Buchungen mit derselben normalisierten E-Mail-Adresse und übereinstimmendem Namen verwenden dasselbe Teilnehmerprofil. Konflikte erscheinen unter **Klärung**.
- Unter **Buchungen** lässt sich die Teilnehmeradresse nach Prüfung manuell ergänzen. Frühere Importe werden nicht pauschal erneut verarbeitet. Der Versand bleibt deaktiviert.

## 0.4.0

- Der Kalender bietet eine Jahresansicht mit Lehrgangsterminen nach Monaten.
- Die Übersicht zeigt anstehende Lehrgänge mit Beginn und Ende.
- Unter **Buchungen** zeigt die Spalte **Zeitraum** den Beginn und das Ende des Lehrgangs.

## 0.3.0

- Neue Klärungsfälle zeigen den konkreten Grund, etwa fehlende Buchungsdaten, eine mehrdeutige Zuordnung oder einen Widerspruch zu einer bestehenden Buchung. Bei älteren Fällen bleibt der Grund als nicht erfasst gekennzeichnet.
- Der Kalender zeigt Lehrgangszeiträume nach Schule in einer Wochen- und Monatsansicht. Ein Klick auf einen Termin öffnet die Details mit Beginn, Ende und zugehörigen Buchungen.
- Der Schulfilter gilt für die gewählte Ansicht. Dieselben Termine sind zusätzlich als zugängliche Liste verfügbar.

## 0.2.0

- Geschützte BMS-Übersicht mit filterbaren Buchungen, Lehrgangskalender und Klärungsliste.
- Synthetische Beispiel-EML mit Vorschau und bewusst ausgelöstem Testimport.
- Lokale Feuerwehr-Stammdaten mit optionalen Koordinaten.
- Zeitlich passende Hin- und Rückfahrten als anonymisierte Hinweise.
- Lesender IMAP-Import mit TLS, Wiederanlauf und Duplikaterkennung. Zugang kann im Browser hinterlegt und verschlüsselt gespeichert werden.
- Genehmigungen und Bestätigungen können dieselbe eindeutige Buchung aktualisieren. Mehrdeutige Ereignisse bleiben zur Klärung offen.
- GitHub-Prüfungen mit PostgreSQL und GitHub-Vorabversionen für geprüfte Versions-Tags.

Der Versand bleibt deaktiviert. Teilnehmerportal, Fahrgemeinschaftsvermittlung und Freigabe von Kontaktdaten folgen in späteren Versionen.

## 0.1.0

- Django- und PostgreSQL-Grundstruktur mit Docker Compose und Caddy-Anbindung.
- Isolierte Vorschau für synthetische EML-Dateien in Browser und Konsole.
