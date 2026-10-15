#!/bin/sh
set -eu
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
