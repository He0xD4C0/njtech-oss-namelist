#!/bin/bash
set -euo pipefail
environment=${1:?usage: deploy.sh staging|production}
case "$environment" in staging|production) ;; *) exit 64 ;; esac
root=$(pwd -P)
case "$root" in /srv/namelist/releases/*) ;; *) echo 'Run from an extracted immutable release directory' >&2; exit 64 ;; esac
runtime="/srv/namelist/$environment/runtime.env"
[ -f "$runtime" ] || { echo 'Server runtime.env is missing' >&2; exit 1; }
exec 9>"/srv/namelist/$environment/deploy.lock"
flock -n 9 || { echo 'A release for this environment is running' >&2; exit 75; }
python3 deploy/release/manifest.py env release.json release.env
if [ "$environment" = production ]; then
    source_url=$(sed -n 's/^SOURCE_REPOSITORY_URL=//p' "$runtime")
    [[ "$source_url" =~ ^https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/?$ && "$source_url" != *OWNER* ]] || { echo 'A real public source repository URL is required for AGPL source access' >&2; exit 1; }
    curl --fail --silent --show-error --max-time 30 "$source_url" > /dev/null
    source_sha=$(python3 -c 'import json; print(json.load(open("release.json"))["source_sha"])')
    curl --location --proto '=https' --fail --silent --show-error --max-time 30 "${source_url%/}/tree/$source_sha" > /dev/null
    staged_run=$(python3 -c 'import json; print(json.load(open("release.json"))["ci_run_id"])')
    [[ "$staged_run" =~ ^[0-9]+$ ]] || exit 64
    python3 deploy/release/manifest.py verify-acceptance release.json "/srv/namelist/staging/accepted/$staged_run.json"
fi
# The server env holds config/secrets paths; the second env file selects only the verified release digests.
unset APP_IMAGE SCANNER_IMAGE MAINTENANCE_IMAGE POSTGRES_BACKUP_IMAGE STATIC_IMAGE
compose=(docker compose --project-name "namelist-$environment" --env-file "$runtime" --env-file release.env -f compose.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" config --format json | python3 deploy/release/validate-runtime.py "$environment"
"${compose[@]}" pull
# Drain old consumers before changing schema; never run a second Beat or auto-restore a database.
"${compose[@]}" stop worker scanner beat maintenance
"${compose[@]}" up --no-build --detach --wait --wait-timeout 600
"${compose[@]}" run --rm --no-deps web python manage.py check --deploy --fail-level ERROR
# Real public DNS/TLS and HTTP response, not an internal port-only assertion.
public_url=$(python3 -c 'import os; print(os.environ.get("RELEASE_PUBLIC_URL", ""))')
if [ -z "$public_url" ]; then
    public_url=$(sed -n 's/^SITE_BASE_URL=//p' "$runtime")
fi
[[ "$public_url" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?/?$ ]] || { echo 'Public HTTPS URL is required' >&2; exit 1; }
curl --fail --silent --show-error --max-time 60 "$public_url/" > /dev/null
cp release.json "/srv/namelist/$environment/current.json"
printf 'Environment %s is healthy; this does not attest OAuth/mail/Webhook acceptance.\n' "$environment"
