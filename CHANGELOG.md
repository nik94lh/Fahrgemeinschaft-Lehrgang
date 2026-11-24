# Änderungen

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
