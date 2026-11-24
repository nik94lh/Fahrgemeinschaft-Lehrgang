# Automatischer Rollout von Codex zum FFP-Testserver

Codex pusht den lokal geprüften Commit als Fast-Forward auf `feat/bms-foundation`. GitHub Actions führt die PostgreSQL-Tests aus und startet danach den vorhandenen Dienst `bms-deploy.service` auf FFP. Der Dienst baut und installiert BMS wie bisher. Die Action meldet den geprüften Commit über den GitHub-Commitstatus `ffp/deployed`. So kann Codex eine Installation bestätigen, auch wenn seine Arbeitsumgebung den FFP-Host nicht direkt erreicht.

## Einmalige Einrichtung

Die GitHub-Umgebung und der FFP-Zugang sind spezifisch für BMS. Die Kiosk-Zugangsdaten gehören zum anderen Server und werden hier nicht verwendet. Für die Einrichtung werden Zugriff auf die Repository-Einstellungen und ein bestehender Administratorzugang zum FFP-Server benötigt.

1. Erzeuge ein eigenes SSH-Schlüsselpaar für BMS. Verwahre den privaten Schlüssel im GitHub-Environment `ffp-bms`. Hinterlege nur den öffentlichen Schlüssel auf FFP.
2. Lege auf FFP das Benutzerkonto `bmsdeploy` mit deaktivierter Passwortanmeldung an. Kopiere `scripts/ffp-deploy-action.sh` aus dem geprüften BMS-Stand als `/usr/local/sbin/bms-deploy-action` mit Eigentümer `root:root` und Modus `0755`. Der Pfad und die Datei dürfen für das Benutzerkonto nicht schreibbar sein.
3. Hinterlege den öffentlichen Schlüssel in der `authorized_keys` dieses Kontos. Setze vor den öffentlichen Schlüssel das folgende Präfix. SSH erlaubt damit diesen festen Befehl und schaltet unter anderem interaktive Shell und Weiterleitungen ab.

```text
restrict,command="/usr/bin/sudo -n /usr/local/sbin/bms-deploy-action"
```

4. Lege mit `sudo visudo -f /etc/sudoers.d/bms-deploy-action` die folgenden Regeln an. Das Konto darf damit genau den root-eigenen Befehl ohne zusätzliche Argumente ausführen. Der Befehl prüft den per SSH übergebenen vollständigen SHA und akzeptiert ihn nur, wenn er dem aktuellen Kopf des BMS-Deployment-Branchs entspricht.

```text
Defaults!/usr/local/sbin/bms-deploy-action env_keep += "SSH_ORIGINAL_COMMAND"
bmsdeploy ALL=(root) NOPASSWD: /usr/local/sbin/bms-deploy-action ""
```

5. Erstelle im BMS-Repository das GitHub-Environment `ffp-bms` und beschränke seine Deployment-Branches auf `feat/bms-foundation`. Den Branch und seine Workflows dürfen nur Personen ändern, denen der Deployment-Zugang anvertraut ist. Hinterlege dort die folgenden Werte. Verifiziere den SSH-Hostschlüssel über die bereits vertrauenswürdige Serververbindung oder die Serverkonsole.

| Art | Name | Inhalt |
| --- | --- | --- |
| Secret | `FFP_SSH_KEY` | Privater BMS-Deployment-Schlüssel |
| Variable | `FFP_SSH_TARGET` | `bmsdeploy@62.83.94.19` |
| Variable | `FFP_SSH_KNOWN_HOSTS` | Verifizierte vollständige `known_hosts`-Zeile für das Ziel |

6. Aktiviere die Action mit der **Repository-Variable** `FFP_AUTO_DEPLOY=1`. Prüfe beim nächsten freigegebenen Push den Job **Install verified BMS revision on FFP** und den Status `ffp/deployed` für genau diesen Commit. Der GitHub-Runner muss den bestehenden SSH-Port des FFP-Servers erreichen können.

Der Timer kann als Fallback weiterlaufen und bereits lokal geprüfte Commits während der GitHub-Tests installieren. Der bestehende FFP-Dienst liest den Branch beim Start selbst. Bei überlappenden Pushes kann er dadurch einen neueren Commit holen. Der GitHub-Job bestätigt ausschließlich seinen eigenen erwarteten Commit. Schließe deshalb die lokalen Prüfungen für jeden Push auf den Deployment-Branch vorher ab.

Bei einem Fehler während der Installation veröffentlicht die Action einen fehlgeschlagenen Commitstatus. Das ausführliche Protokoll bleibt auf dem FFP-Server bei `bms-deploy.service`. Wenn die GitHub-Tests scheitern, überspringt GitHub den Installationsjob. In diesem Fall zeigt der Workflow-Lauf den Fehler.
