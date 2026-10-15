#!/bin/sh
set -eu
state=0
python3 /app/deploy/backup/readiness.py || state=$?
if [ "$state" = 0 ]; then
    # The initial temporary PG server is already available on its local socket.
    # A temporary offsite outage must not destroy/reinitialize an otherwise valid database.
    if ! pgbackrest --stanza="${BACKUP_STANZA:-namelist}" stanza-create; then
        echo 'Initial backup stanza creation failed; WAL is retained for retry, inspect maintenance alerts' >&2
    fi
elif [ "$state" != 2 ]; then
    exit "$state"
fi
