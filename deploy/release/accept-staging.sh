#!/bin/bash
set -euo pipefail
reference=${1:?usage: accept-staging.sh EVIDENCE_REFERENCE 'VERIFIED TLS MAIL OAUTH WEBHOOK'}
[ "${2:-}" = 'VERIFIED TLS MAIL OAUTH WEBHOOK' ] || { echo 'Real external acceptance is required before recording approval' >&2; exit 64; }
case "$(pwd -P)" in /srv/namelist/releases/*) ;; *) echo 'Run inside the deployed release directory' >&2; exit 64 ;; esac
python3 deploy/release/manifest.py env release.json release.env
staged_run=$(python3 -c 'import json; print(json.load(open("release.json"))["ci_run_id"])')
[[ "$staged_run" =~ ^[0-9]+$ ]] || exit 64
exec 9>/srv/namelist/staging/deploy.lock
flock -n 9 || exit 75
cmp release.json /srv/namelist/staging/current.json || { echo 'This is not the currently staged release' >&2; exit 1; }
unset APP_IMAGE SCANNER_IMAGE MAINTENANCE_IMAGE POSTGRES_BACKUP_IMAGE STATIC_IMAGE
compose=(docker compose --project-name namelist-staging --env-file /srv/namelist/staging/runtime.env --env-file release.env -f compose.yaml)
"${compose[@]}" run --rm --no-deps maintenance /app/deploy/backup/backup.sh diff
"${compose[@]}" run --rm --no-deps maintenance /app/deploy/backup/restore-drill.sh
# A receipt is written only after a fresh real backup and isolated restore/decryption have succeeded.
mkdir -p /srv/namelist/staging/accepted
umask 027
python3 deploy/release/manifest.py accept release.json "$reference" "/srv/namelist/staging/accepted/$staged_run.json"
echo 'Exact image digests accepted; production promotion is now eligible for separate approval.'
