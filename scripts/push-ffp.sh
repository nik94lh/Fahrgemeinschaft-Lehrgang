#!/usr/bin/env bash
# Push the selected commit and start the existing deployment over SSH.
set -euo pipefail

deploy_only=false
if [[ ${1:-} == --deploy-only ]]; then
    deploy_only=true
    shift
fi
if (( $# > 1 )); then
    echo "Aufruf: bash scripts/push-ffp.sh [--deploy-only] [Benutzer@Server]" >&2
    exit 2
fi
target=${1:-kioskadmin@62.83.94.19}
if [[ ! $target =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
    echo "Gib das SSH-Ziel als Benutzer@Server an." >&2
    exit 2
fi

directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd -- "$directory/.."
branch=refs/heads/feat/bms-foundation
if [[ $deploy_only == true ]]; then
    remote=$(git ls-remote --exit-code origin "$branch")
    expected=${remote%%$'\t'*}
else
    expected=$(git rev-parse --verify HEAD)
fi

if [[ ! $expected =~ ^[0-9a-f]{40}$ ]]; then
    echo "Der erwartete Commit konnte nicht bestimmt werden." >&2
    exit 1
fi
if [[ $deploy_only == false ]]; then
    git push origin "$expected:$branch"
fi

# Put the script in the remote command so sudo can still use the SSH terminal.
server_script=$(<"$directory/ffp-update.sh")
quoted_script=${server_script//\'/\'\\\'\'}
ssh -t "$target" "sudo bash -c '$quoted_script' bash '$expected'"
