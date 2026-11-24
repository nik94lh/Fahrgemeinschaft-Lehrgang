# Datenschutz und Sicherheit

## Erster Testbetrieb

In Entwicklung und Serverdemo dürfen nur synthetische oder ausreichend anonymisierte Daten verwendet werden. Keine echten Postfächer, Originalnachrichten, Bestätigungslinks oder Originaldaten in die Testumgebung übernehmen. Zugangsdaten gehören in die lokale bzw. serverseitige Konfiguration außerhalb der Versionsverwaltung.

Die Vorschau verarbeitet eine Datei lokal. Sie ist für eine autorisierte Bedienperson gedacht und kann die fachlich erkannten Teilnehmerangaben anzeigen. Ausgabe und Fehlermeldungen mit personenbezogenen Daten gehören weder in Tickets noch in gemeinsame Protokolle. Es gibt in diesem Stand keinen Zugang für Teilnehmer und keinen echten Mailversand.

## Fachliche Anforderungen für den späteren Betrieb

Der Verantwortliche muss vor der ersten Kontaktaufnahme eine geeignete Rechtsgrundlage prüfen und freigeben. Die erste Einladung enthält keine Informationen über andere Teilnehmer. Nach freiwilliger Teilnahme darf ein Vorschlag nur nicht identifizierende Merkmale enthalten. Bei kleinen Wehren können bereits Ort, Abfahrtszeit und Lehrgang zusammen auf eine bestimmte Person hinweisen. Solche Merkmale müssen gegebenenfalls vergröbert oder weggelassen werden.

Die Kontaktdatenfreigabe setzt ausdrückliche gegenseitige Zustimmung zur konkreten Fahrt voraus. Bei Gruppen sind die Zustimmungen entsprechend den Personen und Datenempfängern zu prüfen. Ein Widerruf stoppt künftige Freigaben und ist mit Zeitpunkt und Zweck nachvollziehbar zu dokumentieren. Die dauerhafte Speicherung der Heimatfeuerwehr erfordert eine gesonderte Zustimmung.

## Technische Schutzmaßnahmen

- Rollen und Berechtigungen für Administration, Importprüfung, Vermittlung, Versand und Datenschutz verbindlich festlegen. Zugriffe auf personenbezogene Daten auf die notwendige Rolle begrenzen.
- Persönliche Links mit zufälligen, ausreichend langen Token ausgeben. Serverseitig nur Hashes speichern, eine Gültigkeitsdauer erzwingen und Widerruf unterstützen. Token auch aus URLs in Protokollen und Referer-Informationen fernhalten.
- Bei Webaktionen CSRF-Schutz und serverseitige Berechtigungsprüfungen erzwingen. Datenbankzugriffe parametrisieren, E-Mail-HTML in Text umwandeln und Formulareingaben sicher ausgeben. Anmeldung und Teilnehmerlinks gegen zu viele Anfragen absichern.
- E-Mails, Protokolle und Fehlerdaten auf notwendige Angaben beschränken. Bei späterem IMAP-Import nur den dafür eingerichteten Ordner lesend öffnen und Nachrichten abrufen, ohne den Lesestatus zu ändern.
- Feuerwehrkoordinaten lokal auswerten. Keine Teilnehmerdaten an Geocoding-, Tracking-, Analyse- oder Werbedienste senden.
- Konfigurierbare Löschung und Anonymisierung nach Datenkategorie vorsehen. Zwei Monate im Dashboard sind ein Anzeigezeitraum. Die Löschfristen sind gesondert festzulegen.
- Für den Betrieb verschlüsselte, zugriffsbeschränkte Backups, getrennte Schlüsselverwaltung, Löschfristen und erprobte Wiederherstellung festlegen. Die Erwartungen an Wiederherstellungszeit und tolerierbaren Datenverlust muss der Betreiber definieren.
