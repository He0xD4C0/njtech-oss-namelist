#!/bin/sh
set -eu
if [ "${BACKUP_ENABLED:-1}" = 1 ]; then
    python3 /app/deploy/backup/render-config.py
    chown postgres:postgres /etc/pgbackrest/pgbackrest.conf
    set -- "$@" -c "archive_command=pgbackrest --stanza=${BACKUP_STANZA:-namelist} archive-push %p"
elif [ "${BACKUP_ENABLED:-}" = 0 ]; then
    # Only the explicit local overlay disables WAL archiving.
    set -- "$@" -c archive_mode=off
else
    echo 'BACKUP_ENABLED must be 0 or 1' >&2
    exit 1
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
