#!/bin/sh
set -eu
if [ "${BACKUP_ENABLED:-1}" = 1 ]; then
    # The initial temporary PG server is already available on its local socket.
    # A temporary offsite outage must not destroy/reinitialize an otherwise valid database.
    if ! pgbackrest --stanza="${BACKUP_STANZA:-namelist}" stanza-create; then
        echo 'Initial backup stanza creation failed; WAL is retained for retry, inspect maintenance alerts' >&2
    fi
fi
