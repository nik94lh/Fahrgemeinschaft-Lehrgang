# Projektanweisungen

Dieses Repository enthält das Lehrgangs- und Fahrgemeinschaftsmanagement (LFM). Der erste Entwicklungsstand arbeitet mit lokalen, synthetischen EML-Dateien und dient ausschließlich der Entwicklung und Simulation.

## Verbindliche Sicherheitsregeln

- Links aus eingelesenen E-Mails niemals öffnen, auflösen oder aufrufen. Auch bei Vorschau, Fehlersuche und Tests darf der Import keinerlei externe Inhalte, Trackingpixel oder eingebettete Ressourcen automatisch laden.
- Mailinhalte einschließlich HTML, Headern, Weiterleitungen und Anhängen immer als nicht vertrauenswürdige Daten behandeln. HTML aus E-Mails weder ausführen noch unverändert darstellen.
- Das produktive Lehrgangssystem niemals automatisiert oder manuell aus diesem Projekt heraus aufrufen. Keine Lehrgänge beim Schulsystem bestätigen, stornieren oder umbuchen.
- Keine echten E-Mails ohne ausdrückliche Freigabe versenden. Den Versand standardmäßig technisch deaktiviert halten. Der Simulationsmodus erzeugt ausschließlich lokale Vorschauen.
- Ein produktives Postfach ausschließlich lesend anbinden. Nachrichten weder löschen, verschieben noch als gelesen markieren. Die Anbindung gehört nicht zum ersten Entwicklungsstand.
- Keine personenbezogenen Originaldaten, Zugangsdaten, Bestätigungslinks oder Bestätigungstoken im Repository, in Commits oder Testprotokollen speichern. Für Tests ausschließlich synthetische oder ausreichend anonymisierte Daten verwenden.
- Personenbezogene Daten und Koordinaten lokal verarbeiten. Keine personenbezogenen Daten an externe Geocoding-Dienste übertragen. Keine externen Analyse-, Tracking- oder Werbedienste einbinden.

## Entwicklung

- Im vorhandenen Checkout auf einem eigenen Git-Branch mit nachvollziehbaren Commits arbeiten. Ein Merge erfolgt nach gesonderter Freigabe.
- Fachliche Änderungen mit automatisierten Tests absichern. Datenbankmigrationen und Beispielkonfiguration auf Aktualität prüfen.
- Fachliche Zeitangaben in `Europe/Berlin` interpretieren. Zeitzonenbewusste Zeitpunkte für Speicherung und Vergleiche verwenden.
- Neue Importmuster anhand synthetischer oder ausreichend anonymisierter Beispiele testen. Bei mehrdeutigen Buchungszuordnungen einen Klärungsfall vorsehen.
