# Änderungen

## 0.15.0

- Bei unterstützten persönlichen Genehmigungen und Teilnahmebestätigungen kann BMS eine einzelne Originaladresse ohne Namensbestandteile übernehmen. Die Anrede muss eindeutig zur teilnehmenden Person passen.
- Die Klärung zeigt offene Fälle als Kacheln mit Grund, erkannten Buchungsangaben und direktem Zugang zur Prüfung. Fälle lassen sich nach Grund filtern.
- Bei einem Adresskonflikt zeigt die Klärung die vorhandenen und erkannten Adressangaben entsprechend den Zugriffsrechten. Bei eindeutiger Buchungszuordnung lässt sich die gespeicherte Adresse ausdrücklich beibehalten und der Fall damit abschließen.
- Das Dashboard führt von den Kennzahlen direkt zu den passenden gefilterten Listen. Navigation, Buchungen, Personen und Mail-Eingang sind auf kleinen Bildschirmen kompakter. Beim Wechsel in Details bleiben Auswahl und Filter erhalten.
- Die Live-Aktualisierung läuft nach einer zurückgenommenen Formulareingabe wieder weiter. Auswertungen unterscheiden weiterhin Vorschläge, Klärungsfälle und Simulationsergebnisse.

## 0.14.1

- Der Monatskalender verbindet das vollständige Wochenraster mit beschrifteten Schulblöcken und farbigen Balken für Lehrgänge. Mehrtägige Termine bleiben innerhalb einer Woche zusammenhängend. Pfeile markieren Fortsetzungen über Wochen- oder Monatsgrenzen.

## 0.14.0

- Die Teilnehmerabfrage lässt sich mit drei fiktiven Personen, Einmallinks und einer Kachel für die Rückmeldungen im angemeldeten Bereich testen. Die Simulation versendet keine E-Mails.
- Die Monatsansicht zeigt auch ohne Lehrgänge ein vollständiges Kalenderraster. Die Monats-, Wochen- und Jahresnavigation ist deutlicher gestaltet.
- Der Reiter **Personen** bietet eine durchsuchbare Profilübersicht mit eigenem Ansichtsrecht. Die Startseite zeigt den Stand der passenden Fahrgemeinschaftsvorschläge. BMS hat ein eigenes Tab-Symbol.
- Ein einzelner passender Adressvorschlag ist auch beim direkten Öffnen des Teilnehmerprofils sichtbar. Bei den unterstützten Mailvorlagen kann BMS eine alleinige Originaladresse in der Schreibweise `nachname-vorname` zum vollständigen Teilnehmernamen zuordnen.

## 0.13.0

- Fahrgemeinschaftsvorschläge erscheinen als Kacheln mit Schulungsort, Personenzahl, Hin- und Rückfahrt, Vermittlungsstatus und Detailansicht.
- Vorschläge zeigen bis zum eingestellten Vorlauf **Standby** mit Datum und danach **Bereit zur Vermittlung**. Unter **Fahrgemeinschaften → Einstellungen** lässt sich der Vorlauf ändern. Voreingestellt sind 14 Kalendertage.

## 0.12.0

- Der Import kann gleichnamige Buchungen im selben eindeutigen Lehrgangstermin anhand einer eindeutig erkannten und bereits hinterlegten Teilnehmeradresse unterscheiden.
- Die Buchungsübersicht zeigt Angemeldet, Genehmigt und Storniert als einzelne Statuskacheln. Sie weist auf fehlende Buchungsdaten hin.
- Fahrgemeinschaftsvorschläge berücksichtigen vollständige bestätigte Präsenzbuchungen mit persönlicher E-Mail-Adresse. Offene Klärungsfälle und mögliche Doppelbuchungen sperren die betroffenen Plätze.

## 0.11.0

- BMS schützt den Arbeitsbereich mit persönlichen Benutzerkonten. Administratoren können Konten anlegen, deaktivieren und für jeden Reiter die Rechte Ansehen oder Bearbeiten vergeben.
- Rechte gelten auch für direkte Aufrufe, Formulare und Live-Aktualisierungen. Der Kalender zeigt personenbezogene Buchungsdetails mit dem Recht für Buchungen an.

## 0.10.4

- BMS findet den ursprünglichen Buchungsblock auch über bis zu zwölf zusammenhängende Weiterleitungsstufen aus Klartext, HTML und EML-Anhängen. Geänderte Zwischenbetreffe und umgebrochene Empfängerzeilen bleiben auswertbar.
- Wiederholt eine Weiterleitung dieselbe Adresse als `mailto:`-Zusatz im Originalkopf, erkennt BMS die Adresse. Widersprüchliche oder unabhängige Buchungsblöcke werden als mehrdeutig gemeldet.

## 0.10.3

