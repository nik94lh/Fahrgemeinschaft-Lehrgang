# Teilnehmerabfrage testen

Die Testabfrage verwendet fiktive Buchungen und bereitet Einladungen und Benachrichtigungen als Mailvorschauen vor. Das Teilnahmeportal ist über einen separaten HTTPS-Host erreichbar. Testbuchungen verwenden E-Mail-Adressen mit der Endung `.invalid` und fiktive Telefonnummern wie `000 000 001`.

## Testbetrieb auf dem Server einschalten

Nach der Bereitstellung die Variable `BMS_RIDE_PORTAL_HOST=mitfahren.bms-ab.de` in der BMS-Umgebung ergänzen und Webdienst und Postfach-Worker mit der bestehenden Compose-Konfiguration neu erstellen. BMS erlaubt diesen Host für das Teilnahmeportal. In `BMS_ALLOWED_HOSTS` stehen die Hostnamen der Verwaltung.

Im Caddyfile nur den vorhandenen Site-Block für `mitfahren.bms-ab.de` durch diesen ersetzen:

```caddyfile
mitfahren.bms-ab.de {
    redir / /teilnahme/ 302
    @teilnahme path /teilnahme/ /teilnahme/antwort/ /teilnahme/code.js
    handle @teilnahme {
        reverse_proxy bms-web:8000 {
            header_up -Authorization
        }
    }
    handle {
        respond 404
    }
}
```

Die Caddy-Konfiguration im Container prüfen und erst dann neu laden:

```bash
sudo docker exec proxy-caddy-1 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile &&
sudo docker exec proxy-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

## Rückmeldungen und Widerruf durchspielen

1. Unter **Buchungen → Buchung anlegen** drei fiktive Personen als bestätigte Testbuchungen für denselben zukünftigen Präsenztermin erfassen. Verwende je Person eine eigene Adresse, etwa `person-eins@example.invalid`.
2. Auf der Testkachel unter **Fahrgemeinschaften** die Testabfrage starten. Administratoren können mit **Automatik** im Kopfbereich den Start innerhalb des Vorlaufs einschalten. Den Vorlauf legst du unter **Einstellungen** fest.
3. Die Links aus den Einladungsvorschauen in getrennten Browserprofilen öffnen oder zwischen den Personen im Teilnahmeportal abmelden. Für die Wiederanmeldung lässt sich der persönliche Code mit vier Gruppen zu je vier Zeichen eingeben. Bereits ausgegebene Codes funktionieren bis zum Ablauf weiter.
4. Eine Zusage mit Freigabe der E-Mail-Adresse und eine Zusage mit einem telefonischen Kontaktweg erfassen. Die beiden Personen können die freigegebenen Angaben der jeweils anderen abrufen. Die dritte Person sieht die Kontakte erst nach eigener Zusage und Freigabe.
5. Auf der Fahrgemeinschaftskachel die offenen Antworten, Zusagen, Absagen und den Status der Kontaktfreigabe prüfen. Blau zeigt offene Rückmeldungen an. Bei mindestens zwei gültigen Freigaben ist die Kachel grün und BMS erstellt für beide eine Mailvorschau. Endet die Abfrage ohne aktuelle Freigabe, ist die Kachel rot.
6. Eine Zusage über denselben Zugang zurückziehen. Bei den übrigen Zusagenden erscheint ein Hinweis zum Widerruf. Wenn nur noch eine Zusage vorliegt, blendet das Portal die Kontaktliste aus.

Der Kopf des Teilnahmeportals zeigt die Zahl der möglichen Mitfahrenden und die Zahl der aktuell geteilten Kontakte. Gibt eine weitere eingeladene Person ihre Kontakte frei, erhalten die bereits verbundenen Personen eine eigene Update-Vorschau.

Der Zugang gilt bis 24 Stunden nach dem Ende der jeweiligen Buchung. Das Teilnahmeportal prüft bei jedem Zugriff die Zuordnung zur ursprünglichen Buchung. Bei Stornierung oder Wechsel des Kontakts erlischt der Zugriff.
