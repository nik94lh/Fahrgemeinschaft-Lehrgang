# Pflichtenheft

## Ziel und Entwicklungsstand

LFM unterstützt die Lehrgangsverwaltung und die freiwillige Fahrgemeinschaftsvermittlung für die Feuerwehren eines Landkreises. Die Schulstandorte sind Würzburg, Regensburg und Geretsried. Das System wertet Kopien der Lehrgangskommunikation aus. Die Bearbeitung beim externen Lehrgangssystem bleibt Aufgabe der zuständigen Personen.

Der Entwicklungsstand umfasst Django mit PostgreSQL, das Domänenmodell, eine Buchungsübersicht, einen Monatskalender, eine Vorschau möglicher Fahrten, eine lokale EML-Vorschau, einen manuellen Testmail-Import und einen optionalen lesenden IMAP-Import. Für Entwicklung und Tests werden ausschließlich synthetische oder ausreichend anonymisierte Daten verwendet. Ein separates echtes Importpostfach darf ausschließlich nach Freigabe auf dem zugriffsgeschützten Server aktiviert werden.

## Lehrgänge und Buchungen

Ein Lehrgang kann mehrere Termine haben. Zu einem Termin gehören einzelne Plätze. Die Belegung eines Platzes kann sich durch eine Ummeldung ändern. Buchungshistorie und E-Mail-Ereignisse bilden diese Änderungen ab. Der fachliche Status unterscheidet genehmigt, Bestätigung ausstehend, bestätigt, umgemeldet, storniert, abgeschlossen und Klärungsbedarf.

Importregeln ordnen in unterstützten, eindeutigen Fällen eine Genehmigung, Bestätigung oder Erinnerung dem bestehenden Platz zu, sobald der Bezug eindeutig ist. Eine vorhandene eindeutige Buchungsreferenz hat Vorrang. Andernfalls kommen Lehrgangsart, Schule, Zeitraum, Teilnehmername und weitere belastbare Angaben infrage. Bei Mehrdeutigkeit entsteht ein Klärungsfall. Betroffene Buchungen bleiben bis zur Klärung für die Vermittlung gesperrt.

Eine bestätigte Ummeldung ordnet den neuen Teilnehmer dem bestehenden Platz zu und führt die bisherige Belegung in der Historie fort, soweit zulässig. Alte Vermittlungslinks müssen widerrufen und bestehende Vermittlungen überprüft werden. Statusänderungen in LFM sind interne Verwaltungsaktionen.

## Verwaltung und Kalender

Das deutschsprachige Administrationsdashboard soll Gesamtübersicht, Lehrgangskalender, Lehrgänge, Teilnehmer, Feuerwehren, Fahrgemeinschaften, Klärungsfälle, E-Mail-Import, Versandprotokoll, Einstellungen, Systemstatus und Datenschutz bündeln. Übersichten sollen durchsuchbar und filterbar sein. Der Standardzeitraum umfasst die letzten zwei Monate und alle bekannten künftigen Termine.

Kennzahlen umfassen anstehende Lehrgänge, gebuchte Plätze, bestätigte Teilnehmer, ausstehende Bestätigungen, Ummeldungen, Stornierungen, potenzielle Fahrgemeinschaften, versendete Einladungen, erfolgreiche Vermittlungen, offene Klärungsfälle und Importfehler.

Der Kalender zeigt mehrtägige Termine als interaktive Balken in Tages-, Wochen-, Monats- und Jahresansicht. Er gruppiert nach Schule, Lehrgang oder Teilnehmer und erlaubt Filter nach Zeitraum, Schule, Lehrgangsart und Status. Beginn und Ende, getrennte Schulfarben, mögliche gemeinsame Fahrten, Details und manuelle Korrekturen gehören zur späteren Kalenderfunktion. Die Oberfläche soll auf Desktop, Tablet und Smartphone barrierearm bedienbar bleiben.

## Vermittlung und Teilnehmerportal

Ein Fahrgemeinschaftsvorschlag umfasst Hin- und Rückfahrt. Voraussetzung sind mindestens zwei unterschiedliche Teilnehmer mit jeweils einer bestätigten, eindeutigen Buchung am selben Schulstandort. Für jede Person liefert dieselbe Buchung den Starttag für die Hinfahrt und den Endtag für die Rückfahrt. Bei allen Beteiligten müssen beide Kalendertage in `Europe/Berlin` übereinstimmen. Die Lehrgangsart und die Uhrzeiten spielen für den Vergleich keine Rolle. BMS berücksichtigt alle erfassten künftigen Abfahrten.

Für die spätere Einladungs- und Versandsteuerung bleiben als separat konfigurierbare Vorschläge 28 Tage für eine Vorprüfung, 21 Tage für eine erste Einladung, 14 Tage für eine erneute Prüfung und optionale Erinnerung sowie 7 Tage für die letzte optionale Erinnerung vorgesehen.

Die geografische Prüfung nutzt lokal gepflegte Feuerwehrkoordinaten und einen einstellbaren Suchradius. Die Stammdaten enthalten Name, Gemeinde, Ortsteil, Adresse, Koordinaten und Aktivstatus. Die Auswahl einer Heimatfeuerwehr darf mit gesonderter Zustimmung für spätere Lehrgänge gespeichert werden.

Ein zeitlich begrenzter persönlicher Link ersetzt ein Teilnehmerkonto. Das Portal erfasst Interesse, Fahrtrichtung, Fahrer- oder Mitfahrerrolle, verfügbare Sitze und zeitliche Flexibilität. Die Teilnahme ist freiwillig und die Zustimmung widerrufbar. Gruppen mit mehr als zwei Personen erfordern passende Kapazitäten und eine Zustimmung jeder beteiligten Person vor der Freigabe ihrer Kontaktdaten.

## Offene fachliche Fragen

- Welche stabilen Buchungsreferenzen, Terminidentifikatoren, Personenmerkmale und Zeitstempel enthalten die anonymisierten E-Mail-Beispiele?
- Welche Folgen haben Terminänderungen, Stornierungen und Bestätigungen, wenn sie verspätet eintreffen oder widersprüchlich sind?
- Wie werden Platzhalter erkannt und in welchen Sonderfällen darf eine Historie nachträglich korrigiert werden?
- Welche Zeit- und Richtungsregeln gelten bei abweichenden Uhrzeiten, Teilnehmern ohne bekannte Heimatfeuerwehr oder sehr kurzfristigen Buchungen?
- Welche örtlichen Stammdaten stehen zur Verfügung und wer hält sie aktuell?
- Wer darf welche Daten einsehen, Fälle klären, Einladungen freigeben und verbindliche Löschfristen konfigurieren?
- Welche Rechtsgrundlage und Aufbewahrungsfristen gibt der Verantwortliche für den realen Betrieb vor?