- Bei weitergeleiteten Lehrgangsmails kann BMS einen im Klartext fehlenden Empfängernamen aus der HTML-Fassung ergänzen, wenn beide Fassungen dieselbe Nachricht und Buchung beschreiben. So lassen sich weitere Teilnehmeradressen eindeutig zuordnen.
- Widersprüchliche Empfängernamen, Adressen oder Buchungsdaten kommen in die Klärung.
- GitHub-Vorabversionen übernehmen die Release-Notes aus dem Changelog der jeweiligen Version.

## 0.10.2

- Im Mail-Eingang lässt sich die Originalnachricht zu einer Buchung mit fehlender Adresse einzeln erneut prüfen. BMS zeigt den aktuellen Adressvorschlag oder den Grund für die fehlende Zuordnung.
- Geschützte Leerzeichen in verschachtelten HTML-Weiterleitungen werden als Teil der Kopfzeilenformatierung erkannt.

## 0.10.1

- BMS zeigt eine einzelne Originaladresse ohne Namensbezug im Mail-Eingang zur Bestätigung. Bei einer passenden Buchung lässt sich die Adresse im Teilnehmerprofil speichern.
- Ein bestehendes Profil kann für weitere Mails mit derselben Originaladresse und demselben Teilnehmernamen wiederverwendet werden.
- Wiederholte Infotexte in den Betriebsansichten sind gekürzt.

## 0.10.0

- BMS aktualisiert die geöffnete Ansicht im Hintergrund. Filter und Seitennummer gelten auch für den Live-Abgleich. Bereiche mit laufenden Eingaben warten mit der Aktualisierung.
- Im Postfach aktualisieren sich der Abrufstatus und der Fortschritt des Adressabgleichs separat von den Einstellungen.
- Bei einer eindeutigen Originaladresse berücksichtigt BMS zusätzliche Vornamen zwischen dem ersten Vornamen und dem Nachnamen im Adressteil vor dem `@`.

## 0.9.5

- Der Adressabgleich verwendet den protokollierten Maileingang, um eine erneut gelesene Mail ihrer bestehenden Buchung zuzuordnen. BMS prüft die erkannten Buchungsdaten vor dem Nachtrag erneut.
- Eine unlesbare oder fehlende Mail erscheint mit ihrem Grund im Mail-Eingang. Der Auftrag setzt den Abgleich mit den übrigen Mails fort.

## 0.9.4

- Bei mehrfach weitergeleiteten Lehrgangsmails verwendet BMS den Betreff, die Buchungsdaten und das An-Feld der ursprünglichen Nachricht. Das gilt für eingebetteten Klartext, HTML und Weiterleitungen als EML-Anhang.
- Bei einer einzelnen Adresse der Form `mail@…` kann BMS den vollständigen Teilnehmernamen in der Domain als Nachweis für die Zuordnung verwenden.

## 0.9.3

- Bei erkannten Genehmigungen und Teilnahmebestätigungen prüft BMS das ursprüngliche An-Feld auch bei abweichendem Betreff.
- BMS gleicht bei Genehmigungen und persönlichen Teilnahmebestätigungen den vollständigen Namen mit einer einzelnen Adresse in Punktschreibweise ab. Umlaute dürfen im Adressteil als `ae`, `oe` oder `ue` geschrieben sein.

## 0.9.2

- Der Import erkennt „Ihr Lehrgangstermin wurde bestätigt“ und „Bestätigung Ihrer Lehrgangsanmeldung“ als Teilnahmebestätigungen und kann den Teilnehmer aus der Anrede ermitteln.
- Bei bytegleicher Neuzustellung einer jetzt erkennbaren Bestätigung wertet BMS den offenen Klärungsfall erneut aus und verwendet die bestehende Buchungszuordnung, wenn sie eindeutig ist.

## 0.9.1

- Die Maildetails zeigen die beim Abruf erkannten Angaben und den aktuellen Adressstand der verknüpften Buchung getrennt.

## 0.9.0

- **Mail-Eingang** zeigt die abgerufenen Nachrichten, erkannte Buchungsdaten, Importergebnisse und Gründe für fehlende Teilnehmeradressen.
- **Klärung** stellt erkannte Angaben und Felder der Buchung gegenüber und verweist auf den zugehörigen Mail-Eingang.
- Die gemeinsame Kopfzeile zeigt die installierte BMS-Version neben dem Hinweis auf den Testbetrieb.
- Der Import erkennt weitergeleitete Genehmigungen mit „Datum“ und wiederholten Empfängeradressen. Eindeutige Folgemails ergänzen die bestehende Buchung.
- Die Adresserkennung prüft Klartext und HTML derselben Weiterleitung auf Widersprüche. E-Mail-Vorschau und Adressabgleich erklären ausgebliebene Zuordnungen.
- Online-Lehrgänge erscheinen in Buchungen und Kalender und bleiben bei der Fahrgemeinschaftssuche außen vor.

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
