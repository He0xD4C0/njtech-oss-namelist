#!/bin/sh
set -eu
if [ "${BACKUP_CONFIG_MODE:-server}" = application ]; then
    set -- "$@" -c archive_mode=on -c "archive_command=python3 /app/deploy/backup/pgbackrest_archive.py %p"
    exec /usr/local/bin/docker-entrypoint.sh "$@"
fi
state=0
python3 /app/deploy/backup/readiness.py || state=$?
if [ "$state" = 0 ]; then
    python3 /app/deploy/backup/render-config.py
    chown postgres:postgres /etc/pgbackrest/pgbackrest.conf
    set -- "$@" -c "archive_command=pgbackrest --stanza=${BACKUP_STANZA:-namelist} archive-push %p"
elif [ "$state" = 2 ]; then
    set -- "$@" -c archive_mode=off
else
    exit "$state"
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
