#!/bin/sh
set -eu
case "${1:-}" in full|diff) backup_type=$1 ;; *) echo 'usage: backup.sh full|diff' >&2; exit 64 ;; esac
if [ "${BACKUP_CONFIG_MODE:-server}" = application ]; then
    exec python /app/deploy/backup/managed_backup.py "$backup_type"
fi
python /app/deploy/backup/readiness.py
python /app/deploy/backup/render-config.py
mkdir -p /var/spool/pgbackrest/lock
exec 9>/var/spool/pgbackrest/lock/namelist-backup.lock
flock -n 9 || { echo 'Another backup is already running' >&2; exit 75; }
pgbackrest --stanza="${BACKUP_STANZA:-namelist}" stanza-create
pgbackrest --stanza="${BACKUP_STANZA:-namelist}" check
if [ "$backup_type" = diff ]; then
    # First use and a missed weekly full must not create an indefinitely growing diff chain.
    backup_type=$(pgbackrest --stanza="${BACKUP_STANZA:-namelist}" --output=json info | python -c '
import json, sys, time
records = json.load(sys.stdin)
fulls = [b["timestamp"]["stop"] for s in records for b in s.get("backup", []) if b.get("type") == "full"]
print("full" if not fulls or time.time() - max(fulls) >= 7 * 86400 else "diff")')
fi
exec pgbackrest --stanza="${BACKUP_STANZA:-namelist}" --type="$backup_type" backup
