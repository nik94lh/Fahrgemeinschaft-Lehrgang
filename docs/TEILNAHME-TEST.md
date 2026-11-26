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
2. Auf der Testkachel unter **Fahrgemeinschaften** die Testabfrage starten. Administratoren steuern mit **Automatik** im Kopfbereich alternativ den Start innerhalb des eingestellten Vorlaufs.
3. Die Links aus den Mailvorschauen in getrennten Browserprofilen öffnen. Die Wiederanmeldung funktioniert auch mit dem kopierten Code auf der Startseite des Teilnahmeportals.
4. Eine Zusage mit Freigabe der E-Mail-Adresse und eine Zusage mit einem telefonischen Kontaktweg erfassen. Die beiden Personen können die freigegebenen Angaben der jeweils anderen abrufen. Die dritte Person sieht die Kontakte erst nach eigener Zusage und Freigabe.
5. Eine Zusage über denselben Zugang zurückziehen. Bei den übrigen Zusagenden erscheint ein Hinweis zum Widerruf. Wenn nur noch eine Zusage vorliegt, blendet das Portal die Kontaktliste aus.

Der Zugang gilt bis 24 Stunden nach dem Ende der jeweiligen Buchung. Das Teilnahmeportal prüft bei jedem Zugriff die Zuordnung zur ursprünglichen Buchung. Bei Stornierung oder Wechsel des Kontakts erlischt der Zugriff.
