# E-Mail-Import

## Lokale Vorschau

Die Startseite bietet dieselbe Vorschau für eine einzelne synthetische oder ausreichend anonymisierte EML-Datei. Sie zeigt erkannte Felder und Hinweise im Browser an. Der Upload wird nur im Arbeitsspeicher verarbeitet. Eingaben bleiben unverändert und es wird kein Buchungsdatensatz angelegt.

`python manage.py preview_eml DATEI` verarbeitet eine einzelne EML-Datei und gibt eine strukturierte Vorschau in der Konsole aus. Mit `-` liest das Kommando von der Standardeingabe. Der Befehl verarbeitet lokale, synthetische oder ausreichend anonymisierte Dateien bis zu 1 MiB. Er verändert keine Buchungen und speichert keine Originalnachrichten.

Der Parser bevorzugt Klartext. Falls nur HTML vorhanden ist, extrahiert er Text aus der HTML-Struktur. Attribute und Inhalte von Elementen wie `script`, `style` oder `iframe` werden ignoriert. CSS wird dabei nicht ausgewertet. Der Parser führt kein HTML aus und ruft keine URLs, eingebetteten Ressourcen, Trackingpixel oder Anhänge ab. Er unterstützt einzelne als EML angehängte und einfache eingebettete Weiterleitungen. Mehrere als Originalnachrichten angehängte E-Mails werden als mehrdeutig zurückgewiesen.

Die Vorschau liefert die erkennbaren fachlichen Angaben wie Ereignistyp, Lehrgangsart, Zeitraum, Schule, Teilnehmer und Buchungsreferenz, soweit diese in unterstützter Form vorkommen. Zeitangaben werden in `Europe/Berlin` interpretiert. Unvollständige oder mehrdeutige Angaben erfordern Prüfung. Der SHA-256-Fingerabdruck identifiziert bytegleiche Eingaben. Geänderte Zeilenenden, zusätzliche Weiterleitungsheader oder andere Kopien können bei demselben fachlichen Ereignis unterschiedliche Fingerabdrücke ergeben.

Die ersten inhaltlichen Muster beruhen auf synthetischen Genehmigungs- und Bestätigungsbeispielen. Eine lokale Sichtung der bereitgestellten Ausdrucke zeigt wiederkehrende Betreffmuster mit „Anmeldung“ und „bestätigt“ sowie numerische Zeiträume in Feldern für Lehrgangsart, Lehrgangstermin und Schulungsort. PDF-Ausdrucke zeigen den sichtbaren Text, während EML-Beispiele zusätzlich Header, MIME-Aufbau und Weiterleitungen erkennen lassen. Die Originalausdrucke gehören daher weder zu den Testdaten noch ins Repository. Weitere Ereignisse können nach Bedarf ergänzt werden. Aus einem unbekannten oder unvollständigen Eingang darf keine Buchungsentscheidung abgeleitet werden. Vollständige, ausreichend anonymisierte EML-Beispiele sind für die spätere Prüfung von MIME-Struktur, Weiterleitungen und produktiven Vorlagen erforderlich.

## Geplanter Postfachimport und Versand

Ein separates Importpostfach soll Kopien der Lehrgangskommunikation erhalten. Der geplante IMAP-Import verwendet für dieses Postfach einen ausschließlich lesenden Zugriff, beispielsweise durch schreibgeschützte Ordnerauswahl und `BODY.PEEK`. Er darf keine Nachrichten löschen, verschieben oder als gelesen markieren. Importstatus und Fehler werden datensparsam protokolliert. Für wiederholte Läufe werden Byte-Duplikate und fachlich identische Ereignisse getrennt behandelt. Unklare Zuordnungen gelangen in einen Klärungsworkflow.

Der spätere SMTP-Versand nutzt eine unabhängig konfigurierte Absenderidentität mit eigenen Zugangsdaten. IMAP-Postfach und SMTP-Absender können unterschiedliche Anbieter verwenden. Für diesen Stand sind beide Zugänge unkonfiguriert und es werden keine echten Nachrichten versendet. Die geplanten Betriebsarten sind Entwicklung, Simulation mit lokalen Vorschauen, Testversand nur an ausdrücklich freigegebene Adressen und ausdrücklich freigegebener Produktivbetrieb.
