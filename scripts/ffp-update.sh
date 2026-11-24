#!/usr/bin/env bash
# Run on the FFP server with privileges for systemd and its journal.
set -euo pipefail

if [[ $# != 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
    echo "Aufruf: sudo bash scripts/ffp-update.sh <vollständiger Commit>" >&2
    exit 2
fi
expected=$1
service=bms-deploy.service

if ! systemctl start "$service"; then
    journalctl -u "$service" -n 30 --no-pager >&2
    exit 1
fi

record=$(journalctl -u "$service" --grep='^BMS erfolgreich installiert: [0-9a-f]{40}$' -n 1 --no-pager -o cat)
installed=${record#BMS erfolgreich installiert: }
if [[ $installed != "$expected" ]]; then
    printf 'Erwartet: %s\nLetzte bestätigte Installation: %s\n' "$expected" "${installed:-unbekannt}" >&2
    journalctl -u "$service" -n 30 --no-pager >&2
    exit 1
fi

printf 'FFP-Installation bestätigt: %s\n' "$installed"
