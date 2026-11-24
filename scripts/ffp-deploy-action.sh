#!/bin/bash
# Root-owned forced command for the dedicated FFP deployment key.
set -euo pipefail

expected=${SSH_ORIGINAL_COMMAND:-}
if [[ $# != 0 || ! $expected =~ ^[0-9a-f]{40}$ ]]; then
    echo "FFP erwartet genau einen vollständigen Commit." >&2
    exit 2
fi

remote=$(/usr/bin/env -i PATH=/usr/bin:/bin HOME=/root GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
    /usr/bin/git -C / ls-remote --exit-code \
    https://github.com/nik94lh/Fahrgemeinschaft-Lehrgang.git refs/heads/feat/bms-foundation)
if [[ ${remote%%$'\t'*} != "$expected" ]]; then
    echo "Der Deployment-Branch steht inzwischen auf einem anderen Commit." >&2
    exit 1
fi

if ! /usr/bin/systemctl start bms-deploy.service >/dev/null 2>&1; then
    echo "Der FFP-Deployment-Dienst ist fehlgeschlagen. Details stehen im Serverprotokoll." >&2
    exit 1
fi

record=$(/usr/bin/journalctl -u bms-deploy.service --grep='^BMS erfolgreich installiert: [0-9a-f]{40}$' -n 1 --no-pager -o cat)
if [[ $record != "BMS erfolgreich installiert: $expected" ]]; then
    echo "Das FFP-Serverprotokoll bestätigt den erwarteten Commit noch nicht." >&2
    exit 1
fi

printf 'FFP-Installation bestätigt: %s\n' "$expected"
