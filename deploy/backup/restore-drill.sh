#!/bin/sh
set -eu
# Can be called directly with Docker's --entrypoint override while the application DB/Redis is down.
python /app/deploy/backup/readiness.py >&2
python /app/deploy/backup/render-config.py
exec timeout --signal=TERM --kill-after=60 "${RESTORE_TIMEOUT_SECONDS:-14400}" \
    python /app/deploy/backup/restore.py "$@"
