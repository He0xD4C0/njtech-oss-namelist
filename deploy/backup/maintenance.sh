#!/bin/sh
set -eu
state=0
python /app/deploy/backup/readiness.py || state=$?
if [ "$state" = 0 ]; then
    python /app/deploy/backup/render-config.py
    python /app/deploy/backup/scheduler.py &
    scheduler=$!
elif [ "$state" = 2 ]; then
    scheduler=''
else
    exit "$state"
fi
celery -A config worker --loglevel=INFO --queues=maintenance --concurrency=1 --max-tasks-per-child=10 --soft-time-limit=18030 --time-limit=18300 &
worker=$!
cleanup() {
    kill -TERM "$worker" ${scheduler:+"$scheduler"} 2>/dev/null || true
    wait "$worker" ${scheduler:+"$scheduler"} 2>/dev/null || true
}
trap 'cleanup; exit 0' TERM INT
while kill -0 "$worker" 2>/dev/null; do
    if [ -n "$scheduler" ] && ! kill -0 "$scheduler" 2>/dev/null; then
        echo 'Backup scheduler exited unexpectedly' >&2
        cleanup
        exit 1
    fi
    sleep 2 & wait $! || true
done
wait "$worker" || status=$?
cleanup
exit "${status:-1}"
