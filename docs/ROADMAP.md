# Roadmap

Die Projektgrundstruktur ist vorhanden. Der lokale Parser, der optionale lesende Postfachimport, konservative Buchungsregeln und eine Buchungsübersicht mit Wochen- und Monatskalender decken Teile der Phasen 2 bis 4 ab. Für Phase 6 gibt es einen Vorabvergleich von Zeitfenstern bestätigter Lehrgänge, getrennt nach Hin- und Rückfahrt. Er zeigt mögliche Fahrten in den nächsten 28 Tagen ohne Einladung oder personenbezogene Kontaktfreigabe. Neue Klärungsfälle zeigen den Auslöser der manuellen Prüfung. Der Kalender bietet Zeitbalken, einen Schulfilter, Termindetails mit Buchungen und eine zugängliche Terminliste. Die Tages- und Jahresansicht sowie erweiterte Kalenderfunktionen sind weitere Ziele.

| Phase | Umfang | Nachweis |
| --- | --- | --- |
| 1. Projektgrundstruktur und Datenbank | Django, PostgreSQL, Compose, Domänenmodell und Migrationen | Anwendung startet, Migrationen und Modelltests laufen |
| 2. E-Mail-Parser und synthetische Testdaten | Lokale EML-Vorschau für Klartext, HTML und Weiterleitungen. Lesender IMAP-Import für ein freigegebenes separates Postfach. Importmuster anhand anonymisierter Beispiele ergänzen | Synthetische Beispiele, Tests zu Sicherheitsgrenzen, Duplikaten, Fehlern und Importvorschau |
| 3. Buchungszuordnung und Klärungsfälle | Buchungsreferenzen, Ereignisreihenfolge, Platzhistorie, Gründe für neue Klärungsfälle und manuelle Auflösung bei Mehrdeutigkeit | Genehmigung und Bestätigung teilen einen Platz. Mehrere Teilnehmer und Termine bleiben getrennt. Eindeutige und uneindeutige Ummeldung sowie Stornierung werden geprüft |
| 4. Administrationsdashboard und Kalender | Barrierearme deutschsprachige Verwaltung, Statuskennzahlen und interaktiver Belegungsplan | Filter, Detailansichten, Zeitbalken und Korrektur auf verschiedenen Bildschirmgrößen geprüft |
| 5. Feuerwehr-Stammdaten | Lokale Stammdaten und Koordinaten, sichere Zuordnung der Heimatfeuerwehr | Stammdatenpflege und lokale Distanzberechnung geprüft |
| 6. Fahrgemeinschaftserkennung | Schulstandort, getrennte Fahrtzeiten, Toleranzen und Fristen, Geografie und Kapazität | Hin- und Rückfahrt getrennt getestet, einschließlich verschiedener Schulen, Einzelteilnehmern und kurzfristigen Anmeldungen |
| 7. Teilnehmerportal und Einwilligungen | Sichere Links, freiwillige Rückmeldung, nicht identifizierende Vorschläge und explizite Zustimmungen | Ablauf, Widerruf, gegenseitige Zustimmung und unzulässige Kontaktfreigabe getestet |
| 8. Versandsteuerung | Entwicklung, Simulation, freigegebener Testversand und ausdrücklich freigegebener Produktivbetrieb | Versand technisch standardmäßig deaktiviert. Tests für Empfängerfreigabe, Wiederholschutz und widerrufene oder obsolet gewordene Buchungen |
| 9. Sicherheitstests und Datenschutz | Berechtigungen, Rate-Limiting, Protokollierung, Löschung und Anonymisierung | Sicherheits- und Datenschutzanforderungen einschließlich Backup-Wiederherstellung nachgewiesen |
| 10. Installation und Betriebsdokumentation | Wiederholbares Deployment auf eigenem Linux-Server, Betriebs- und Wiederherstellungsanleitung | Installation und Wiederanlauf mit gesonderter Testumgebung nachvollzogen |

Vor der fachlichen Freigabe der Automatisierung müssen die vollständigen anonymisierten E-Mail-Beispiele geprüft, die Importregeln angepasst und die fachlichen Fragen in [Pflichtenheft](PFLICHTENHEFT.md) entschieden werden.
